"""Room thermometers over HTTP: readings on the zone, the temperature rule,
and the alerts it raises.

Runs in demo mode against the simulated provider, so a thermometer here is a
row the simulator made up. What that proves is the wiring from provider to
zone payload to Nobø override; it proves nothing about an Aqara on a wall.
"""

import pytest

import server
from tests.test_sensor_api import (  # noqa: F401 - fixtures are used by name
    add_sensor, client, enable, isolated_sensor_service, set_schedule, zone,
)


@pytest.fixture
def sent(monkeypatch):
    """What would have been emailed. The notifier's own condition tracking
    stays real, so this sees exactly one message per change of state."""
    posted = []
    ours = (
        "temperature_too_high", "temperature_too_low", "temperature_back_in_range",
        "humidity_high", "room_near_freezing",
    )

    def fake_notify(event_type, subject, body, severity="warning", key=None,
                    highlight=(), facts=()):
        # Other alerts (a pinned schedule reads as a change made elsewhere)
        # are not this file's subject.
        if event_type in ours:
            posted.append({"type": event_type, "subject": subject, "severity": severity})
        return True

    monkeypatch.setattr(server.notifier, "notify", fake_notify)
    server.notifier._conditions.clear()
    yield posted
    server.notifier._conditions.clear()


def climate_rule(client, zone_id="1", **fields):
    body = {"enabled": True, "zones": {zone_id: {
        "warning_delay_seconds": 300,
        "action_when_open": "nothing",
        "action_delay_seconds": 300,
        "override_all_modes": False,
        **fields,
    }}}
    return client.put("/api/sensors/settings", json=body)


def simulate(client, sensor_id, **fields):
    return client.post(f"/api/sensors/{sensor_id}/simulate", json=fields)


def test_disabled_feature_adds_no_climate_fields(client):
    item = zone(client)
    assert "climate" not in item
    assert "climate_sensors" not in item
    assert item["current_temperature"] is None
    assert item["temperature_source"] is None


def test_a_thermometer_fills_in_a_room_the_hub_cannot_measure(client):
    enable(client)
    sensor = add_sensor(client, name="Bath Thermometer", kind="climate")
    assert sensor["kind"] == "climate"

    item = zone(client)
    # A thermometer is not a contact, so nothing counting windows counts it.
    assert item["sensors"] == []
    assert item["sensor_summary"]["sensor_count"] == 0
    assert [s["sensor_id"] for s in item["climate_sensors"]] == [sensor["sensor_id"]]
    assert item["current_temperature"] == 21.0
    assert item["temperature_source"] == "sensor"
    assert item["climate"]["humidity"] == 45.0
    assert item["climate"]["pressure"] == 1013.0
    assert item["climate"]["fresh_count"] == 1


def test_a_heaters_own_thermometer_is_not_overwritten(client):
    enable(client, zone_id="12")
    add_sensor(client, zone_id="12", kind="climate")
    item = zone(client, "12")
    assert item["current_temperature"] == 20.4
    assert item["temperature_source"] == "hub"
    assert item["climate"]["temperature"] == 21.0


def test_two_thermometers_are_averaged(client):
    enable(client)
    first = add_sensor(client, name="By the door", kind="climate")
    second = add_sensor(client, name="By the bath", kind="climate")
    assert simulate(client, first["sensor_id"], temperature=20.0).status_code == 200
    assert simulate(client, second["sensor_id"], temperature=23.0).status_code == 200
    assert zone(client)["climate"]["temperature"] == 21.5


def test_an_offline_thermometer_leaves_no_reading(client):
    enable(client)
    sensor = add_sensor(client, kind="climate")
    assert simulate(client, sensor["sensor_id"], available=False).status_code == 200
    item = zone(client)
    assert item["current_temperature"] is None
    assert item["temperature_source"] is None
    # And a quiet thermometer is not an unknown window.
    assert item["sensor_summary"]["state"] == "empty"


@pytest.mark.parametrize("fields", [
    {"temperature_max": 20, "temperature_min": 19.5},
    {"temperature_max": 50},
    {"temperature_min": -5},
    {"temperature_max": 25, "action_when_too_warm": "comfort"},
    {"temperature_min": 10, "action_when_too_cold": "away"},
])
def test_impossible_rules_are_refused(client, fields):
    # 422 where the model's own range refuses it, 400 where the policy does.
    assert climate_rule(client, **fields).status_code in (400, 422)
    saved = client.get("/api/sensors/settings").json()["zones"]["1"]
    assert saved["temperature_max"] is None
    assert saved["temperature_min"] is None


def test_a_contact_only_save_keeps_the_temperature_rule(client):
    response = climate_rule(
        client, temperature_max=24, action_when_too_warm="eco",
        temperature_min=12, action_when_too_cold="comfort",
    )
    assert response.status_code == 200, response.text
    # The contact rule's own save does not know about thermometers.
    enable(client, warning=600)
    saved = client.get("/api/sensors/settings").json()["zones"]["1"]
    assert saved["warning_delay_seconds"] == 600
    assert saved["temperature_max"] == 24
    assert saved["action_when_too_warm"] == "eco"
    assert saved["temperature_min"] == 12
    assert saved["action_when_too_cold"] == "comfort"


