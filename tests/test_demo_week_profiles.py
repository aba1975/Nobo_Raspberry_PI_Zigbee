"""Week profiles in demo mode, which used to be stubs that lied.

Every profile operation returned success and stored nothing: creating a
schedule in Settings reported "Schedule added" and changed nothing, the list
showed one built-in profile claiming every zone used it, and saving a zone's
week as a new schedule wrote a profile that never existed. These tests hold the
simulated hub to the same rules the real one follows.
"""

import copy
import json

import pytest
from fastapi.testclient import TestClient

import config_persistence
import demo_week
import server


DAYS = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
]


def week(mode):
    return {day: [{"start": "00:00", "end": "24:00", "mode": mode}] for day in DAYS}


@pytest.fixture(autouse=True)
def isolated_week_profiles(tmp_path, monkeypatch):
    """Give each test its own simulated hub, restored afterwards."""
    monkeypatch.setattr(
        config_persistence, "DEMO_WEEK_PROFILES_FILE",
        tmp_path / "demo_week_profiles.json", raising=False,
    )
    original = server.demo_week_profiles
    original_flat = copy.deepcopy(server.demo_schedules)
    server.demo_week_profiles = demo_week.DemoWeekProfiles(
        default_schedule=server.DEFAULT_DEMO_SCHEDULE
    )
    server.demo_week_profiles.sync_zones(
        [str(z["zone_id"]) for z in server.DEMO_ZONES]
    )
    server.demo_schedules = server.demo_week_profiles.as_per_zone_schedules()
    yield
    server.demo_week_profiles = original
    server.demo_schedules.clear()
    server.demo_schedules.update(original_flat)


@pytest.fixture
def client():
    with TestClient(server.app) as value:
        value.cookies.set("session_id", "pytest-fixed-session-id")
        yield value


def profiles(client):
    return client.get("/api/week_profiles").json()["week_profiles"]


# ---------------------------------------------------------------------------
# Creating one in Settings
# ---------------------------------------------------------------------------

def test_a_schedule_added_in_settings_is_actually_there_afterwards(client):
    response = client.post(
        "/api/week_profiles", json={"name": "Weekend", "schedule": week("eco")}
    )
    assert response.status_code == 200, response.text
    profile_id = response.json()["profile_id"]

    listed = profiles(client)
    made = next(p for p in listed if p["profile_id"] == profile_id)
    assert made["name"] == "Weekend"
    assert made["schedule"] == week("eco")
    assert made["used_by"] == []
    assert made["can_edit"] is True


def test_a_new_schedule_is_written_to_disk(client, tmp_path):
    client.post(
        "/api/week_profiles", json={"name": "Weekend", "schedule": week("eco")}
    )
    saved = json.loads((tmp_path / "demo_week_profiles.json").read_text())
    assert any(row["name"] == "Weekend" for row in saved["profiles"].values())


def test_a_repeated_name_is_numbered_rather_than_refused(client):
    for _ in range(2):
        assert client.post(
            "/api/week_profiles", json={"name": "Weekend", "schedule": week("eco")}
        ).status_code == 200
    names = sorted(p["name"] for p in profiles(client))
    assert "Weekend" in names and "Weekend 2" in names


def test_an_empty_name_is_refused(client):
    assert client.post(
        "/api/week_profiles", json={"name": "  ", "schedule": week("eco")}
    ).status_code == 400


# ---------------------------------------------------------------------------
# Changing and removing one
# ---------------------------------------------------------------------------

def test_a_schedule_can_be_renamed_and_re_scheduled(client):
    profile_id = client.post(
        "/api/week_profiles", json={"name": "Weekend", "schedule": week("eco")}
    ).json()["profile_id"]

    assert client.patch(
        f"/api/week_profiles/{profile_id}",
        json={"name": "Weekends", "schedule": week("away")},
    ).status_code == 200

    made = next(p for p in profiles(client) if p["profile_id"] == profile_id)
    assert made["name"] == "Weekends"
    assert made["schedule"] == week("away")


