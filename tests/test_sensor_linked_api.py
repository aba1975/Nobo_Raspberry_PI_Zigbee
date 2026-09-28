"""HTTP for a contact that also changes the heating in other rooms.

The engine's semantics are in test_sensor_linked_rooms.py. These are the
edges a browser reaches: who may set it, which rooms may be chosen, what the
zone payload says, and what happens to a link when its sensor or room goes.
"""

import json

import pytest

import auth
import sensor_persistence
import server
from tests.test_sensor_api import (  # noqa: F401 - fixtures are used by name
    add_sensor, client, enable, isolated_sensor_service, set_schedule, zone,
)


LIVING, KITCHEN, HALLWAY = "12", "5", "3"


@pytest.fixture(autouse=True)
def no_links():
    server.sensor_heating_links = {}
    yield
    server.sensor_heating_links = {}


def enable_rooms(client, rules):
    response = client.put("/api/sensors/settings", json={
        "enabled": True,
        "zones": {
            zone_id: {
                "warning_delay_seconds": 300,
                "action_when_open": action,
                "action_delay_seconds": 0,
                "override_all_modes": False,
            }
            for zone_id, action in rules.items()
        },
    })
    assert response.status_code == 200, response.text


def link(client, sensor_id, zone_ids):
    return client.put(f"/api/sensors/{sensor_id}/heating", json={"zone_ids": zone_ids})


def simulate(client, sensor_id, state):
    response = client.post(f"/api/sensors/{sensor_id}/simulate", json={"state": state})
    assert response.status_code == 200, response.text


def test_the_patio_door_turns_down_the_kitchen_and_hallway_and_lets_them_go(client):
    for zone_id in (LIVING, KITCHEN, HALLWAY):
        set_schedule(client, "comfort", zone_id)
    enable_rooms(client, {LIVING: "eco"})
    door = add_sensor(client, "Patio Door", zone_id=LIVING, kind="door")

    saved = link(client, door["sensor_id"], [KITCHEN, HALLWAY])
    assert saved.status_code == 200, saved.text
    assert saved.json()["controls_zone_ids"] == [KITCHEN, HALLWAY]

    kitchen = zone(client, KITCHEN)
    assert kitchen["controlled_by"][0]["name"] == "Patio Door"
    assert kitchen["controlled_by"][0]["zone_name"] == "Living Room"

    simulate(client, door["sensor_id"], "open")
    for zone_id in (LIVING, KITCHEN, HALLWAY):
        assert zone(client, zone_id)["current_mode"] == "eco", zone_id
    kitchen = zone(client, KITCHEN)
    assert kitchen["sensor_summary"]["owned_action"] == "eco"
    assert kitchen["sensor_summary"]["warning_raised"] is False
    [hold] = kitchen["sensor_summary"]["linked"]
    assert hold["zone_id"] == LIVING
    assert hold["zone_name"] == "Living Room"
    assert hold["sensors"] == [{"sensor_id": door["sensor_id"], "name": "Patio Door"}]
    assert hold["action_when_open"] == "eco"
    assert hold["due"] is True

    simulate(client, door["sensor_id"], "closed")
    for zone_id in (LIVING, KITCHEN, HALLWAY):
        current = zone(client, zone_id)
        assert current["current_mode"] == "normal", zone_id
        assert current["sensor_summary"]["owned_action"] is None
        assert current["sensor_summary"]["linked"] == []


def test_links_are_saved_to_disk_and_read_back(client):
    enable_rooms(client, {LIVING: "eco"})
    door = add_sensor(client, "Patio Door", zone_id=LIVING, kind="door")
    link(client, door["sensor_id"], [KITCHEN])
    assert sensor_persistence.load_heating_links() == {door["sensor_id"]: [KITCHEN]}


def test_its_own_room_and_repeats_are_dropped_not_refused(client):
    enable_rooms(client, {LIVING: "eco"})
    door = add_sensor(client, "Patio Door", zone_id=LIVING, kind="door")
    saved = link(client, door["sensor_id"], [LIVING, KITCHEN, KITCHEN])
    assert saved.status_code == 200
    assert saved.json()["controls_zone_ids"] == [KITCHEN]


def test_a_room_without_a_heater_cannot_be_chosen(client):
    storage = client.post("/api/zones", json={"name": "Private Storage"}).json()["zone_id"]
    enable_rooms(client, {LIVING: "eco"})
    door = add_sensor(client, "Patio Door", zone_id=LIVING, kind="door")
    refused = link(client, door["sensor_id"], [storage])
    assert refused.status_code == 400
    assert "no heater" in refused.json()["detail"]
    assert link(client, door["sensor_id"], ["999"]).status_code == 400
    assert server.sensor_heating_links == {}


