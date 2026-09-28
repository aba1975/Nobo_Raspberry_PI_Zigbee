"""A zone with no heater can go without a heating schedule.

The hub cannot express this: every zone record (A00/U00) carries a week profile
id, and a new zone is put on the built-in one. A storeroom with only a door
sensor therefore showed "Schedule · Comfort" and a week of Comfort and Eco
blocks that nothing follows. The setting lives on the Pi, and:

  - it is refused for a zone with heaters, since that schedule is what drives
    them, and it stops applying the moment a heater is added;
  - the zone is parked on the built-in profile, so it cannot block deleting a
    custom schedule, and is not listed as using the one it is parked on;
  - it is forgotten when the zone is deleted, because the hub reuses ids;
  - demo and a real hub behave the same.
"""

import copy
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config_persistence
import demo_week
import server

ROOT = Path(__file__).resolve().parent.parent
CABIN = (ROOT / "app" / "static" / "ui" / "cabin" / "cabin.js").read_text(encoding="utf-8")

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def week(mode):
    return {day: [{"start": "00:00", "end": "24:00", "mode": mode}] for day in DAYS}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config_persistence, "DEMO_WEEK_PROFILES_FILE",
        tmp_path / "demo_week_profiles.json", raising=False,
    )
    monkeypatch.setattr(config_persistence, "save_demo_zones", lambda zones: None)
    original_zones = copy.deepcopy(server.DEMO_ZONES)
    original_profiles = server.demo_week_profiles
    original_flat = copy.deepcopy(server.demo_schedules)
    server.demo_week_profiles = demo_week.DemoWeekProfiles(
        default_schedule=server.DEFAULT_DEMO_SCHEDULE
    )
    server.demo_week_profiles.sync_zones([str(z["zone_id"]) for z in server.DEMO_ZONES])
    server.demo_schedules = server.demo_week_profiles.as_per_zone_schedules()
    server.zones_without_schedule.clear()
    yield tmp_path
    server.zones_without_schedule.clear()
    server.DEMO_ZONES[:] = original_zones
    server.demo_week_profiles = original_profiles
    server.demo_schedules.clear()
    server.demo_schedules.update(original_flat)


@pytest.fixture
def client():
    with TestClient(server.app) as value:
        value.cookies.set("session_id", "pytest-fixed-session-id")
        yield value


def zones(client):
    return {z["zone_id"]: z for z in client.get("/api/zones").json()["zones"]}


def add_storeroom(client, **extra):
    response = client.post("/api/zones", json={"name": "Private Storage", **extra})
    assert response.status_code == 200, response.text
    return response.json()["zone_id"]


# ---------------------------------------------------------------------------
# Demo mode
# ---------------------------------------------------------------------------


def test_a_zone_has_a_schedule_unless_told_otherwise(client):
    zone_id = add_storeroom(client)
    assert zones(client)[zone_id]["no_schedule"] is False
    assert all(z["no_schedule"] is False for z in zones(client).values())


def test_a_new_zone_can_start_without_a_schedule(client, isolated):
    zone_id = add_storeroom(client, no_schedule=True)
    assert zones(client)[zone_id]["no_schedule"] is True
    saved = json.loads((isolated / "zones_without_schedule.json").read_text())
    assert saved == [zone_id]


def test_the_schedule_can_be_turned_off_and_on_again(client):
    zone_id = add_storeroom(client)
    assert client.put(f"/api/zones/{zone_id}", json={"no_schedule": True}).status_code == 200
    assert zones(client)[zone_id]["no_schedule"] is True
    assert client.put(f"/api/zones/{zone_id}", json={"no_schedule": False}).status_code == 200
    assert zones(client)[zone_id]["no_schedule"] is False


def test_other_edits_leave_the_setting_alone(client):
    zone_id = add_storeroom(client, no_schedule=True)
    assert client.put(f"/api/zones/{zone_id}", json={"name": "Store"}).status_code == 200
    assert zones(client)[zone_id]["no_schedule"] is True


def test_a_zone_with_heaters_keeps_its_schedule(client):
    heated = next(z for z in server.DEMO_ZONES if z["components"])
    zone_id = heated["zone_id"]
    response = client.put(
        f"/api/zones/{zone_id}", json={"name": "Renamed", "no_schedule": True}
    )
    assert response.status_code == 400
    assert "heater" in response.json()["detail"].lower()
    # Refused as a whole, not half applied.
    assert zones(client)[zone_id]["name"] == heated["name"]
    assert zones(client)[zone_id]["no_schedule"] is False