def test_a_schedule_in_use_cannot_be_deleted_until_nothing_follows_it(client):
    profile_id = client.post(
        "/api/week_profiles", json={"name": "Weekend", "schedule": week("eco")}
    ).json()["profile_id"]
    assert client.post(
        "/api/zones/2/week-profile", json={"profile_id": profile_id}
    ).status_code == 200

    refused = client.delete(f"/api/week_profiles/{profile_id}")
    assert refused.status_code == 400
    assert "following" in refused.json()["detail"]
    listed = next(p for p in profiles(client) if p["profile_id"] == profile_id)
    assert listed["can_delete"] is False
    assert [user["zone_id"] for user in listed["used_by"]] == ["2"]

    client.post("/api/zones/2/week-profile", json={"profile_id": "1"})
    assert client.delete(f"/api/week_profiles/{profile_id}").status_code == 200
    assert all(p["profile_id"] != profile_id for p in profiles(client))


@pytest.mark.parametrize("call", [
    lambda c: c.patch("/api/week_profiles/1", json={"name": "Renamed"}),
    lambda c: c.delete("/api/week_profiles/1"),
])
def test_the_built_in_schedule_is_protected_as_it_is_on_the_hub(client, call):
    assert call(client).status_code == 400
    built_in = next(p for p in profiles(client) if p["profile_id"] == "1")
    assert built_in["name"] == "Default"
    assert built_in["can_edit"] is False and built_in["can_delete"] is False


def test_a_schedule_that_is_gone_is_reported_rather_than_invented(client):
    assert client.patch(
        "/api/week_profiles/99", json={"name": "Ghost"}
    ).status_code == 400
    assert client.delete("/api/week_profiles/99").status_code == 400
    assert client.post(
        "/api/zones/1/week-profile", json={"profile_id": "99"}
    ).status_code == 404


# ---------------------------------------------------------------------------
# A zone's own week
# ---------------------------------------------------------------------------

def test_a_zone_reports_which_schedule_it_follows_and_who_shares_it(client):
    body = client.get("/api/zones/1/schedule").json()
    assert body["week_profile_id"] == "1"
    assert body["week_profile_name"] == "Default"
    # Every zone starts on the built-in one, as they do on the hub.
    assert len(body["shared_with_zones"]) == len(server.DEMO_ZONES) - 1


def test_editing_a_shared_week_copies_it_rather_than_moving_everyone(client):
    before = client.get("/api/zones/2/schedule").json()["schedule"]
    assert client.post(
        "/api/zones/1/schedule", json={"schedule": week("eco")}
    ).status_code == 200

    mine = client.get("/api/zones/1/schedule").json()
    assert mine["week_profile_id"] != "1"
    assert mine["schedule"] == week("eco")
    assert mine["week_profile_name"].startswith(server.DEMO_ZONES[0]["name"])

    # The room next door is exactly as it was.
    assert client.get("/api/zones/2/schedule").json()["schedule"] == before


def test_editing_a_week_a_zone_owns_alone_changes_it_in_place(client):
    client.post("/api/zones/1/schedule", json={"schedule": week("eco")})
    own = client.get("/api/zones/1/schedule").json()["week_profile_id"]

    client.post("/api/zones/1/schedule", json={"schedule": week("comfort")})
    after = client.get("/api/zones/1/schedule").json()
    assert after["week_profile_id"] == own, "a second copy was made"
    assert after["schedule"] == week("comfort")


def test_asking_to_change_the_schedule_itself_moves_every_zone_on_it(client):
    client.post("/api/zones/1/schedule", json={"schedule": week("eco")})
    shared = client.get("/api/zones/1/schedule").json()["week_profile_id"]
    client.post("/api/zones/3/week-profile", json={"profile_id": shared})

    assert client.post(
        "/api/zones/1/schedule",
        json={"schedule": week("away"), "apply_to": "profile"},
    ).status_code == 200
    assert client.get("/api/zones/3/schedule").json()["schedule"] == week("away")


