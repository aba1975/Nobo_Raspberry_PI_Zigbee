"""
Tests for the Away Schedule feature (away_schedule.py + server.py endpoints).

Isolates schedule file I/O using monkeypatching, similar to test_auth.py.
"""

import os
import sys
import copy
from datetime import datetime, timezone, timedelta

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import away_schedule as aws
import server
from server import app, DEMO_ZONES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_schedule(tmp_path, monkeypatch):
    """Redirect away_schedule storage to a temp directory for each test."""
    monkeypatch.setattr(aws, "DATA_DIR", tmp_path)
    monkeypatch.setattr(aws, "SCHEDULE_FILE", tmp_path / "away_schedule.json")
    # Reset global_mode_source
    monkeypatch.setattr(server, "global_mode_source", "manual")
    yield


@pytest.fixture(autouse=True)
def reset_demo_state():
    """Restore DEMO_ZONES to their original state after each test."""
    original_zones = copy.deepcopy(DEMO_ZONES)
    yield
    DEMO_ZONES.clear()
    DEMO_ZONES.extend(original_zones)


@pytest.fixture(scope="module")
def client():
    """TestClient — background tasks in lifespan are started but the schedule loop
    sleeps 30 s between checks so tests complete before any automatic transitions."""
    with TestClient(app, raise_server_exceptions=True) as c:
        # The API is deny-by-default; conftest.authenticated_session keeps this
        # session id valid for every test (see conftest.TEST_SESSION_ID).
        c.cookies.set("session_id", "pytest-fixed-session-id")
        yield c


# ---------------------------------------------------------------------------
# away_schedule module unit tests
# ---------------------------------------------------------------------------

class TestAwayScheduleModule:
    def test_load_schedule_returns_empty_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(aws, "SCHEDULE_FILE", tmp_path / "nonexistent.json")
        s = aws.load_schedule()
        assert s == {"enabled": False, "start_at": None, "end_at": None}

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(aws, "DATA_DIR", tmp_path)
        monkeypatch.setattr(aws, "SCHEDULE_FILE", tmp_path / "away_schedule.json")
        schedule = {"enabled": True, "start_at": "2026-04-22T10:00:00Z", "end_at": "2026-05-01T13:00:00Z"}
        aws.save_schedule(schedule)
        loaded = aws.load_schedule()
        assert loaded["enabled"] is True
        assert loaded["start_at"] == "2026-04-22T10:00:00Z"
        assert loaded["end_at"] == "2026-05-01T13:00:00Z"

    def test_clear_schedule(self, tmp_path, monkeypatch):
        monkeypatch.setattr(aws, "DATA_DIR", tmp_path)
        monkeypatch.setattr(aws, "SCHEDULE_FILE", tmp_path / "away_schedule.json")
        aws.save_schedule({"enabled": True, "start_at": "2026-04-22T10:00:00Z", "end_at": "2026-05-01T13:00:00Z"})
        result = aws.clear_schedule()
        assert result == {"enabled": False, "start_at": None, "end_at": None}
        loaded = aws.load_schedule()
        assert loaded["enabled"] is False

    def test_is_schedule_active_inside_window(self):
        schedule = {"enabled": True, "start_at": "2026-04-22T10:00:00Z", "end_at": "2026-05-01T13:00:00Z"}
        now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
        assert aws.is_schedule_active(schedule, now) is True

    def test_is_schedule_active_before_window(self):
        schedule = {"enabled": True, "start_at": "2026-04-22T10:00:00Z", "end_at": "2026-05-01T13:00:00Z"}
        now = datetime(2026, 4, 21, 9, 0, 0, tzinfo=timezone.utc)
        assert aws.is_schedule_active(schedule, now) is False

    def test_is_schedule_active_after_window(self):
        schedule = {"enabled": True, "start_at": "2026-04-22T10:00:00Z", "end_at": "2026-05-01T13:00:00Z"}
        now = datetime(2026, 5, 2, 0, 0, 0, tzinfo=timezone.utc)
        assert aws.is_schedule_active(schedule, now) is False

    def test_is_schedule_active_at_exact_start(self):
        schedule = {"enabled": True, "start_at": "2026-04-22T10:00:00Z", "end_at": "2026-05-01T13:00:00Z"}
        now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        assert aws.is_schedule_active(schedule, now) is True

    def test_is_schedule_active_at_exact_end(self):
        # end is exclusive
        schedule = {"enabled": True, "start_at": "2026-04-22T10:00:00Z", "end_at": "2026-05-01T13:00:00Z"}
        now = datetime(2026, 5, 1, 13, 0, 0, tzinfo=timezone.utc)
        assert aws.is_schedule_active(schedule, now) is False

    def test_is_schedule_active_disabled(self):
        schedule = {"enabled": False, "start_at": "2026-04-22T10:00:00Z", "end_at": "2026-05-01T13:00:00Z"}
        now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
        assert aws.is_schedule_active(schedule, now) is False

    def test_is_schedule_expired(self):
        schedule = {"enabled": True, "start_at": "2026-04-22T10:00:00Z", "end_at": "2026-05-01T13:00:00Z"}
        now = datetime(2026, 5, 2, 0, 0, 0, tzinfo=timezone.utc)
        assert aws.is_schedule_expired(schedule, now) is True

    def test_is_schedule_not_expired_inside(self):
        schedule = {"enabled": True, "start_at": "2026-04-22T10:00:00Z", "end_at": "2026-05-01T13:00:00Z"}
        now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
        assert aws.is_schedule_expired(schedule, now) is False

    def test_validate_schedule_valid(self):
        ok, err = aws.validate_schedule(True, "2026-04-22T10:00:00Z", "2026-05-01T13:00:00Z")
        assert ok is True
        assert err is None

    def test_validate_schedule_end_before_start(self):
        ok, err = aws.validate_schedule(True, "2026-05-01T13:00:00Z", "2026-04-22T10:00:00Z")
        assert ok is False
        assert "end_at" in err

    def test_validate_schedule_equal_times(self):
        ok, err = aws.validate_schedule(True, "2026-04-22T10:00:00Z", "2026-04-22T10:00:00Z")
        assert ok is False

    def test_validate_schedule_invalid_start(self):
        ok, err = aws.validate_schedule(True, "not-a-date", "2026-05-01T13:00:00Z")
        assert ok is False
        assert "start_at" in err

    def test_validate_schedule_invalid_end(self):
        ok, err = aws.validate_schedule(True, "2026-04-22T10:00:00Z", "not-a-date")
        assert ok is False
        assert "end_at" in err

    def test_validate_schedule_disabled_no_dates_required(self):
        ok, err = aws.validate_schedule(False, None, None)
        assert ok is True

    def test_validate_schedule_enabled_missing_dates(self):
        ok, err = aws.validate_schedule(True, None, None)
        assert ok is False


