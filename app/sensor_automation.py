"""Contact aggregation and the heating rules that hang off it.

This module is deliberately free of FastAPI, pynobo and the demo house. It is
handed a snapshot of the contacts, the per-zone policy and what each zone is
currently doing, and it returns what it decided plus the commands it ran
through an injected adapter. That is what makes the whole state machine
testable on a fake clock with no hub of any kind.

Two ideas carry most of the weight.

**Warmth has an order.** ``off`` is colder than ``away``, which is colder than
``eco``, which is colder than ``comfort``.

**While a contact is open, the room runs whichever is colder: what the rule
asks for, or what the room would be doing anyway.** That single sentence is the
whole policy, and it is worked out afresh on every pass rather than remembered.
Press Comfort for the house with a window open and an Eco rule, and the room
goes back to Eco — the rule has not been "overruled", it is simply still the
colder of the two. Press Away and Away wins, because now *that* is colder. A
zone whose policy has ``override_all_modes`` set skips the comparison: its
action holds until the contact closes, whatever anyone else asks for.

Deciding this from scratch each time is what makes it predictable. An earlier
version remembered that somebody had "taken over" and stood down for the rest
of the open cycle, which meant the room's temperature depended on the order
things had happened in rather than on what was true now.

**Only what we created is ours to undo.** An override this automation applied
is written down, and on closure exactly that override is cancelled with a
Nobø ``NORMAL``. What happens next — the global mode, the week profile — is the
hub's business, not ours. We never send Comfort to "restore" a room, because we
do not know that Comfort is where it came from.

**Temperature rules share that one ledger.** A zone has a single zone
override, so a room that is too warm, too cold and open at once still has one
owner, recorded as ``owned_reason``. While an open contact's rule is due it
alone decides — a window open in winter is not a reason to heat harder. The
rest of the time the temperature rule does: a ceiling holds the colder of its
action and the house, exactly as a contact rule does, and a floor holds the
*warmer* of the two, which is the point of a floor. When one rule stops wanting
the hold and the other still wants it, ownership passes across without the
room being released in between; only when neither wants it is the override
cancelled. A reading that has gone stale, or a sensor that has gone offline,
counts as not knowing the temperature, and not knowing is never a reason to
hold a room anywhere.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Optional, Protocol, Sequence

from sensor_persistence import (
    ActionWhenOpen,
    AutomationZoneState,
    ClimateCondition,
    HOLD_ACTIONS,
    HoldReason,
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

# How long an open room may go without its decision being re-taken. Which mode
# should be running depends on the week profile, and a profile switching from
# Comfort to Eco at ten at night is not an event anything here can subscribe
# to. A minute is far below the resolution anybody heats a room at, and it only
# applies while something is actually open.
RECHECK_WHILE_OPEN_SECONDS = 60

# How long a zone we have just written to is allowed to disagree with us before
# we believe it. A real hub applies an override asynchronously and only shows it
# once it echoes back, so for a moment after sending one the zone still reports
# its old mode and no override at all. Without this, the very next evaluation —
# and any contact anywhere in the house triggers one — would read that as
# "somebody has taken the room off us" and send the same command again.
SETTLING_SECONDS = 5.0

# How old a climate reading may be and still be believed. An Aqara thermometer
# reports on every half-degree change and otherwise about once an hour, so
# three hours of silence is two missed heartbeats — long enough not to cry
# wolf, short enough that a room is not held in Eco on the strength of a
# temperature from this morning.
CLIMATE_STALE_SECONDS = 3 * 3600

# How far back inside a threshold a room has to come before the condition
# clears. Without it a room sitting on its maximum, reporting 26.0 then 26.1
# then 26.0, would warn and recover — and switch the heating — every few
# minutes. Half a degree is the resolution the sensor reports changes at.
CLIMATE_HYSTERESIS = 0.5


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
    # False while invented contacts sit beside a real hub. A demo sensor is
    # there to be opened and closed by hand, and nothing a person does with
    # one should ever leave a real room in Eco.
    sensors_may_act: bool = True


class AggregateState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    EMPTY = "empty"


class ActionStatus(str, Enum):
    """What the zone's heating rule is doing, for the interface to explain."""

    IDLE = "idle"        # no open contact, or nothing that needs doing
    PENDING = "pending"  # open, waiting for the action delay
    ACTIVE = "active"    # this automation is holding an override
    BLOCKED = "blocked"  # due, but standing down — see ``block_reason``


