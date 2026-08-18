"""
tests/test_persistence.py — Tests for config_persistence.py

Verifies that demo zones, demo schedules, and server state are correctly
saved to disk and reloaded after a simulated server restart.

All file I/O is redirected to a per-test ``tmp_path`` directory by the
``redirect_persistence`` autouse fixture defined in conftest.py.
"""

import copy
import json
import os

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import config_persistence
import server
from server import app, DEMO_ZONES, demo_schedules


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sample_zones():
    """Return a minimal but valid zones list for persistence tests."""
    return [
        {
            "zone_id": "1",
            "name": "Test Zone",
            "icon": "🏠",
            "rooms": ["Room A"],
            "components": ["210000016247"],
            "component_names": ["Test Heater"],
            "current_temp": 22.0,
            "comfort_temp": 22.0,
            "eco_temp": 18.0,
            "mode": "comfort",
            "override_id": None,
        }
    ]


def _sample_schedules():
    return {
        "1": {
            "monday": [{"start": "00:00", "end": "24:00", "mode": "comfort"}],
            "tuesday": [{"start": "00:00", "end": "24:00", "mode": "eco"}],
            "wednesday": [{"start": "00:00", "end": "24:00", "mode": "comfort"}],
            "thursday": [{"start": "00:00", "end": "24:00", "mode": "eco"}],
            "friday": [{"start": "00:00", "end": "24:00", "mode": "comfort"}],
            "saturday": [{"start": "00:00", "end": "24:00", "mode": "eco"}],
            "sunday": [{"start": "00:00", "end": "24:00", "mode": "comfort"}],
        }
    }


# ---------------------------------------------------------------------------
# Demo zones persistence
# ---------------------------------------------------------------------------

class TestDemoZonesPersistence:
    def test_save_and_reload(self, tmp_path, monkeypatch):
        """Saved zones survive a reload (simulated restart)."""
        monkeypatch.setattr(config_persistence, "DEMO_ZONES_FILE", tmp_path / "demo_zones.json")
        zones = _sample_zones()
        config_persistence.save_demo_zones(zones)

        loaded = config_persistence.load_demo_zones()
        assert loaded is not None
        assert len(loaded) == 1
        assert loaded[0]["name"] == "Test Zone"
        assert loaded[0]["mode"] == "comfort"

    def test_load_returns_none_when_missing(self, tmp_path, monkeypatch):
        """Missing file returns None so caller falls back to defaults."""
        monkeypatch.setattr(config_persistence, "DEMO_ZONES_FILE", tmp_path / "nonexistent.json")
        assert config_persistence.load_demo_zones() is None

    def test_load_returns_none_on_corrupt_file(self, tmp_path, monkeypatch):
        """Corrupt JSON is backed up and None is returned."""
        bad_file = tmp_path / "demo_zones.json"
        bad_file.write_text("this is not json", encoding="utf-8")
        monkeypatch.setattr(config_persistence, "DEMO_ZONES_FILE", bad_file)

        result = config_persistence.load_demo_zones()
        assert result is None
        # Original file should have been renamed to .backup
        assert (tmp_path / "demo_zones.backup").exists()
        assert not bad_file.exists()

    def test_load_returns_none_when_content_is_not_list(self, tmp_path, monkeypatch):
        """File containing a dict (not a list) returns None and does not crash."""
        bad_file = tmp_path / "demo_zones.json"
        bad_file.write_text(json.dumps({"zone": "data"}), encoding="utf-8")
        monkeypatch.setattr(config_persistence, "DEMO_ZONES_FILE", bad_file)

        result = config_persistence.load_demo_zones()
        assert result is None

    def test_saved_file_is_valid_json(self, tmp_path, monkeypatch):
        """After save, the file must be parseable as valid JSON."""
        dest = tmp_path / "demo_zones.json"
        monkeypatch.setattr(config_persistence, "DEMO_ZONES_FILE", dest)
        monkeypatch.setattr(config_persistence, "DATA_DIR", tmp_path)

        config_persistence.save_demo_zones(_sample_zones())
        with dest.open() as fh:
            data = json.load(fh)
        assert isinstance(data, list)

    def test_modified_zone_persists(self, tmp_path, monkeypatch):
        """Changing a zone field and saving causes the new value to be loaded."""
        dest = tmp_path / "demo_zones.json"
        monkeypatch.setattr(config_persistence, "DEMO_ZONES_FILE", dest)
        monkeypatch.setattr(config_persistence, "DATA_DIR", tmp_path)

        zones = _sample_zones()
        zones[0]["mode"] = "away"
        zones[0]["comfort_temp"] = 25.5
        config_persistence.save_demo_zones(zones)

        loaded = config_persistence.load_demo_zones()
        assert loaded[0]["mode"] == "away"
        assert loaded[0]["comfort_temp"] == 25.5


