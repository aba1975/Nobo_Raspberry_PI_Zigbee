"""
Tests for how long an override lives.

Every override carries a lifetime as well as a mode, and the two useful values
behave very differently:

    NOW       the hub cancels the override at the next week-profile switch
              point. The official Nobø app calls this "automatic return".
    CONSTANT  the override stands until something cancels it. The app calls
              this "konstant".

This application sent NOW for every override it created, which quietly broke
three separate promises:

  * An away period with no return date ended itself at the next scheduled
    change. A cabin left empty warmed back up on its own, and — because the app
    kept believing its own plan — the front page still said "Empty" with an
    "I'm back now" button. Found in use, by the owner, on the real system.
  * A zone put on Eco by hand did the same thing.
  * Worst of all, the Eco override that holds a room *above* the 7 °C
    anti-frost temperature during Away — the pipes-in-the-wall feature —
    expired at the first schedule transition and let that room fall to 7 °C
    after all. Nothing surfaced this; the feature simply stopped working part
    way through every trip.

These tests drive the real wire path against the fake hub and assert on the
override records it stores, because the lifetime is a field on the wire that no
higher-level API response reflects.
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import pynobo
import server
from server import app
from tests.fake_hub import FakeHubThread

HUB_SERIAL = "123123123123"
SESSION_ID = "pytest-fixed-session-id"

CONSTANT = pynobo.nobo.API.OVERRIDE_TYPE_CONSTANT   # '3'
NOW = pynobo.nobo.API.OVERRIDE_TYPE_NOW             # '0'

# Position of each field in the record the hub stores, per STRUCT_KEYS_OVERRIDE:
#   override_id, mode, type, end_time, start_time, target_type, target_id
TYPE = 2
MODE = 1
TARGET_TYPE = 5
TARGET_ID = 6


def wait_until(predicate, timeout=10.0, message="condition not met in time"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


@pytest.fixture
def hub_env(monkeypatch, tmp_path):
    with FakeHubThread() as fake:
        monkeypatch.setattr(server, "DEMO_MODE", False)
        monkeypatch.setattr(server, "NOBO_SERIAL", HUB_SERIAL)
        monkeypatch.setattr(server, "NOBO_IP", "127.0.0.1")
        monkeypatch.setattr(
            server.config_persistence, "ZONE_ICONS_FILE", tmp_path / "zone_icons.json"
        )
        monkeypatch.setattr(
            server.config_persistence, "AWAY_EXCEPTIONS_FILE",
            tmp_path / "away_exceptions.json"
        )
        monkeypatch.setattr(
            server.config_persistence, "AWAY_EXCEPTIONS_APPLIED_FILE",
            tmp_path / "away_exceptions_applied.json", raising=False
        )
        monkeypatch.setattr(server.away_schedule, "DATA_DIR", tmp_path)
        monkeypatch.setattr(server.away_schedule, "SCHEDULE_FILE",
                            tmp_path / "away_schedule.json")
        server.connect_to_hub_sync()
        try:
            yield fake
        finally:
            server.disconnect_from_hub()


@pytest.fixture
def client(hub_env):
    with TestClient(app) as test_client:
        test_client.cookies.set("session_id", SESSION_ID)
        yield test_client


@pytest.fixture
def fake(hub_env):
    return hub_env


def live_overrides(fake):
    """Override records the hub is currently holding."""
    return list(fake.overrides.values())


def sole_override(fake):
    wait_until(lambda: len(fake.overrides) == 1,
               message=f"expected exactly one override, got {fake.overrides}")
    return list(fake.overrides.values())[0]


# ---------------------------------------------------------------------------
# The defect the owner found
# ---------------------------------------------------------------------------

class TestGlobalModesHoldUntilCancelled:
    @pytest.mark.parametrize("mode", ["away", "comfort", "eco"])
    def test_a_global_mode_is_sent_as_constant(self, client, fake, mode):
        """
        NOW would have the hub drop this at the next week-profile change, which
        is how an empty cabin warmed itself back up.
        """
        assert client.post(f"/api/global/override/{mode}").status_code == 200

        record = sole_override(fake)
        assert record[TARGET_TYPE] == pynobo.nobo.API.OVERRIDE_TARGET_GLOBAL
        assert record[TYPE] == CONSTANT, (
            f"global {mode} was sent with lifetime {record[TYPE]!r}; "
            f"{NOW!r} (NOW) expires at the next schedule change"
        )

    def test_an_away_period_with_no_return_date_is_constant(self, client, fake):
        """The exact case reported: away with no return, ended by itself."""
        start = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        r = client.put("/api/global-mode/away-schedule",
                       json={"enabled": True, "start_at": start})
        assert r.status_code == 200
        assert r.json()["currently_active"] is True

        record = sole_override(fake)
        assert record[MODE] == pynobo.nobo.API.OVERRIDE_MODE_AWAY
        assert record[TYPE] == CONSTANT


class TestZoneOverridesHoldUntilCancelled:
    @pytest.mark.parametrize("mode", ["comfort", "eco", "away"])
    def test_a_zone_set_by_hand_is_constant(self, client, fake, mode):
        zone_id = client.get("/api/zones").json()["zones"][0]["zone_id"]
        assert client.post(f"/api/zones/{zone_id}/override/{mode}").status_code == 200

        record = sole_override(fake)
        assert record[TARGET_TYPE] == pynobo.nobo.API.OVERRIDE_TARGET_ZONE
        assert record[TARGET_ID] == zone_id
        assert record[TYPE] == CONSTANT


class TestAwayExceptionsHoldForTheWholeTrip:
    def test_a_room_held_above_anti_frost_is_constant(self, client, fake):
        """
        This is the one with teeth. The exception exists to keep a room warmer
        than 7 °C while the house is away. With NOW the hub dropped that Eco
        override at the first schedule transition and the room fell to the away
        temperature — silently, and for the rest of the trip.
        """
        zone_id = client.get("/api/zones").json()["zones"][0]["zone_id"]
        assert client.put("/api/global-mode/away-exceptions",
                          json={"zone_ids": [zone_id]}).status_code == 200

        assert client.post("/api/global/override/away").status_code == 200

        wait_until(lambda: len(fake.overrides) == 2,
                   message=f"expected a global and a zone override, got {fake.overrides}")

        zone_records = [r for r in live_overrides(fake)
                        if r[TARGET_TYPE] == pynobo.nobo.API.OVERRIDE_TARGET_ZONE]
        assert len(zone_records) == 1
        exception = zone_records[0]
        assert exception[TARGET_ID] == zone_id
        assert exception[MODE] == pynobo.nobo.API.OVERRIDE_MODE_ECO
        assert exception[TYPE] == CONSTANT, (
            "the away exception must outlive a schedule transition, or the room "
            "it protects quietly drops to the anti-frost temperature"
        )


class TestTheSchedulerAgrees:
    @pytest.mark.asyncio
    async def test_a_scheduled_away_is_constant(self, client, fake):
        """
        An away period that starts while nobody is there is exactly when an
        override quietly expiring goes unnoticed longest.
        """
        await server._apply_global_mode_internal("away", source="schedule")

        record = sole_override(fake)
        assert record[MODE] == pynobo.nobo.API.OVERRIDE_MODE_AWAY
        assert record[TYPE] == CONSTANT


# ---------------------------------------------------------------------------
# Nothing should still be asking for the old behaviour
# ---------------------------------------------------------------------------

def test_no_override_is_created_with_the_expiring_lifetime():
    """
    A guard against reintroducing this one call site at a time. Every override
    this application creates goes through OVERRIDE_UNTIL_CANCELLED.
    """
    import io
    src = io.open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "app", "server.py"),
        encoding="utf-8",
    ).read()
    assert "OVERRIDE_TYPE_NOW" not in src, (
        "server.py creates an override that expires at the next schedule change"
    )
    assert server.OVERRIDE_UNTIL_CANCELLED == CONSTANT


def test_the_command_log_reports_the_lifetime_actually_sent():
    """
    The command log is the first place anybody looks when the heating does
    something unexpected. It said NOW for years while sending NOW, which was at
    least honest; it must not now say NOW while sending CONSTANT.
    """
    import io, re
    src = io.open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "app", "server.py"),
        encoding="utf-8",
    ).read()
    lying = re.findall(r'create_override[^\n"]*\bNOW\b[^\n"]*', src)
    assert not lying, f"command log still claims NOW: {lying}"