# ---------------------------------------------------------------------------
# GET /api/global-mode/away-schedule
# ---------------------------------------------------------------------------

class TestGetAwaySchedule:
    def test_returns_disabled_by_default(self, client):
        r = client.get("/api/global-mode/away-schedule")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False
        assert body["start_at"] is None
        assert body["end_at"] is None
        assert body["currently_active"] is False

    def test_returns_saved_schedule(self, client):
        aws.save_schedule({
            "enabled": True,
            "start_at": "2026-04-22T10:00:00Z",
            "end_at": "2026-05-01T13:00:00Z",
        })
        r = client.get("/api/global-mode/away-schedule")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["start_at"] == "2026-04-22T10:00:00Z"

    def test_returns_disabled_schedule_with_dates(self, client):
        """A disabled schedule that still has dates should return them."""
        aws.save_schedule({
            "enabled": False,
            "start_at": "2026-04-22T10:00:00Z",
            "end_at": "2026-05-01T13:00:00Z",
        })
        r = client.get("/api/global-mode/away-schedule")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False
        assert body["start_at"] == "2026-04-22T10:00:00Z"
        assert body["end_at"] == "2026-05-01T13:00:00Z"
        assert body["currently_active"] is False


# ---------------------------------------------------------------------------
# PUT /api/global-mode/away-schedule
# ---------------------------------------------------------------------------