def test_a_thermometer_cannot_heat_other_rooms(client):
    enable_rooms(client, {LIVING: "eco"})
    thermometer = add_sensor(client, "Thermometer", zone_id=LIVING, kind="climate")
    assert link(client, thermometer["sensor_id"], [KITCHEN]).status_code == 400


def test_an_unknown_sensor_is_404(client):
    enable_rooms(client, {LIVING: "eco"})
    assert link(client, "nope", [KITCHEN]).status_code == 404


def test_only_an_admin_may_choose(client, monkeypatch):
    enable_rooms(client, {LIVING: "eco"})
    door = add_sensor(client, "Patio Door", zone_id=LIVING, kind="door")
    original = auth.load_users

    def users():
        data = dict(original())
        data["admin"] = {**data["admin"], "role": "user"}
        return data

    monkeypatch.setattr(auth, "load_users", users)
    assert link(client, door["sensor_id"], [KITCHEN]).status_code == 403


def test_refused_while_sensors_are_off(client):
    assert link(client, "anything", [KITCHEN]).status_code == 409


def test_removing_the_sensor_removes_its_links(client):
    enable_rooms(client, {LIVING: "eco"})
    door = add_sensor(client, "Patio Door", zone_id=LIVING, kind="door")
    link(client, door["sensor_id"], [KITCHEN])
    assert client.delete(f"/api/sensors/{door['sensor_id']}").status_code == 200
    assert server.sensor_heating_links == {}
    assert sensor_persistence.load_heating_links() == {}
    assert zone(client, KITCHEN)["controlled_by"] == []


def test_moving_into_a_linked_room_makes_it_simply_its_own(client):
    enable_rooms(client, {LIVING: "eco"})
    door = add_sensor(client, "Patio Door", zone_id=LIVING, kind="door")
    link(client, door["sensor_id"], [KITCHEN, HALLWAY])
    moved = client.put(
        f"/api/sensors/{door['sensor_id']}",
        json={"name": "Patio Door", "zone_id": KITCHEN, "kind": "door"},
    )
    assert moved.status_code == 200
    assert moved.json()["controls_zone_ids"] == [HALLWAY]


def test_deleting_a_room_removes_it_from_every_link(client):
    spare = client.post("/api/zones", json={"name": "Annex"}).json()["zone_id"]
    # A room with a heater, so the link is allowed at all.
    next(item for item in server.DEMO_ZONES if item["zone_id"] == spare)[
        "components"
    ].append("999999999999")
    enable_rooms(client, {LIVING: "eco"})
    door = add_sensor(client, "Patio Door", zone_id=LIVING, kind="door")
    assert link(client, door["sensor_id"], [spare, KITCHEN]).status_code == 200
    next(item for item in server.DEMO_ZONES if item["zone_id"] == spare)["components"] = []
    assert client.delete(f"/api/zones/{spare}").status_code == 200
    assert server.sensor_heating_links == {door["sensor_id"]: [KITCHEN]}


def test_a_new_link_reaches_websocket_clients(client):
    enable_rooms(client, {LIVING: "eco"})
    door = add_sensor(client, "Patio Door", zone_id=LIVING, kind="door")
    with client.websocket_connect("/ws") as websocket:
        assert json.loads(websocket.receive_text())["type"] == "zones_update"
        link(client, door["sensor_id"], [KITCHEN])
        message = json.loads(websocket.receive_text())
        kitchen = next(item for item in message["data"] if item["zone_id"] == KITCHEN)
        assert kitchen["controlled_by"][0]["sensor_id"] == door["sensor_id"]


def test_the_disabled_feature_says_nothing_about_links(client):
    enable_rooms(client, {LIVING: "eco"})
    door = add_sensor(client, "Patio Door", zone_id=LIVING, kind="door")
    link(client, door["sensor_id"], [KITCHEN])
    client.put("/api/sensors/settings", json={"enabled": False, "zones": {}})
    assert "controlled_by" not in zone(client, KITCHEN)


def test_taking_it_out_of_every_room_forgets_what_it_heated(client):
    enable_rooms(client, {LIVING: "eco"})
    door = add_sensor(client, "Patio Door", zone_id=LIVING, kind="door")
    link(client, door["sensor_id"], [KITCHEN])
    cleared = client.put(f"/api/sensors/{door['sensor_id']}", json={"clear_zone": True})
    assert cleared.status_code == 200, cleared.text
    assert server.sensor_heating_links == {}
    assert link(client, door["sensor_id"], [KITCHEN]).status_code == 400
