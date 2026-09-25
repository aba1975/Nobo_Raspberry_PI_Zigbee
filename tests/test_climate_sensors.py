"""Room thermometers: readings, thresholds and the temperature rule.

Read as a specification. The rules the file is about:

    A room is too warm once a fresh reading is above its maximum, and stops
    being too warm only half a degree below it; too cold is the mirror image.
    A maximum may only make the room colder and a minimum only warmer. While
    an open window's own rule has the room, the window decides.

Everything is on a fake clock with no hub, as in ``test_sensor_automation``.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from sensor_automation import (
    CLIMATE_HYSTERESIS,
    CLIMATE_STALE_SECONDS,
    ActionStatus,
    AggregateState,
    BlockReason,
    ConditionEventKind,
    HeatingZone,
    SETTLING_SECONDS,
    SensorAutomation,
    climate_reading,
)
from sensor_persistence import (
    ActionWhenOpen,
    AutomationZoneState,
    ClimateCondition,
    HoldReason,
    ZoneSensorPolicy,
    load_automation_state,
    load_sensor_settings,
    load_simulated_sensors,
    load_zigbee_metadata,
    save_automation_state,
    save_sensor_settings,
    SensorSettings,
)
from sensor_provider import (
    ContactSnapshot, ContactState, SensorKind, check_kind_change, valid_reading,
)
from sensor_simulated import (
    DEMO_CLIMATE, SIMULATED_REPORT_SECONDS, SimulatedContactSensorProvider,
)

START = 1_800_000_000.0


class Clock:
    def __init__(self, value=START):
        self.value = value

    def __call__(self):
        return self.value


class Commands:
    def __init__(self):
        self.calls = []

    async def apply_override(self, zone_id, action):
        self.calls.append((action.value, zone_id))

    async def release_override(self, zone_id):
        self.calls.append(("normal", zone_id))


def at(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc)


def thermometer(sensor_id="t1", temperature=21.0, *, heard=START, available=True,
                zone="1", humidity=45.0, pressure=1013.0):
    return ContactSnapshot(
        sensor_id, f"p:{sensor_id}", sensor_id, zone, ContactState.UNKNOWN,
        available, 90, at(heard), at(heard), kind=SensorKind.CLIMATE,
        temperature=temperature, humidity=humidity, pressure=pressure,
    )


def contact(sensor_id, state, *, zone="1"):
    return ContactSnapshot(
        sensor_id, f"p:{sensor_id}", sensor_id, zone, ContactState(state),
        True, 90, at(START), at(START),
    )


def room(running="comfort", *, fallback=None, held=False, equipment=True,
         connected=True, may_act=True):
    return {"1": HeatingZone(
        zone_id="1", has_equipment=equipment, connected=connected,
        effective_mode=running,
        fallback_mode=fallback if fallback is not None else running,
        has_zone_override=held, sensors_may_act=may_act,
    )}


def rules(*, high=None, warm="nothing", low=None, cold="nothing",
          open_action="nothing", open_delay=0):
    return {"1": ZoneSensorPolicy(
        300, ActionWhenOpen(open_action), open_delay, False,
        temperature_max=high, action_when_too_warm=ActionWhenOpen(warm),
        temperature_min=low, action_when_too_cold=ActionWhenOpen(cold),
    )}


def machine(commands=None, clock=None, states=None, saved=None):
    return SensorAutomation(
        states=states, commands=commands, clock=clock or Clock(),
        save=(saved.append if saved is not None else None),
    )


# ---------------------------------------------------------------------------
# The reading
# ---------------------------------------------------------------------------

def test_the_room_reading_is_the_average_of_fresh_available_thermometers():
    now = START
    reading = climate_reading([
        thermometer("a", 20.0, humidity=40.0),
        thermometer("b", 22.0, humidity=50.0),
        thermometer("offline", 5.0, available=False),
        thermometer("stale", 30.0, heard=now - CLIMATE_STALE_SECONDS - 1),
        thermometer("silent", None),
    ], now)

    assert reading.temperature == 21.0
    assert reading.humidity == 45.0
    assert reading.sensor_count == 5
    assert reading.fresh_count == 2


def test_a_contact_is_never_averaged_as_a_thermometer():
    reading = climate_reading([contact("w", "open")], START)
    assert reading.sensor_count == 0
    assert reading.temperature is None


@pytest.mark.parametrize("name, value, expected", [
    ("temperature", 21.26, 21.3),
    ("temperature", -41, None),
    ("temperature", 81, None),
    ("humidity", 101, None),
    ("pressure", 299, None),
    ("pressure", 1013.04, 1013.0),
    ("temperature", True, None),
    ("temperature", "21", None),
])
def test_readings_are_range_checked_against_what_the_hardware_reports(name, value, expected):
    assert valid_reading(name, value) == expected


def test_a_contact_cannot_become_a_thermometer_or_the_other_way():
    with pytest.raises(ValueError):
        check_kind_change(SensorKind.WINDOW, SensorKind.CLIMATE)
    with pytest.raises(ValueError):
        check_kind_change(SensorKind.CLIMATE, SensorKind.DOOR)
    check_kind_change(SensorKind.WINDOW, SensorKind.DOOR)
    check_kind_change(SensorKind.CLIMATE, SensorKind.CLIMATE)


# ---------------------------------------------------------------------------
# Thresholds and hysteresis
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_too_warm_warns_at_once_and_does_nothing_by_default():
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate([thermometer(temperature=24.1)], rules(high=24), room())

    assert [(e.kind, e.condition) for e in result.events] == [
        (ConditionEventKind.WARNING, "too_warm")
    ]
    assert result.zones["1"].climate.condition is ClimateCondition.TOO_WARM
    assert commands.calls == []


@pytest.mark.asyncio
async def test_a_reading_exactly_on_the_limit_is_not_over_it():
    engine = machine()
    result = await engine.evaluate([thermometer(temperature=24.0)], rules(high=24), room())
    assert result.events == ()
    assert result.zones["1"].climate.condition is None


@pytest.mark.asyncio
async def test_too_warm_ends_only_half_a_degree_back_inside():
    clock = Clock()
    engine = machine(clock=clock)
    policy = rules(high=24)
    await engine.evaluate([thermometer(temperature=24.5)], policy, room())

    # Hovering on the line does not flap.
    for value in (24.0, 23.6):
        result = await engine.evaluate([thermometer(temperature=value)], policy, room())
        assert result.events == ()
        assert result.zones["1"].climate.condition is ClimateCondition.TOO_WARM

    result = await engine.evaluate(
        [thermometer(temperature=24 - CLIMATE_HYSTERESIS)], policy, room()
    )
    assert [(e.kind, e.condition) for e in result.events] == [
        (ConditionEventKind.RECOVERY, "too_warm")
    ]
    assert result.zones["1"].climate.condition is None


@pytest.mark.asyncio
async def test_too_cold_is_the_mirror_image():
    engine = machine()
    policy = rules(low=10)
    first = await engine.evaluate([thermometer(temperature=9.9)], policy, room())
    assert [(e.kind, e.condition) for e in first.events] == [
        (ConditionEventKind.WARNING, "too_cold")
    ]
    still = await engine.evaluate([thermometer(temperature=10.4)], policy, room())
    assert still.zones["1"].climate.condition is ClimateCondition.TOO_COLD
    over = await engine.evaluate([thermometer(temperature=10.5)], policy, room())
    assert [e.kind for e in over.events] == [ConditionEventKind.RECOVERY]


@pytest.mark.asyncio
async def test_a_warning_already_raised_is_not_raised_again_after_a_restart():
    saved = []
    engine = machine(saved=saved)
    await engine.evaluate([thermometer(temperature=26)], rules(high=24), room())
    persisted = saved[-1]

    restarted = machine(states=persisted)
    result = await restarted.evaluate([thermometer(temperature=26)], rules(high=24), room())
    assert result.events == ()
    assert result.zones["1"].climate.condition is ClimateCondition.TOO_WARM


# ---------------------------------------------------------------------------
# What the heating does
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_ceiling_holds_eco_until_the_room_has_cooled_then_lets_go():
    commands = Commands()
    engine = machine(commands)
    policy = rules(high=24, warm="eco")

    held = await engine.evaluate([thermometer(temperature=25)], policy, room("comfort"))
    assert commands.calls == [("eco", "1")]
    assert engine.states["1"].owned_reason is HoldReason.TOO_WARM
    assert held.zones["1"].climate.owned_action is ActionWhenOpen.ECO
    assert held.zones["1"].climate.action_status is ActionStatus.ACTIVE
    # The contact rule's own field says nothing: this hold is not a window's.
    assert held.zones["1"].owned_action is None

    await engine.evaluate(
        [thermometer(temperature=23.5)], policy,
        room("eco", fallback="comfort", held=True),
    )
    # Released to the schedule. Never "set Comfort": the room goes back to
    # whatever it would have been doing.
    assert commands.calls == [("eco", "1"), ("normal", "1")]
    assert engine.states["1"].owned_action is None


@pytest.mark.asyncio
async def test_a_ceiling_never_warms_a_room_that_is_already_colder():
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [thermometer(temperature=25)], rules(high=24, warm="eco"), room("away"),
    )
    assert commands.calls == []
    assert result.zones["1"].climate.action_status is ActionStatus.BLOCKED
    assert result.zones["1"].climate.block_reason is BlockReason.COLDER_MODE


@pytest.mark.asyncio
async def test_a_floor_beats_a_global_away():
    """The point of a minimum: the house is on Away, and a room with plumbing
    in it must still not freeze."""
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [thermometer(temperature=4)], rules(low=6, cold="eco"), room("away"),
    )
    assert commands.calls == [("eco", "1")]
    assert engine.states["1"].owned_reason is HoldReason.TOO_COLD
    assert result.zones["1"].climate.owned_action is ActionWhenOpen.ECO


@pytest.mark.asyncio
async def test_a_floor_never_cools_a_room_that_is_already_warmer():
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [thermometer(temperature=4)], rules(low=6, cold="eco"), room("comfort"),
    )
    assert commands.calls == []
    assert result.zones["1"].climate.block_reason is BlockReason.WARMER_MODE


@pytest.mark.asyncio
async def test_a_stale_reading_ends_the_condition_quietly_and_lets_go():
    """A flat battery is not news that the room has cooled down."""
    clock = Clock()
    commands = Commands()
    engine = machine(commands, clock)
    policy = rules(high=24, warm="eco")
    await engine.evaluate([thermometer(temperature=26)], policy, room("comfort"))

    clock.value = START + CLIMATE_STALE_SECONDS + 1
    result = await engine.evaluate(
        [thermometer(temperature=26)], policy, room("eco", fallback="comfort", held=True),
    )
    assert result.events == ()
    assert result.zones["1"].climate.condition is None
    assert commands.calls == [("eco", "1"), ("normal", "1")]


@pytest.mark.asyncio
async def test_an_offline_thermometer_ends_it_quietly_too():
    commands = Commands()
    engine = machine(commands)
    policy = rules(high=24, warm="eco")
    await engine.evaluate([thermometer(temperature=26)], policy, room("comfort"))
    result = await engine.evaluate(
        [thermometer(temperature=26, available=False)], policy,
        room("eco", fallback="comfort", held=True),
    )
    assert result.events == ()
    assert commands.calls[-1] == ("normal", "1")


@pytest.mark.asyncio
async def test_switching_the_maximum_off_lets_go_without_a_recovery():
    commands = Commands()
    engine = machine(commands)
    await engine.evaluate([thermometer(temperature=26)], rules(high=24, warm="eco"), room())
    result = await engine.evaluate(
        [thermometer(temperature=26)], rules(), room("eco", fallback="comfort", held=True),
    )
    assert result.events == ()
    assert commands.calls[-1] == ("normal", "1")


@pytest.mark.asyncio
async def test_a_hand_set_mode_ends_the_ledger_and_is_not_released_by_it():
    """Somebody pressed Away on the zone while the ceiling held Eco. The rule
    stops calling the zone its own and never sends NORMAL over their choice;
    Away is colder than Eco, so the ceiling has nothing left to hold."""
    clock = Clock()
    commands = Commands()
    engine = machine(commands, clock)
    policy = rules(high=24, warm="eco")
    await engine.evaluate([thermometer(temperature=26)], policy, room("comfort"))
    # Past the moment where a disagreement could be our own command in flight.
    clock.value += SETTLING_SECONDS + 1
    result = await engine.evaluate(
        [thermometer(temperature=26, heard=clock.value)], policy,
        room("away", fallback="comfort", held=True),
    )
    assert commands.calls == [("eco", "1")]
    assert engine.states["1"].owned_action is None
    assert result.zones["1"].climate.block_reason is BlockReason.COLDER_MODE


@pytest.mark.asyncio
async def test_demo_thermometers_warn_but_never_touch_a_real_heater():
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [thermometer(temperature=26)], rules(high=24, warm="eco"), room(may_act=False),
    )
    assert [e.condition for e in result.events] == ["too_warm"]
    assert commands.calls == []
    assert result.zones["1"].climate.block_reason is BlockReason.DEMO_SENSORS


@pytest.mark.asyncio
async def test_a_room_with_no_heater_still_warns():
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [thermometer(temperature=3)], rules(low=5, cold="comfort"), room(equipment=False),
    )
    assert [e.condition for e in result.events] == ["too_cold"]
    assert commands.calls == []
    assert result.zones["1"].climate.block_reason is BlockReason.NO_EQUIPMENT


@pytest.mark.asyncio
async def test_a_thermometer_does_not_make_a_room_read_state_unknown():
    engine = machine()
    result = await engine.evaluate([thermometer()], rules(), room())
    assert result.zones["1"].sensor_count == 0
    assert result.zones["1"].state is AggregateState.EMPTY


# ---------------------------------------------------------------------------
# Windows and thermometers in the same room
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_open_window_has_the_room_while_its_rule_is_due():
    commands = Commands()
    engine = machine(commands)
    policy = rules(high=24, warm="away", open_action="eco", open_delay=0)
    result = await engine.evaluate(
        [thermometer(temperature=26), contact("w", "open")], policy, room("comfort"),
    )
    assert commands.calls == [("eco", "1")]
    assert engine.states["1"].owned_reason is HoldReason.OPEN
    assert result.zones["1"].climate.block_reason is BlockReason.CONTACT_OPEN
    # Both are still announced: the rule stood down, the warning did not.
    assert result.zones["1"].climate.condition is ClimateCondition.TOO_WARM


@pytest.mark.asyncio
async def test_a_window_closing_in_a_hot_room_hands_its_eco_across_without_a_flicker():
    commands = Commands()
    engine = machine(commands)
    policy = rules(high=24, warm="eco", open_action="eco", open_delay=0)
    await engine.evaluate(
        [thermometer(temperature=26), contact("w", "open")], policy, room("comfort"),
    )
    await engine.evaluate(
        [thermometer(temperature=26), contact("w", "closed")], policy,
        room("eco", fallback="comfort", held=True),
    )
    # No release and re-apply: the one Eco simply changes owner.
    assert commands.calls == [("eco", "1")]
    assert engine.states["1"].owned_reason is HoldReason.TOO_WARM


@pytest.mark.asyncio
async def test_a_window_opening_takes_over_the_ceilings_hold_the_same_way():
    commands = Commands()
    engine = machine(commands)
    policy = rules(high=24, warm="eco", open_action="eco", open_delay=0)
    await engine.evaluate([thermometer(temperature=26)], policy, room("comfort"))
    await engine.evaluate(
        [thermometer(temperature=26), contact("w", "open")], policy,
        room("eco", fallback="comfort", held=True),
    )
    assert commands.calls == [("eco", "1")]
    assert engine.states["1"].owned_reason is HoldReason.OPEN


@pytest.mark.asyncio
async def test_a_window_closing_in_a_room_that_is_fine_releases_as_before():
    commands = Commands()
    engine = machine(commands)
    policy = rules(high=24, warm="eco", open_action="eco", open_delay=0)
    await engine.evaluate(
        [thermometer(temperature=21), contact("w", "open")], policy, room("comfort"),
    )
    await engine.evaluate(
        [thermometer(temperature=21), contact("w", "closed")], policy,
        room("eco", fallback="comfort", held=True),
    )
    assert commands.calls == [("eco", "1"), ("normal", "1")]


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_engine_looks_again_before_a_reading_goes_stale():
    engine = machine()
    result = await engine.evaluate(
        [thermometer(temperature=21, heard=START - 100)], rules(high=24), room(),
    )
    assert result.next_deadline == START - 100 + CLIMATE_STALE_SECONDS


# ---------------------------------------------------------------------------
# Rules and persistence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"temperature_max": 41},
    {"temperature_min": -1},
    {"temperature_max": 20, "temperature_min": 19.5},
    {"action_when_too_warm": ActionWhenOpen.COMFORT},
    {"action_when_too_cold": ActionWhenOpen.AWAY},
    {"action_when_too_warm": ActionWhenOpen.SCHEDULE},
])
def test_rules_that_make_no_sense_are_refused(kwargs):
    with pytest.raises(ValueError):
        ZoneSensorPolicy(**kwargs)


def test_a_threshold_is_kept_to_a_tenth_of_a_degree():
    assert ZoneSensorPolicy(temperature_max=23.456).temperature_max == 23.5


def test_settings_from_before_thermometers_load_with_no_limits(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "schema_version": 4, "enabled": True, "provider": "zigbee2mqtt",
        "zones": {"1": {
            "warning_delay_seconds": 60, "action_when_open": "eco",
            "action_delay_seconds": 120, "override_all_modes": False,
        }},
    }))
    policy = load_sensor_settings(path).zones["1"]
    assert policy == ZoneSensorPolicy(60, ActionWhenOpen.ECO, 120, False)
    assert policy.temperature_max is None and policy.temperature_min is None


def test_limits_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    policy = ZoneSensorPolicy(
        temperature_max=24, action_when_too_warm=ActionWhenOpen.AWAY,
        temperature_min=8, action_when_too_cold=ActionWhenOpen.COMFORT,
    )
    save_sensor_settings(SensorSettings(True, "simulated", {"1": policy}), path)
    assert json.loads(path.read_text())["schema_version"] == 5
    assert load_sensor_settings(path).zones["1"] == policy


def test_automation_state_from_before_thermometers_belongs_to_the_window(tmp_path):
    path = tmp_path / "automation.json"
    path.write_text(json.dumps({
        "schema_version": 4,
        "zones": {"1": {
            "open_started_at": 10, "warning_raised": True, "owned_action": "eco",
        }},
    }))
    state = load_automation_state(path)["1"]
    assert state.owned_reason is HoldReason.OPEN
    assert state.climate_condition is None


def test_a_climate_condition_survives_a_restart(tmp_path):
    path = tmp_path / "automation.json"
    save_automation_state({"1": AutomationZoneState(
        owned_action=ActionWhenOpen.ECO, owned_reason=HoldReason.TOO_WARM,
        climate_condition=ClimateCondition.TOO_WARM, climate_since=START,
    )}, path)
    state = load_automation_state(path)["1"]
    assert state.owned_reason is HoldReason.TOO_WARM
    assert state.climate_condition is ClimateCondition.TOO_WARM
    assert state.climate_since == START


def test_the_zigbee_names_written_before_thermometers_still_load(tmp_path):
    """The production Pi has a v4 metadata file. Refusing it would drop every
    sensor's name and room on the first start of this build."""
    path = tmp_path / "zigbee.json"
    path.write_text(json.dumps({
        "schema_version": 4,
        "sensors": {"0x00158d0001a2b3c4": {
            "name": "Bathroom window", "kind": "window", "zone_id": "3",
            "last_seen": "2026-09-01T10:00:00+00:00", "battery": 91, "link_quality": 120,
        }},
    }))
    row = load_zigbee_metadata(path)["0x00158d0001a2b3c4"]
    assert row["name"] == "Bathroom window"
    assert row["zone_id"] == "3"
    assert row["temperature"] is None


