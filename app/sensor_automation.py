"""Contact aggregation and the heating rules that hang off it.

This module is deliberately free of FastAPI, pynobo and the demo house. It is
handed a snapshot of the contacts, the per-zone policy and what each zone is
currently doing, and it returns what it decided plus the commands it ran
through an injected adapter. That is what makes the whole state machine
testable on a fake clock with no hub of any kind.

Two ideas carry most of the weight.

**Warmth has an order.** ``off`` is colder than ``away``, which is colder than
``eco``, which is colder than ``comfort``. A contact rule is a safety net, not
a thermostat, so by default it is only allowed to move a room *down* that
order. A window left open in a house that is already Away must not pull the
room up to Eco. A zone whose policy has ``override_all_modes`` set has said, in
so many words, "ignore that" — and that is the only way past it.

**Only what we created is ours to undo.** An override this automation applied
is written down, and on closure exactly that override is cancelled with a
Nobø ``NORMAL``. What happens next — the global mode, the week profile — is the
hub's business, not ours. We never send Comfort to "restore" a room, because we
do not know that Comfort is where it came from.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Optional, Protocol, Sequence

from sensor_persistence import (
    ActionWhenOpen,
    AutomationZoneState,
    HOLD_ACTIONS,
    ZoneSensorPolicy,
)
from sensor_provider import ContactSnapshot, ContactState


# How cold each mode is. Only used for comparisons, so the spacing is
# arbitrary; the order is not. ``off`` is a valid week-profile state and is
# colder than Away, which is a fixed 7 °C anti-frost temperature.
HEATING_PRIORITY = {
    "off": -1,
    "away": 0,
    "eco": 1,
    "comfort": 2,
}

def is_colder(candidate: str, reference: str) -> Optional[bool]:
    """Whether *candidate* is colder than *reference*, or None if unrankable."""
    left = HEATING_PRIORITY.get((candidate or "").lower())
    right = HEATING_PRIORITY.get((reference or "").lower())
    if left is None or right is None:
        return None
    return left < right


class HeatingCommandAdapter(Protocol):
    async def apply_override(self, zone_id: str, action: ActionWhenOpen) -> None: ...

    async def release_override(self, zone_id: str) -> None: ...


@dataclass(frozen=True)
class HeatingZone:
    """What a zone is doing, as far as the heating side of the app can tell.

    ``effective_mode`` is what the room is running right now. ``fallback_mode``
    is what it would run if its own zone override were cancelled — the global
    override when one is active and the zone follows it, otherwise the week
    profile. Keeping both is what lets "Follow schedule" tell whether letting go
    would warm the room up.
    """

    zone_id: str
    has_equipment: bool
    connected: bool
    effective_mode: str
    fallback_mode: str
    has_zone_override: bool = False


class AggregateState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    EMPTY = "empty"


class ActionStatus(str, Enum):
    """What the zone's heating rule is doing, for the interface to explain."""

    IDLE = "idle"                # no open contact, or nothing left to do
    PENDING = "pending"          # open, waiting for the action delay
    ACTIVE = "active"            # this automation is holding an override
    BLOCKED = "blocked"          # due, but not permitted — see ``block_reason``
    SUPPRESSED = "suppressed"    # somebody took the zone over this open cycle


class BlockReason(str, Enum):
    COLDER_MODE = "colder_mode"                # already at or below the target
    MANUAL_OVERRIDE = "manual_override"        # somebody else's zone override
    NO_EQUIPMENT = "no_equipment"              # monitoring-only room
    DISCONNECTED = "disconnected"              # no hub to ask
    NOTHING_TO_RELEASE = "nothing_to_release"  # "Follow schedule" with no hold


@dataclass(frozen=True)
class Permission:
    allowed: bool
    reason: Optional[BlockReason] = None


@dataclass(frozen=True)
class ZoneAggregate:
    zone_id: str
    state: AggregateState
    sensor_count: int
    open_count: int
    unavailable_count: int
    warning_raised: bool
    open_started_at: Optional[float]
    warning_deadline: Optional[float]
    action_deadline: Optional[float]
    owned_action: Optional[ActionWhenOpen]
    suppressed: bool
    action_status: ActionStatus = ActionStatus.IDLE
    block_reason: Optional[BlockReason] = None


class ActionKind(str, Enum):
    APPLY_OVERRIDE = "apply_override"
    RELEASE_OVERRIDE = "release_override"


@dataclass(frozen=True)
class RequestedAction:
    zone_id: str
    kind: ActionKind
    action: Optional[ActionWhenOpen]
    succeeded: bool
    error: Optional[str] = None


