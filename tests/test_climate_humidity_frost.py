"""Humidity, frost and the 24-hour history of a room thermometer.

Read as a specification:

    Damp air warns only once it has stayed above the room's maximum for the
    whole delay, so a shower is not an alarm, and ends five points under it.
    A room below 5 °C warns at once, whatever its own minimum, and ends at
    6 °C. Neither touches the heating. The history keeps the lowest and
    highest reading of each of the last 24 hours and nothing older.

Fake clock, no hub, as in ``test_climate_sensors``.
"""

import json

import pytest

import sensor_persistence
from climate_history import ClimateHistory, HISTORY_HOURS
from sensor_automation import (
    FROST_HYSTERESIS, FROST_TEMPERATURE, HUMIDITY_HYSTERESIS, ConditionEventKind,
)
from sensor_persistence import (
    ActionWhenOpen, AutomationZoneState, SensorSettings, ZoneSensorPolicy,
    load_automation_state, load_climate_history, load_sensor_settings,
    save_automation_state, save_climate_history, save_sensor_settings,
)
from tests.test_climate_sensors import START, Clock, Commands, machine, room, thermometer


def damp(limit=70, delay=3600, frost=True):
    return {"1": ZoneSensorPolicy(
        humidity_max=limit, humidity_delay_seconds=delay, frost_warning=frost,
    )}


def kinds(result):
    return [(event.kind, event.condition) for event in result.events]


# ---------------------------------------------------------------------------
# Humidity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_damp_air_warns_only_after_the_whole_delay():
    clock = Clock()
    commands = Commands()
    engine = machine(commands, clock=clock)
    first = await engine.evaluate([thermometer(humidity=80)], damp(), room())
    assert first.events == ()
    assert first.zones["1"].climate.humidity_since == START
    assert first.zones["1"].climate.humidity_deadline == START + 3600

    clock.value = START + 3599
    assert (await engine.evaluate([thermometer(humidity=80, heard=clock.value)],
                                  damp(), room())).events == ()
    clock.value = START + 3600
    result = await engine.evaluate([thermometer(humidity=80, heard=clock.value)],
                                   damp(), room())
    assert kinds(result) == [(ConditionEventKind.WARNING, "humid")]
    assert result.zones["1"].climate.humidity_raised is True
    # Warn-only: there is no heating answer to a shower.
    assert commands.calls == []


@pytest.mark.asyncio
async def test_a_shower_that_clears_inside_the_delay_never_warns():
    clock = Clock()
    engine = machine(clock=clock)
    await engine.evaluate([thermometer(humidity=85)], damp(), room())
    clock.value = START + 1800
    back = await engine.evaluate([thermometer(humidity=70, heard=clock.value)],
                                 damp(), room())
    # On the limit is not over it, and the count starts again.
    assert back.zones["1"].climate.humidity_since is None
    clock.value = START + 1900
    again = await engine.evaluate([thermometer(humidity=85, heard=clock.value)],
                                  damp(), room())
    assert again.zones["1"].climate.humidity_since == START + 1900
    clock.value = START + 3700
    assert (await engine.evaluate([thermometer(humidity=85, heard=clock.value)],
                                  damp(), room())).events == ()


@pytest.mark.asyncio
async def test_no_delay_warns_on_the_first_damp_reading():
    engine = machine()
    result = await engine.evaluate([thermometer(humidity=71)], damp(delay=0), room())
    assert kinds(result) == [(ConditionEventKind.WARNING, "humid")]


@pytest.mark.asyncio
async def test_damp_air_ends_only_five_points_under_the_maximum():
    engine = machine()
    policy = damp(delay=0)
    await engine.evaluate([thermometer(humidity=80)], policy, room())
    for value in (70, 66):
        result = await engine.evaluate([thermometer(humidity=value)], policy, room())
        assert result.events == ()
        assert result.zones["1"].climate.humidity_raised is True
    result = await engine.evaluate(
        [thermometer(humidity=70 - HUMIDITY_HYSTERESIS)], policy, room()
    )
    assert kinds(result) == [(ConditionEventKind.RECOVERY, "humid")]
    assert result.zones["1"].climate.humidity_raised is False