def test_a_contact_row_carrying_a_reading_is_refused(tmp_path):
    path = tmp_path / "sensors.json"
    path.write_text(json.dumps({"schema_version": 5, "sensors": [{
        "sensor_id": "x", "provider_id": "simulated:x", "name": "Window",
        "zone_id": "1", "state": "closed", "available": True, "battery": None,
        "changed_at": "2026-01-01T00:00:00+00:00",
        "last_seen_at": "2026-01-01T00:00:00+00:00",
        "kind": "window", "link_quality": None,
        "temperature": 21.0, "humidity": None, "pressure": None,
    }]}))
    # Refused files are backed up and replaced with the default.
    assert load_simulated_sensors(path) == []


# ---------------------------------------------------------------------------
# The simulator
# ---------------------------------------------------------------------------

class WallClock:
    def __init__(self):
        self.value = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


@pytest.mark.asyncio
async def test_a_simulated_thermometer_starts_with_a_believable_room(tmp_path):
    provider = SimulatedContactSensorProvider(path=tmp_path / "s.json")
    await provider.start()
    created = await provider.create("Lounge", "1", SensorKind.CLIMATE)
    assert created.temperature == DEMO_CLIMATE["temperature"]
    assert created.humidity == DEMO_CLIMATE["humidity"]
    assert created.pressure == DEMO_CLIMATE["pressure"]
    # It has no contact, so it claims no contact state.
    assert created.state is ContactState.UNKNOWN