# ---------------------------------------------------------------------------
# Demo schedules persistence
# ---------------------------------------------------------------------------

class TestDemoSchedulesPersistence:
    def test_save_and_reload(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_persistence, "DEMO_SCHEDULES_FILE", tmp_path / "demo_schedules.json")
        schedules = _sample_schedules()
        config_persistence.save_demo_schedules(schedules)

        loaded = config_persistence.load_demo_schedules()
        assert "1" in loaded
        assert loaded["1"]["monday"][0]["mode"] == "comfort"

    def test_load_returns_empty_dict_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_persistence, "DEMO_SCHEDULES_FILE", tmp_path / "nonexistent.json")
        assert config_persistence.load_demo_schedules() == {}

    def test_load_returns_empty_dict_on_corrupt_file(self, tmp_path, monkeypatch):
        bad_file = tmp_path / "demo_schedules.json"
        bad_file.write_text("{invalid json}", encoding="utf-8")
        monkeypatch.setattr(config_persistence, "DEMO_SCHEDULES_FILE", bad_file)

        result = config_persistence.load_demo_schedules()
        assert result == {}
        assert (tmp_path / "demo_schedules.backup").exists()

    def test_saved_file_is_valid_json(self, tmp_path, monkeypatch):
        dest = tmp_path / "demo_schedules.json"
        monkeypatch.setattr(config_persistence, "DEMO_SCHEDULES_FILE", dest)
        monkeypatch.setattr(config_persistence, "DATA_DIR", tmp_path)

        config_persistence.save_demo_schedules(_sample_schedules())
        with dest.open() as fh:
            data = json.load(fh)
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# Server state persistence
# ---------------------------------------------------------------------------

class TestServerStatePersistence:
    def test_save_and_reload(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_persistence, "SERVER_STATE_FILE", tmp_path / "server_state.json")
        config_persistence.save_server_state({"global_mode_source": "schedule"})

        loaded = config_persistence.load_server_state()
        assert loaded["global_mode_source"] == "schedule"

    def test_load_returns_defaults_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_persistence, "SERVER_STATE_FILE", tmp_path / "nonexistent.json")
        loaded = config_persistence.load_server_state()
        assert loaded["global_mode_source"] == "manual"

    def test_load_returns_defaults_on_corrupt_file(self, tmp_path, monkeypatch):
        bad_file = tmp_path / "server_state.json"
        bad_file.write_text("INVALID", encoding="utf-8")
        monkeypatch.setattr(config_persistence, "SERVER_STATE_FILE", bad_file)

        result = config_persistence.load_server_state()
        assert result["global_mode_source"] == "manual"
        assert (tmp_path / "server_state.backup").exists()

    def test_missing_keys_filled_with_defaults(self, tmp_path, monkeypatch):
        """A state file that lacks some keys still returns complete defaults."""
        state_file = tmp_path / "server_state.json"
        state_file.write_text(json.dumps({}), encoding="utf-8")
        monkeypatch.setattr(config_persistence, "SERVER_STATE_FILE", state_file)

        loaded = config_persistence.load_server_state()
        assert "global_mode_source" in loaded
        assert loaded["global_mode_source"] == "manual"

    def test_saved_file_is_valid_json(self, tmp_path, monkeypatch):
        dest = tmp_path / "server_state.json"
        monkeypatch.setattr(config_persistence, "SERVER_STATE_FILE", dest)
        monkeypatch.setattr(config_persistence, "DATA_DIR", tmp_path)

        config_persistence.save_server_state({"global_mode_source": "manual"})
        with dest.open() as fh:
            data = json.load(fh)
        assert isinstance(data, dict)

    def test_global_mode_source_persists(self, tmp_path, monkeypatch):
        dest = tmp_path / "server_state.json"
        monkeypatch.setattr(config_persistence, "SERVER_STATE_FILE", dest)
        monkeypatch.setattr(config_persistence, "DATA_DIR", tmp_path)

        for value in ("manual", "schedule"):
            config_persistence.save_server_state({"global_mode_source": value})
            loaded = config_persistence.load_server_state()
            assert loaded["global_mode_source"] == value


