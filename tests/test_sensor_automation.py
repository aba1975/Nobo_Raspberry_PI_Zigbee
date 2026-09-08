from datetime import datetime, timezone

import pytest

from sensor_automation import (
    ActionKind,
    AggregateState,
    ConditionEventKind,
    HeatingZone,
    SensorAutomation,
)
from sensor_persistence import AutomationZoneState, ZoneSensorPolicy
from sensor_persistence import ActionWhenOpen
from sensor_provider import ContactSnapshot, ContactState


def sensor(sensor_id, state, *, available=True, zone="1"):
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ContactSnapshot(
        sensor_id, f"p:{sensor_id}", sensor_id, zone, ContactState(state),
        available, 50, stamp, stamp,
    )


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


class Commands:
    def __init__(self):
        self.calls = []
        self.fail_release = False

    async def apply_override(self, zone_id, action):
        self.calls.append((action.value, zone_id))

    async def release_override(self, zone_id):
        self.calls.append(("normal", zone_id))
        if self.fail_release:
            raise RuntimeError("hub unavailable")


def heating(mode="comfort", override=None, equipment=True, connected=True):
    return {
        "1": HeatingZone("1", equipment, connected, mode, override),
    }


@pytest.mark.asyncio
async def test_separate_warning_and_eco_deadlines_and_safe_release():
    clock, commands, saves = Clock(), Commands(), []
    machine = SensorAutomation(
        clock=clock, commands=commands,
        save=lambda state: saves.append(state["1"].owned_action),
    )
    policy = {"1": ZoneSensorPolicy(10, ActionWhenOpen.ECO, 20)}
    first = await machine.evaluate([sensor("a", "open")], policy, heating())
    assert first.next_deadline == 110
    clock.value = 111
    warned = await machine.evaluate([sensor("a", "open")], policy, heating())
    assert [event.kind for event in warned.events] == [ConditionEventKind.WARNING]
    assert commands.calls == []
    clock.value = 121
    applied = await machine.evaluate([sensor("a", "open")], policy, heating())
    assert applied.actions[0].kind is ActionKind.APPLY_OVERRIDE
    assert machine.states["1"].owned_action is ActionWhenOpen.ECO
    closed = await machine.evaluate(
        [sensor("a", "closed")], policy, heating("eco", "zone-override")
    )
    assert closed.actions[0].kind is ActionKind.RELEASE_OVERRIDE
    assert commands.calls[-1] == ("normal", "1")
    assert [event.kind for event in closed.events] == [ConditionEventKind.RECOVERY]
    assert machine.states["1"].open_started_at is None


@pytest.mark.asyncio
async def test_many_sensors_unknown_or_unavailable_cannot_close_cycle():
    clock = Clock()
    machine = SensorAutomation(clock=clock)
    policy = {"1": ZoneSensorPolicy(0, ActionWhenOpen.NOTHING, 0)}
    await machine.evaluate(
        [sensor("a", "open"), sensor("b", "closed")], policy, heating()
    )
    result = await machine.evaluate(
        [sensor("a", "closed"), sensor("b", "unknown")], policy, heating()
    )
    assert result.zones["1"].state is AggregateState.UNKNOWN
    assert machine.states["1"].open_started_at is not None
    result = await machine.evaluate(
        [sensor("a", "closed"), sensor("b", "closed", available=False)],
        policy,
        heating(),
    )
    assert result.zones["1"].state is AggregateState.UNAVAILABLE
    assert machine.states["1"].warning_raised
    recovered = await machine.evaluate(
        [sensor("a", "closed"), sensor("b", "closed")], policy, heating()
    )
    assert recovered.events[-1].kind is ConditionEventKind.RECOVERY