class TestPutAwaySchedule:
    def test_valid_schedule_saved(self, client):
        r = client.put("/api/global-mode/away-schedule", json={
            "enabled": True,
            "start_at": "2026-04-22T10:00:00Z",
            "end_at": "2026-05-01T13:00:00Z",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["start_at"] == "2026-04-22T10:00:00Z"
        assert body["end_at"] == "2026-05-01T13:00:00Z"

    def test_end_before_start_returns_400(self, client):
        r = client.put("/api/global-mode/away-schedule", json={
            "enabled": True,
            "start_at": "2026-05-01T13:00:00Z",
            "end_at": "2026-04-22T10:00:00Z",
        })
        assert r.status_code == 400

    def test_invalid_start_datetime_returns_400(self, client):
        r = client.put("/api/global-mode/away-schedule", json={
            "enabled": True,
            "start_at": "not-a-date",
            "end_at": "2026-05-01T13:00:00Z",
        })
        assert r.status_code == 400

    def test_invalid_end_datetime_returns_400(self, client):
        r = client.put("/api/global-mode/away-schedule", json={
            "enabled": True,
            "start_at": "2026-04-22T10:00:00Z",
            "end_at": "not-a-date",
        })
        assert r.status_code == 400

    def test_schedule_inside_window_sets_away(self, client):
        """If we save a schedule and we're currently inside the window, Away should be set."""
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=1)).isoformat()
        end = (now + timedelta(hours=1)).isoformat()
        r = client.put("/api/global-mode/away-schedule", json={
            "enabled": True,
            "start_at": start,
            "end_at": end,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["currently_active"] is True
        # All demo zones should be set to 'away'
        assert all(z["mode"] == "away" for z in DEMO_ZONES)

    def test_schedule_outside_window_does_not_change_mode(self, client):
        """If we save a schedule for the future, mode should remain unchanged."""
        now = datetime.now(timezone.utc)
        start = (now + timedelta(days=1)).isoformat()
        end = (now + timedelta(days=2)).isoformat()
        original_modes = [z["mode"] for z in DEMO_ZONES]
        r = client.put("/api/global-mode/away-schedule", json={
            "enabled": True,
            "start_at": start,
            "end_at": end,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["currently_active"] is False
        # Modes should be unchanged
        assert [z["mode"] for z in DEMO_ZONES] == original_modes

    def test_disable_schedule(self, client):
        """Disabling a schedule with enabled=False should not require dates."""
        r = client.put("/api/global-mode/away-schedule", json={
            "enabled": False,
            "start_at": None,
            "end_at": None,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False


# ---------------------------------------------------------------------------
# DELETE /api/global-mode/away-schedule
# ---------------------------------------------------------------------------

class TestDeleteAwaySchedule:
    def test_clear_inactive_schedule(self, client):
        aws.save_schedule({
            "enabled": True,
            "start_at": "2026-04-22T10:00:00Z",
            "end_at": "2026-05-01T13:00:00Z",
        })
        r = client.delete("/api/global-mode/away-schedule")
        assert r.status_code == 200
        assert r.json() == {"status": "cleared"}
        loaded = aws.load_schedule()
        assert loaded["enabled"] is False

    def test_clear_active_schedule_returns_home(self, client):
        """Clearing an active schedule (inside window) should switch back to Home."""
        now = datetime.now(timezone.utc)
        aws.save_schedule({
            "enabled": True,
            "start_at": (now - timedelta(hours=1)).isoformat(),
            "end_at": (now + timedelta(hours=1)).isoformat(),
        })
        # First set all zones to away to simulate active schedule
        for z in DEMO_ZONES:
            z["mode"] = "away"

        r = client.delete("/api/global-mode/away-schedule")
        assert r.status_code == 200
        # All zones should now be 'normal' (home)
        assert all(z["mode"] == "normal" for z in DEMO_ZONES)

    def test_clear_no_schedule(self, client):
        """Delete with no schedule saved should succeed."""
        r = client.delete("/api/global-mode/away-schedule")
        assert r.status_code == 200
        assert r.json() == {"status": "cleared"}


# ---------------------------------------------------------------------------
# GET /api/status includes away_schedule
# ---------------------------------------------------------------------------

class TestStatusIncludesSchedule:
    def test_status_includes_away_schedule(self, client):
        r = client.get("/api/status")
        assert r.status_code == 200
        body = r.json()
        assert "away_schedule" in body
        away = body["away_schedule"]
        assert "enabled" in away
        assert "start_at" in away
        assert "end_at" in away
        assert "currently_active" in away

    def test_status_includes_global_mode_source(self, client):
        r = client.get("/api/status")
        assert r.status_code == 200
        body = r.json()
        assert "global_mode_source" in body
        assert body["global_mode_source"] in ("manual", "schedule")


# ---------------------------------------------------------------------------
# POST /api/global/override/{mode} — existing behavior unchanged
# ---------------------------------------------------------------------------

class TestGlobalOverrideSourceField:
    def test_global_override_includes_source_manual(self, client):
        r = client.post("/api/global/override/away")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "manual"

    def test_manual_away_without_schedule_unchanged(self, client):
        """Manual Away without a schedule should behave exactly as before."""
        r = client.post("/api/global/override/away")
        assert r.status_code == 200
        assert all(z["mode"] == "away" for z in DEMO_ZONES)

    def test_manual_home_without_schedule_unchanged(self, client):
        r = client.post("/api/global/override/home")
        assert r.status_code == 200
        assert all(z["mode"] == "normal" for z in DEMO_ZONES)


# ---------------------------------------------------------------------------
# GET /api/global-mode/away-schedule — disabled schedule with dates
# ---------------------------------------------------------------------------

class TestGetDisabledSchedule:
    def test_returns_disabled_schedule_with_dates(self, client):
        """A disabled schedule that still has dates should return them."""
        aws.save_schedule({
            "enabled": False,
            "start_at": "2026-04-22T10:00:00Z",
            "end_at": "2026-05-01T13:00:00Z",
        })
        r = client.get("/api/global-mode/away-schedule")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False
        assert body["start_at"] == "2026-04-22T10:00:00Z"
        assert body["end_at"] == "2026-05-01T13:00:00Z"
        assert body["currently_active"] is False
