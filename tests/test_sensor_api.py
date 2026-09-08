"""HTTP, auth, live-update and Nobø integration for contact sensors."""

import copy
import json
import time

import pytest
from fastapi.testclient import TestClient

import auth
import config_persistence
import sensor_persistence
import server
from sensor_automation import SensorAutomation
from sensor_persistence import ActionWhenOpen, SensorSettings


@pytest.fixture(autouse=True)
def isolated_sensor_service():
    original_zones = copy.deepcopy(server.DEMO_ZONES)
    original_schedules = copy.deepcopy(server.demo_schedules)
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
    # Pinned week profiles are module state too, and a schedule left behind by
    # one test decides whether the next one's rule is allowed to act.
    server.demo_schedules.clear()
    server.demo_schedules.update(original_schedules)
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


def enable(
    client, *, zone_id="1", warning=300,
    action="nothing", delay=300, override=False,
):
    response = client.put("/api/sensors/settings", json={
        "enabled": True,
        "zones": {
            zone_id: {
                "warning_delay_seconds": warning,
                "action_when_open": action,
                "action_delay_seconds": delay,
                "override_all_modes": override,
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


DAYS = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
]


def set_schedule(client, mode, zone_id="1"):
    """Pin a zone's week profile so a test does not depend on the wall clock.

    Whether a rule may act is decided against what the room would otherwise be
    running, and the demo house's default profile changes mode during the day —
    so without this, a test that passes in the afternoon fails at ten at night.
    """
    response = client.post(f"/api/zones/{zone_id}/schedule", json={"schedule": {
        day: [{"start": "00:00", "end": "24:00", "mode": mode}] for day in DAYS
    }})
    assert response.status_code == 200, response.text


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
    set_schedule(client, "comfort")
    enable(client, warning=300, action="eco", delay=0)
    created = add_sensor(client)
    sensor_id = created["sensor_id"]
    client.post(f"/api/sensors/{sensor_id}/simulate", json={"state": "open"})
    assert zone(client)["current_mode"] == "eco"
    assert zone(client)["sensor_summary"]["owned_action"] == "eco"

    client.post(f"/api/sensors/{sensor_id}/simulate", json={"state": "closed"})
    current = zone(client)
    assert current["current_mode"] == "normal"
    assert current["sensor_summary"]["owned_action"] is None


def test_monitoring_only_zone_warns_but_never_changes_heating(client):
    created_zone = client.post("/api/zones", json={"name": "Entertainment Room"})
    zone_id = created_zone.json()["zone_id"]
    enable(client, zone_id=zone_id, warning=0, action="eco", delay=0)
    created = add_sensor(client, zone_id=zone_id)
    client.post(
        f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"}
    )
    current = zone(client, zone_id)
    assert current["components"] == []
    assert current["sensor_summary"]["warning_raised"] is True
    assert current["sensor_summary"]["action_available"] is False
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


def test_a_zone_left_out_of_the_request_keeps_its_rule(client):
    enable(client, zone_id="1", warning=60, action="eco", delay=600, override=True)
    # A client that only cares about zone 2 must not reset zone 1 by omission.
    assert client.put("/api/sensors/settings", json={
        "enabled": True,
        "zones": {
            "2": {
                "warning_delay_seconds": 30,
                "action_when_open": "away",
                "action_delay_seconds": 30,
                "override_all_modes": False,
            }
        },
    }).status_code == 200
    kept = client.get("/api/sensors/settings").json()["zones"]["1"]
    assert kept["action_when_open"] == "eco"
    assert kept["action_delay_seconds"] == 600
    assert kept["override_all_modes"] is True


def test_settings_survive_a_get_put_round_trip(client):
    response = client.put("/api/sensors/settings", json={
        "enabled": True,
        "zones": {
            "1": {
                "warning_delay_seconds": 60,
                "action_when_open": "away",
                "action_delay_seconds": 600,
                "override_all_modes": True,
            }
        },
    })
    assert response.status_code == 200
    response = client.put("/api/sensors/settings", json=response.json())
    assert response.status_code == 200
    assert response.json()["zones"]["1"]["action_when_open"] == "away"
    assert response.json()["zones"]["1"]["action_delay_seconds"] == 600
    assert response.json()["zones"]["1"]["override_all_modes"] is True


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


def test_a_demo_restart_keeps_ownership_because_the_override_survives_too(client):
    """The simulated hub has to remember its overrides, exactly as a real one does.

    Zone overrides used to live only in memory, so a restart left a room whose
    mode still said "eco" and whose override set said nobody was holding it.
    That made the automation unable to tell its own work from a room somebody
    had simply left on Eco, and it was papered over by feeding the automation
    its own ledger back as evidence. Both ends are fixed here: the override is
    persisted, and reconciliation asks only the hub.
    """
    set_schedule(client, "comfort")
    enable(client, action="eco", delay=0)
    created = add_sensor(client)
    client.post(
        f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"}
    )
    assert server.sensor_automation.states["1"].owned_action is ActionWhenOpen.ECO

    saved = config_persistence.load_server_state()
    assert saved["demo_zone_overrides"] == ["1"]

    server.sensor_automation.reconcile_owned(server._sensor_heating_state())
    assert server.sensor_automation.states["1"].owned_action is ActionWhenOpen.ECO


def test_global_away_beats_eco_unless_sensor_override_is_enabled(client):
    assert client.post("/api/global/override/away").status_code == 200
    response = client.put("/api/sensors/settings", json={
        "enabled": True,
        "zones": {
            "1": {
                "warning_delay_seconds": 0,
                "action_when_open": "eco",
                "action_delay_seconds": 0,
                "override_all_modes": False,
            }
        },
    })
    assert response.status_code == 200
    created = add_sensor(client)
    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"})
    current = zone(client)
    assert current["current_mode"] == "away"
    assert current["sensor_summary"]["owned_action"] is None

    settings = response.json()
    settings["zones"]["1"]["override_all_modes"] = True
    assert client.put("/api/sensors/settings", json=settings).status_code == 200
    current = zone(client)
    assert current["current_mode"] == "eco"
    assert current["sensor_summary"]["owned_action"] == "eco"

    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "closed"})
    current = zone(client)
    assert current["current_mode"] == "away"
    assert current["sensor_summary"]["owned_action"] is None


def test_follow_schedule_will_not_warm_a_room_by_letting_go(client):
    """A manual Away hold is not undone just because the schedule is warmer."""
    set_schedule(client, "comfort")
    assert client.post("/api/zones/1/override/away").status_code == 200
    enable(client, action="schedule", delay=0)
    created = add_sensor(client)
    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"})
    current = zone(client)
    assert current["current_mode"] == "away"
    assert current["sensor_summary"]["owned_action"] is None
    assert current["sensor_summary"]["block_reason"] == "colder_mode"


def test_follow_schedule_releases_a_hold_when_that_cools_the_room(client):
    """Opening a window on a room held at Comfort puts it back on its schedule."""
    # Its week profile has to be colder than Comfort for the release to help.
    set_schedule(client, "eco")
    assert client.post("/api/zones/1/override/comfort").status_code == 200

    enable(client, action="schedule", delay=0)
    created = add_sensor(client)
    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"})
    current = zone(client)
    assert current["current_mode"] == "normal"
    # Nothing is owned: there is no override left to give back on closure.
    assert current["sensor_summary"]["owned_action"] is None


def test_a_zone_released_under_global_away_lands_on_away_not_its_schedule(client):
    """The bug this whole ordering came from, held down by a test.

    Closing a window used to send Nobø NORMAL and then report the room as
    "normal", which in a globally-Away house is a room that has quietly warmed
    up. Cancelling a zone override means the global one applies again.
    """
    enable(client, action="eco", delay=0, override=True)
    created = add_sensor(client)
    assert client.post("/api/global/override/away").status_code == 200
    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"})
    assert zone(client)["current_mode"] == "eco"

    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "closed"})
    current = zone(client)
    assert current["current_mode"] == "away"
    assert current["sensor_summary"]["owned_action"] is None