class BlockReason(str, Enum):
    COLDER_MODE = "colder_mode"      # the room is already colder than the rule
    WARMER_MODE = "warmer_mode"      # already at least as warm as a floor asks
    NO_EQUIPMENT = "no_equipment"    # monitoring-only room
    DISCONNECTED = "disconnected"    # no hub to ask
    DEMO_SENSORS = "demo_sensors"    # invented contacts, real heaters
    CONTACT_OPEN = "contact_open"    # an open window's rule has the room


@dataclass(frozen=True)
class ClimateReading:
    """A zone's room climate, from whichever of its thermometers are fresh.

    Averaged, because two thermometers in one room disagree by a degree as a
    matter of course and neither is more right. Unavailable and stale sensors
    are left out rather than averaged in, and a zone none of whose
    thermometers is fresh has no reading at all — which is not the same as a
    reading of zero, and is never treated like one.
    """

    sensor_count: int = 0
    fresh_count: int = 0
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    pressure: Optional[float] = None
    #: When the oldest reading used was taken, which is when this goes stale.
    oldest_at: Optional[float] = None
    newest_at: Optional[float] = None


def climate_reading(
    sensors: Sequence[ContactSnapshot], now: float
) -> ClimateReading:
    thermometers = [item for item in sensors if item.is_climate]
    fresh = [
        item for item in thermometers
        if item.available
        and item.temperature is not None
        and now - item.last_seen_at.timestamp() <= CLIMATE_STALE_SECONDS
    ]

    def mean(name: str) -> Optional[float]:
        values = [getattr(item, name) for item in fresh if getattr(item, name) is not None]
        return round(sum(values) / len(values), 1) if values else None

    stamps = [item.last_seen_at.timestamp() for item in fresh]
    return ClimateReading(
        sensor_count=len(thermometers),
        fresh_count=len(fresh),
        temperature=mean("temperature"),
        humidity=mean("humidity"),
        pressure=mean("pressure"),
        oldest_at=min(stamps) if stamps else None,
        newest_at=max(stamps) if stamps else None,
    )