@pytest.mark.asyncio
async def test_eco_safety_and_manual_suppression_for_current_cycle():
    clock, commands = Clock(), Commands()
    machine = SensorAutomation(clock=clock, commands=commands)
    policy = {"1": ZoneSensorPolicy(300, ActionWhenOpen.ECO, 0)}
    await machine.evaluate([sensor("a", "open")], policy, heating(override="manual"))
    assert commands.calls == []
    machine.manual_takeover("1")
    await machine.evaluate([sensor("a", "open")], policy, heating())
    assert commands.calls == []
    await machine.evaluate([sensor("a", "closed")], policy, heating())
    await machine.evaluate([sensor("a", "open")], policy, heating())
    assert commands.calls == [("eco", "1")]


@pytest.mark.asyncio
async def test_restart_reconciliation_never_reasserts_mismatched_ownership():
    commands = Commands()
    state = {"1": AutomationZoneState(50, True, ActionWhenOpen.ECO, False)}
    machine = SensorAutomation(states=state, commands=commands, clock=Clock())
    machine.reconcile_owned(heating("comfort"))
    assert not machine.states["1"].eco_owned
    assert machine.states["1"].suppressed
    await machine.evaluate(
        [sensor("a", "open")],
        {"1": ZoneSensorPolicy(0, ActionWhenOpen.ECO, 0)},
        heating(),
    )
    assert commands.calls == []


@pytest.mark.asyncio
async def test_failed_release_remains_owned_and_disable_reports_failure():
    commands = Commands()
    commands.fail_release = True
    machine = SensorAutomation(
        states={"1": AutomationZoneState(50, True, ActionWhenOpen.ECO, False)},
        commands=commands,
        clock=Clock(),
    )
    result = await machine.evaluate(
        [sensor("a", "closed")],
        {"1": ZoneSensorPolicy(0, ActionWhenOpen.ECO, 0)},
        heating("eco", "owned"),
    )
    assert not result.actions[0].succeeded
    assert machine.states["1"].eco_owned
    assert not await machine.disable(heating("eco", "owned"))
    assert machine.states["1"].eco_owned


@pytest.mark.asyncio
async def test_monitoring_only_disconnected_and_manually_overridden_rooms_are_unchanged():
    for observed in (
        heating(equipment=False),
        heating(connected=False),
        heating(mode="away", override="manual"),
    ):
        commands = Commands()
        machine = SensorAutomation(clock=Clock(), commands=commands)
        await machine.evaluate(
            [sensor("a", "open")],
            {"1": ZoneSensorPolicy(300, ActionWhenOpen.ECO, 0)},
            observed,
        )
        assert commands.calls == []


def test_disconnect_does_not_relinquish_persisted_ownership():
    machine = SensorAutomation(
        states={"1": AutomationZoneState(50, True, ActionWhenOpen.ECO, False)},
        clock=Clock(),
    )
    machine.reconcile_owned(heating("eco", "owned", connected=False))
    assert machine.states["1"].eco_owned


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chosen", "current", "override_all_modes"),
    [
        (ActionWhenOpen.AWAY, "comfort", False),
        (ActionWhenOpen.ECO, "comfort", False),
        (ActionWhenOpen.COMFORT, "eco", True),
    ],
)
async def test_each_override_action_is_owned_and_mode_aware(
    chosen, current, override_all_modes
):
    commands = Commands()
    machine = SensorAutomation(clock=Clock(), commands=commands)
    policy = {"1": ZoneSensorPolicy(300, chosen, 0, override_all_modes)}
    await machine.evaluate([sensor("a", "open")], policy, heating(mode=current))
    assert commands.calls == [(chosen.value, "1")]
    assert machine.states["1"].owned_action is chosen

    machine.reconcile_owned(heating(chosen.value, "owned"))
    assert machine.states["1"].owned_action is chosen
    machine.reconcile_owned(heating("eco" if chosen is not ActionWhenOpen.ECO else "away", "other"))
    assert machine.states["1"].owned_action is None
    assert machine.states["1"].suppressed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "desired"),
    [
        ("away", ActionWhenOpen.ECO),
        ("away", ActionWhenOpen.COMFORT),
        ("eco", ActionWhenOpen.COMFORT),
        ("off", ActionWhenOpen.AWAY),
    ],
)
async def test_sensor_action_does_not_raise_a_colder_mode(current, desired):
    commands = Commands()
    machine = SensorAutomation(clock=Clock(), commands=commands)
    policy = {"1": ZoneSensorPolicy(300, desired, 0)}
    await machine.evaluate([sensor("a", "open")], policy, heating(mode=current))
    assert commands.calls == []
    assert machine.states["1"].owned_action is None


