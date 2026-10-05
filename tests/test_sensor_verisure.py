"""The alarm's own doors, windows and thermometers, used as sensors.

Everything runs against the demo hub and the demo alarm, which reports four
door contacts and two smoke detectors. The rules they feed are the ordinary
sensor rules; what is tested here is the part that is new — choosing a
device, where it counts, and Zigbee coming first.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import sensor_verisure
import server
from sensor_provider import ContactSnapshot, ContactState, SensorKind
from tests.test_sensor_api import (  # noqa: F401 - fixtures are used by name
    add_sensor, client, enable, isolated_sensor_service, zone,
)

WOODSHED = "contact:DEMO 0004"
PATIO = "contact:DEMO 0002"
HALLWAY_SMOKE = "climate:DEMO 0101"


@pytest.fixture(autouse=True)
def house_at_home(monkeypatch):
    monkeypatch.setattr(server, "demo_global_mode", "normal")
    monkeypatch.setattr(server, "global_mode_source", "manual")
    yield


def alarm_on(client):
    response = client.put("/api/alarm/settings", json={"enabled": True, "provider": "simulated"})
    assert response.status_code == 200, response.text


def add_device(client, device_id, name, **extra):
    response = client.post("/api/sensors/verisure", json={
        "device_id": device_id, "name": name, **extra,
    })
    assert response.status_code == 200, response.text
    return response.json()


def simulate(client, sensor_id, **body):
    response = client.post(f"/api/sensors/{sensor_id}/simulate", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def listed(client, sensor_id):
    return next(
        item for item in client.get("/api/sensors").json()["sensors"]
        if item["sensor_id"] == sensor_id
    )


# -- offered only where it can work ----------------------------------------


def test_nothing_is_offered_while_the_alarm_is_off(client):
    enable(client)
    body = client.get("/api/sensors/verisure").json()
    assert body["available"] is False
    assert "alarm integration" in body["reason"]
    assert body["devices"] == []
    refused = client.post("/api/sensors/verisure", json={"device_id": WOODSHED, "name": "x"})
    assert refused.status_code == 409


def test_the_sensor_feature_has_to_be_on(client):
    alarm_on(client)
    assert client.get("/api/sensors/verisure").status_code == 409


def test_only_admins_choose(client, monkeypatch):
    import auth

    enable(client)
    alarm_on(client)
    original = auth.load_users

    def users():
        data = dict(original())
        data["admin"] = {**data["admin"], "role": "user"}
        return data

    monkeypatch.setattr(auth, "load_users", users)
    assert client.get("/api/sensors/verisure").status_code == 403
    assert client.post("/api/sensors/verisure",
                       json={"device_id": WOODSHED, "name": "x"}).status_code == 403


def test_the_list_is_what_the_alarm_reported(client):
    enable(client)
    alarm_on(client)
    body = client.get("/api/sensors/verisure").json()
    assert body["available"] is True
    ids = [item["device_id"] for item in body["devices"]]
    assert WOODSHED in ids and HALLWAY_SMOKE in ids
    smoke = next(item for item in body["devices"] if item["device_id"] == HALLWAY_SMOKE)
    assert smoke["kind"] == "climate" and smoke["temperature"] == 19.5
    assert all(item["sensor_id"] is None for item in body["devices"])
    # The label printed on the device, beside the name, to check one by.
    woodshed = next(item for item in body["devices"] if item["device_id"] == WOODSHED)
    assert woodshed["label"] == "DEMO 0004"


def test_settings_say_how_many_devices_the_alarm_reported(client):
    alarm_on(client)
    status = client.get("/api/alarm/settings").json()["status"]
    assert status["device_counts"] == {"contact": 4, "climate": 2}, status
    assert status["device_count"] == 6


# -- a monitoring-only outbuilding -----------------------------------------


def test_the_woodshed_door_warns_like_any_other(client):
    enable(client, zone_id="1", warning=0)
    alarm_on(client)
    door = add_device(client, WOODSHED, "Woodshed door", zone_id="1", kind="door")
    assert door["source"] == "verisure"
    assert door["kind"] == "door"
    assert door["counts"] is True

    simulate(client, door["sensor_id"], state="open")
    room = zone(client, "1")
    [row] = [s for s in room["sensors"] if s["sensor_id"] == door["sensor_id"]]
    assert row["state"] == "open" and row["available"] is True
    assert room["sensor_summary"]["state"] == "open"
    assert room["sensor_summary"]["warning_raised"] is True

    simulate(client, door["sensor_id"], state="closed")
    assert zone(client, "1")["sensor_summary"]["warning_raised"] is False


def test_it_is_saved_and_read_back(client):
    enable(client)
    alarm_on(client)
    door = add_device(client, WOODSHED, "Woodshed door", zone_id="1", kind="door")
    saved = json.loads(sensor_verisure.VERISURE_SENSORS_FILE.read_text())
    assert saved["sensors"][0]["device_id"] == WOODSHED
    assert sensor_verisure.load()[door["sensor_id"]].name == "Woodshed door"


def test_a_device_is_added_once(client):
    enable(client)
    alarm_on(client)
    add_device(client, WOODSHED, "Woodshed door", zone_id="1")
    again = client.post("/api/sensors/verisure",
                        json={"device_id": WOODSHED, "name": "Again", "zone_id": "1"})
    assert again.status_code == 409


def test_a_smoke_detector_is_a_thermometer_and_nothing_else(client):
    enable(client)
    alarm_on(client)
    wrong = client.post("/api/sensors/verisure", json={
        "device_id": HALLWAY_SMOKE, "name": "Hall", "zone_id": "1", "kind": "door"})
    assert wrong.status_code == 400
    smoke = add_device(client, HALLWAY_SMOKE, "Hall smoke detector", zone_id="1")
    assert smoke["kind"] == "climate"
    assert smoke["temperature"] == 19.5


def test_it_cannot_be_set_by_hand_off_the_demo_alarm(client, monkeypatch):
    enable(client)
    alarm_on(client)
    door = add_device(client, WOODSHED, "Woodshed door", zone_id="1")
    monkeypatch.setattr(server, "alarm_provider", object())
    response = client.post(f"/api/sensors/{door['sensor_id']}/simulate", json={"state": "open"})
    assert response.status_code == 501


def test_removing_it_leaves_the_alarm_alone(client):
    enable(client)
    alarm_on(client)
    door = add_device(client, WOODSHED, "Woodshed door", zone_id="1")
    assert client.delete(f"/api/sensors/{door['sensor_id']}").status_code == 200
    assert server.verisure_sensors == {}
    body = client.get("/api/sensors/verisure").json()
    assert any(item["device_id"] == WOODSHED and item["sensor_id"] is None
               for item in body["devices"])


def test_turning_the_alarm_off_hides_it_and_keeps_the_choice(client):
    enable(client)
    alarm_on(client)
    door = add_device(client, WOODSHED, "Woodshed door", zone_id="1")
    client.put("/api/alarm/settings", json={"enabled": False})
    assert all(s["sensor_id"] != door["sensor_id"]
               for s in client.get("/api/sensors").json()["sensors"])
    assert door["sensor_id"] in server.verisure_sensors
    alarm_on(client)
    assert listed(client, door["sensor_id"])["name"] == "Woodshed door"


def test_an_alarm_that_cannot_be_read_makes_it_offline_not_closed(client, monkeypatch):
    enable(client)
    alarm_on(client)
    door = add_device(client, WOODSHED, "Woodshed door", zone_id="1")
    stale = server.alarm_reading
    monkeypatch.setattr(server, "alarm_reading", type(stale)(
        stale.arm_state, stale.arm_changed_at, stale.locks,
        stale.read_at - server.ALARM_STALE_SECONDS - 60, stale.devices))
    import asyncio
    asyncio.run(server.evaluate_sensor_automation())
    row = listed(client, door["sensor_id"])
    assert row["available"] is False


def test_deleting_its_room_unassigns_it(client):
    enable(client)
    alarm_on(client)
    door = add_device(client, WOODSHED, "Woodshed door", zone_id="1")
    server._forget_zone_in_heating_links("1")
    assert server.verisure_sensors[door["sensor_id"]].zone_id is None


# -- Zigbee first ------------------------------------------------------------


def test_a_backup_stands_by_while_the_zigbee_sensor_reports(client):
    enable(client, zone_id="1", warning=0)
    alarm_on(client)
    zigbee = add_sensor(client, "Patio Door", zone_id="1", kind="door")
    backup = add_device(client, PATIO, "Patio door (alarm)", kind="door",
                        backup_for=zigbee["sensor_id"])
    assert backup["zone_id"] == "1", "a backup lives in its sensor's room"

    row = listed(client, backup["sensor_id"])
    assert row["counts"] is False
    assert row["standing_by"] == sensor_verisure.STANDING_BY_ZIGBEE
    assert row["backup_for_name"] == "Patio Door"

    # The alarm says open; the Zigbee sensor says closed and is believed.
    simulate(client, backup["sensor_id"], state="open")
    assert zone(client, "1")["sensor_summary"]["state"] == "closed"


def test_a_backup_stands_in_while_the_zigbee_sensor_is_offline(client):
    enable(client, zone_id="1", warning=0)
    alarm_on(client)
    zigbee = add_sensor(client, "Patio Door", zone_id="1", kind="door")
    backup = add_device(client, PATIO, "Patio door (alarm)", kind="door",
                        backup_for=zigbee["sensor_id"])
    simulate(client, zigbee["sensor_id"], available=False)
    simulate(client, backup["sensor_id"], state="open")

    zig = listed(client, zigbee["sensor_id"])
    assert zig["counts"] is False
    assert zig["stood_in_by"] == backup["sensor_id"]
    assert listed(client, backup["sensor_id"])["counts"] is True
    room = zone(client, "1")["sensor_summary"]
    assert room["state"] == "open"
    assert room["warning_raised"] is True
    # One door, counted once.
    assert room["sensor_count"] == 1


def test_one_backup_per_sensor(client):
    enable(client)
    alarm_on(client)
    zigbee = add_sensor(client, "Patio Door", zone_id="1", kind="door")
    add_device(client, PATIO, "Patio door (alarm)", backup_for=zigbee["sensor_id"])
    second = client.post("/api/sensors/verisure", json={
        "device_id": WOODSHED, "name": "Also", "backup_for": zigbee["sensor_id"]})
    assert second.status_code == 409


def test_a_backup_follows_its_sensor_and_cannot_be_moved_alone(client):
    enable(client)
    alarm_on(client)
    zigbee = add_sensor(client, "Patio Door", zone_id="1", kind="door")
    backup = add_device(client, PATIO, "Patio door (alarm)", backup_for=zigbee["sensor_id"])
    moved = client.put(f"/api/sensors/{backup['sensor_id']}", json={"zone_id": "2"})
    assert moved.status_code == 400
    assert client.put(f"/api/sensors/{zigbee['sensor_id']}", json={"zone_id": "2"}).status_code == 200
    assert listed(client, backup["sensor_id"])["zone_id"] == "2"
    # It heats what its sensor heats, so it has no list of its own.
    refused = client.put(f"/api/sensors/{backup['sensor_id']}/heating", json={"zone_ids": []})
    assert refused.status_code == 400


def test_removing_the_zigbee_sensor_leaves_the_backup_in_its_room(client):
    enable(client)
    alarm_on(client)
    zigbee = add_sensor(client, "Patio Door", zone_id="1", kind="door")
    backup = add_device(client, PATIO, "Patio door (alarm)", backup_for=zigbee["sensor_id"])
    assert client.delete(f"/api/sensors/{zigbee['sensor_id']}").status_code == 200
    kept = server.verisure_sensors[backup["sensor_id"]]
    assert kept.backup_for is None and kept.zone_id == "1"
    assert listed(client, backup["sensor_id"])["counts"] is True


def test_a_backup_can_be_released(client):
    enable(client)
    alarm_on(client)
    zigbee = add_sensor(client, "Patio Door", zone_id="1", kind="door")
    backup = add_device(client, PATIO, "Patio door (alarm)", backup_for=zigbee["sensor_id"])
    response = client.put(f"/api/sensors/{backup['sensor_id']}", json={"clear_backup": True})
    assert response.status_code == 200, response.text
    assert response.json()["backup_for"] is None
    assert response.json()["zone_id"] == "1"


def test_only_an_alarm_device_can_be_a_backup(client):
    enable(client)
    zigbee = add_sensor(client, "Patio Door", zone_id="1", kind="door")
    other = add_sensor(client, "Window", zone_id="1")
    response = client.put(f"/api/sensors/{other['sensor_id']}",
                          json={"backup_for": zigbee["sensor_id"]})
    assert response.status_code == 400


def test_a_zigbee_thermometer_outranks_a_smoke_detector(client):
    enable(client, zone_id="1")
    alarm_on(client)
    thermometer = add_sensor(client, "Hall thermometer", zone_id="1", kind="climate")
    simulate(client, thermometer["sensor_id"], temperature=18.0)
    smoke = add_device(client, HALLWAY_SMOKE, "Hall smoke detector", zone_id="1")
    row = listed(client, smoke["sensor_id"])
    assert row["counts"] is False
    assert row["standing_by"] == sensor_verisure.STANDING_BY_THERMOMETER
    assert zone(client, "1")["current_temperature"] == 18.0

    simulate(client, thermometer["sensor_id"], available=False)
    assert listed(client, smoke["sensor_id"])["counts"] is True
    assert zone(client, "1")["current_temperature"] == 19.5


def test_a_smoke_detector_alone_gives_the_room_a_temperature(client):
    enable(client, zone_id="1")
    alarm_on(client)
    add_device(client, HALLWAY_SMOKE, "Hall smoke detector", zone_id="1")
    room = zone(client, "1")
    assert room["current_temperature"] == 19.5
    assert room["temperature_source"] == "sensor"


def test_the_precedence_rules_directly():
    now = datetime.now(timezone.utc)

    def snap(sensor_id, *, source="", available=True, state="closed", kind=SensorKind.DOOR,
             zone_id="1", temperature=None, age=0):
        seen = now - timedelta(seconds=age)
        return ContactSnapshot(
            sensor_id=sensor_id, provider_id=sensor_id, name=sensor_id, zone_id=zone_id,
            state=ContactState(state), available=available, battery=None,
            changed_at=seen, last_seen_at=seen, kind=kind, temperature=temperature,
            source=source,
        )

    chosen = {
        "verisure-b": sensor_verisure.VerisureSensor(
            "verisure-b", "contact:B", "B", SensorKind.DOOR, None, "zig"),
        "verisure-t": sensor_verisure.VerisureSensor(
            "verisure-t", "climate:T", "T", SensorKind.CLIMATE, "1", None),
    }

    def run(*items):
        return sensor_verisure.apply_precedence(
            items, chosen, now=now.timestamp(), climate_stale_seconds=3600)

    # A Zigbee sensor reading "unknown" cannot be believed either.
    unknown = run(snap("zig", state="unknown"), snap("verisure-b", source="verisure"))
    assert unknown.stood_in_for == {"zig": "verisure-b"}
    # Neither can be believed: the Zigbee one stays counted, as before a backup.
    neither = run(snap("zig", available=False),
                  snap("verisure-b", source="verisure", available=False))
    assert [s.sensor_id for s in neither.counted] == ["zig"]
    # A stale Zigbee thermometer is no reason to set the smoke detector aside.
    stale = run(snap("therm", kind=SensorKind.CLIMATE, state="unknown", temperature=20.0,
                     age=7200),
                snap("verisure-t", source="verisure", kind=SensorKind.CLIMATE,
                     state="unknown", temperature=22.0))
    assert "verisure-t" not in stale.standing_by


# -- what it is excluded from ---------------------------------------------


def test_the_battery_and_silence_alerts_ignore_it(client, monkeypatch):
    enable(client, zone_id="1")
    alarm_on(client)
    door = add_device(client, WOODSHED, "Woodshed door", zone_id="1")
    zigbee = add_sensor(client, "Window", zone_id="1")
    seen = []
    original = server.notifier.set_condition

    def spy(event_type, key, *args, **kwargs):
        seen.append((event_type, key))
        return original(event_type, key, *args, **kwargs)

    monkeypatch.setattr(server.notifier, "set_condition", spy)
    import asyncio
    asyncio.run(server.evaluate_sensor_automation())
    health = [key for event, key in seen if event in ("sensor_quiet", "sensor_battery_low")]
    assert any(zigbee["sensor_id"] in key for key in health), "the spy saw nothing"
    assert not any(door["sensor_id"] in key for key in health)


def test_the_alerts_menu_says_which_alerts_it_can_raise(client):
    enable(client)
    alarm_on(client)
    add_device(client, WOODSHED, "Woodshed door", zone_id="1")
    add_device(client, HALLWAY_SMOKE, "Hall smoke detector", zone_id="1")
    types = client.get("/api/notifications").json()["event_types"]
    assert "1 Verisure door or window sensor" in types["contact_left_open"]["note"]
    assert "1 Verisure temperature reading" in types["temperature_too_low"]["note"]
    assert "Zigbee sensors only" in types["sensor_battery_low"]["note"]
    assert "note" not in types.get("hub_offline", {})


def test_the_alerts_menu_says_nothing_of_verisure_while_the_alarm_is_off(client):
    enable(client)
    types = client.get("/api/notifications").json()["event_types"]
    assert all("note" not in spec for spec in types.values())
