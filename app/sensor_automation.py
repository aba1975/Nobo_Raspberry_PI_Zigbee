"""Provider-independent contact aggregation and conservative Eco automation."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Mapping, Optional, Protocol, Sequence

from sensor_persistence import AutomationZoneState, ZoneSensorPolicy
from sensor_provider import ContactSnapshot, ContactState


class HeatingCommandAdapter(Protocol):
    async def apply_eco(self, zone_id: str) -> None: ...

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
    eco_deadline: Optional[float]
    eco_owned: bool
    suppressed: bool


class ActionKind(str, Enum):
    APPLY_ECO = "apply_eco"
    RELEASE_OVERRIDE = "release_override"


@dataclass(frozen=True)
class RequestedAction:
    zone_id: str
    kind: ActionKind
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
            str(zone_id): AutomationZoneState(**state.__dict__)
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
        """Aggregate current snapshots, advance timers, and execute safe actions."""
        now = self._clock()
        grouped: dict[str, list[ContactSnapshot]] = {
            str(zone_id): [] for zone_id in policies
        }
        for sensor in sensors:
            if sensor.zone_id is not None and sensor.zone_id in grouped:
                grouped[sensor.zone_id].append(sensor)

        actions: list[RequestedAction] = []
        events: list[ConditionEvent] = []
        aggregates: dict[str, ZoneAggregate] = {}
        changed = False

        for zone_id, policy in policies.items():
            zone_id = str(zone_id)
            state = self.states.setdefault(zone_id, AutomationZoneState())
            release_attempted = False
            items = grouped[zone_id]
            aggregate_state = self.aggregate(items)
            any_open = any(
                item.available and item.state is ContactState.OPEN for item in items
            )
            explicitly_closed = bool(items) and all(
                item.available and item.state is ContactState.CLOSED for item in items
            )
            # Removing the final sensor is also a definitive end to that zone's cycle.
            cycle_ended = explicitly_closed or (not items and state.open_started_at is not None)

            if any_open and state.open_started_at is None:
                state.open_started_at = now
                state.warning_raised = False
                state.suppressed = False
                changed = True

            if state.open_started_at is not None and not cycle_ended:
                warning_due = state.open_started_at + policy.warning_delay_seconds
                if not state.warning_raised and now >= warning_due:
                    state.warning_raised = True
                    events.append(ConditionEvent(zone_id, ConditionEventKind.WARNING))
                    changed = True

                eco_due = state.open_started_at + policy.eco_delay_seconds
                observed = heating.get(zone_id)
                if (
                    state.eco_owned
                    and observed is not None
                    and observed.connected
                    and not self._still_owned(observed)
                ):
                    state.eco_owned = False
                    state.suppressed = True
                    changed = True
                if state.eco_owned and not policy.eco_enabled:
                    action = await self._command(zone_id, ActionKind.RELEASE_OVERRIDE)
                    actions.append(action)
                    release_attempted = True
                    if action.succeeded:
                        state.eco_owned = False
                        state.suppressed = True
                        changed = True
                if (
                    policy.eco_enabled
                    and not state.eco_owned
                    and not state.suppressed
                    and now >= eco_due
                    and self._safe_to_apply(observed)
                ):
                    action = await self._command(zone_id, ActionKind.APPLY_ECO)
                    actions.append(action)
                    if action.succeeded:
                        state.eco_owned = True
                        changed = True

            if cycle_ended:
                if state.eco_owned and not release_attempted:
                    action = await self._command(zone_id, ActionKind.RELEASE_OVERRIDE)
                    actions.append(action)
                    if action.succeeded:
                        state.eco_owned = False
                        changed = True
                    else:
                        # Keep the cycle and ownership so the next evaluation retries.
                        cycle_ended = False
                if state.eco_owned:
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
                if state.open_started_at is not None and not state.warning_raised else None
            )
            eco_deadline = (
                state.open_started_at + policy.eco_delay_seconds
                if state.open_started_at is not None
                and policy.eco_enabled
                and not state.eco_owned
                and not state.suppressed
                else None
            )
            aggregates[zone_id] = ZoneAggregate(
                zone_id=zone_id,
                state=aggregate_state,
                sensor_count=len(items),
                open_count=sum(
                    item.available and item.state is ContactState.OPEN for item in items
                ),
                unavailable_count=sum(not item.available for item in items),
                warning_raised=state.warning_raised,
                open_started_at=state.open_started_at,
                warning_deadline=warning_deadline,
                eco_deadline=eco_deadline,
                eco_owned=state.eco_owned,
                suppressed=state.suppressed,
            )

        stale = set(self.states) - {str(zone_id) for zone_id in policies}
        for zone_id in stale:
            state = self.states[zone_id]
            if state.eco_owned:
                observed = heating.get(zone_id)
                if observed is not None and observed.connected and not self._still_owned(observed):
                    state.eco_owned = False
                    changed = True
                else:
                    action = await self._command(zone_id, ActionKind.RELEASE_OVERRIDE)
                    actions.append(action)
                    if action.succeeded:
                        state.eco_owned = False
                        changed = True
            if not state.eco_owned:
                del self.states[zone_id]
                changed = True

        if changed:
            self._save()
        deadlines = [
            deadline
            for aggregate in aggregates.values()
            for deadline in (aggregate.warning_deadline, aggregate.eco_deadline)
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
        state.eco_owned = False
        if state.open_started_at is not None:
            state.suppressed = True
        self._save()

    def reconcile_owned(self, heating: Mapping[str, HeatingZone]) -> None:
        """On restart, retain ownership only while the observed override is Eco."""
        changed = False
        for zone_id, state in self.states.items():
            observed = heating.get(zone_id)
            if (
                state.eco_owned
                and observed is not None
                and observed.connected
                and not self._still_owned(observed)
            ):
                state.eco_owned = False
                state.suppressed = state.open_started_at is not None
                changed = True
        if changed:
            self._save()

    async def disable(self, heating: Mapping[str, HeatingZone]) -> bool:
        """Release every safely owned override; false means the feature must stay on."""
        success = True
        changed = False
        for zone_id, state in self.states.items():
            if not state.eco_owned:
                continue
            observed = heating.get(zone_id)
            if observed is None or not observed.connected:
                success = False
                continue
            if not self._still_owned(observed):
                state.eco_owned = False
                changed = True
                continue
            action = await self._command(zone_id, ActionKind.RELEASE_OVERRIDE)
            if action.succeeded:
                state.eco_owned = False
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
            and zone.effective_mode.lower() == "comfort"
            and zone.active_override in (None, "", "-1")
        )

    @staticmethod
    def _still_owned(zone: Optional[HeatingZone]) -> bool:
        return bool(
            zone
            and zone.connected
            and zone.effective_mode.lower() == "eco"
            and zone.active_override not in (None, "", "-1")
        )

    async def _command(self, zone_id: str, kind: ActionKind) -> RequestedAction:
        if self._commands is None:
            return RequestedAction(zone_id, kind, False, "no command adapter configured")
        try:
            if kind is ActionKind.APPLY_ECO:
                await self._commands.apply_eco(zone_id)
            else:
                await self._commands.release_override(zone_id)
            return RequestedAction(zone_id, kind, True)
        except Exception as exc:
            return RequestedAction(zone_id, kind, False, str(exc))

    def _save(self) -> None:
        if self._save_fn is not None:
            self._save_fn(self.states)
