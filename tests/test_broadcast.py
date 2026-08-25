"""
Tests for live zone broadcasts after a write (QA defect D-02).

Before this fix, broadcast_zone_update() was only called when the hub itself
pushed something. Changing a zone from the web UI updated your own screen, but
a second browser or phone kept showing the old temperature and mode until the
page was reloaded.
"""

import os
import sys
import json

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import server
from server import app

SESSION_COOKIE = "pytest-fixed-session-id"


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        c.cookies.set("session_id", SESSION_COOKIE)
        yield c


def drain(ws):
    """Consume the zones_update the server sends on connect."""
    assert json.loads(ws.receive_text())["type"] == "zones_update"


def next_update(ws):
    msg = json.loads(ws.receive_text())
    assert msg["type"] == "zones_update"
    return {z["zone_id"]: z for z in msg["data"]}


class TestWritesAreBroadcast:
    def test_zone_override_reaches_a_second_client(self, client):
        """The core of D-02: one browser changes a zone, the other must see it."""
        with client.websocket_connect("/ws") as watcher:
            drain(watcher)
            assert client.post("/api/zones/1/override/eco").status_code == 200
            zones = next_update(watcher)
            assert zones["1"]["current_mode"] == "eco"

            client.post("/api/zones/1/override/normal")
            zones = next_update(watcher)
            assert zones["1"]["current_mode"] != "eco"

    def test_temperature_change_is_broadcast(self, client):
        with client.websocket_connect("/ws") as watcher:
            drain(watcher)
            r = client.post("/api/zones/1/temperature", json={"comfort": 23, "eco": 19})
            assert r.status_code == 200
            zones = next_update(watcher)
            assert zones["1"]["comfort_temperature"] == 23
            assert zones["1"]["eco_temperature"] == 19

    def test_zone_rename_is_broadcast(self, client):
        with client.websocket_connect("/ws") as watcher:
            drain(watcher)
            assert client.put("/api/zones/1", json={"name": "Broadcast Test"}).status_code == 200
            assert next_update(watcher)["1"]["name"] == "Broadcast Test"

    def test_new_zone_is_broadcast(self, client):
        with client.websocket_connect("/ws") as watcher:
            drain(watcher)
            r = client.post("/api/zones", json={"name": "Fresh Zone", "icon": "🔥"})
            assert r.status_code == 200
            new_id = r.json()["zone_id"]
            assert next_update(watcher)[new_id]["name"] == "Fresh Zone"

    def test_deleted_zone_is_broadcast(self, client):
        created = client.post("/api/zones", json={"name": "Doomed", "icon": "X"})
        new_id = created.json()["zone_id"]
        with client.websocket_connect("/ws") as watcher:
            drain(watcher)
            assert client.delete(f"/api/zones/{new_id}").status_code == 200
            assert new_id not in next_update(watcher)

    def test_global_override_is_broadcast(self, client):
        with client.websocket_connect("/ws") as watcher:
            drain(watcher)
            assert client.post("/api/global/override/away").status_code == 200
            zones = next_update(watcher)
            assert all(z["current_mode"] == "away" for z in zones.values())
            client.post("/api/global/override/normal")

    def test_device_rename_is_broadcast(self, client):
        with client.websocket_connect("/ws") as watcher:
            drain(watcher)
            r = client.patch("/api/devices/210000016247/name", json={"name": "Renamed Heater"})
            assert r.status_code == 200
            zones = next_update(watcher)
            assert "Renamed Heater" in zones["1"]["components_names"]

    def test_every_client_gets_it_not_just_one(self, client):
        with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
            drain(a)
            drain(b)
            client.post("/api/zones/2/override/eco")
            assert next_update(a)["2"]["current_mode"] == "eco"
            assert next_update(b)["2"]["current_mode"] == "eco"
            client.post("/api/zones/2/override/normal")


class TestNoPointlessBroadcasts:
    def test_reads_do_not_broadcast(self, client):
        """A GET must not wake up every other client."""
        with client.websocket_connect("/ws") as watcher:
            drain(watcher)
            client.get("/api/zones")
            client.get("/api/devices")
            # Prove the queue is empty by triggering a real one and checking it
            # is the first message that arrives.
            client.post("/api/zones/3/override/eco")
            assert next_update(watcher)["3"]["current_mode"] == "eco"
            client.post("/api/zones/3/override/normal")

    def test_failed_writes_do_not_broadcast(self, client):
        """Nothing changed, so there is nothing to tell anyone about."""
        with client.websocket_connect("/ws") as watcher:
            drain(watcher)
            assert client.delete("/api/zones/does-not-exist").status_code == 404
            assert client.post("/api/zones/1/override/nonsense").status_code >= 400

            client.post("/api/zones/4/override/eco")
            assert next_update(watcher)["4"]["current_mode"] == "eco"
            client.post("/api/zones/4/override/normal")

    def test_unauthenticated_writes_do_not_broadcast(self, client):
        with client.websocket_connect("/ws") as watcher:
            drain(watcher)
            # No "with" here on purpose: entering a second TestClient context
            # would re-run the app lifespan and close the watcher socket.
            anon = TestClient(app)
            anon.cookies.clear()
            assert anon.post("/api/global/override/away").status_code == 401

            client.post("/api/zones/5/override/eco")
            assert next_update(watcher)["5"]["current_mode"] == "eco"
            client.post("/api/zones/5/override/normal")

    def test_clearing_the_log_does_not_broadcast(self, client):
        with client.websocket_connect("/ws") as watcher:
            drain(watcher)
            assert client.post("/api/log/clear").status_code == 200

            client.post("/api/zones/6/override/eco")
            assert next_update(watcher)["6"]["current_mode"] == "eco"
            client.post("/api/zones/6/override/normal")


class TestPolicyShape:
    def test_every_exclusion_is_a_deliberate_one(self):
        """Broadcasting is opt-out, and each opt-out needs a reason.

        A write that changes zone state and forgets to broadcast leaves every
        other open browser showing stale rooms, which is the bug this middleware
        exists to prevent. Pinning the set means a new exclusion cannot be added
        without someone reading this list and justifying it:

        * ``/api/log/clear`` — the log is diagnostics, not zone state.
        * ``/api/hub/config`` — ``apply_hub_config`` broadcasts itself, once the
          new zones have actually loaded. Broadcasting here as well would push
          the *old* hub's zones.
        * ``/api/site`` — renames the installation. No zone card shows it.
        """
        assert server.NO_BROADCAST_PATHS == frozenset(
            {"/api/log/clear", "/api/hub/config", "/api/site"}
        )

    def test_all_write_methods_are_covered(self):
        assert server.MUTATING_METHODS == frozenset({"POST", "PUT", "PATCH", "DELETE"})
