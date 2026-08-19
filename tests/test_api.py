"""
Integration tests for all API endpoints in demo mode.

Uses FastAPI's TestClient (synchronous httpx wrapper) — no real hub required.
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
    """TestClient with lifespan disabled so the background reconnect task isn't started during tests."""
    with TestClient(app, raise_server_exceptions=True) as c:
        # The API is deny-by-default; conftest.authenticated_session keeps this
        # session id valid for every test (see conftest.TEST_SESSION_ID).
        c.cookies.set("session_id", "pytest-fixed-session-id")
        yield c


# ---------------------------------------------------------------------------
# Health / Status
# ---------------------------------------------------------------------------

class TestHealthAndStatus:
    def test_health_returns_ok(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["demo_mode"] is True
        assert body["connected"] is True

    def test_status_endpoint(self, client):
        r = client.get("/api/status")
        assert r.status_code == 200
        body = r.json()
        assert body["demo_mode"] is True
        assert body["connected"] is True


# ---------------------------------------------------------------------------
# Hub info
# ---------------------------------------------------------------------------

class TestHubInfo:
    def test_hub_info_in_demo_mode(self, client):
        r = client.get("/api/hub")
        assert r.status_code == 200
        body = r.json()
        assert body["demo_mode"] is True
        assert "serial" in body


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------

class TestGetZones:
    def test_returns_zones_list(self, client):
        r = client.get("/api/zones")
        assert r.status_code == 200
        body = r.json()
        assert "zones" in body
        assert len(body["zones"]) > 0

    def test_zone_has_required_fields(self, client):
        r = client.get("/api/zones")
        zone = r.json()["zones"][0]
        for field in ("zone_id", "name", "current_mode", "comfort_temperature", "eco_temperature"):
            assert field in zone, f"Missing field: {field}"

    def test_new_zone_has_component_names(self, client):
        """POST /api/zones must include component_names in the created zone."""
        r = client.post("/api/zones", json={"name": "Test Zone", "icon": "🏠"})
        assert r.status_code == 200
        new_id = r.json()["zone_id"]
        # Check DEMO_ZONES directly
        new_zone = next(z for z in DEMO_ZONES if z["zone_id"] == new_id)
        assert "component_names" in new_zone
        assert isinstance(new_zone["component_names"], list)


class TestZoneCRUD:
    def test_add_zone(self, client):
        count_before = len(DEMO_ZONES)
        r = client.post("/api/zones", json={"name": "New Zone"})
        assert r.status_code == 200
        assert len(DEMO_ZONES) == count_before + 1

    def test_rename_zone(self, client):
        zone_id = DEMO_ZONES[0]["zone_id"]
        r = client.put(f"/api/zones/{zone_id}", json={"name": "Renamed"})
        assert r.status_code == 200
        assert DEMO_ZONES[0]["name"] == "Renamed"

    def test_rename_nonexistent_zone_returns_404(self, client):
        r = client.put("/api/zones/9999", json={"name": "Ghost"})
        assert r.status_code == 404

    def test_delete_zone(self, client):
        # Add a zone first so we don't destroy fixture data
        client.post("/api/zones", json={"name": "Temp Zone"})
        new_zone = DEMO_ZONES[-1]
        zone_id = new_zone["zone_id"]
        r = client.delete(f"/api/zones/{zone_id}")
        assert r.status_code == 200
        assert not any(z["zone_id"] == zone_id for z in DEMO_ZONES)

    def test_delete_nonexistent_zone_returns_404(self, client):
        r = client.delete("/api/zones/9999")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Zone overrides
# ---------------------------------------------------------------------------

class TestZoneOverride:
    def test_set_comfort_override(self, client):
        zone_id = DEMO_ZONES[0]["zone_id"]
        r = client.post(f"/api/zones/{zone_id}/override/comfort")
        assert r.status_code == 200
        assert DEMO_ZONES[0]["mode"] == "comfort"

    def test_set_eco_override(self, client):
        zone_id = DEMO_ZONES[0]["zone_id"]
        r = client.post(f"/api/zones/{zone_id}/override/eco")
        assert r.status_code == 200
        assert DEMO_ZONES[0]["mode"] == "eco"

    def test_set_away_override(self, client):
        zone_id = DEMO_ZONES[0]["zone_id"]
        r = client.post(f"/api/zones/{zone_id}/override/away")
        assert r.status_code == 200
        assert DEMO_ZONES[0]["mode"] == "away"

    def test_cancel_override_sets_normal(self, client):
        zone_id = DEMO_ZONES[0]["zone_id"]
        client.post(f"/api/zones/{zone_id}/override/comfort")
        r = client.post(f"/api/zones/{zone_id}/override/normal")
        assert r.status_code == 200
        assert DEMO_ZONES[0]["mode"] == "normal"

    def test_invalid_mode_returns_400(self, client):
        zone_id = DEMO_ZONES[0]["zone_id"]
        r = client.post(f"/api/zones/{zone_id}/override/off")
        assert r.status_code == 400

    def test_off_mode_rejected(self, client):
        """'off' is not a valid Nobø mode and must be rejected."""
        zone_id = DEMO_ZONES[0]["zone_id"]
        r = client.post(f"/api/zones/{zone_id}/override/off")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Global override
# ---------------------------------------------------------------------------

class TestGlobalOverride:
    def test_global_comfort(self, client):
        r = client.post("/api/global/override/comfort")
        assert r.status_code == 200
        assert all(z["mode"] == "comfort" for z in DEMO_ZONES)

    def test_global_home_sets_normal(self, client):
        r = client.post("/api/global/override/home")
        assert r.status_code == 200
        assert all(z["mode"] == "normal" for z in DEMO_ZONES)

    def test_global_invalid_mode_returns_400(self, client):
        r = client.post("/api/global/override/invalid")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Temperature
# ---------------------------------------------------------------------------

class TestTemperature:
    def test_set_comfort_temperature(self, client):
        # Zone 1 (Large Bathroom, NTB-2R) supports temperature
        zone_id = "1"
        r = client.post(f"/api/zones/{zone_id}/temperature", json={"comfort": 22.0})
        assert r.status_code == 200
        zone = next(z for z in DEMO_ZONES if z["zone_id"] == zone_id)
        assert zone["comfort_temp"] == 22.0

    def test_set_eco_temperature(self, client):
        zone_id = "1"
        r = client.post(f"/api/zones/{zone_id}/temperature", json={"eco": 18.0})
        assert r.status_code == 200

    def test_temperature_out_of_range_returns_400(self, client):
        zone_id = "1"
        r = client.post(f"/api/zones/{zone_id}/temperature", json={"comfort": 35.0})
        assert r.status_code == 400

    def test_no_temperature_device_returns_400(self, client):
        # Zone 4 (Upstairs Bedrooms, R80 RDC 700) — R80 without temp adjust
        # Find a zone with has_manual_devices only by checking detect_device_type
        zone_id = "4"
        zone = next(z for z in DEMO_ZONES if z["zone_id"] == zone_id)
        all_manual = all(
            not server.detect_device_type(c)[1] and not server.detect_device_type(c)[2]
            for c in zone["components"]
        )
        if all_manual:
            r = client.post(f"/api/zones/{zone_id}/temperature", json={"comfort": 21.0})
            assert r.status_code == 400


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

VALID_SCHEDULE_PAYLOAD = {
    "schedule": {
        day: [
            {"start": "00:00", "end": "07:00", "mode": "eco"},
            {"start": "07:00", "end": "22:00", "mode": "comfort"},
            {"start": "22:00", "end": "24:00", "mode": "eco"},
        ]
        for day in server.SCHEDULE_DAYS
    }
}


class TestSchedule:
    def test_get_schedule_default(self, client):
        zone_id = "1"
        r = client.get(f"/api/zones/{zone_id}/schedule")
        assert r.status_code == 200
        body = r.json()
        assert "schedule" in body
        assert "monday" in body["schedule"]

    def test_update_schedule_valid(self, client):
        zone_id = "1"
        r = client.post(f"/api/zones/{zone_id}/schedule", json=VALID_SCHEDULE_PAYLOAD)
        assert r.status_code == 200
        assert demo_schedules.get(zone_id) is not None

    def test_update_schedule_missing_day_returns_400(self, client):
        bad = {
            "schedule": {
                k: v for k, v in VALID_SCHEDULE_PAYLOAD["schedule"].items()
                if k != "friday"
            }
        }
        r = client.post("/api/zones/1/schedule", json=bad)
        assert r.status_code == 400

    def test_update_schedule_invalid_mode_returns_400(self, client):
        bad = copy.deepcopy(VALID_SCHEDULE_PAYLOAD)
        bad["schedule"]["monday"][0]["mode"] = "sauna"
        r = client.post("/api/zones/1/schedule", json=bad)
        assert r.status_code == 400

    def test_update_schedule_accepts_off(self, client):
        """A schedule may turn heating off; the hub has a state for it."""
        payload = copy.deepcopy(VALID_SCHEDULE_PAYLOAD)
        payload["schedule"]["monday"][0]["mode"] = "off"
        r = client.post("/api/zones/1/schedule", json=payload)
        assert r.status_code == 200

    def test_update_schedule_gap_returns_400(self, client):
        bad = copy.deepcopy(VALID_SCHEDULE_PAYLOAD)
        bad["schedule"]["monday"] = [
            {"start": "00:00", "end": "06:00", "mode": "eco"},
            # gap from 06:00 to 07:00
            {"start": "07:00", "end": "22:00", "mode": "comfort"},
            {"start": "22:00", "end": "24:00", "mode": "eco"},
        ]
        r = client.post("/api/zones/1/schedule", json=bad)
        assert r.status_code == 400

    def test_schedule_for_nonexistent_zone_returns_404(self, client):
        r = client.get("/api/zones/9999/schedule")
        assert r.status_code == 404

    def test_update_schedule_nonexistent_zone_returns_404(self, client):
        r = client.post("/api/zones/9999/schedule", json=VALID_SCHEDULE_PAYLOAD)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Week profiles
# ---------------------------------------------------------------------------

class TestWeekProfiles:
    def test_returns_profiles_in_demo_mode(self, client):
        """GET /api/week_profiles must not return 503 in demo mode."""
        r = client.get("/api/week_profiles")
        assert r.status_code == 200
        body = r.json()
        assert "week_profiles" in body
        assert len(body["week_profiles"]) > 0


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

class TestDevices:
    def test_get_devices(self, client):
        r = client.get("/api/devices")
        assert r.status_code == 200
        body = r.json()
        assert "devices" in body

    def test_add_device_to_zone(self, client):
        zone_id = DEMO_ZONES[0]["zone_id"]
        r = client.post("/api/devices", json={"serial": "210000099999", "zone_id": zone_id})
        assert r.status_code == 200
        zone = next(z for z in DEMO_ZONES if z["zone_id"] == zone_id)
        assert "210000099999" in zone["components"]

    def test_add_duplicate_device_returns_400(self, client):
        zone_id = DEMO_ZONES[0]["zone_id"]
        serial = DEMO_ZONES[0]["components"][0]
        r = client.post("/api/devices", json={"serial": serial, "zone_id": zone_id})
        assert r.status_code == 400

    def test_remove_device(self, client):
        # Add a device first
        zone_id = DEMO_ZONES[0]["zone_id"]
        client.post("/api/devices", json={"serial": "210000088888", "zone_id": zone_id})
        r = client.delete("/api/devices/210000088888")
        assert r.status_code == 200
        zone = next(z for z in DEMO_ZONES if z["zone_id"] == zone_id)
        assert "210000088888" not in zone["components"]

    def test_remove_nonexistent_device_returns_404(self, client):
        r = client.delete("/api/devices/999999999999")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Command log
# ---------------------------------------------------------------------------

class TestCommandLog:
    def test_get_log(self, client):
        r = client.get("/api/log")
        assert r.status_code == 200
        body = r.json()
        assert "entries" in body
        assert "demo_mode" in body

    def test_clear_log(self, client):
        client.post("/api/zones/1/override/comfort")  # Generate a log entry
        r = client.post("/api/log/clear")
        assert r.status_code == 200
        r2 = client.get("/api/log")
        assert r2.json()["total"] == 0