def test_adding_a_heater_brings_the_schedule_back(client):
    zone_id = add_storeroom(client, no_schedule=True)
    demo_zone = next(z for z in server.DEMO_ZONES if z["zone_id"] == zone_id)
    demo_zone["components"].append("186100000099")
    assert zones(client)[zone_id]["no_schedule"] is False


def test_it_is_not_listed_as_using_the_schedule_it_is_parked_on(client):
    zone_id = add_storeroom(client, no_schedule=True)
    default = next(
        p for p in client.get("/api/week_profiles").json()["week_profiles"]
        if p["profile_id"] == demo_week.DEFAULT_PROFILE_ID
    )
    assert zone_id not in {u["zone_id"] for u in default["used_by"]}

    heated = next(z for z in server.DEMO_ZONES if z["components"])
    if server.demo_week_profiles.profile_id_for(heated["zone_id"]) == demo_week.DEFAULT_PROFILE_ID:
        shared = client.get(f"/api/zones/{heated['zone_id']}/schedule").json()
        assert "Private Storage" not in shared["shared_with_zones"]


def test_turning_it_off_frees_a_custom_schedule_for_deletion(client):
    zone_id = add_storeroom(client)
    created = client.post(
        "/api/week_profiles", json={"name": "Storage week", "schedule": week("eco")}
    ).json()["profile_id"]
    assert client.post(
        f"/api/zones/{zone_id}/week-profile", json={"profile_id": created}
    ).status_code == 200

    assert client.put(f"/api/zones/{zone_id}", json={"no_schedule": True}).status_code == 200

    assert server.demo_week_profiles.profile_id_for(zone_id) == demo_week.DEFAULT_PROFILE_ID
    assert client.delete(f"/api/week_profiles/{created}").status_code == 200


def test_deleting_the_zone_forgets_the_setting(client, isolated):
    zone_id = add_storeroom(client, no_schedule=True)
    assert client.delete(f"/api/zones/{zone_id}").status_code == 200
    assert zone_id not in server.zones_without_schedule
    assert json.loads((isolated / "zones_without_schedule.json").read_text()) == []
    # The next zone given this id starts with a schedule, as any new zone does.
    assert add_storeroom(client) == zone_id
    assert zones(client)[zone_id]["no_schedule"] is False


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_the_setting_survives_a_restart(isolated):
    config_persistence.save_zones_without_schedule({"13", "2"})
    assert config_persistence.load_zones_without_schedule() == {"13", "2"}


def test_no_file_means_every_zone_has_a_schedule(isolated):
    assert config_persistence.load_zones_without_schedule() == set()


def test_a_corrupt_file_is_set_aside(isolated):
    path = isolated / "zones_without_schedule.json"
    path.write_text("{not json", encoding="utf-8")
    assert config_persistence.load_zones_without_schedule() == set()
    assert path.with_suffix(".backup").exists()


def test_the_wrong_shape_is_ignored(isolated):
    (isolated / "zones_without_schedule.json").write_text('{"13": true}', encoding="utf-8")
    assert config_persistence.load_zones_without_schedule() == set()


# ---------------------------------------------------------------------------
# The cabin interface
# ---------------------------------------------------------------------------


def _function(name: str) -> str:
    start = CABIN.index(f"function {name}(")
    return CABIN[start:CABIN.index("\n  }\n", start)]


def test_the_zone_card_drops_the_schedule_badge():
    body = _function("zoneRow")
    assert re.search(r"modeBadge = zone\.no_schedule === true \? ''", body)


def test_the_zone_page_hides_the_week_and_the_modes():
    body = _function("renderZoneDetail")
    assert "const noSchedule = zone.no_schedule === true;" in body
    assert re.search(r"\$\{noSchedule \? '' : `\s*<div class=\"mode-row\"", body)
    assert 'data-act="schedule-on"' in body
    # The shortcut to drop the schedule is only offered where there is no heater.
    assert re.search(r"\$\{noHeaters \? `<button class=\"btn\" type=\"button\" data-act=\"schedule-off\"", body)


def test_the_room_sheets_offer_the_choice():
    rename = _function("renameZone")
    assert "noHeaters ?" in rename and 'id="rzSchedule"' in rename
    assert "body.no_schedule = !scheduleBox.checked" in rename
    add = _function("addZoneSheet")
    assert 'id="azSchedule" checked' in add
    assert "no_schedule: !root.querySelector('#azSchedule').checked" in add