def test_a_blocked_rule_says_why_it_is_standing_down(client):
    enable(client, zone_id="12", warning=0, action="eco", delay=0)
    created = add_sensor(client, zone_id="12")
    assert client.post("/api/global/override/away").status_code == 200
    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"})
    summary = zone(client, "12")["sensor_summary"]
    assert summary["action_status"] == "blocked"
    assert summary["block_reason"] == "colder_mode"
    assert summary["warning_raised"] is True


# ---------------------------------------------------------------------------
# Whichever is colder, end to end
# ---------------------------------------------------------------------------

def test_a_warmer_global_mode_does_not_take_a_room_off_its_rule(client):
    """Comfort for the house, with a window open and an Eco rule: stays Eco."""
    set_schedule(client, "comfort")
    enable(client, action="eco", delay=0)
    created = add_sensor(client)
    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"})
    assert zone(client)["current_mode"] == "eco"

    assert client.post("/api/global/override/comfort").status_code == 200
    current = zone(client)
    assert current["current_mode"] == "eco"
    assert current["sensor_summary"]["owned_action"] == "eco"


def test_a_colder_global_mode_wins_while_the_window_is_still_open(client):
    set_schedule(client, "comfort")
    enable(client, action="eco", delay=0)
    created = add_sensor(client)
    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"})
    assert zone(client)["current_mode"] == "eco"

    assert client.post("/api/global/override/away").status_code == 200
    current = zone(client)
    assert current["current_mode"] == "away"
    assert current["sensor_summary"]["owned_action"] is None
    assert current["sensor_summary"]["block_reason"] == "colder_mode"