@pytest.mark.asyncio
async def test_sensor_override_can_raise_mode_until_manual_takeover():
    commands = Commands()
    machine = SensorAutomation(clock=Clock(), commands=commands)
    policy = {
        "1": ZoneSensorPolicy(300, ActionWhenOpen.COMFORT, 0, True)
    }
    await machine.evaluate([sensor("a", "open")], policy, heating(mode="away"))
    assert commands.calls == [("comfort", "1")]
    assert machine.states["1"].owned_action is ActionWhenOpen.COMFORT

    machine.manual_takeover("1")
    await machine.evaluate([sensor("a", "open")], policy, heating(mode="away"))
    assert commands.calls == [("comfort", "1")]
    assert machine.states["1"].suppressed


@pytest.mark.asyncio
async def test_disabling_sensor_override_releases_warmer_owned_action():
    commands = Commands()
    machine = SensorAutomation(clock=Clock(), commands=commands)
    override_policy = {
        "1": ZoneSensorPolicy(300, ActionWhenOpen.COMFORT, 0, True)
    }
    await machine.evaluate(
        [sensor("a", "open")], override_policy, heating(mode="away")
    )
    result = await machine.evaluate(
        [sensor("a", "open")],
        {"1": ZoneSensorPolicy(300, ActionWhenOpen.COMFORT, 0, False)},
        heating(mode="comfort", override="owned"),
    )
    assert result.actions[-1].kind is ActionKind.RELEASE_OVERRIDE
    assert commands.calls == [("comfort", "1"), ("normal", "1")]
    assert machine.states["1"].owned_action is None
    assert machine.states["1"].suppressed


@pytest.mark.asyncio
async def test_schedule_never_clears_manual_override_or_creates_one():
    commands = Commands()
    machine = SensorAutomation(clock=Clock(), commands=commands)
    policy = {"1": ZoneSensorPolicy(300, ActionWhenOpen.SCHEDULE, 0)}
    await machine.evaluate(
        [sensor("a", "open")], policy, heating("comfort", "manual")
    )
    await machine.evaluate([sensor("a", "open")], policy, heating("comfort"))
    assert commands.calls == []
    assert machine.states["1"].owned_action is None


@pytest.mark.asyncio
async def test_policy_change_releases_owned_override_but_not_replacement():
    commands = Commands()
    machine = SensorAutomation(
        states={
            "1": AutomationZoneState(
                50, False, ActionWhenOpen.AWAY, False
            )
        },
        commands=commands,
        clock=Clock(),
    )
    await machine.evaluate(
        [sensor("a", "open")],
        {"1": ZoneSensorPolicy(300, ActionWhenOpen.SCHEDULE, 0)},
        heating("away", "owned"),
    )
    assert commands.calls == [("normal", "1")]
    assert machine.states["1"].owned_action is None

    machine = SensorAutomation(
        states={
            "1": AutomationZoneState(
                50, False, ActionWhenOpen.AWAY, False
            )
        },
        commands=commands,
        clock=Clock(),
    )
    commands.calls.clear()
    await machine.evaluate(
        [sensor("a", "open")],
        {"1": ZoneSensorPolicy(300, ActionWhenOpen.SCHEDULE, 0)},
        heating("comfort", "manual-replacement"),
    )
    assert commands.calls == []
    assert machine.states["1"].suppressed