def test_too_warm_holds_eco_and_releases_only_its_own_hold(client, sent):
    set_schedule(client, "comfort")
    assert climate_rule(
        client, temperature_max=24, action_when_too_warm="eco",
    ).status_code == 200
    sensor = add_sensor(client, kind="climate")

    assert simulate(client, sensor["sensor_id"], temperature=26.0).status_code == 200
    item = zone(client)
    assert item["climate"]["condition"] == "too_warm"
    assert item["climate"]["owned_action"] == "eco"
    assert item["current_mode"] == "eco"
    # The contact rule owns nothing: the hold is the thermometer's.
    assert item["sensor_summary"]["owned_action"] is None
    assert [m["type"] for m in sent] == ["temperature_too_high"]
    assert sent[0]["severity"] == "warning"

    # Inside the hysteresis band the hold stays.
    assert simulate(client, sensor["sensor_id"], temperature=23.8).status_code == 200
    assert zone(client)["climate"]["owned_action"] == "eco"

    assert simulate(client, sensor["sensor_id"], temperature=23.0).status_code == 200
    item = zone(client)
    assert item["climate"]["condition"] is None
    assert item["climate"]["owned_action"] is None
    assert item["current_mode"] == "normal"
    assert [m["type"] for m in sent] == [
        "temperature_too_high", "temperature_back_in_range",
    ]


def test_too_cold_warns_without_touching_the_heating(client, sent):
    set_schedule(client, "eco")
    assert climate_rule(
        client, temperature_min=15, action_when_too_cold="nothing",
    ).status_code == 200
    sensor = add_sensor(client, kind="climate")
    before = zone(client)["current_mode"]
    assert simulate(client, sensor["sensor_id"], temperature=12.0).status_code == 200
    item = zone(client)
    assert item["climate"]["condition"] == "too_cold"
    assert item["climate"]["owned_action"] is None
    assert item["current_mode"] == before
    assert [m["type"] for m in sent] == ["temperature_too_low"]


def test_readings_and_kinds_cannot_be_crossed(client):
    enable(client)
    contact = add_sensor(client, kind="window")
    thermometer = add_sensor(client, kind="climate")
    assert simulate(client, contact["sensor_id"], temperature=20).status_code == 400
    assert simulate(client, thermometer["sensor_id"], state="open").status_code == 400
    assert client.put(
        f"/api/sensors/{thermometer['sensor_id']}", json={"kind": "window"}
    ).status_code == 400
    assert client.put(
        f"/api/sensors/{contact['sensor_id']}", json={"kind": "climate"}
    ).status_code == 400


def test_an_impossible_simulated_reading_is_refused(client):
    enable(client)
    sensor = add_sensor(client, kind="climate")
    assert simulate(client, sensor["sensor_id"], temperature=120).status_code == 400
    assert simulate(client, sensor["sensor_id"], humidity=140).status_code == 400


def test_the_temperature_alerts_are_offered_and_off(client):
    keys = ("temperature_too_high", "temperature_too_low", "temperature_back_in_range")
    types = client.get("/api/notifications").json()["event_types"]
    for key in keys:
        assert types[key]["default"] is False
        # Offered, but greyed out while the sensors are off.
        assert "unavailable" in types[key]
    enable(client)
    types = client.get("/api/notifications").json()["event_types"]
    for key in keys:
        assert "unavailable" not in types[key]


# ---------------------------------------------------------------------------
# Humidity, frost, the 24-hour history and the wall display
# ---------------------------------------------------------------------------

def test_the_zone_carries_humidity_frost_and_history(client):
    enable(client)
    add_sensor(client, kind="climate")
    climate = zone(client)["climate"]
    assert climate["humidity_max"] is None
    assert climate["humidity_delay_seconds"] == 3600
    assert climate["humidity_raised"] is False
    assert climate["frost_warning"] is True
    assert climate["frost"] is False
    assert climate["frost_temperature"] == 5.0
    history = climate["last_24h"]
    assert history["temperature_min"] == history["temperature_max"] == 21.0
    assert history["humidity_min"] == history["humidity_max"] == 45.0
    assert len(history["hours"]) == 1


def test_a_contact_only_save_keeps_the_humidity_and_frost_rule(client):
    assert climate_rule(
        client, humidity_max=65, humidity_delay_seconds=900, frost_warning=False,
    ).status_code == 200
    enable(client, warning=600)
    saved = client.get("/api/sensors/settings").json()["zones"]["1"]
    assert saved["humidity_max"] == 65
    assert saved["humidity_delay_seconds"] == 900
    assert saved["frost_warning"] is False


@pytest.mark.parametrize("fields", [
    {"humidity_max": 20},
    {"humidity_max": 120},
    {"humidity_delay_seconds": -1},
    {"humidity_delay_seconds": 90000},
])
def test_impossible_humidity_rules_are_refused(client, fields):
    assert climate_rule(client, **fields).status_code in (400, 422)
    saved = client.get("/api/sensors/settings").json()["zones"]["1"]
    assert saved["humidity_max"] is None


