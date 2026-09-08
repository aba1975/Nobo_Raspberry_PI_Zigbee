"""HTTP, auth, live-update and Nobø integration for contact sensors."""

import copy
import json
import time

import pytest
from fastapi.testclient import TestClient

import auth
import sensor_persistence
import server
from sensor_automation import SensorAutomation
from sensor_persistence import SensorSettings


@pytest.fixture(autouse=True)
def isolated_sensor_service():
    original_zones = copy.deepcopy(server.DEMO_ZONES)
    original_settings = server.sensor_settings
    original_automation = server.sensor_automation
    server.sensor_settings = SensorSettings()
    server.sensor_provider = None
    server.sensor_unsubscribe = None
    server.sensor_snapshots = []
    server.sensor_zone_aggregates = {}
    server.sensor_wakeup = None
    server.sensor_automation = SensorAutomation(
        states={},
        save=sensor_persistence.save_automation_state,
        commands=server.SensorHeatingCommands(),
    )
    yield
    server.DEMO_ZONES[:] = original_zones
    server.sensor_settings = original_settings
    server.sensor_automation = original_automation
    server.sensor_provider = None
    server.sensor_unsubscribe = None
    server.sensor_snapshots = []
    server.sensor_zone_aggregates = {}
    server.sensor_wakeup = None


@pytest.fixture
def client():
    with TestClient(server.app) as value:
        value.cookies.set("session_id", "pytest-fixed-session-id")
        yield value


def enable(client, *, zone_id="1", warning=300, eco=False, eco_delay=300):
    response = client.put("/api/sensors/settings", json={
        "enabled": True,
        "zones": {
            zone_id: {
                "warning_delay_seconds": warning,
                "eco_enabled": eco,
                "eco_delay_seconds": eco_delay,
            }
        },
    })
    assert response.status_code == 200, response.text
    return response.json()


def add_sensor(client, name="Window", zone_id="1", kind="window"):
    response = client.post(
        "/api/sensors", json={"name": name, "zone_id": zone_id, "kind": kind}
    )
    assert response.status_code == 200, response.text
    return response.json()


def zone(client, zone_id="1"):
    return next(
        item for item in client.get("/api/zones").json()["zones"]
        if item["zone_id"] == zone_id
    )


def test_disabled_feature_is_absent_from_zone_payloads(client):
    capabilities = client.get("/api/capabilities").json()
    assert capabilities["sensors"]["enabled"] is False
    assert "sensors" not in zone(client)
    assert client.get("/api/sensors").status_code == 409


def test_only_admins_can_configure_or_simulate(client, monkeypatch):
    original = auth.load_users

    def users():
        data = dict(original())
        data["admin"] = {**data["admin"], "role": "user"}
        return data

    monkeypatch.setattr(auth, "load_users", users)
    assert client.get("/api/sensors/settings").status_code == 403
    assert client.put(
        "/api/sensors/settings", json={"enabled": True, "zones": {}}
    ).status_code == 403


