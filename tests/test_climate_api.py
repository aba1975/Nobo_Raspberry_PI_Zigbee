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
    ours = ("temperature_too_high", "temperature_too_low", "temperature_back_in_range")

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
