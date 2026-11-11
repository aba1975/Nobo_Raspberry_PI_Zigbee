"""The house view's API: the floor plan, placements, and each user's own choice.

Pinned here: who may read and who may change (signed in reads, admin writes,
a display key opens nothing), that nothing about the plan touches the heating
or pushes to other browsers, and that a user's choice is theirs alone.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

import auth
import house_model
import server
import user_prefs

SESSION = "pytest-fixed-session-id"


@pytest.fixture
def client():
    with TestClient(server.app) as c:
        c.cookies.set("session_id", SESSION)
        yield c


@pytest.fixture
def ordinary_user():
    """A signed-in user who is not an administrator."""
    users = auth.load_users()
    users["guest"] = {"password_hash": auth.hash_password("guest-password"), "role": "user"}
    auth.save_users(users)
    sid = "session-guest"
    auth.sessions[sid] = {"username": "guest", "created": time.time()}
    with TestClient(server.app) as c:
        c.cookies.set("session_id", sid)
        yield c
    auth.sessions.pop(sid, None)


def demo_heaters():
    return [str(serial) for zone in server.DEMO_ZONES for serial in zone["components"]]


def small_plan():
    return {
        "schema": 1,
        "levels": [{"id": "ground", "elevation": 0, "height": 2.5}],
        "walls": [{"id": "s", "level": "ground", "a": [0, 0], "b": [5, 0], "exterior": True,
                   "openings": [{"code": "W1", "kind": "window", "at": 1, "width": 1, "sill": 0.9, "height": 1.2}]}],
        "rooms": [{"id": "hall", "level": "ground", "zone_id": "3",
                   "polygon": [[0, 0], [5, 0], [5, 4], [0, 4]]}],
    }


class TestReading:
    def test_needs_a_session(self):
        with TestClient(server.app) as anon:
            anon.cookies.clear()
            assert anon.get("/api/house").status_code == 401

    def test_a_display_key_does_not_open_it(self, client):
        _, key = server.display_registry.create("Hall")
        response = client.get("/api/house", headers={"Authorization": f"Bearer {key}"})
        assert response.status_code == 401

    def test_demo_mode_shows_the_demo_building(self, client):
        body = client.get("/api/house").json()
        assert body["source"] == "demo"
        placed = body["house"]["placements"]["heaters"]
        assert body["status"]["unplaced"]["heaters"] == [s for s in demo_heaters() if s not in placed]
        assert body["status"]["stale"]["rooms"] == []

    def test_an_ordinary_user_can_read(self, ordinary_user):
        assert ordinary_user.get("/api/house").status_code == 200

    def test_real_mode_without_a_plan_is_404(self, client, monkeypatch):
        monkeypatch.setattr(server, "DEMO_MODE", False)
        response = client.get("/api/house")
        assert response.status_code == 404
        assert "No floor plan" in response.json()["detail"]

    def test_status_is_unknown_while_the_hub_is_away(self, client, monkeypatch):
        monkeypatch.setattr(server, "hub_connected", False)
        status = client.get("/api/house").json()["status"]
        assert status["unplaced"]["heaters"] is None
        assert status["stale"]["heaters"] is None
        assert status["stale"]["rooms"] is None

    def test_sensor_status_is_unknown_while_sensors_are_off(self, client):
        # Off is the default in the tests. An empty list here would call every
        # placed sensor gone.
        model = small_plan()
        model["placements"] = {"heaters": {}, "sensors": {"0x00158d008c8bc54f": {"opening": "W1"}}}
        status = client.put("/api/house", content=json.dumps(model)).json()["status"]
        assert status["unplaced"]["sensors"] is None
        assert status["stale"]["sensors"] is None

    def test_a_room_for_a_zone_the_hub_does_not_have_is_listed(self, client):
        model = small_plan()
        model["rooms"][0]["zone_id"] = "99"
        client.put("/api/house", content=json.dumps(model))
        assert client.get("/api/house").json()["status"]["stale"]["rooms"] == ["hall"]


class TestLoadingAPlan:
    def test_admin_loads_a_plan(self, client):
        response = client.put("/api/house", content=json.dumps(small_plan()))
        assert response.status_code == 200, response.text
        assert response.json()["source"] == "file"
        assert client.get("/api/house").json()["source"] == "file"
        # In demo mode, never the real building's file.
        assert house_model.house_file(True).exists()
        assert not house_model.house_file(False).exists()

    def test_an_ordinary_user_cannot(self, ordinary_user):
        assert ordinary_user.put("/api/house", content=json.dumps(small_plan())).status_code == 403
        assert ordinary_user.delete("/api/house").status_code == 403

    def test_an_invalid_plan_says_why(self, client):
        model = small_plan()
        model["walls"][0]["openings"][0]["at"] = 4.5
        response = client.put("/api/house", content=json.dumps(model))
        assert response.status_code == 400
        assert "runs past the end of its wall" in response.json()["detail"]

    def test_too_large_is_413(self, client):
        response = client.put("/api/house", content=b" " * (house_model.MAX_BYTES + 1))
        assert response.status_code == 413

    def test_not_json_is_400(self, client):
        assert client.put("/api/house", content=b"<svg/>").status_code == 400

    def test_replacing_keeps_what_still_fits_and_reports_the_rest(self, client):
        client.put("/api/house", content=json.dumps(small_plan()))
        serial = demo_heaters()[0]
        client.put(f"/api/house/placements/heaters/{serial}", json={"wall": "s", "at": 0, "length": 1})
        moved = small_plan()
        moved["walls"][0]["id"] = "south"
        body = client.put("/api/house", content=json.dumps(moved)).json()
        assert body["dropped"] == {"heaters": [serial], "sensors": []}

    def test_the_demo_buildings_placements_are_not_inherited(self, client):
        body = client.put("/api/house", content=json.dumps(small_plan())).json()
        assert body["house"]["placements"] == {"heaters": {}, "sensors": {}}
        assert body["dropped"] == {"heaters": [], "sensors": []}

    def test_delete(self, client):
        client.put("/api/house", content=json.dumps(small_plan()))
        assert client.delete("/api/house").status_code == 200
        assert client.delete("/api/house").status_code == 404
        # Demo mode falls back to the demo building again.
        assert client.get("/api/house").json()["source"] == "demo"


class TestPlacing:
    def test_place_and_unplace_a_heater(self, client):
        client.put("/api/house", content=json.dumps(small_plan()))
        serial = demo_heaters()[0]
        response = client.put(f"/api/house/placements/heaters/{serial}", json={"floor": "hall"})
        assert response.status_code == 200, response.text
        assert serial not in response.json()["status"]["unplaced"]["heaters"]
        response = client.delete(f"/api/house/placements/heaters/{serial}")
        assert serial in response.json()["status"]["unplaced"]["heaters"]

    def test_placing_from_the_demo_building_saves_a_copy(self, client):
        serial = demo_heaters()[0]
        response = client.put(f"/api/house/placements/heaters/{serial}",
                              json={"wall": "s", "at": 0.5, "length": 1})
        assert response.status_code == 200, response.text
        assert house_model.load(True)[1] == "file"

    def test_an_unknown_heater_is_404(self, client):
        response = client.put("/api/house/placements/heaters/123456789012", json={"floor": "living"})
        assert response.status_code == 404

    def test_an_unknown_kind_is_404(self, client):
        assert client.put("/api/house/placements/lamps/x", json={}).status_code == 404

    def test_a_bad_placement_says_why(self, client):
        serial = demo_heaters()[0]
        response = client.put(f"/api/house/placements/heaters/{serial}", json={"wall": "nowhere", "at": 0, "length": 1})
        assert response.status_code == 400
        assert "no wall called 'nowhere'" in response.json()["detail"]

    def test_needs_the_hub_to_know_the_device(self, client, monkeypatch):
        monkeypatch.setattr(server, "hub_connected", False)
        serial = demo_heaters()[0]
        assert client.put(f"/api/house/placements/heaters/{serial}", json={"floor": "living"}).status_code == 503

    def test_a_stale_placement_can_be_removed(self, client):
        model = small_plan()
        model["placements"] = {"heaters": {"999999999999": {"floor": "hall"}}, "sensors": {}}
        body = client.put("/api/house", content=json.dumps(model)).json()
        assert body["status"]["stale"]["heaters"] == ["999999999999"]
        assert client.delete("/api/house/placements/heaters/999999999999").status_code == 200
        assert client.delete("/api/house/placements/heaters/999999999999").status_code == 404

    def test_an_ordinary_user_cannot_place(self, ordinary_user):
        serial = demo_heaters()[0]
        assert ordinary_user.put(f"/api/house/placements/heaters/{serial}", json={"floor": "living"}).status_code == 403

    def test_placements_sent_together_all_land(self, client):
        """Several at once — what dragging devices onto the plan will send."""
        import asyncio

        import httpx

        client.put("/api/house", content=json.dumps(small_plan()))
        serials = demo_heaters()[:6]

        async def place_all():
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                         cookies={"session_id": SESSION}) as ac:
                return await asyncio.gather(*(
                    ac.put(f"/api/house/placements/heaters/{serial}", json={"floor": "hall"})
                    for serial in serials))

        responses = asyncio.run(place_all())
        assert [r.status_code for r in responses] == [200] * len(serials)
        placed = client.get("/api/house").json()["house"]["placements"]["heaters"]
        assert sorted(placed) == sorted(serials)


class TestNothingReachesTheHeating:
    def test_writes_do_not_push_zone_updates(self, client, monkeypatch):
        pushed = []

        async def record():
            pushed.append(True)

        monkeypatch.setattr(server, "broadcast_zone_update", record)
        client.put("/api/house", content=json.dumps(small_plan()))
        client.put(f"/api/house/placements/heaters/{demo_heaters()[0]}", json={"floor": "hall"})
        client.put("/api/me/preferences", json={"rooms_view": "house"})
        client.delete("/api/house")
        assert pushed == []

    def test_zones_are_unchanged(self, client):
        before = json.dumps(server.DEMO_ZONES, sort_keys=True)
        client.put("/api/house", content=json.dumps(small_plan()))
        client.put(f"/api/house/placements/heaters/{demo_heaters()[0]}", json={"floor": "hall"})
        assert json.dumps(server.DEMO_ZONES, sort_keys=True) == before


class TestPreferences:
    def test_default_is_the_list(self, client):
        assert client.get("/api/me/preferences").json() == {"rooms_view": "list"}

    def test_a_choice_is_the_users_own(self, client, ordinary_user):
        assert client.put("/api/me/preferences", json={"rooms_view": "house"}).json() == {"rooms_view": "house"}
        assert client.get("/api/me/preferences").json() == {"rooms_view": "house"}
        assert ordinary_user.get("/api/me/preferences").json() == {"rooms_view": "list"}

    def test_an_ordinary_user_may_choose(self, ordinary_user):
        assert ordinary_user.put("/api/me/preferences", json={"rooms_view": "house"}).status_code == 200

    def test_a_bad_value_is_400(self, client):
        response = client.put("/api/me/preferences", json={"rooms_view": "ghost"})
        assert response.status_code == 400
        assert client.get("/api/me/preferences").json() == {"rooms_view": "list"}

    def test_needs_a_session_even_with_the_anonymous_api(self, monkeypatch):
        monkeypatch.setattr(server, "ALLOW_ANON_API", True)
        with TestClient(server.app) as anon:
            anon.cookies.clear()
            assert anon.get("/api/me/preferences").status_code == 401

    def test_follows_a_rename(self, ordinary_user):
        ordinary_user.put("/api/me/preferences", json={"rooms_view": "house"})
        assert ordinary_user.post("/auth/rename", json={"new_username": "visitor"}).status_code == 200
        assert user_prefs.get("visitor") == {"rooms_view": "house"}
        assert ordinary_user.get("/api/me/preferences").json() == {"rooms_view": "house"}

    def test_removed_with_the_user(self, client, ordinary_user):
        ordinary_user.put("/api/me/preferences", json={"rooms_view": "house"})
        assert client.delete("/auth/admin/users/guest").status_code == 200
        assert user_prefs.get("guest") == {"rooms_view": "list"}