def test_pair_rename_assign_remove_and_persisted_state(client):
    enable(client)
    created = add_sensor(client, kind="door")
    sensor_id = created["sensor_id"]
    assert created["kind"] == "door"
    assert zone(client)["sensor_summary"]["state"] == "closed"

    renamed = client.put(
        f"/api/sensors/{sensor_id}",
        json={"name": "Patio Door", "zone_id": "2", "kind": "window"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Patio Door"
    assert renamed.json()["kind"] == "window"
    assert next(item for item in client.get("/api/sensors").json()["sensors"]
                if item["sensor_id"] == sensor_id)["zone_id"] == "2"

    assert client.delete(f"/api/sensors/{sensor_id}").status_code == 200
    assert client.get("/api/sensors").json()["sensors"] == []


def test_warning_aggregates_many_sensors_and_unknown_is_not_closed(client):
    enable(client, warning=0)
    first = add_sensor(client, "Door")
    second = add_sensor(client, "Window")
    client.post(f"/api/sensors/{first['sensor_id']}/simulate", json={"state": "open"})
    summary = zone(client)["sensor_summary"]
    assert summary["warning_raised"] is True
    assert summary["open_count"] == 1

    client.post(
        f"/api/sensors/{second['sensor_id']}/simulate", json={"state": "unknown"}
    )
    client.post(
        f"/api/sensors/{first['sensor_id']}/simulate", json={"state": "closed"}
    )
    summary = zone(client)["sensor_summary"]
    assert summary["state"] == "unknown"
    assert summary["warning_raised"] is True

    client.post(
        f"/api/sensors/{second['sensor_id']}/simulate",
        json={"state": "closed", "available": False, "battery": 12},
    )
    current = zone(client)
    assert current["sensor_summary"]["state"] == "unavailable"
    assert current["sensor_summary"]["warning_raised"] is True
    assert current["sensors"][1]["battery"] == 12

    client.post(
        f"/api/sensors/{second['sensor_id']}/simulate", json={"available": True}
    )
    assert zone(client)["sensor_summary"]["warning_raised"] is False


def test_eco_is_owned_then_released_to_normal_not_comfort(client):
    enable(client, warning=300, eco=True, eco_delay=0)
    created = add_sensor(client)
    sensor_id = created["sensor_id"]
    client.post(f"/api/sensors/{sensor_id}/simulate", json={"state": "open"})
    assert zone(client)["current_mode"] == "eco"
    assert zone(client)["sensor_summary"]["eco_owned"] is True

    client.post(f"/api/sensors/{sensor_id}/simulate", json={"state": "closed"})
    current = zone(client)
    assert current["current_mode"] == "normal"
    assert current["sensor_summary"]["eco_owned"] is False


def test_manual_change_suppresses_eco_for_the_current_open_cycle(client):
    enable(client, eco=True, eco_delay=0)
    created = add_sensor(client)
    sensor_id = created["sensor_id"]
    client.post(f"/api/sensors/{sensor_id}/simulate", json={"state": "open"})
    assert zone(client)["sensor_summary"]["eco_owned"] is True

    assert client.post("/api/zones/1/override/comfort").status_code == 200
    assert zone(client)["current_mode"] == "comfort"
    assert zone(client)["sensor_summary"]["eco_owned"] is False
    assert server.sensor_automation.states["1"].suppressed is True

    client.post(f"/api/sensors/{sensor_id}/simulate", json={"state": "closed"})
    client.post("/api/zones/1/override/normal")
    client.post(f"/api/sensors/{sensor_id}/simulate", json={"state": "open"})
    assert zone(client)["current_mode"] == "eco"


def test_monitoring_only_zone_warns_but_never_changes_heating(client):
    created_zone = client.post("/api/zones", json={"name": "Entertainment Room"})
    zone_id = created_zone.json()["zone_id"]
    enable(client, zone_id=zone_id, warning=0, eco=True, eco_delay=0)
    created = add_sensor(client, zone_id=zone_id)
    client.post(
        f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"}
    )
    current = zone(client, zone_id)
    assert current["components"] == []
    assert current["sensor_summary"]["warning_raised"] is True
    assert current["sensor_summary"]["eco_available"] is False
    assert current["current_mode"] == "normal"


def test_sensor_mutation_reaches_websocket_clients(client):
    enable(client)
    with client.websocket_connect("/ws") as websocket:
        assert json.loads(websocket.receive_text())["type"] == "zones_update"
        add_sensor(client)
        message = json.loads(websocket.receive_text())
        assert message["type"] == "zones_update"
        first = next(item for item in message["data"] if item["zone_id"] == "1")
        assert first["sensor_summary"]["sensor_count"] == 1


def test_real_mode_cannot_enable_the_simulated_provider(client, monkeypatch):
    monkeypatch.setattr(server, "DEMO_MODE", False)
    response = client.put(
        "/api/sensors/settings", json={"enabled": True, "zones": {}}
    )
    assert response.status_code == 501


def test_enabled_simulated_provider_blocks_switch_to_real_hub(client):
    enable(client)
    response = client.post(
        "/api/hub/config",
        json={
            "demo_mode": False,
            "serial": "123456789012",
            "ip": "192.0.2.10",
        },
    )
    assert response.status_code == 409
    assert server.DEMO_MODE is True
    assert server.sensor_provider is not None


def test_empty_policy_update_preserves_defaults_for_every_zone(client):
    response = client.put(
        "/api/sensors/settings", json={"enabled": True, "zones": {}}
    )
    assert response.status_code == 200
    assert set(response.json()["zones"]) == {
        str(zone["zone_id"]) for zone in server.DEMO_ZONES
    }


def test_settings_response_keeps_legacy_eco_aliases(client):
    response = client.put("/api/sensors/settings", json={
        "enabled": True,
        "zones": {
            "1": {
                "warning_delay_seconds": 60,
                "action_when_open": "eco",
                "action_delay_seconds": 600,
            }
        },
    })
    assert response.status_code == 200
    policy = response.json()["zones"]["1"]
    assert policy["eco_enabled"] is True
    assert policy["eco_delay_seconds"] == 600


def test_settings_get_put_round_trip_preserves_v2_action(client):
    response = client.put("/api/sensors/settings", json={
        "enabled": True,
        "zones": {
            "1": {
                "warning_delay_seconds": 60,
                "action_when_open": "away",
                "action_delay_seconds": 600,
            }
        },
    })
    assert response.status_code == 200
    response = client.put("/api/sensors/settings", json=response.json())
    assert response.status_code == 200
    assert response.json()["zones"]["1"]["action_when_open"] == "away"
    assert response.json()["zones"]["1"]["action_delay_seconds"] == 600


def test_disable_then_enable_keeps_deadline_wakeup_alive(client):
    enable(client, warning=1)
    assert client.put(
        "/api/sensors/settings", json={"enabled": False, "zones": {}}
    ).status_code == 200
    enable(client, warning=1)
    created = add_sensor(client)
    client.post(
        f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"}
    )
    time.sleep(1.2)
    assert zone(client)["sensor_summary"]["warning_raised"] is True


def test_demo_restart_reconciles_persisted_sensor_owned_eco(client):
    enable(client, eco=True, eco_delay=0)
    created = add_sensor(client)
    client.post(
        f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"}
    )
    server.DEMO_ZONE_OVERRIDES.clear()
    server.sensor_automation.reconcile_owned(server._sensor_heating_state())
    assert server.sensor_automation.states["1"].eco_owned is True


@pytest.mark.parametrize("action", ["away", "eco", "comfort"])
def test_api_applies_and_releases_each_configured_action(client, action):
    response = client.put("/api/sensors/settings", json={
        "enabled": True,
        "zones": {
            "1": {
                "warning_delay_seconds": 300,
                "action_when_open": action,
                "action_delay_seconds": 0,
            }
        },
    })
    assert response.status_code == 200, response.text
    assert response.json()["zones"]["1"]["action_when_open"] == action
    created = add_sensor(client)
    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"})
    current = zone(client)
    assert current["current_mode"] == action
    assert current["sensor_summary"]["action_owned"] == action
    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "closed"})
    assert zone(client)["current_mode"] == "normal"


def test_api_schedule_action_does_not_clear_manual_override(client):
    assert client.post("/api/zones/1/override/away").status_code == 200
    response = client.put("/api/sensors/settings", json={
        "enabled": True,
        "zones": {
            "1": {
                "warning_delay_seconds": 300,
                "action_when_open": "schedule",
                "action_delay_seconds": 0,
            }
        },
    })
    assert response.status_code == 200
    created = add_sensor(client)
    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"})
    assert zone(client)["current_mode"] == "away"
    assert zone(client)["sensor_summary"]["action_owned"] is None
