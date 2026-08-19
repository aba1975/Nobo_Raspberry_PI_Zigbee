"""
Timezone handling (QA defect D-06).

The container runs in UTC unless it is told otherwise, but a week schedule of
"comfort from 07:00" means 07:00 on the kitchen clock. On a Pi set to CEST the
schedule was being evaluated two hours out, so heating came on late every
morning. The away schedule was unaffected because the browser converts to UTC
before sending, but that split made the two features disagree about what "now"
means.
"""

import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import away_schedule
import server
from server import app

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.cookies.set("session_id", "pytest-fixed-session-id")
        yield c


class TestLocalNow:
    def test_is_timezone_aware(self):
        """A naive timestamp cannot be compared with the away schedule's UTC."""
        assert server.local_now().tzinfo is not None

    def test_agrees_with_utc(self):
        """Same instant, just expressed in the local offset."""
        delta = abs(
            (server.local_now() - datetime.now(timezone.utc)).total_seconds()
        )
        assert delta < 5

    def test_reports_wall_clock_hour(self):
        """The hour a week schedule is compared against is the local one."""
        assert server.local_now().hour == time.localtime().tm_hour

    def test_timezone_name_is_reported(self):
        assert server.local_timezone_name()


class TestTimestampsCarryAnOffset:
    """Without an offset the browser has to guess, and guesses wrong."""

    def test_status_timestamp_is_aware(self, client):
        ts = client.get("/api/status").json()["timestamp"]
        assert datetime.fromisoformat(ts).tzinfo is not None

    def test_health_timestamp_is_aware(self, client):
        ts = client.get("/api/health").json()["timestamp"]
        assert datetime.fromisoformat(ts).tzinfo is not None

    def test_status_reports_the_timezone(self, client):
        """Makes a misconfigured container obvious instead of silent."""
        assert client.get("/api/status").json()["timezone"]


class TestScheduleUsesWallClock:
    def test_week_schedule_is_evaluated_in_local_time(self, monkeypatch):
        """get_current_schedule_mode must ask local_now(), not UTC."""
        seen = {}

        def fake_local_now():
            seen["called"] = True
            return datetime.now().astimezone()

        monkeypatch.setattr(server, "local_now", fake_local_now)
        server.get_current_schedule_mode("1")
        assert seen.get("called"), (
            "get_current_schedule_mode() no longer goes through local_now(), so "
            "it is back to whatever timezone the container happens to run in"
        )

    def test_away_schedule_still_compares_absolute_instants(self):
        """Away times are absolute, and the browser already sends UTC."""
        schedule = {
            "enabled": True,
            "start_at": "2030-01-01T00:00:00+00:00",
            "end_at": "2030-01-02T00:00:00+00:00",
        }
        inside = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        outside = datetime(2030, 1, 3, 12, 0, tzinfo=timezone.utc)
        assert away_schedule.is_schedule_active(schedule, inside)
        assert not away_schedule.is_schedule_active(schedule, outside)

    def test_offset_datetimes_are_honoured_not_stripped(self):
        """A 22:00+02:00 start must not be read as 22:00 UTC."""
        schedule = {
            "enabled": True,
            "start_at": "2030-06-01T22:00:00+02:00",
            "end_at": "2030-06-02T06:00:00+02:00",
        }
        # 20:30 UTC is before the 20:00 UTC start... it is after it.
        assert away_schedule.is_schedule_active(
            schedule, datetime(2030, 6, 1, 20, 30, tzinfo=timezone.utc)
        )
        # 19:30 UTC is 21:30 local, still half an hour early.
        assert not away_schedule.is_schedule_active(
            schedule, datetime(2030, 6, 1, 19, 30, tzinfo=timezone.utc)
        )


class TestDeploymentSharesTheHostClock:
    """The code cannot fix this alone; compose has to hand over the clock."""

    def test_compose_mounts_the_host_timezone(self):
        compose = REPO_ROOT / "compose.yml"
        if not compose.is_file():
            pytest.skip("compose.yml not available in this checkout")
        text = compose.read_text(encoding="utf-8")
        assert "/etc/localtime:/etc/localtime:ro" in text, (
            "without the host clock the container runs in UTC and every week "
            "schedule fires at the wrong hour"
        )

    def test_compose_does_not_set_an_empty_tz(self):
        """An empty TZ is treated as UTC and would override the mount."""
        compose = REPO_ROOT / "compose.yml"
        if not compose.is_file():
            pytest.skip("compose.yml not available in this checkout")
        text = compose.read_text(encoding="utf-8")
        assert not re.search(r"^\s*-\s*TZ=\s*$", text, re.M)