@pytest.mark.asyncio
async def test_damp_air_ends_quietly_when_the_reading_or_the_rule_goes():
    engine = machine()
    await engine.evaluate([thermometer(humidity=80)], damp(delay=0), room())
    gone = await engine.evaluate([thermometer(humidity=80, available=False)],
                                 damp(delay=0), room())
    assert gone.events == ()
    assert gone.zones["1"].climate.humidity_raised is False

    await engine.evaluate([thermometer(humidity=80)], damp(delay=0), room())
    off = await engine.evaluate([thermometer(humidity=80)], damp(limit=None), room())
    assert off.events == ()
    assert off.zones["1"].climate.humidity_raised is False


@pytest.mark.asyncio
async def test_a_humidity_warning_is_not_raised_again_after_a_restart():
    saved = []
    engine = machine(saved=saved)
    await engine.evaluate([thermometer(humidity=80)], damp(delay=0), room())
    restarted = machine(states=saved[-1])
    result = await restarted.evaluate([thermometer(humidity=80)], damp(delay=0), room())
    assert result.events == ()
    assert result.zones["1"].climate.humidity_raised is True


@pytest.mark.asyncio
async def test_a_room_with_no_heater_still_warns_about_damp():
    engine = machine()
    result = await engine.evaluate(
        [thermometer(humidity=90)], damp(delay=0), room(equipment=False)
    )
    assert kinds(result) == [(ConditionEventKind.WARNING, "humid")]


# ---------------------------------------------------------------------------
# Frost
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_room_near_freezing_warns_at_once_without_any_minimum():
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [thermometer(temperature=FROST_TEMPERATURE - 0.1)], damp(limit=None), room()
    )
    assert kinds(result) == [(ConditionEventKind.WARNING, "frost")]
    assert result.zones["1"].climate.frost_raised is True
    assert result.zones["1"].climate.condition is None
    assert commands.calls == []


@pytest.mark.asyncio
async def test_exactly_five_degrees_is_not_near_freezing():
    engine = machine()
    result = await engine.evaluate(
        [thermometer(temperature=FROST_TEMPERATURE)], damp(limit=None), room()
    )
    assert result.events == ()


@pytest.mark.asyncio
async def test_frost_ends_a_whole_degree_above_the_line():
    engine = machine()
    policy = damp(limit=None)
    await engine.evaluate([thermometer(temperature=3.0)], policy, room())
    held = await engine.evaluate([thermometer(temperature=5.9)], policy, room())
    assert held.events == ()
    over = await engine.evaluate(
        [thermometer(temperature=FROST_TEMPERATURE + FROST_HYSTERESIS)], policy, room()
    )
    assert kinds(over) == [(ConditionEventKind.RECOVERY, "frost")]


@pytest.mark.asyncio
async def test_a_room_meant_to_be_cold_can_switch_frost_off():
    engine = machine()
    result = await engine.evaluate(
        [thermometer(temperature=1.0)], damp(limit=None, frost=False), room()
    )
    assert result.events == ()
    assert result.zones["1"].climate.frost_raised is False


@pytest.mark.asyncio
async def test_switching_frost_off_while_raised_clears_it_quietly():
    engine = machine()
    await engine.evaluate([thermometer(temperature=1.0)], damp(limit=None), room())
    result = await engine.evaluate(
        [thermometer(temperature=1.0)], damp(limit=None, frost=False), room()
    )
    assert result.events == ()
    assert result.zones["1"].climate.frost_raised is False


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_humidity_rules_are_checked():
    with pytest.raises(ValueError):
        ZoneSensorPolicy(humidity_max=30)
    with pytest.raises(ValueError):
        ZoneSensorPolicy(humidity_max=101)
    with pytest.raises(ValueError):
        ZoneSensorPolicy(humidity_max=70, humidity_delay_seconds=-1)


def test_settings_from_before_humidity_load_with_frost_on(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "schema_version": 5, "enabled": True, "provider": "simulated",
        "zones": {"1": {
            "warning_delay_seconds": 60, "action_when_open": "eco",
            "action_delay_seconds": 120, "override_all_modes": False,
            "temperature_max": 24, "action_when_too_warm": "eco",
            "temperature_min": None, "action_when_too_cold": "nothing",
        }},
    }))
    policy = load_sensor_settings(path).zones["1"]
    assert policy.temperature_max == 24
    assert policy.humidity_max is None
    assert policy.humidity_delay_seconds == 3600
    assert policy.frost_warning is True


def test_humidity_and_frost_settings_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    policy = ZoneSensorPolicy(
        action_when_open=ActionWhenOpen.ECO,
        humidity_max=65, humidity_delay_seconds=900, frost_warning=False,
    )
    save_sensor_settings(SensorSettings(True, "simulated", {"1": policy}), path)
    assert json.loads(path.read_text())["schema_version"] == sensor_persistence.SCHEMA_VERSION
    assert load_sensor_settings(path).zones["1"] == policy


