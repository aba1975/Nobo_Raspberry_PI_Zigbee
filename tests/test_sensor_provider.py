import asyncio
import json
from datetime import datetime, timezone

import pytest

from sensor_persistence import (
    ActionWhenOpen,
    AutomationZoneState,
    SensorSettings,
    ZoneSensorPolicy,
    load_automation_state,
    load_sensor_settings,
    load_simulated_sensors,
    save_automation_state,
    save_sensor_settings,
)
from sensor_provider import ContactState, SensorEventKind, SensorKind, create_provider
from sensor_simulated import SensorNotFound, SimulatedContactSensorProvider


def test_settings_and_automation_round_trip(tmp_path):
    settings_path = tmp_path / "settings.json"
    state_path = tmp_path / "state.json"
    settings = SensorSettings(
        enabled=True,
        zones={"7": ZoneSensorPolicy(10, ActionWhenOpen.AWAY, 20)},
    )
    save_sensor_settings(settings, settings_path)
    save_automation_state(
        {"7": AutomationZoneState(12.5, True, ActionWhenOpen.AWAY, False)}, state_path
    )
    assert load_sensor_settings(settings_path) == settings
    assert load_automation_state(state_path) == {
        "7": AutomationZoneState(12.5, True, ActionWhenOpen.AWAY, False)
    }


def test_invalid_store_is_backed_up_and_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"schema_version": 1, "enabled": "yes"}')
    assert load_sensor_settings(path) == SensorSettings()
    assert not path.exists()
    assert path.with_suffix(".backup").exists()


def test_simulated_store_rejects_partial_rows(tmp_path):
    path = tmp_path / "sensors.json"
    path.write_text(json.dumps({"schema_version": 1, "sensors": [{"sensor_id": "x"}]}))
    assert load_simulated_sensors(path) == []
    assert path.with_suffix(".backup").exists()


@pytest.mark.asyncio
async def test_simulated_crud_events_and_restart(tmp_path):
    path = tmp_path / "sensors.json"
    moments = iter(
        datetime(2026, 1, day, tzinfo=timezone.utc) for day in range(1, 6)
    )
    provider = SimulatedContactSensorProvider(
        path=path, now=lambda: next(moments), id_factory=lambda: "sensor-1"
    )
    events = []
    provider.subscribe(events.append)
    await provider.start()
    made = await provider.create(" Window ", "7", SensorKind.DOOR)
    assert made.name == "Window"
    assert made.state is ContactState.CLOSED
    assert made.kind is SensorKind.DOOR
    changed = await provider.simulate("sensor-1", state="open", battery=18)
    assert changed.state is ContactState.OPEN
    renamed = await provider.update(
        "sensor-1", name="North window", kind=SensorKind.WINDOW, clear_zone=True
    )
    assert renamed.zone_id is None
    assert [event.kind for event in events] == [
        SensorEventKind.CREATED,
        SensorEventKind.UPDATED,
        SensorEventKind.UPDATED,
    ]

    restarted = SimulatedContactSensorProvider(path=path)
    await restarted.start()
    assert (await restarted.list())[0].name == "North window"
    assert (await restarted.list())[0].kind is SensorKind.WINDOW
    await restarted.remove("sensor-1")
    assert await restarted.list() == []
    with pytest.raises(SensorNotFound):
        await restarted.remove("sensor-1")


def test_provider_factory_is_demo_only(tmp_path):
    with pytest.raises(RuntimeError):
        create_provider("simulated", demo_mode=False, path=tmp_path / "x.json")
    provider = create_provider("simulated", demo_mode=True, path=tmp_path / "x.json")
    assert isinstance(provider, SimulatedContactSensorProvider)


def test_write_failures_are_not_hidden(tmp_path):
    directory = tmp_path / "as-directory"
    directory.mkdir()
    with pytest.raises(OSError):
        save_sensor_settings(SensorSettings(), directory)


def test_schema_one_settings_sensor_and_ownership_migrate(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "schema_version": 1,
        "enabled": True,
        "provider": "simulated",
        "zones": {
            "1": {
                "warning_delay_seconds": 12,
                "eco_enabled": True,
                "eco_delay_seconds": 34,
            },
            "2": {
                "warning_delay_seconds": 56,
                "eco_enabled": False,
                "eco_delay_seconds": 78,
            },
        },
    }))
    settings = load_sensor_settings(settings_path)
    assert settings.zones["1"] == ZoneSensorPolicy(12, ActionWhenOpen.ECO, 34)
    assert settings.zones["2"] == ZoneSensorPolicy(56, ActionWhenOpen.NOTHING, 78)

    sensor_path = tmp_path / "sensors.json"
    sensor_path.write_text(json.dumps({
        "schema_version": 1,
        "sensors": [{
            "sensor_id": "old",
            "provider_id": "simulated:old",
            "name": "Existing opening",
            "zone_id": "1",
            "state": "closed",
            "available": True,
            "battery": None,
            "changed_at": "2026-01-01T00:00:00+00:00",
            "last_seen_at": "2026-01-01T00:00:00+00:00",
        }],
    }))
    assert load_simulated_sensors(sensor_path)[0]["kind"] == "window"

    state_path = tmp_path / "automation.json"
    state_path.write_text(json.dumps({
        "schema_version": 1,
        "zones": {
            "1": {
                "open_started_at": 10,
                "warning_raised": True,
                "eco_owned": True,
                "suppressed": False,
            }
        },
    }))
    assert load_automation_state(state_path)["1"].owned_action is ActionWhenOpen.ECO