@pytest.mark.asyncio
async def test_readings_can_be_simulated_and_are_range_checked(tmp_path):
    provider = SimulatedContactSensorProvider(path=tmp_path / "s.json")
    await provider.start()
    created = await provider.create("Lounge", "1", SensorKind.CLIMATE)
    changed = await provider.simulate(created.sensor_id, temperature=25.26, humidity=60)
    assert changed.temperature == 25.3
    assert changed.humidity == 60.0
    assert changed.pressure == DEMO_CLIMATE["pressure"]
    with pytest.raises(ValueError):
        await provider.simulate(created.sensor_id, temperature=90)
    with pytest.raises(ValueError):
        await provider.simulate(created.sensor_id, state="open")

    window = await provider.create("Window", "1")
    with pytest.raises(ValueError):
        await provider.simulate(window.sensor_id, temperature=20)
    with pytest.raises(ValueError):
        await provider.update(window.sensor_id, kind=SensorKind.CLIMATE)


@pytest.mark.asyncio
async def test_a_simulated_thermometer_keeps_reporting_like_a_real_one(tmp_path):
    clock = WallClock()
    provider = SimulatedContactSensorProvider(path=tmp_path / "s.json", now=clock)
    await provider.start()
    created = await provider.create("Lounge", "1", SensorKind.CLIMATE)

    clock.value += timedelta(seconds=SIMULATED_REPORT_SECONDS + 1)
    heard = (await provider.list())[0].last_seen_at
    assert heard == clock.value
    assert heard > created.last_seen_at


@pytest.mark.asyncio
async def test_a_thermometer_set_to_unavailable_stays_silent(tmp_path):
    clock = WallClock()
    provider = SimulatedContactSensorProvider(path=tmp_path / "s.json", now=clock)
    await provider.start()
    created = await provider.create("Lounge", "1", SensorKind.CLIMATE)
    await provider.simulate(created.sensor_id, available=False)
    before = (await provider.list())[0].last_seen_at

    clock.value += timedelta(seconds=CLIMATE_STALE_SECONDS + 1)
    assert (await provider.list())[0].last_seen_at == before


@pytest.mark.asyncio
async def test_simulated_readings_survive_a_restart(tmp_path):
    path = tmp_path / "s.json"
    provider = SimulatedContactSensorProvider(path=path)
    await provider.start()
    created = await provider.create("Lounge", "1", SensorKind.CLIMATE)
    await provider.simulate(created.sensor_id, temperature=18.5)

    restarted = SimulatedContactSensorProvider(path=path)
    await restarted.start()
    again = (await restarted.list())[0]
    assert again.kind is SensorKind.CLIMATE
    assert again.temperature == 18.5
