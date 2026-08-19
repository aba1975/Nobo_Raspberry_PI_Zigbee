"""
Integration tests for Device API endpoints in demo mode.

Uses FastAPI's TestClient — no real Nobø Hub required.
"""

import os
import copy

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import server
from server import app, DEMO_ZONES, demo_schedules


@pytest.fixture(autouse=True)
def reset_demo_state():
    """Restore DEMO_ZONES and demo_schedules to their original state after each test."""
    original_zones = copy.deepcopy(DEMO_ZONES)
    original_schedules = dict(demo_schedules)
    yield
    DEMO_ZONES.clear()
    DEMO_ZONES.extend(original_zones)
    demo_schedules.clear()
    demo_schedules.update(original_schedules)


@pytest.fixture(scope="module")
def client():
    """TestClient with lifespan disabled so the background reconnect task isn't started."""
    with TestClient(app, raise_server_exceptions=True) as c:
        # The API is deny-by-default; conftest.authenticated_session keeps this
        # session id valid for every test (see conftest.TEST_SESSION_ID).
        c.cookies.set("session_id", "pytest-fixed-session-id")
        yield c


# ---------------------------------------------------------------------------
# GET /api/devices
# ---------------------------------------------------------------------------

class TestGetDevices:
    def test_returns_devices_list(self, client):
        r = client.get("/api/devices")
        assert r.status_code == 200
        body = r.json()
        assert "devices" in body
        assert isinstance(body["devices"], list)
        assert len(body["devices"]) > 0

    def test_device_has_required_fields(self, client):
        r = client.get("/api/devices")
        device = r.json()["devices"][0]
        for field in ("serial", "device_type", "zone_id", "zone_name"):
            assert field in device

    def test_demo_mode_returns_demo_devices(self, client):
        r = client.get("/api/devices")
        assert r.status_code == 200
        # Demo mode should always return data
        assert len(r.json()["devices"]) > 0


# ---------------------------------------------------------------------------
# POST /api/devices (add device)
# ---------------------------------------------------------------------------

class TestAddDevice:
    def test_add_device_with_valid_serial(self, client):
        r = client.post("/api/devices", json={
            "serial": "210000099999",
            "zone_id": "1",
            "name": "Test Device"
        })
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "success"
        assert body["serial"] == "210000099999"

    def test_add_device_with_valid_serial_with_spaces(self, client):
        r = client.post("/api/devices", json={
            "serial": "210 000 099 998",
            "zone_id": "1",
            "name": "Spaced Serial"
        })
        assert r.status_code == 200
        body = r.json()
        assert body["serial"] == "210000099998"

    def test_add_device_with_letters_returns_400(self, client):
        r = client.post("/api/devices", json={
            "serial": "ABCDEFGHIJKL",
            "zone_id": "1"
        })
        assert r.status_code == 400
        assert "12 digits" in r.json()["detail"]

    def test_add_device_with_mixed_alphanumeric_returns_400(self, client):
        r = client.post("/api/devices", json={
            "serial": "21000001624A",
            "zone_id": "1"
        })
        assert r.status_code == 400
        assert "12 digits" in r.json()["detail"]

    def test_add_device_with_too_short_serial_returns_400(self, client):
        r = client.post("/api/devices", json={
            "serial": "12345",
            "zone_id": "1"
        })
        assert r.status_code == 400
        assert "12 digits" in r.json()["detail"]

    def test_add_device_with_too_long_serial_returns_400(self, client):
        r = client.post("/api/devices", json={
            "serial": "2100000162471",
            "zone_id": "1"
        })
        assert r.status_code == 400
        assert "12 digits" in r.json()["detail"]

    def test_add_device_to_nonexistent_zone_returns_404(self, client):
        r = client.post("/api/devices", json={
            "serial": "210000099997",
            "zone_id": "9999"
        })
        assert r.status_code == 404

    def test_add_duplicate_device_returns_400(self, client):
        # First addition
        client.post("/api/devices", json={"serial": "210000088888", "zone_id": "1"})
        # Duplicate attempt
        r = client.post("/api/devices", json={"serial": "210000088888", "zone_id": "2"})
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# PUT /api/devices/{serial} (replace device)
# ---------------------------------------------------------------------------