class ConditionEventKind(str, Enum):
    WARNING = "warning"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class ConditionEvent:
    zone_id: str
    kind: ConditionEventKind


@dataclass(frozen=True)
class AutomationResult:
    zones: Mapping[str, ZoneAggregate]
    actions: Sequence[RequestedAction] = field(default_factory=tuple)
    events: Sequence[ConditionEvent] = field(default_factory=tuple)
    next_deadline: Optional[float] = None


@dataclass
class _ZonePass:
    """Everything one zone's evaluation needs, gathered in one place.

    Passing this between the phases below, instead of threading a growing set
    of flags through one long function, is what lets each phase be read on its
    own and named after what it decides.
    """

    zone_id: str
    policy: ZoneSensorPolicy
    state: AutomationZoneState
    contacts: Sequence[ContactSnapshot]
    zone: Optional[HeatingZone]
    now: float

    changed: bool = False
    released_this_pass: bool = False
    status: ActionStatus = ActionStatus.IDLE
    block_reason: Optional[BlockReason] = None

    @property
    def any_open(self) -> bool:
        return any(
            item.available and item.state is ContactState.OPEN
            for item in self.contacts
        )

    @property
    def all_closed(self) -> bool:
        """Every assigned contact is reachable and explicitly closed.

        Unknown and unavailable are deliberately not closed. A sensor whose
        battery died mid-gesture must not be read as "the window was shut".
        """
        return bool(self.contacts) and all(
            item.available and item.state is ContactState.CLOSED
            for item in self.contacts
        )

    @property
    def contacts_settled(self) -> bool:
        """Whether the facts say this open cycle is over.

        Removing the last sensor from a zone also ends its cycle — there is
        nothing left that could ever report the window shut.
        """
        return self.all_closed or (
            not self.contacts and self.state.open_started_at is not None
        )