def test_damp_air_alerts_as_a_warning_and_recovers(client, sent):
    assert climate_rule(
        client, humidity_max=70, humidity_delay_seconds=0,
    ).status_code == 200
    sensor = add_sensor(client, kind="climate")
    before = zone(client)["current_mode"]
    assert simulate(client, sensor["sensor_id"], humidity=82).status_code == 200
    item = zone(client)
    assert item["climate"]["humidity_raised"] is True
    assert item["current_mode"] == before
    assert [(m["type"], m["severity"]) for m in sent] == [("humidity_high", "warning")]

    assert simulate(client, sensor["sensor_id"], humidity=60).status_code == 200
    assert zone(client)["climate"]["humidity_raised"] is False
    assert [m["type"] for m in sent] == ["humidity_high", "temperature_back_in_range"]


def test_a_room_near_freezing_alerts_as_critical_with_no_rule_set(client, sent):
    enable(client)
    sensor = add_sensor(client, kind="climate")
    assert simulate(client, sensor["sensor_id"], temperature=3.5).status_code == 200
    item = zone(client)
    assert item["climate"]["frost"] is True
    assert item["climate"]["condition"] is None
    assert [(m["type"], m["severity"]) for m in sent] == [("room_near_freezing", "critical")]
    assert "3.5" in sent[0]["subject"]

    assert simulate(client, sensor["sensor_id"], temperature=6.5).status_code == 200
    assert zone(client)["climate"]["frost"] is False
    assert [m["type"] for m in sent] == ["room_near_freezing", "temperature_back_in_range"]


def test_a_room_meant_to_be_cold_does_not_alert(client, sent):
    assert climate_rule(client, frost_warning=False).status_code == 200
    sensor = add_sensor(client, kind="climate")
    assert simulate(client, sensor["sensor_id"], temperature=2.0).status_code == 200
    assert zone(client)["climate"]["frost"] is False
    assert sent == []


def test_an_offline_freezing_thermometer_clears_without_saying_all_is_well(client, sent):
    enable(client)
    sensor = add_sensor(client, kind="climate")
    assert simulate(client, sensor["sensor_id"], temperature=2.0).status_code == 200
    assert simulate(client, sensor["sensor_id"], available=False).status_code == 200
    assert zone(client)["climate"]["frost"] is False
    assert [m["type"] for m in sent] == ["room_near_freezing"]


def test_the_display_needs_a_session(client):
    anonymous = client.__class__(server.app)
    response = anonymous.get("/api/display", follow_redirects=False)
    assert response.status_code in (302, 303, 307, 401)


def test_the_display_without_sensors_is_the_heating_alone(client):
    body = client.get("/api/display").json()
    assert body["sensors_enabled"] is False
    assert body["open_contacts"] == [] and body["unavailable_sensors"] == []
    assert body["status"] in ("ok", "unknown")
    # Other tests rename and add demo zones, so compare with the live list.
    zones = client.get("/api/zones").json()["zones"]
    assert sorted(room["zone_id"] for room in body["rooms"]) == sorted(z["zone_id"] for z in zones)
    keys = {
        "zone_id", "name", "mode", "on_schedule", "has_heater", "set_temperature",
        "actual_temperature", "temperature_source", "humidity", "open", "warnings",
    }
    for room in body["rooms"]:
        assert set(room) == keys
        assert room["mode"] in ("comfort", "eco", "away", "off")
    assert any(room["has_heater"] for room in body["rooms"])
    assert all(room["warnings"] == [] for room in body["rooms"])


def test_the_display_reports_an_open_window_and_an_actual_temperature(client):
    enable(client)
    window = add_sensor(client, name="Bath window")
    add_sensor(client, kind="climate")
    assert simulate(client, window["sensor_id"], state="open").status_code == 200
    body = client.get("/api/display").json()
    assert body["sensors_enabled"] is True
    assert body["status"] == "open"
    bath = next(room for room in body["rooms"] if room["zone_id"] == "1")
    # Other tests rename demo zones, so take the name from the payload.
    assert body["open_contacts"] == [{
        "zone": bath["name"], "sensor": "Bath window", "kind": "window",
        "since": body["open_contacts"][0]["since"],
    }]
    # The set point and the measurement are separate fields, never one number.
    zone = next(z for z in client.get("/api/zones").json()["zones"] if z["zone_id"] == "1")
    assert bath["set_temperature"] == float(zone[f"{bath['mode']}_temperature"])
    assert bath["actual_temperature"] == 21.0
    assert bath["temperature_source"] == "sensor"
    assert bath["humidity"] == 45.0
    assert bath["open"] == 1


def test_the_display_calls_a_room_near_freezing_a_warning(client):
    enable(client)
    sensor = add_sensor(client, kind="climate")
    assert simulate(client, sensor["sensor_id"], temperature=2.0).status_code == 200
    body = client.get("/api/display").json()
    assert body["status"] == "warning"
    bath = next(room for room in body["rooms"] if room["zone_id"] == "1")
    assert bath["warnings"] == ["frost"]