class TestReplaceDevice:
    def test_replace_device_with_valid_serial(self, client):
        # First add a device to replace
        client.post("/api/devices", json={"serial": "210000077777", "zone_id": "1"})
        # Now replace it
        r = client.put("/api/devices/210000077777", json={"new_serial": "210000066666"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "success"
        assert body["new_serial"] == "210000066666"

    def test_replace_device_with_invalid_serial_returns_400(self, client):
        # Add a device to replace
        client.post("/api/devices", json={"serial": "210000055555", "zone_id": "1"})
        r = client.put("/api/devices/210000055555", json={"new_serial": "INVALID_SERIAL"})
        assert r.status_code == 400
        assert "12 digits" in r.json()["detail"]

    def test_replace_device_with_too_short_serial_returns_400(self, client):
        client.post("/api/devices", json={"serial": "210000044444", "zone_id": "1"})
        r = client.put("/api/devices/210000044444", json={"new_serial": "123"})
        assert r.status_code == 400
        assert "12 digits" in r.json()["detail"]

    def test_replace_nonexistent_device_returns_404(self, client):
        r = client.put("/api/devices/000000000000", json={"new_serial": "210000033333"})
        assert r.status_code == 404

    def test_replace_device_invalid_old_serial_returns_400(self, client):
        """Verify that an invalid old serial path parameter is rejected with 400."""
        r = client.put("/api/devices/NOT-A-SERIAL", json={
            "new_serial": "210000099004"
        })
        assert r.status_code == 400
        assert "12 digits" in r.json()["detail"]


# ---------------------------------------------------------------------------
# PATCH /api/devices/{serial}/name (rename device)
# ---------------------------------------------------------------------------

class TestRenameDevice:
    def test_rename_valid_device(self, client):
        # Use an existing demo device serial
        r = client.patch("/api/devices/210000016247/name", json={
            "name": "My Custom Name"
        })
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "success"
        assert body["name"] == "My Custom Name"

    def test_rename_invalid_serial_returns_400(self, client):
        """Verify that an invalid serial path parameter is rejected with 400."""
        r = client.patch("/api/devices/NOTADEVICE12/name", json={
            "name": "New Name"
        })
        assert r.status_code == 400
        assert "12 digits" in r.json()["detail"]

    def test_rename_nonexistent_device_returns_404(self, client):
        r = client.patch("/api/devices/210000099999/name", json={
            "name": "Ghost Device"
        })
        assert r.status_code == 404

    def test_rename_empty_name_returns_400(self, client):
        r = client.patch("/api/devices/210000016247/name", json={
            "name": "   "
        })
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Demo mode behaviour verification
# ---------------------------------------------------------------------------

class TestDemoModeBehaviour:
    def test_demo_mode_is_active(self, client):
        r = client.get("/api/status")
        assert r.status_code == 200
        assert r.json()["demo_mode"] is True

    def test_demo_mode_does_not_require_hub_connection(self, client):
        """All device endpoints should work without a real hub in demo mode."""
        r = client.get("/api/devices")
        assert r.status_code == 200

    def test_add_and_remove_device_in_demo(self, client):
        r_add = client.post("/api/devices", json={"serial": "210000022222", "zone_id": "2"})
        assert r_add.status_code == 200
        r_remove = client.delete("/api/devices/210000022222")
        assert r_remove.status_code == 200

    def test_device_appears_in_list_after_add(self, client):
        client.post("/api/devices", json={"serial": "210000011111", "zone_id": "3"})
        r = client.get("/api/devices")
        serials = [d["serial"] for d in r.json()["devices"]]
        assert "210000011111" in serials

    def test_device_removed_from_list_after_delete(self, client):
        client.post("/api/devices", json={"serial": "210000000001", "zone_id": "3"})
        client.delete("/api/devices/210000000001")
        r = client.get("/api/devices")
        serials = [d["serial"] for d in r.json()["devices"]]
        assert "210000000001" not in serials