@dataclass(frozen=True)
class ClimateStatus:
    """What a zone's temperature rule sees and is doing about it."""

    reading: ClimateReading
    condition: Optional[ClimateCondition]
    since: Optional[float]
    action_status: "ActionStatus"
    block_reason: Optional[BlockReason]
    owned_action: Optional[ActionWhenOpen]


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
    action_status: ActionStatus = ActionStatus.IDLE
    block_reason: Optional[BlockReason] = None
    #: Contacts only. ``owned_action`` above is the contact rule's hold and is
    #: None while the temperature rule owns the zone; that one is here.
    climate: Optional[ClimateStatus] = None


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
    #: ``open`` for a contact left open, else the ClimateCondition value.
    condition: str = "open"


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
    climate: ClimateReading = field(default_factory=ClimateReading)

    settling: bool = False
    changed: bool = False
    status: ActionStatus = ActionStatus.IDLE
    block_reason: Optional[BlockReason] = None
    climate_status: ActionStatus = ActionStatus.IDLE
    climate_block: Optional[BlockReason] = None

    @property
    def owns_override(self) -> bool:
        return self.state.owned_action is not None

    @property
    def contact_owns(self) -> bool:
        return self.owns_override and self.state.owned_reason is HoldReason.OPEN

    @property
    def climate_owns(self) -> bool:
        return self.owns_override and self.state.owned_reason is not HoldReason.OPEN

    @property
    def contact_rule_due(self) -> bool:
        """Whether an open contact's heating rule has the room right now.

        True from the moment its action delay runs out until every contact
        closes, whatever the rule then decided — including standing down
        because the house is already colder. While it is true, the
        temperature rule watches and warns but does not touch the heating.
        """
        state, policy = self.state, self.policy
        return (
            policy.action_when_open is not ActionWhenOpen.NOTHING
            and state.open_started_at is not None
            and not self.contacts_settled
            and self.now >= state.open_started_at + policy.action_delay_seconds
        )

    @property
    def ambient_mode(self) -> str:
        """What this room would be running if this automation did nothing.

        While we hold the override it is masking whatever the house would
        otherwise be showing, so the answer is the zone's fallback — the global
        override if one is active and the zone follows it, else the week
        profile. When we hold nothing, what the room is running *is* the
        answer, including somebody else's hold on it.
        """
        if self.zone is None:
            return ""
        return (
            self.zone.fallback_mode if self.owns_override
            else self.zone.effective_mode
        )

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
                owned_reason=state.owned_reason,
                climate_condition=state.climate_condition,
                climate_since=state.climate_since,
            )
            for zone_id, state in (states or {}).items()
        }
        self._save_fn = save
        self._commands = commands
        self._clock = clock
        # When we last sent a command for each zone. In memory only and on
        # purpose: it exists to cover the gap before a hub confirms a write,
        # and a restart means there is nothing in flight to cover.
        self._commanded_at: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Deciding
    # ------------------------------------------------------------------

    @staticmethod
    def mode_to_hold(
        action: ActionWhenOpen, ambient: str, override_all_modes: bool
    ) -> Optional[ActionWhenOpen]:
        """Which mode this rule should be holding, or None to hold nothing.

        The whole policy, in one place. With the escape hatch off, the rule
        gets its way only while its mode is colder than what the room would be
        doing anyway; the moment the house asks for something colder still,
        that wins and this holds nothing. With it on, the rule always wins.

        ``ambient`` unrankable — a mode this build does not know — is treated
        as "leave it alone", because guessing at an unfamiliar mode is how a
        room ends up warmer than somebody meant it to be.
        """
        if action not in HOLD_ACTIONS:
            return None
        if override_all_modes:
            return action
        return action if is_colder(action.value, ambient) else None

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
        thermometers: dict[str, list[ContactSnapshot]] = {
            str(zone_id): [] for zone_id in policies
        }
        for sensor in sensors:
            if sensor.zone_id is not None and str(sensor.zone_id) in grouped:
                # A thermometer is never a contact: counting one would make
                # every room with a thermometer read "state unknown".
                target = grouped if sensor.is_contact else thermometers
                target[str(sensor.zone_id)].append(sensor)

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
                climate=climate_reading(thermometers[zone_id], now),
                settling=self._settling(zone_id, now),
            )

            self._drop_ownership_taken_by_others(step)
            self._begin_cycle_if_newly_open(step)
            self._raise_warning_if_due(step, events)
            await self._run_action_if_due(step, actions)
            self._update_climate_condition(step, events)
            # Before the contact cycle is closed, so that a window shutting in
            # a room that is still too warm hands its Eco across to the
            # temperature rule instead of releasing it and taking it again.
            await self._run_climate_rule(step, actions)
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
        if any(
            aggregate.open_started_at is not None
            and policies[zone_id].action_when_open is not ActionWhenOpen.NOTHING
            for zone_id, aggregate in aggregates.items()
        ) or any(
            # A held temperature rule depends on the schedule in the same way,
            # and the moment a reading goes stale is an event nothing reports.
            aggregate.climate is not None
            and aggregate.climate.condition is not None
            for aggregate in aggregates.values()
        ):
            deadlines.append(now + RECHECK_WHILE_OPEN_SECONDS)
        deadlines.extend(
            aggregate.climate.reading.oldest_at + CLIMATE_STALE_SECONDS
            for aggregate in aggregates.values()
            if aggregate.climate is not None
            and aggregate.climate.reading.oldest_at is not None
            and aggregate.climate.reading.oldest_at + CLIMATE_STALE_SECONDS > now
        )
        # Look again as soon as a zone stops settling, so a change made while
        # our own write was in flight is honoured in seconds rather than at the
        # next routine re-check.
        deadlines.extend(
            sent + SETTLING_SECONDS
            for sent in self._commanded_at.values()
            if sent + SETTLING_SECONDS > now
        )
        return AutomationResult(
            zones=aggregates,
            actions=tuple(actions),
            events=tuple(events),
            next_deadline=min(deadlines) if deadlines else None,
        )

    def _drop_ownership_taken_by_others(self, step: _ZonePass) -> None:
        """Stop claiming a zone that no longer looks like what we applied.

        Somebody has changed it in the official app, or a global mode has
        released our override. Nothing is sent: we simply stop calling it ours,
        and the next decision is taken against what the room is now doing. If
        that new mode is warmer than the rule asks for, the rule takes the room
        back on this same pass — the colder of the two always runs.
        """
        state, zone = step.state, step.zone
        if state.owned_action is None or zone is None or not zone.connected:
            return
        if self._still_ours(zone, state.owned_action):
            self._commanded_at.pop(step.zone_id, None)
            return
        if step.settling:
            # We wrote to this zone a moment ago and the hub has not echoed it
            # back yet. Disagreement this soon is our own command in flight,
            # not somebody overruling us.
            return
        state.owned_action = None
        step.changed = True

    def _begin_cycle_if_newly_open(self, step: _ZonePass) -> None:
        state = step.state
        if not step.any_open or state.open_started_at is not None:
            return
        state.open_started_at = step.now
        state.warning_raised = False
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
        """Bring the room to whichever is colder, the rule or the house.

        Re-decided on every pass, so pressing Comfort for the house while a
        window is open puts the room straight back on the rule's mode, and
        pressing Away lets Away through — without either being remembered as
        having "won".
        """
        state, policy, zone = step.state, step.policy, step.zone
        action = policy.action_when_open
        if action is ActionWhenOpen.NOTHING:
            return
        if state.open_started_at is None or step.contacts_settled:
            return
        if zone is not None and not zone.sensors_may_act:
            # Before the delay, not after it, so the room never says it is
            # "about to" do something it is not going to do. Anything held
            # from before the source changed goes back now rather than
            # waiting for a contact that may never be closed.
            if step.contact_owns:
                await self._hand_back_ownership(step, actions)
            step.status = ActionStatus.BLOCKED
            step.block_reason = BlockReason.DEMO_SENSORS
            return
        if step.now < state.open_started_at + policy.action_delay_seconds:
            step.status = ActionStatus.PENDING
            return
        if zone is None or not zone.connected:
            step.status = ActionStatus.BLOCKED
            step.block_reason = BlockReason.DISCONNECTED
            return
        if not zone.has_equipment:
            step.status = ActionStatus.BLOCKED
            step.block_reason = BlockReason.NO_EQUIPMENT
            return

        if action not in HOLD_ACTIONS:
            await self._return_zone_to_its_schedule(step, actions)
            return

        wanted = self.mode_to_hold(action, step.ambient_mode, policy.override_all_modes)
        if wanted is None:
            # The house is asking for something at least as cold as the rule,
            # so there is nothing for the rule to add. Anything we were holding
            # goes back, which is what lets a global Away through.
            if state.owned_action is not None:
                await self._hand_back_ownership(step, actions)
            step.status = ActionStatus.BLOCKED
            step.block_reason = BlockReason.COLDER_MODE
            return

        if state.owned_action is wanted:
            # Possibly the temperature rule's hold, which is exactly what the
            # window wants too. It becomes the window's without a command.
            if state.owned_reason is not HoldReason.OPEN:
                state.owned_reason = HoldReason.OPEN
                step.changed = True
            step.status = ActionStatus.ACTIVE
            return

        # A zone override replaces whatever override was on the zone, so this
        # is one command whether we are taking a warmer hold off somebody or
        # starting from nothing.
        applied = await self._command_apply(step.zone_id, wanted)
        actions.append(applied)
        if not applied.succeeded:
            step.status = ActionStatus.PENDING
            return
        state.owned_action = wanted
        state.owned_reason = HoldReason.OPEN
        step.status = ActionStatus.ACTIVE
        step.changed = True

    def _update_climate_condition(
        self, step: _ZonePass, events: list[ConditionEvent]
    ) -> None:
        """Whether the room is outside its thresholds, with hysteresis.

        A condition is raised the moment a reading crosses a threshold and
        cleared only once the room is ``CLIMATE_HYSTERESIS`` back inside it.
        Recovery is announced only when a reading says so. A condition that
        ends because the reading went stale, the sensor went away or the
        threshold was switched off ends quietly: none of those is news that
        the room is fine.
        """
        state, policy = step.state, step.policy
        temperature = step.climate.temperature
        previous = state.climate_condition
        high, low = policy.temperature_max, policy.temperature_min

        condition: Optional[ClimateCondition] = None
        if temperature is not None:
            if previous is ClimateCondition.TOO_WARM and high is not None:
                if temperature > high - CLIMATE_HYSTERESIS:
                    condition = previous
            elif previous is ClimateCondition.TOO_COLD and low is not None:
                if temperature < low + CLIMATE_HYSTERESIS:
                    condition = previous
            if condition is None:
                if high is not None and temperature > high:
                    condition = ClimateCondition.TOO_WARM
                elif low is not None and temperature < low:
                    condition = ClimateCondition.TOO_COLD

        if condition is previous:
            return
        if previous is not None:
            threshold_kept = (
                high if previous is ClimateCondition.TOO_WARM else low
            ) is not None
            if temperature is not None and threshold_kept:
                events.append(ConditionEvent(
                    step.zone_id, ConditionEventKind.RECOVERY, previous.value
                ))
        if condition is not None:
            events.append(ConditionEvent(
                step.zone_id, ConditionEventKind.WARNING, condition.value
            ))
        state.climate_condition = condition
        state.climate_since = step.now if condition is not None else None
        step.changed = True

    def climate_mode_to_hold(
        self, condition: ClimateCondition, action: ActionWhenOpen, ambient: str
    ) -> Optional[ActionWhenOpen]:
        """Which mode a temperature rule should hold, or None for none.

        A ceiling only ever cools and a floor only ever warms, so each holds
        its mode only while that is on the right side of what the room would
        do anyway. An unrankable ambient mode is left alone, as it is for
        contacts.
        """
        if action not in HOLD_ACTIONS:
            return None
        if condition is ClimateCondition.TOO_WARM:
            return action if is_colder(action.value, ambient) else None
        return action if is_colder(ambient, action.value) else None

    async def _run_climate_rule(
        self, step: _ZonePass, actions: list[RequestedAction]
    ) -> None:
        state, policy, zone = step.state, step.policy, step.zone
        condition = state.climate_condition
        action = ActionWhenOpen.NOTHING
        if condition is ClimateCondition.TOO_WARM:
            action = policy.action_when_too_warm
        elif condition is ClimateCondition.TOO_COLD:
            action = policy.action_when_too_cold

        if step.contact_rule_due:
            # The window's rule has the room. Its hold, or its decision to
            # hold nothing, stands until every contact closes.
            if action in HOLD_ACTIONS:
                step.climate_status = ActionStatus.BLOCKED
                step.climate_block = BlockReason.CONTACT_OPEN
            return

        wanted: Optional[ActionWhenOpen] = None
        if action in HOLD_ACTIONS:
            if zone is None or not zone.connected:
                # Nothing can be sent, and nothing held is given up: the
                # ledger is kept until the hub can be asked again.
                step.climate_status = ActionStatus.BLOCKED
                step.climate_block = BlockReason.DISCONNECTED
                return
            if not zone.has_equipment:
                step.climate_status = ActionStatus.BLOCKED
                step.climate_block = BlockReason.NO_EQUIPMENT
            elif not zone.sensors_may_act:
                step.climate_status = ActionStatus.BLOCKED
                step.climate_block = BlockReason.DEMO_SENSORS
            else:
                wanted = self.climate_mode_to_hold(condition, action, step.ambient_mode)
                if wanted is None:
                    step.climate_status = ActionStatus.BLOCKED
                    step.climate_block = (
                        BlockReason.COLDER_MODE
                        if condition is ClimateCondition.TOO_WARM
                        else BlockReason.WARMER_MODE
                    )

        reason = HoldReason(condition.value) if condition is not None else None
        if wanted is None:
            if step.climate_owns:
                await self._hand_back_ownership(step, actions)
            return

        if state.owned_action is wanted:
            # Already there — perhaps the window's hold, handed across now
            # that the window has shut, perhaps this rule's own.
            if state.owned_reason is not reason:
                state.owned_reason = reason
                step.changed = True
            step.climate_status = ActionStatus.ACTIVE
            return
        if step.settling:
            step.climate_status = ActionStatus.PENDING
            return

        applied = await self._command_apply(step.zone_id, wanted)
        actions.append(applied)
        if not applied.succeeded:
            step.climate_status = ActionStatus.PENDING
            return
        state.owned_action = wanted
        state.owned_reason = reason
        step.climate_status = ActionStatus.ACTIVE
        step.changed = True

    async def _return_zone_to_its_schedule(
        self, step: _ZonePass, actions: list[RequestedAction]
    ) -> None:
        """Cancel the hold on a room so the house decides what it does.

        The one action that lets go rather than taking hold. Nothing is owned
        afterwards — there is no override left to give back when the contact
        closes — and it is self-limiting, because once the hold is gone there
        is nothing here to cancel.
        """
        state, zone = step.state, step.zone
        if step.settling:
            # A release is already on its way; sending a second would achieve
            # nothing except another line in the hub log.
            step.status = ActionStatus.IDLE
            return
        if not zone.has_zone_override:
            step.status = ActionStatus.IDLE
            return
        # Letting go warms the room whenever the schedule is warmer than the
        # hold, so it answers to the same ordering as everything else.
        if not (
            step.policy.override_all_modes
            or is_colder(zone.fallback_mode, zone.effective_mode)
        ):
            step.status = ActionStatus.BLOCKED
            step.block_reason = BlockReason.COLDER_MODE
            return
        released = await self._command_release(step.zone_id)
        actions.append(released)
        if not released.succeeded:
            step.status = ActionStatus.PENDING
            return
        if state.owned_action is not None:
            state.owned_action = None
            step.changed = True
        step.status = ActionStatus.IDLE

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
        # Only the window's own hold. One the temperature rule has taken over
        # on this pass, or held all along, is not the window's to give back.
        if step.contact_owns:
            if not await self._hand_back_ownership(step, actions):
                return
        if state.open_started_at is None and not state.warning_raised:
            return

        if state.warning_raised:
            events.append(ConditionEvent(step.zone_id, ConditionEventKind.RECOVERY))
        state.open_started_at = None
        state.warning_raised = False
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
            step.changed = True
            return True
        action = await self._command_release(step.zone_id)
        actions.append(action)
        if not action.succeeded:
            return False
        state.owned_action = None
        state.owned_reason = HoldReason.OPEN
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
            and not step.contact_owns
            and step.block_reason is not BlockReason.DEMO_SENSORS
            else None
        )
        status = step.status
        if step.contact_owns:
            status = ActionStatus.ACTIVE
        elif not open_cycle:
            status = ActionStatus.IDLE
        climate_status = step.climate_status
        if step.climate_owns:
            climate_status = ActionStatus.ACTIVE
        elif state.climate_condition is None:
            climate_status = ActionStatus.IDLE
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
            owned_action=state.owned_action if step.contact_owns else None,
            action_status=status,
            block_reason=step.block_reason if status is ActionStatus.BLOCKED else None,
            climate=ClimateStatus(
                reading=step.climate,
                condition=state.climate_condition,
                since=state.climate_since,
                action_status=climate_status,
                block_reason=(
                    step.climate_block
                    if climate_status is ActionStatus.BLOCKED else None
                ),
                owned_action=state.owned_action if step.climate_owns else None,
            ),
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
            del self.states[zone_id]
            self._commanded_at.pop(zone_id, None)
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

    def _settling(self, zone_id: str, now: float) -> bool:
        """Whether a command we sent for this zone may still be in flight."""
        sent = self._commanded_at.get(zone_id)
        return sent is not None and now - sent < SETTLING_SECONDS

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
        self._commanded_at[zone_id] = self._clock()
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
        self._commanded_at[zone_id] = self._clock()
        try:
            await self._commands.release_override(zone_id)
            return RequestedAction(zone_id, ActionKind.RELEASE_OVERRIDE, None, True)
        except Exception as exc:
            return RequestedAction(
                zone_id, ActionKind.RELEASE_OVERRIDE, None, False, str(exc)
            )

    def _save(self) -> None:
        # Several paths let go of a hold; rather than each remembering to
        # reset whose it was, a hold nobody has carries no owner.
        for state in self.states.values():
            if state.owned_action is None:
                state.owned_reason = HoldReason.OPEN
        if self._save_fn is not None:
            self._save_fn(self.states)