def test_raised_humidity_and_frost_survive_a_restart(tmp_path):
    path = tmp_path / "automation.json"
    save_automation_state({"1": AutomationZoneState(
        humidity_since=START, humidity_raised=True, frost_raised=True,
    )}, path)
    state = load_automation_state(path)["1"]
    assert state.humidity_since == START
    assert state.humidity_raised is True
    assert state.frost_raised is True


def test_automation_state_from_before_humidity_loads_clear(tmp_path):
    path = tmp_path / "automation.json"
    path.write_text(json.dumps({
        "schema_version": 5,
        "zones": {"1": {
            "open_started_at": 10, "warning_raised": True, "owned_action": None,
            "owned_reason": "open", "climate_condition": None, "climate_since": None,
        }},
    }))
    state = load_automation_state(path)["1"]
    assert state.warning_raised is True
    assert state.humidity_raised is False and state.frost_raised is False


# ---------------------------------------------------------------------------
# 24-hour history
# ---------------------------------------------------------------------------

HOUR = 3600
BASE = (int(START) // HOUR) * HOUR


def test_history_keeps_the_lowest_and_highest_of_each_hour():
    saved = []
    history = ClimateHistory(save=saved.append)
    history.record({"1": (BASE + 60, 20.0, 50.0)}, BASE + 60)
    history.record({"1": (BASE + 120, 22.0, 40.0)}, BASE + 120)
    history.record({"1": (BASE + HOUR + 5, 19.0, 60.0)}, BASE + HOUR + 5)
    summary = history.summary("1", BASE + HOUR + 5)
    assert summary["temperature_min"] == 19.0
    assert summary["temperature_max"] == 22.0
    assert summary["humidity_min"] == 40.0
    assert summary["humidity_max"] == 60.0
    assert [row["start"] for row in summary["hours"]] == [BASE, BASE + HOUR]
    assert summary["hours"][0] == {
        "start": BASE, "t_min": 20.0, "t_max": 22.0, "h_min": 40.0, "h_max": 50.0,
    }


def test_history_forgets_anything_older_than_a_day():
    history = ClimateHistory(save=None)
    history.record({"1": (BASE, 10.0, None)}, BASE)
    later = BASE + HISTORY_HOURS * HOUR
    history.record({"1": (later, 20.0, None)}, later)
    summary = history.summary("1", later)
    assert summary["temperature_min"] == 20.0
    assert [row["start"] for row in summary["hours"]] == [later]
    assert summary["humidity_min"] is None


def test_a_reading_older_than_the_window_is_not_recorded():
    history = ClimateHistory(save=None)
    history.record({"1": (BASE - HISTORY_HOURS * HOUR, 5.0, 30.0)}, BASE)
    assert history.summary("1", BASE) is None


def test_history_saves_only_when_something_changed():
    saved = []
    history = ClimateHistory(save=saved.append)
    history.record({"1": (BASE + 10, 20.0, 45.0)}, BASE + 10)
    history.record({"1": (BASE + 20, 20.0, 45.0)}, BASE + 20)
    history.record({}, BASE + 30)
    assert len(saved) == 1


def test_history_round_trips_and_refuses_bad_rows(tmp_path):
    path = tmp_path / "history.json"
    zones = {"1": [{"start": BASE, "t_min": 20.0, "t_max": 21.0,
                    "h_min": 40.0, "h_max": 45.0}]}
    save_climate_history(zones, path)
    assert load_climate_history(path) == zones

    for bad in (
        {"start": BASE + 1, "t_min": 20.0, "t_max": 21.0, "h_min": None, "h_max": None},
        {"start": BASE, "t_min": 22.0, "t_max": 21.0, "h_min": None, "h_max": None},
        {"start": BASE, "t_min": 500.0, "t_max": 501.0, "h_min": None, "h_max": None},
        {"start": BASE, "t_min": 20.0, "t_max": 21.0},
    ):
        path.write_text(json.dumps({"schema_version": 1, "zones": {"1": [bad]}}))
        assert load_climate_history(path) == {}
    # The same file with a good row loads, so the refusals above are the rows'.
    path.write_text(json.dumps({"schema_version": 1, "zones": zones}))
    assert load_climate_history(path) == zones