# ---------------------------------------------------------------------------
# Atomic write safety
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_no_tmp_file_left_after_successful_save(self, tmp_path, monkeypatch):
        """After a successful save the .tmp file must not remain on disk."""
        dest = tmp_path / "demo_zones.json"
        monkeypatch.setattr(config_persistence, "DEMO_ZONES_FILE", dest)
        monkeypatch.setattr(config_persistence, "DATA_DIR", tmp_path)

        config_persistence.save_demo_zones(_sample_zones())
        assert not (tmp_path / "demo_zones.tmp").exists()
        assert dest.exists()

    def test_data_dir_created_if_missing(self, tmp_path, monkeypatch):
        """The data directory is created automatically when saving."""
        new_dir = tmp_path / "subdir" / "data"
        dest = new_dir / "demo_zones.json"
        monkeypatch.setattr(config_persistence, "DATA_DIR", new_dir)
        monkeypatch.setattr(config_persistence, "DEMO_ZONES_FILE", dest)

        config_persistence.save_demo_zones(_sample_zones())
        assert dest.exists()


# ---------------------------------------------------------------------------
# Integration tests: API endpoints trigger persistence
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient


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
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


class TestAPITriggersPersistence:
    def test_zone_override_saves_demo_zones(self, client, tmp_path, monkeypatch):
        """Setting a zone override writes demo_zones.json."""
        dest = tmp_path / "demo_zones.json"
        monkeypatch.setattr(config_persistence, "DEMO_ZONES_FILE", dest)
        monkeypatch.setattr(config_persistence, "DATA_DIR", tmp_path)

        r = client.post("/api/zones/1/override/away")
        assert r.status_code == 200
        assert dest.exists()
        data = json.loads(dest.read_text())
        zone = next(z for z in data if z["zone_id"] == "1")
        assert zone["mode"] == "away"

    def test_global_override_saves_server_state(self, client, tmp_path, monkeypatch):
        """Setting a global override writes server_state.json."""
        dest = tmp_path / "server_state.json"
        monkeypatch.setattr(config_persistence, "SERVER_STATE_FILE", dest)
        monkeypatch.setattr(config_persistence, "DATA_DIR", tmp_path)

        r = client.post("/api/global/override/away")
        assert r.status_code == 200
        assert dest.exists()
        state = json.loads(dest.read_text())
        assert state["global_mode_source"] == "manual"

    def test_zone_schedule_saves_demo_schedules(self, client, tmp_path, monkeypatch):
        """Saving a zone schedule writes demo_schedules.json."""
        dest = tmp_path / "demo_schedules.json"
        monkeypatch.setattr(config_persistence, "DEMO_SCHEDULES_FILE", dest)
        monkeypatch.setattr(config_persistence, "DATA_DIR", tmp_path)

        schedule_payload = {
            "schedule": {
                day: [{"start": "00:00", "end": "24:00", "mode": "comfort"}]
                for day in [
                    "monday", "tuesday", "wednesday", "thursday",
                    "friday", "saturday", "sunday",
                ]
            }
        }
        r = client.post("/api/zones/1/schedule", json=schedule_payload)
        assert r.status_code == 200
        assert dest.exists()
        saved = json.loads(dest.read_text())
        assert "1" in saved
        assert saved["1"]["monday"][0]["mode"] == "comfort"

    def test_add_zone_saves_demo_zones(self, client, tmp_path, monkeypatch):
        """Adding a zone writes demo_zones.json."""
        dest = tmp_path / "demo_zones.json"
        monkeypatch.setattr(config_persistence, "DEMO_ZONES_FILE", dest)
        monkeypatch.setattr(config_persistence, "DATA_DIR", tmp_path)

        r = client.post("/api/zones", json={"name": "New Zone", "icon": "🏡"})
        assert r.status_code == 200
        assert dest.exists()
        data = json.loads(dest.read_text())
        assert any(z["name"] == "New Zone" for z in data)

    def test_delete_zone_saves_demo_zones(self, client, tmp_path, monkeypatch):
        """Deleting a zone updates demo_zones.json."""
        dest = tmp_path / "demo_zones.json"
        monkeypatch.setattr(config_persistence, "DEMO_ZONES_FILE", dest)
        monkeypatch.setattr(config_persistence, "DATA_DIR", tmp_path)

        r = client.delete("/api/zones/1")
        assert r.status_code == 200
        assert dest.exists()
        data = json.loads(dest.read_text())
        assert not any(z["zone_id"] == "1" for z in data)

    def test_temperature_update_saves_demo_zones(self, client, tmp_path, monkeypatch):
        """Updating temperatures writes demo_zones.json."""
        dest = tmp_path / "demo_zones.json"
        monkeypatch.setattr(config_persistence, "DEMO_ZONES_FILE", dest)
        monkeypatch.setattr(config_persistence, "DATA_DIR", tmp_path)

        # Zone 1 has NTB-2R which supports temperature adjustment
        r = client.post("/api/zones/1/temperature", json={"comfort": 25.0, "eco": 19.0})
        assert r.status_code == 200
        assert dest.exists()
        data = json.loads(dest.read_text())
        zone = next(z for z in data if z["zone_id"] == "1")
        assert zone["comfort_temp"] == 25.0
        assert zone["eco_temp"] == 19.0

    def test_simulated_restart_loads_persisted_zones(self, tmp_path, monkeypatch):
        """
        End-to-end restart simulation:
        1. Save modified zones to disk.
        2. Call load_demo_zones() (simulating a fresh process start).
        3. Assert the modifications are present.
        """
        dest = tmp_path / "demo_zones.json"
        monkeypatch.setattr(config_persistence, "DEMO_ZONES_FILE", dest)
        monkeypatch.setattr(config_persistence, "DATA_DIR", tmp_path)

        # Simulate a user change
        modified_zones = copy.deepcopy(DEMO_ZONES)
        modified_zones[0]["mode"] = "eco"
        modified_zones[0]["name"] = "Renamed Zone"
        config_persistence.save_demo_zones(modified_zones)

        # Simulate restart: load from disk
        loaded = config_persistence.load_demo_zones()
        assert loaded is not None
        assert loaded[0]["mode"] == "eco"
        assert loaded[0]["name"] == "Renamed Zone"

    def test_simulated_restart_loads_persisted_server_state(self, tmp_path, monkeypatch):
        """
        End-to-end restart simulation for server_state:
        1. Save state to disk.
        2. Load via load_server_state() (simulating a fresh process start).
        3. Assert the value persisted.
        """
        dest = tmp_path / "server_state.json"
        monkeypatch.setattr(config_persistence, "SERVER_STATE_FILE", dest)
        monkeypatch.setattr(config_persistence, "DATA_DIR", tmp_path)

        config_persistence.save_server_state({"global_mode_source": "schedule"})
        loaded = config_persistence.load_server_state()
        assert loaded["global_mode_source"] == "schedule"
