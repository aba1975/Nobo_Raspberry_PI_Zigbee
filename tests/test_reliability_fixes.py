"""
Three smaller reliability defects (D-05, D-11, D-12).

  D-05  Several handlers caught HTTPException in their generic `except
        Exception` and re-raised it as a 500, so "Zone not found" reached the
        browser as "Internal server error" and the UI could not react properly.
  D-11  An away period whose end had already passed was accepted, listed as an
        upcoming holiday and then never ran. Usually a mistyped year.
  D-12  /api/devices never returned the friendly device name, so a rename
        looked like it worked and then reverted on the next reload.
"""

import ast
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import away_schedule
import server
from server import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.cookies.set("session_id", "pytest-fixed-session-id")
        yield c


class TestHttpExceptionsSurvive:
    """D-05"""

    def test_no_handler_swallows_http_exceptions(self):
        """
        A `try` that raises HTTPException but only catches `Exception` turns
        every deliberate 400/404/503 into a 500.
        """
        tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
        offenders = []

        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for block in ast.walk(fn):
                if not isinstance(block, ast.Try):
                    continue

                caught = [
                    getattr(h.type, "id", None) if h.type else None
                    for h in block.handlers
                ]
                if "Exception" not in caught or "HTTPException" in caught:
                    continue

                raises_http = any(
                    (
                        isinstance(n, ast.Raise)
                        and isinstance(n.exc, ast.Call)
                        and getattr(n.exc.func, "id", None) == "HTTPException"
                    )
                    or (
                        isinstance(n, ast.Call)
                        and getattr(n.func, "id", "") == "require_capability"
                    )
                    for n in ast.walk(block)
                )
                if raises_http:
                    offenders.append(f"{fn.name} (line {block.lineno})")

        assert not offenders, (
            "these blocks re-raise a deliberate HTTP error as a 500, so the "
            f"browser sees 'Internal server error': {offenders}"
        )

    def test_unknown_zone_reports_404_not_500(self, client):
        r = client.post("/api/zones/does-not-exist/override/comfort")
        assert r.status_code == 404, "a missing zone was reported as a server fault"

    def test_invalid_override_mode_reports_400(self, client):
        r = client.post("/api/zones/1/override/banana")
        assert r.status_code == 400


class TestAwayScheduleInThePast:
    """D-11"""

    @staticmethod
    def iso(offset_hours):
        return (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).isoformat()

    def test_a_finished_period_is_rejected(self):
        ok, err = away_schedule.validate_schedule(True, self.iso(-48), self.iso(-24))
        assert ok is False
        assert "already in the past" in err

    def test_a_period_ending_right_now_is_rejected(self):
        now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
        ok, _ = away_schedule.validate_schedule(
            True, "2026-02-28T12:00:00+00:00", "2026-03-01T12:00:00+00:00", now=now
        )
        assert ok is False

    def test_a_period_still_running_is_accepted(self):
        """Starting a holiday that began yesterday is legitimate."""
        ok, err = away_schedule.validate_schedule(True, self.iso(-24), self.iso(+24))
        assert ok is True, err

    def test_a_future_period_is_accepted(self):
        ok, err = away_schedule.validate_schedule(True, self.iso(+24), self.iso(+48))
        assert ok is True, err

    def test_disabling_is_never_blocked_by_a_stale_date(self):
        """You must always be able to clear a schedule that has expired."""
        ok, _ = away_schedule.validate_schedule(False, self.iso(-48), self.iso(-24))
        assert ok is True

    def test_ordering_error_takes_priority(self):
        ok, err = away_schedule.validate_schedule(True, self.iso(-24), self.iso(-48))
        assert ok is False
        assert "strictly after" in err

    def test_endpoint_rejects_a_past_period(self, client):
        r = client.put(
            "/api/global-mode/away-schedule",
            json={"enabled": True, "start_at": self.iso(-48), "end_at": self.iso(-24)},
        )
        assert r.status_code == 400
        assert "past" in r.json()["detail"]


class TestDeviceNames:
    """D-12"""

    def test_devices_carry_their_friendly_name(self, client):
        devices = client.get("/api/devices").json()["devices"]
        assert devices

        for d in devices:
            assert "name" in d, "the device list still omits the friendly name"
            assert "display_name" in d

    def test_display_name_falls_back_to_the_model(self, client):
        for d in client.get("/api/devices").json()["devices"]:
            assert d["display_name"], "a device would render with a blank label"
            if not d["name"]:
                assert d["display_name"] == d["device_type"]

    def test_a_rename_is_visible_in_the_device_list(self, client):
        serial = client.get("/api/devices").json()["devices"][0]["serial"]

        assert client.patch(
            f"/api/devices/{serial}/name", json={"name": "Guest Room Panel"}
        ).status_code == 200

        after = next(
            d for d in client.get("/api/devices").json()["devices"] if d["serial"] == serial
        )
        assert after["name"] == "Guest Room Panel", "the rename did not survive a reload"
        assert after["display_name"] == "Guest Room Panel"