def test_coming_home_again_puts_the_rule_back_in_charge(client):
    """Nothing is stood down for good: the colder of the two always runs."""
    set_schedule(client, "comfort")
    enable(client, action="eco", delay=0)
    created = add_sensor(client)
    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"})
    assert client.post("/api/global/override/away").status_code == 200
    assert zone(client)["current_mode"] == "away"

    assert client.post("/api/global/override/home").status_code == 200
    current = zone(client)
    assert current["current_mode"] == "eco"
    assert current["sensor_summary"]["owned_action"] == "eco"


def test_setting_the_room_warmer_by_hand_is_taken_back_by_the_rule(client):
    set_schedule(client, "comfort")
    enable(client, action="eco", delay=0)
    created = add_sensor(client)
    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"})

    assert client.post("/api/zones/1/override/comfort").status_code == 200
    assert zone(client)["current_mode"] == "eco"


def test_setting_the_room_colder_by_hand_stands(client):
    set_schedule(client, "comfort")
    enable(client, action="eco", delay=0)
    created = add_sensor(client)
    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"})

    assert client.post("/api/zones/1/override/away").status_code == 200
    current = zone(client)
    assert current["current_mode"] == "away"
    assert current["sensor_summary"]["owned_action"] is None


def test_with_override_on_the_rule_holds_through_a_global_away(client):
    set_schedule(client, "comfort")
    enable(client, action="eco", delay=0, override=True)
    created = add_sensor(client)
    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"})

    assert client.post("/api/global/override/away").status_code == 200
    current = zone(client)
    assert current["current_mode"] == "eco"
    assert current["sensor_summary"]["owned_action"] == "eco"

    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "closed"})
    assert zone(client)["current_mode"] == "away"


def test_an_immediate_delay_is_accepted_end_to_end(client):
    set_schedule(client, "comfort")
    response = client.put("/api/sensors/settings", json={
        "enabled": True,
        "zones": {"1": {
            "warning_delay_seconds": 0,
            "action_when_open": "eco",
            "action_delay_seconds": 0,
            "override_all_modes": False,
        }},
    })
    assert response.status_code == 200
    assert response.json()["zones"]["1"]["warning_delay_seconds"] == 0
    created = add_sensor(client)
    client.post(f"/api/sensors/{created['sensor_id']}/simulate", json={"state": "open"})
    current = zone(client)
    assert current["sensor_summary"]["warning_raised"] is True
    assert current["current_mode"] == "eco"


def test_a_sensor_the_provider_cannot_reach_is_reported_with_its_last_seen(client):
    enable(client, warning=0)
    created = add_sensor(client)
    client.post(
        f"/api/sensors/{created['sensor_id']}/simulate",
        json={"available": False, "battery": 8},
    )
    reported = zone(client)["sensors"][0]
    assert reported["available"] is False
    assert reported["battery"] == 8
    assert reported["last_seen_at"]
    assert zone(client)["sensor_summary"]["unavailable_count"] == 1