def test_a_zone_that_does_not_exist_is_a_404(client):
    assert client.get("/api/zones/999/schedule").status_code == 404
    assert client.post(
        "/api/zones/999/schedule", json={"schedule": week("eco")}
    ).status_code == 404


# ---------------------------------------------------------------------------
# The rest of the app reads the flat per-zone view
# ---------------------------------------------------------------------------

def test_the_per_zone_view_and_the_schedule_lookup_stay_in_step(client):
    client.post("/api/zones/1/schedule", json={"schedule": week("away")})
    assert server.demo_schedules["1"] == week("away")
    assert server.get_current_schedule_mode("1") == "away"

    client.post("/api/zones/1/week-profile", json={"profile_id": "1"})
    assert server.demo_schedules["1"] == server.DEFAULT_DEMO_SCHEDULE


def test_a_deleted_zone_stops_being_counted_as_following_anything(client):
    made = client.post("/api/zones", json={"name": "Spare Room"})
    assert made.status_code == 200
    zone_id = str(server.DEMO_ZONES[-1]["zone_id"])
    profile_id = client.post(
        "/api/week_profiles", json={"name": "Spare", "schedule": week("eco")}
    ).json()["profile_id"]
    client.post(f"/api/zones/{zone_id}/week-profile", json={"profile_id": profile_id})

    assert client.delete(f"/api/zones/{zone_id}").status_code == 200
    listed = next(p for p in profiles(client) if p["profile_id"] == profile_id)
    assert listed["used_by"] == []
    assert listed["can_delete"] is True


# ---------------------------------------------------------------------------
# Coming from an installation that only had per-zone weeks
# ---------------------------------------------------------------------------

def test_older_per_zone_schedules_migrate_to_a_profile_per_room():
    model = demo_week.DemoWeekProfiles.migrated(
        {"1": week("eco"), "2": server.DEFAULT_DEMO_SCHEDULE},
        default_schedule=server.DEFAULT_DEMO_SCHEDULE,
        zone_name=lambda zone_id: {"1": "Large Bathroom", "2": "Kitchen"}[zone_id],
    )
    # An edited week becomes a schedule of its own, named after the room.
    assert model.schedule_for("1") == week("eco")
    assert model.name_for("1") == "Large Bathroom schedule"
    # One that matched the default simply follows the default.
    assert model.profile_id_for("2") == demo_week.DEFAULT_PROFILE_ID


def test_a_zone_nobody_had_edited_starts_on_the_built_in_schedule():
    model = demo_week.DemoWeekProfiles.migrated(
        {}, default_schedule=server.DEFAULT_DEMO_SCHEDULE, zone_name=lambda z: z
    )
    model.sync_zones(["1", "2"])
    assert model.profile_id_for("1") == demo_week.DEFAULT_PROFILE_ID
    assert model.schedule_for("2") == server.DEFAULT_DEMO_SCHEDULE


def test_a_room_split_out_by_the_older_migration_keeps_the_week_it_inherited():
    """The grouped-demo split copies the source room's week to the new rooms.

    It does that by writing the flat per-zone map, which is now derived from
    the profiles — so the profile model has to be built after it has run, or
    the inherited weeks are shown as the default and then overwritten with it
    by the first save.
    """
    edited = week("away")
    model = demo_week.DemoWeekProfiles.migrated(
        # As the split leaves things: the source room and the rooms carved out
        # of it all holding the same edited week.
        {"7": edited, "9": copy.deepcopy(edited), "10": copy.deepcopy(edited)},
        default_schedule=server.DEFAULT_DEMO_SCHEDULE,
        zone_name=lambda zone_id: f"Room {zone_id}",
    )
    for zone_id in ("7", "9", "10"):
        assert model.schedule_for(zone_id) == edited, zone_id
        assert model.profile_id_for(zone_id) != demo_week.DEFAULT_PROFILE_ID

    # Saving must not quietly replace them with the built-in schedule.
    assert model.as_per_zone_schedules()["9"] == edited
