"""Provider-independent contact aggregation and conservative heating automation."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Optional, Protocol, Sequence

from sensor_persistence import (
    ActionWhenOpen,
    AutomationZoneState,
    ZoneSensorPolicy,
)
from sensor_provider import ContactSnapshot, ContactState


OVERRIDE_ACTIONS = frozenset({
    ActionWhenOpen.AWAY,
    ActionWhenOpen.ECO,
    ActionWhenOpen.COMFORT,
})


class HeatingCommandAdapter(Protocol):
    async def apply_override(self, zone_id: str, action: ActionWhenOpen) -> None: ...

    async def release_override(self, zone_id: str) -> None: ...


@dataclass(frozen=True)
class HeatingZone:
    zone_id: str
    has_equipment: bool
    connected: bool
    effective_mode: str
    active_override: Optional[str] = None


class AggregateState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    EMPTY = "empty"


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

    @property
    def eco_deadline(self) -> Optional[float]:
        return self.action_deadline

    @property
    def eco_owned(self) -> bool:
        return self.owned_action is ActionWhenOpen.ECO


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
            )
            for zone_id, state in (states or {}).items()
        }
        self._save_fn = save
        self._commands = commands
        self._clock = clock

    async def evaluate(
        self,
        sensors: Sequence[ContactSnapshot],
        policies: Mapping[str, ZoneSensorPolicy],
        heating: Mapping[str, HeatingZone],
    ) -> AutomationResult:
        """Aggregate snapshots, advance deadlines, and execute only owned actions."""
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
            state = self.states.setdefault(zone_id, AutomationZoneState())
            items = grouped[zone_id]
            observed = heating.get(zone_id)
            any_open = any(
                item.available and item.state is ContactState.OPEN for item in items
            )
            explicitly_closed = bool(items) and all(
                item.available and item.state is ContactState.CLOSED for item in items
            )
            cycle_ended = explicitly_closed or (
                not items and state.open_started_at is not None
            )

            if any_open and state.open_started_at is None:
                state.open_started_at = now
                state.warning_raised = False
                state.suppressed = False
                changed = True

            if state.owned_action is not None and observed is not None and observed.connected:
                if not self._still_owned(observed, state.owned_action):
                    state.owned_action = None
                    state.suppressed = state.open_started_at is not None
                    changed = True

            release_attempted = False
            if (
                state.owned_action is not None
                and state.owned_action is not policy.action_when_open
                and not cycle_ended
            ):
                if self._still_owned(observed, state.owned_action):
                    action = await self._command_release(zone_id)
                    actions.append(action)
                    release_attempted = True
                    if action.succeeded:
                        state.owned_action = None
                        state.suppressed = True
                        changed = True

            if state.open_started_at is not None and not cycle_ended:
                warning_due = state.open_started_at + policy.warning_delay_seconds
                if not state.warning_raised and now >= warning_due:
                    state.warning_raised = True
                    events.append(ConditionEvent(zone_id, ConditionEventKind.WARNING))
                    changed = True

                action_due = state.open_started_at + policy.action_delay_seconds
                desired = policy.action_when_open
                if (
                    desired in OVERRIDE_ACTIONS
                    and state.owned_action is None
                    and not state.suppressed
                    and now >= action_due
                    and self._safe_to_apply(observed)
                ):
                    action = await self._command_apply(zone_id, desired)
                    actions.append(action)
                    if action.succeeded:
                        state.owned_action = desired
                        changed = True

            if cycle_ended:
                if state.owned_action is not None and not release_attempted:
                    if observed is None or not observed.connected:
                        cycle_ended = False
                    elif self._still_owned(observed, state.owned_action):
                        action = await self._command_release(zone_id)
                        actions.append(action)
                        if action.succeeded:
                            state.owned_action = None
                            changed = True
                        else:
                            cycle_ended = False
                    else:
                        # A person replaced our override. Drop the ledger without
                        # sending NORMAL, which could clear their replacement.
                        state.owned_action = None
                        changed = True
                if state.owned_action is not None:
                    cycle_ended = False
                if cycle_ended:
                    if state.warning_raised:
                        events.append(ConditionEvent(zone_id, ConditionEventKind.RECOVERY))
                    if (
                        state.open_started_at is not None
                        or state.warning_raised
                        or state.suppressed
                    ):
                        state.open_started_at = None
                        state.warning_raised = False
                        state.suppressed = False
                        changed = True

            warning_deadline = (
                state.open_started_at + policy.warning_delay_seconds
                if state.open_started_at is not None and not state.warning_raised
                else None
            )
            action_deadline = (
                state.open_started_at + policy.action_delay_seconds
                if state.open_started_at is not None
                and policy.action_when_open in OVERRIDE_ACTIONS
                and state.owned_action is None
                and not state.suppressed
                else None
            )
            aggregates[zone_id] = ZoneAggregate(
                zone_id=zone_id,
                state=self.aggregate(items),
                sensor_count=len(items),
                open_count=sum(
                    item.available and item.state is ContactState.OPEN for item in items
                ),
                unavailable_count=sum(not item.available for item in items),
                warning_raised=state.warning_raised,
                open_started_at=state.open_started_at,
                warning_deadline=warning_deadline,
                action_deadline=action_deadline,
                owned_action=state.owned_action,
                suppressed=state.suppressed,
            )

        stale = set(self.states) - {str(zone_id) for zone_id in policies}
        for zone_id in stale:
            state = self.states[zone_id]
            observed = heating.get(zone_id)
            if state.owned_action is not None:
                if observed is None or not observed.connected:
                    continue
                if self._still_owned(observed, state.owned_action):
                    action = await self._command_release(zone_id)
                    actions.append(action)
                    if not action.succeeded:
                        continue
                state.owned_action = None
                changed = True
            del self.states[zone_id]
            changed = True

        if changed:
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

    def manual_takeover(self, zone_id: str) -> None:
        """Relinquish ownership before a manual/global write and suppress this cycle."""
        state = self.states.get(str(zone_id))
        if state is None:
            return
        state.owned_action = None
        if state.open_started_at is not None:
            state.suppressed = True
        self._save()

    def reconcile_owned(self, heating: Mapping[str, HeatingZone]) -> None:
        """Retain persisted ownership only when the connected hub still agrees."""
        changed = False
        for zone_id, state in self.states.items():
            observed = heating.get(zone_id)
            if (
                state.owned_action is not None
                and observed is not None
                and observed.connected
                and not self._still_owned(observed, state.owned_action)
            ):
                state.owned_action = None
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
            observed = heating.get(zone_id)
            if observed is None or not observed.connected:
                success = False
                continue
            if not self._still_owned(observed, state.owned_action):
                state.owned_action = None
                changed = True
                continue
            action = await self._command_release(zone_id)
            if action.succeeded:
                state.owned_action = None
                changed = True
            else:
                success = False
        if changed:
            self._save()
        return success

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
    def _safe_to_apply(zone: Optional[HeatingZone]) -> bool:
        return bool(
            zone
            and zone.has_equipment
            and zone.connected
            and zone.active_override in (None, "", "-1")
        )

    @staticmethod
    def _still_owned(
        zone: Optional[HeatingZone], action: ActionWhenOpen
    ) -> bool:
        return bool(
            zone
            and zone.connected
            and zone.effective_mode.lower() == action.value
            and zone.active_override not in (None, "", "-1")
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
