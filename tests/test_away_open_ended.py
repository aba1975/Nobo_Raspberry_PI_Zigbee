"""
Tests for an away period with no return date.

A cabin that is let out has a known handover and an unknown return: the owner
knows the tenants leave on Sunday evening, but not when anybody is next in the
building. Until now the away period demanded both ends, so that trip could not
be planned at all — the only open-ended option was the Away button, which starts
this instant and cannot be scheduled for Sunday.

What is pinned down here:

  - a period can be saved with a start and no end;
  - it becomes active when its start passes, exactly like a closed one;
  - it never expires by itself, so nothing quietly brings the heating back on
    while the building is empty;
  - "I'm back" is what ends it;
  - a period *with* an end still behaves exactly as it did, including the
    guards against a return before the departure or in the past.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import away_schedule
import server

TEST_SESSION_ID = "pytest-fixed-session-id"


@pytest.fixture(autouse=True)
def isolated_schedule(tmp_path, monkeypatch):
    monkeypatch.setattr(away_schedule, "DATA_DIR", tmp_path)
    monkeypatch.setattr(away_schedule, "SCHEDULE_FILE", tmp_path / "away_schedule.json")
    yield


@pytest.fixture
def client():
    c = TestClient(server.app)
    c.cookies.set("session_id", TEST_SESSION_ID)
    return c


def iso(dt):
    return dt.isoformat()


NOW = datetime(2026, 9, 6, 18, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_a_start_with_no_end_is_valid():
    ok, err = away_schedule.validate_schedule(
        True, iso(NOW + timedelta(days=1)), None, now=NOW
    )
    assert ok, err


def test_a_start_with_an_empty_end_is_valid():
    """The browser sends "" for a field it is not using, not null."""
    ok, err = away_schedule.validate_schedule(
        True, iso(NOW + timedelta(days=1)), "", now=NOW
    )
    assert ok, err


def test_a_start_is_still_required():
    ok, err = away_schedule.validate_schedule(True, None, None, now=NOW)
    assert not ok
    assert "start_at" in err


def test_an_end_before_the_start_is_still_rejected():
    ok, err = away_schedule.validate_schedule(
        True, iso(NOW + timedelta(days=2)), iso(NOW + timedelta(days=1)), now=NOW
    )
    assert not ok


def test_an_end_in_the_past_is_still_rejected():
    ok, err = away_schedule.validate_schedule(
        True, iso(NOW - timedelta(days=5)), iso(NOW - timedelta(days=1)), now=NOW
    )
    assert not ok


# ---------------------------------------------------------------------------
# When it runs
# ---------------------------------------------------------------------------

def test_open_ended_is_not_active_before_it_starts():
    s = {"enabled": True, "start_at": iso(NOW + timedelta(days=1)), "end_at": None}
    assert away_schedule.is_schedule_active(s, NOW) is False


def test_open_ended_becomes_active_at_its_start():
    s = {"enabled": True, "start_at": iso(NOW), "end_at": None}
    assert away_schedule.is_schedule_active(s, NOW) is True


def test_open_ended_is_still_active_a_year_later():
    """The whole point: nothing brings the heating back on its own."""
    s = {"enabled": True, "start_at": iso(NOW), "end_at": None}
    assert away_schedule.is_schedule_active(s, NOW + timedelta(days=365)) is True


def test_open_ended_never_expires():
    """
    The scheduler returns the house to Home from is_schedule_expired alone, so
    this answering False is what keeps an empty building on Away.
    """
    s = {"enabled": True, "start_at": iso(NOW), "end_at": None}
    assert away_schedule.is_schedule_expired(s, NOW + timedelta(days=365)) is False


def test_a_closed_period_still_expires():
    s = {
        "enabled": True,
        "start_at": iso(NOW),
        "end_at": iso(NOW + timedelta(days=7)),
    }
    assert away_schedule.is_schedule_expired(s, NOW + timedelta(days=8)) is True
    assert away_schedule.is_schedule_active(s, NOW + timedelta(days=8)) is False


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------

def test_the_api_accepts_a_period_with_no_return(client):
    start = datetime.now(timezone.utc) + timedelta(days=2)
    r = client.put("/api/global-mode/away-schedule",
                   json={"enabled": True, "start_at": iso(start)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["end_at"] is None
    assert body["currently_active"] is False


def test_the_api_accepts_an_open_ended_period_starting_now(client):
    start = datetime.now(timezone.utc) - timedelta(minutes=1)
    r = client.put("/api/global-mode/away-schedule",
                   json={"enabled": True, "start_at": iso(start), "end_at": None})
    assert r.status_code == 200, r.text
    assert r.json()["currently_active"] is True

    # And it reads back the same way, which is what the card renders from.
    got = client.get("/api/global-mode/away-schedule").json()
    assert got["end_at"] is None
    assert got["currently_active"] is True


def test_an_open_ended_period_puts_the_house_on_away_at_once(client):
    start = datetime.now(timezone.utc) - timedelta(minutes=1)
    client.put("/api/global-mode/away-schedule",
               json={"enabled": True, "start_at": iso(start)})

    zones = client.get("/api/zones").json()["zones"]
    assert all(z["current_mode"] == "away" for z in zones), \
        "an open-ended period that has started should behave exactly like a closed one"


def test_coming_back_ends_an_open_ended_period(client):
    start = datetime.now(timezone.utc) - timedelta(minutes=1)
    client.put("/api/global-mode/away-schedule",
               json={"enabled": True, "start_at": iso(start)})

    # "I'm back" clears the window, then puts the house Home.
    client.delete("/api/global-mode/away-schedule")
    client.post("/api/global/override/home")

    assert client.get("/api/global-mode/away-schedule").json()["enabled"] is False
    zones = client.get("/api/zones").json()["zones"]
    assert all(z["current_mode"] == "normal" for z in zones)


def test_a_return_date_can_be_added_later(client):
    """Somebody finds out when they are coming back. Nothing should be lost."""
    start = datetime.now(timezone.utc) + timedelta(days=1)
    client.put("/api/global-mode/away-schedule",
               json={"enabled": True, "start_at": iso(start)})

    end = start + timedelta(days=10)
    r = client.put("/api/global-mode/away-schedule",
                   json={"enabled": True, "start_at": iso(start), "end_at": iso(end)})
    assert r.status_code == 200
    assert r.json()["end_at"] is not None


def test_the_api_still_rejects_a_period_with_no_start(client):
    r = client.put("/api/global-mode/away-schedule", json={"enabled": True})
    assert r.status_code == 400