class SensorAutomation:
    """State machine whose only side effects are injected persistence and commands."""

    def __init__(
        self,
        *,
        states: Optional[Mapping[str, AutomationZoneState]] = None,
        save: Optional[Callable[[Mapping[str, AutomationZoneState]], None]] = None,
        commands: Optional[HeatingCommandAdapter] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.states = {
            str(zone_id): AutomationZoneState(
                open_started_at=state.open_started_at,
                warning_raised=state.warning_raised,
                owned_action=state.owned_action,
                suppressed=state.suppressed,
                owned_with_override=state.owned_with_override,
            )
            for zone_id, state in (states or {}).items()
        }
        self._save_fn = save
        self._commands = commands
        self._clock = clock

    # ------------------------------------------------------------------
    # Permission
    # ------------------------------------------------------------------

    @staticmethod
    def permission(
        zone: Optional[HeatingZone], policy: ZoneSensorPolicy
    ) -> Permission:
        """Whether this zone's configured action may run right now.

        The rules differ by kind of action, but the shape is the same: the room
        has to be reachable and heatable, nobody else may be holding it, and —
        unless the zone has explicitly opted out — the result has to be colder
        than what the room is doing already.
        """
        action = policy.action_when_open
        if action is ActionWhenOpen.NOTHING:
            return Permission(False)
        if zone is None or not zone.connected:
            return Permission(False, BlockReason.DISCONNECTED)
        if not zone.has_equipment:
            return Permission(False, BlockReason.NO_EQUIPMENT)

        if action is ActionWhenOpen.SCHEDULE:
            # Letting go of a hold rather than taking one. There has to be a
            # hold to let go of, and where the room would land has to be colder
            # — releasing a manual Away onto a Comfort week profile is a warming
            # change like any other.
            if not zone.has_zone_override:
                return Permission(False, BlockReason.NOTHING_TO_RELEASE)
            if policy.override_all_modes:
                return Permission(True)
            if is_colder(zone.fallback_mode, zone.effective_mode):
                return Permission(True)
            return Permission(False, BlockReason.COLDER_MODE)

        # Taking a hold. Somebody else's zone override outranks us, always.
        if zone.has_zone_override:
            return Permission(False, BlockReason.MANUAL_OVERRIDE)
        if policy.override_all_modes:
            return Permission(True)
        if is_colder(action.value, zone.effective_mode):
            return Permission(True)
        return Permission(False, BlockReason.COLDER_MODE)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        sensors: Sequence[ContactSnapshot],
        policies: Mapping[str, ZoneSensorPolicy],
        heating: Mapping[str, HeatingZone],
    ) -> AutomationResult:
        """Aggregate the contacts, advance the timers, and run what is due."""
        now = self._clock()
        grouped: dict[str, list[ContactSnapshot]] = {
            str(zone_id): [] for zone_id in policies
        }
        for sensor in sensors:
            if sensor.zone_id is not None and str(sensor.zone_id) in grouped:
                grouped[str(sensor.zone_id)].append(sensor)

        actions: list[RequestedAction] = []
        events: list[ConditionEvent] = []
        aggregates: dict[str, ZoneAggregate] = {}
        changed = False

        for raw_zone_id, policy in policies.items():
            zone_id = str(raw_zone_id)
            step = _ZonePass(
                zone_id=zone_id,
                policy=policy,
                state=self.states.setdefault(zone_id, AutomationZoneState()),
                contacts=grouped[zone_id],
                zone=heating.get(zone_id),
                now=now,
            )

            self._drop_ownership_taken_by_others(step)
            self._begin_cycle_if_newly_open(step)
            await self._release_ownership_the_policy_no_longer_allows(step, actions)
            self._raise_warning_if_due(step, events)
            await self._run_action_if_due(step, actions)
            await self._finish_cycle_if_settled(step, actions, events)

            aggregates[zone_id] = self._summarise(step)
            changed = changed or step.changed

        forgotten = await self._forget_zones_that_no_longer_exist(
            policies, heating, actions
        )

        if changed or forgotten:
            self._save()
        deadlines = [
            deadline
            for aggregate in aggregates.values()
            for deadline in (aggregate.warning_deadline, aggregate.action_deadline)
            if deadline is not None and deadline > now
        ]
        return AutomationResult(
            zones=aggregates,
            actions=tuple(actions),
            events=tuple(events),
            next_deadline=min(deadlines) if deadlines else None,
        )

    def _drop_ownership_taken_by_others(self, step: _ZonePass) -> None:
        """Let go the moment the zone stops looking like what we applied.

        Somebody turning a room to Comfort in the official app is a decision,
        and it outranks a window. Ownership is dropped without sending
        anything, and the rest of this open cycle is suppressed so we do not
        immediately undo them.
        """
        state, zone = step.state, step.zone
        if state.owned_action is None or zone is None or not zone.connected:
            return
        if self._still_ours(zone, state.owned_action):
            return
        state.owned_action = None
        state.owned_with_override = False
        state.suppressed = state.open_started_at is not None
        step.changed = True

    def _begin_cycle_if_newly_open(self, step: _ZonePass) -> None:
        state = step.state
        if not step.any_open or state.open_started_at is not None:
            return
        state.open_started_at = step.now
        state.warning_raised = False
        state.suppressed = False
        step.changed = True

    async def _release_ownership_the_policy_no_longer_allows(
        self, step: _ZonePass, actions: list[RequestedAction]
    ) -> None:
        """Hand back an override the current policy would not create today.

        Two ways that happens: the action was changed to something else, or
        Sensor override was switched off under a hold that only existed because
        it was on. Whether the hold is still permitted cannot be re-derived once
        it is in place — our own override masks the mode the room would
        otherwise show — which is exactly why that flag is recorded alongside
        the ownership rather than worked out again here.
        """
        state = step.state
        if state.owned_action is None or step.contacts_settled:
            return
        stale_action = state.owned_action is not step.policy.action_when_open
        lost_permission = (
            state.owned_with_override and not step.policy.override_all_modes
        )
        if not (stale_action or lost_permission):
            return
        if not self._still_ours(step.zone, state.owned_action):
            return
        action = await self._command_release(step.zone_id)
        actions.append(action)
        step.released_this_pass = True
        if not action.succeeded:
            return
        state.owned_action = None
        state.owned_with_override = False
        state.suppressed = True
        step.changed = True

    def _raise_warning_if_due(
        self, step: _ZonePass, events: list[ConditionEvent]
    ) -> None:
        state = step.state
        if state.open_started_at is None or step.contacts_settled:
            return
        if state.warning_raised:
            return
        if step.now < state.open_started_at + step.policy.warning_delay_seconds:
            return
        state.warning_raised = True
        events.append(ConditionEvent(step.zone_id, ConditionEventKind.WARNING))
        step.changed = True

    async def _run_action_if_due(
        self, step: _ZonePass, actions: list[RequestedAction]
    ) -> None:
        """Apply or release once the action delay has passed, if permitted."""
        state, policy = step.state, step.policy
        if policy.action_when_open is ActionWhenOpen.NOTHING:
            return
        if state.open_started_at is None or step.contacts_settled:
            return
        if state.owned_action is not None:
            step.status = ActionStatus.ACTIVE
            return
        if state.suppressed:
            step.status = ActionStatus.SUPPRESSED
            return
        if step.now < state.open_started_at + policy.action_delay_seconds:
            step.status = ActionStatus.PENDING
            return

        permission = self.permission(step.zone, policy)
        if not permission.allowed:
            # "Nothing to release" is the resting state of a Follow schedule
            # rule, not a fault worth colouring a card over.
            step.status = (
                ActionStatus.IDLE
                if permission.reason is BlockReason.NOTHING_TO_RELEASE
                else ActionStatus.BLOCKED
            )
            step.block_reason = permission.reason
            return

        if policy.action_when_open not in HOLD_ACTIONS:
            # Follow schedule. One-shot, and self-limiting: once the hold is
            # gone the permission check above reports nothing left to release,
            # so it cannot loop.
            action = await self._command_release(step.zone_id)
            actions.append(action)
            step.released_this_pass = True
            step.status = (
                ActionStatus.IDLE if action.succeeded else ActionStatus.PENDING
            )
            return

        action = await self._command_apply(step.zone_id, policy.action_when_open)
        actions.append(action)
        if not action.succeeded:
            step.status = ActionStatus.PENDING
            return
        state.owned_action = policy.action_when_open
        state.owned_with_override = policy.override_all_modes
        step.status = ActionStatus.ACTIVE
        step.changed = True

    async def _finish_cycle_if_settled(
        self,
        step: _ZonePass,
        actions: list[RequestedAction],
        events: list[ConditionEvent],
    ) -> None:
        """Close the books once every contact reports shut.

        The override we own is cancelled first. If that cannot be done — the hub
        is unreachable, or the command failed — the cycle stays open on purpose
        so the next evaluation tries again. A room must never be left holding an
        override nobody is tracking any more.
        """
        state = step.state
        if not step.contacts_settled:
            return
        if state.owned_action is not None and not step.released_this_pass:
            if not await self._hand_back_ownership(step, actions):
                return
        if state.owned_action is not None:
            return
        if (
            state.open_started_at is None
            and not state.warning_raised
            and not state.suppressed
        ):
            return

        if state.warning_raised:
            events.append(ConditionEvent(step.zone_id, ConditionEventKind.RECOVERY))
        state.open_started_at = None
        state.warning_raised = False
        state.suppressed = False
        step.changed = True

    async def _hand_back_ownership(
        self, step: _ZonePass, actions: list[RequestedAction]
    ) -> bool:
        """Release what we hold. False means keep the cycle open and retry."""
        state, zone = step.state, step.zone
        if zone is None or not zone.connected:
            return False
        if not self._still_ours(zone, state.owned_action):
            # Somebody replaced our override while the contact was closing.
            # Drop the ledger without sending NORMAL, which would wipe out
            # their choice instead of ours.
            state.owned_action = None
            state.owned_with_override = False
            step.changed = True
            return True
        action = await self._command_release(step.zone_id)
        actions.append(action)
        if not action.succeeded:
            return False
        state.owned_action = None
        state.owned_with_override = False
        step.changed = True
        return True

    def _summarise(self, step: _ZonePass) -> ZoneAggregate:
        state, policy = step.state, step.policy
        open_cycle = state.open_started_at is not None and not step.contacts_settled
        warning_deadline = (
            state.open_started_at + policy.warning_delay_seconds
            if open_cycle and not state.warning_raised
            else None
        )
        action_deadline = (
            state.open_started_at + policy.action_delay_seconds
            if open_cycle
            and policy.action_when_open is not ActionWhenOpen.NOTHING
            and state.owned_action is None
            and not state.suppressed
            else None
        )
        status = step.status
        if state.owned_action is not None:
            status = ActionStatus.ACTIVE
        elif not open_cycle:
            status = ActionStatus.IDLE
        return ZoneAggregate(
            zone_id=step.zone_id,
            state=self.aggregate(step.contacts),
            sensor_count=len(step.contacts),
            open_count=sum(
                item.available and item.state is ContactState.OPEN
                for item in step.contacts
            ),
            unavailable_count=sum(not item.available for item in step.contacts),
            warning_raised=state.warning_raised,
            open_started_at=state.open_started_at,
            warning_deadline=warning_deadline,
            action_deadline=action_deadline,
            owned_action=state.owned_action,
            suppressed=state.suppressed,
            action_status=status,
            block_reason=step.block_reason if status is ActionStatus.BLOCKED else None,
        )

    async def _forget_zones_that_no_longer_exist(
        self,
        policies: Mapping[str, ZoneSensorPolicy],
        heating: Mapping[str, HeatingZone],
        actions: list[RequestedAction],
    ) -> bool:
        """Drop bookkeeping for deleted zones, releasing anything we still hold."""
        changed = False
        for zone_id in set(self.states) - {str(zone_id) for zone_id in policies}:
            state = self.states[zone_id]
            zone = heating.get(zone_id)
            if state.owned_action is not None:
                if zone is None or not zone.connected:
                    continue
                if self._still_ours(zone, state.owned_action):
                    action = await self._command_release(zone_id)
                    actions.append(action)
                    if not action.succeeded:
                        continue
                state.owned_action = None
                state.owned_with_override = False
            del self.states[zone_id]
            changed = True
        return changed

    # ------------------------------------------------------------------
    # Events from outside
    # ------------------------------------------------------------------

    def manual_takeover(self, zone_id: str) -> None:
        """A person or a global mode is about to write to this zone.

        Called *before* the command goes out, so the write cannot be mistaken
        for our own override a moment later. Whoever asked keeps the room for
        the rest of this open cycle; a later cycle may automate again.
        """
        state = self.states.get(str(zone_id))
        if state is None:
            return
        state.owned_action = None
        state.owned_with_override = False
        if state.open_started_at is not None:
            state.suppressed = True
        self._save()

    def reconcile_owned(self, heating: Mapping[str, HeatingZone]) -> None:
        """Keep persisted ownership only where the connected hub still agrees.

        A mismatch after a restart is never corrected by re-applying the
        override; the room is left as it is and we stop claiming it.
        """
        changed = False
        for zone_id, state in self.states.items():
            zone = heating.get(zone_id)
            if (
                state.owned_action is not None
                and zone is not None
                and zone.connected
                and not self._still_ours(zone, state.owned_action)
            ):
                state.owned_action = None
                state.owned_with_override = False
                state.suppressed = state.open_started_at is not None
                changed = True
        if changed:
            self._save()

    async def disable(self, heating: Mapping[str, HeatingZone]) -> bool:
        """Release every safely owned override; false means sensors must stay on."""
        success = True
        changed = False
        for zone_id, state in self.states.items():
            if state.owned_action is None:
                continue
            zone = heating.get(zone_id)
            if zone is None or not zone.connected:
                success = False
                continue
            if not self._still_ours(zone, state.owned_action):
                state.owned_action = None
                state.owned_with_override = False
                changed = True
                continue
            action = await self._command_release(zone_id)
            if action.succeeded:
                state.owned_action = None
                state.owned_with_override = False
                changed = True
            else:
                success = False
        if changed:
            self._save()
        return success

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def aggregate(sensors: Sequence[ContactSnapshot]) -> AggregateState:
        if not sensors:
            return AggregateState.EMPTY
        if any(item.available and item.state is ContactState.OPEN for item in sensors):
            return AggregateState.OPEN
        if any(not item.available for item in sensors):
            return AggregateState.UNAVAILABLE
        if all(item.state is ContactState.CLOSED for item in sensors):
            return AggregateState.CLOSED
        return AggregateState.UNKNOWN

    @staticmethod
    def _still_ours(
        zone: Optional[HeatingZone], action: Optional[ActionWhenOpen]
    ) -> bool:
        """Whether the zone still looks like the override we applied."""
        return bool(
            zone
            and action is not None
            and zone.connected
            and zone.has_zone_override
            and zone.effective_mode.lower() == action.value
        )

    async def _command_apply(
        self, zone_id: str, action: ActionWhenOpen
    ) -> RequestedAction:
        if self._commands is None:
            return RequestedAction(
                zone_id, ActionKind.APPLY_OVERRIDE, action, False,
                "no command adapter configured",
            )
        try:
            await self._commands.apply_override(zone_id, action)
            return RequestedAction(zone_id, ActionKind.APPLY_OVERRIDE, action, True)
        except Exception as exc:
            return RequestedAction(
                zone_id, ActionKind.APPLY_OVERRIDE, action, False, str(exc)
            )

    async def _command_release(self, zone_id: str) -> RequestedAction:
        if self._commands is None:
            return RequestedAction(
                zone_id, ActionKind.RELEASE_OVERRIDE, None, False,
                "no command adapter configured",
            )
        try:
            await self._commands.release_override(zone_id)
            return RequestedAction(zone_id, ActionKind.RELEASE_OVERRIDE, None, True)
        except Exception as exc:
            return RequestedAction(
                zone_id, ActionKind.RELEASE_OVERRIDE, None, False, str(exc)
            )

    def _save(self) -> None:
        if self._save_fn is not None:
            self._save_fn(self.states)
