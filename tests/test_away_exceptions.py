"""
Tests for away exceptions — zones kept on Eco while the house is Away.

Nobø's Away is a fixed 7 °C anti-frost temperature that cannot be configured.
A room that must not get that cold (pipes, an instrument, a workshop) has only
one warmer setting available to it: its own Eco temperature. An away exception
puts such a zone on Eco whenever the rest of the house goes Away.

The behaviour that matters, and that these tests pin down:

  - the list survives a restart (it is a file, not memory);
  - it is applied when Away is set manually;
  - it is applied when an away *period* starts, which happens in a background
    loop with no browser involved — this is the whole reason the feature is on
    the server and not in the UI;
  - Home clears it, exactly like any other override;
  - unknown zone ids are rejected rather than silently stored.
"""

import copy
import os
import sys

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import config_persistence
import server
from server import app, DEMO_ZONES

TEST_SESSION_ID = "pytest-fixed-session-id"


@pytest.fixture(autouse=True)
def isolated_exceptions(tmp_path, monkeypatch):
    """Keep the exception file out of the real data directory."""
    monkeypatch.setattr(config_persistence, "AWAY_EXCEPTIONS_FILE",
                        tmp_path / "away_exceptions.json")
    # save_demo_zones would otherwise write the modified demo house to disk
    monkeypatch.setattr(config_persistence, "save_demo_zones", lambda zones: None)
    monkeypatch.setattr(config_persistence, "save_server_state", lambda state: None)
    yield


@pytest.fixture(autouse=True)
def reset_demo_state():
    original = copy.deepcopy(DEMO_ZONES)
    yield
    DEMO_ZONES[:] = copy.deepcopy(original)


@pytest.fixture
def client():
    c = TestClient(app)
    c.cookies.set("session_id", TEST_SESSION_ID)
    return c


def zone_mode(zone_id):
    return next(z['mode'] for z in DEMO_ZONES if str(z['zone_id']) == str(zone_id))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_defaults_to_no_exceptions(client):
    r = client.get("/api/global-mode/away-exceptions")
    assert r.status_code == 200
    assert r.json()["zone_ids"] == []


def test_reports_the_fixed_away_temperature(client):
    """The UI needs this to explain what the exception is protecting against."""
    r = client.get("/api/global-mode/away-exceptions")
    assert r.json()["away_temperature"] == 7.0


def test_saved_list_is_returned_with_names(client):
    client.put("/api/global-mode/away-exceptions", json={"zone_ids": ["1"]})
    body = client.get("/api/global-mode/away-exceptions").json()
    assert body["zone_ids"] == ["1"]
    assert body["zone_names"] == [DEMO_ZONES[0]["name"]]


def test_list_survives_a_restart(client):
    """It is persisted to a file, so a reboot does not lose the protection."""
    client.put("/api/global-mode/away-exceptions", json={"zone_ids": ["1", "2"]})
    assert config_persistence.load_away_exceptions() == ["1", "2"]


def test_unknown_zone_is_rejected(client):
    r = client.put("/api/global-mode/away-exceptions", json={"zone_ids": ["999"]})
    assert r.status_code == 400
    assert "999" in r.json()["detail"]
    assert config_persistence.load_away_exceptions() == []


def test_duplicates_are_collapsed(client):
    client.put("/api/global-mode/away-exceptions", json={"zone_ids": ["1", "1", "2"]})
    assert config_persistence.load_away_exceptions() == ["1", "2"]


def test_corrupt_file_falls_back_to_no_exceptions(tmp_path, monkeypatch):
    """A broken file must not mean "protect nothing silently" AND must not crash."""
    path = tmp_path / "away_exceptions.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(config_persistence, "AWAY_EXCEPTIONS_FILE", path)
    assert config_persistence.load_away_exceptions() == []


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

def test_manual_away_puts_the_exception_zone_on_eco(client):
    client.put("/api/global-mode/away-exceptions", json={"zone_ids": ["1"]})
    client.post("/api/global/override/away")

    assert zone_mode("1") == "eco", "the exception zone should hold its eco temperature"
    assert zone_mode("2") == "away", "every other zone should be away"


def test_manual_away_reports_what_it_applied(client):
    client.put("/api/global-mode/away-exceptions", json={"zone_ids": ["1"]})
    r = client.post("/api/global/override/away")
    assert r.json()["away_exceptions_applied"] == ["1"]


def test_other_global_modes_are_untouched(client):
    """Comfort means comfort everywhere. The exception is only about Away."""
    client.put("/api/global-mode/away-exceptions", json={"zone_ids": ["1"]})
    client.post("/api/global/override/comfort")
    assert zone_mode("1") == "comfort"


def test_home_clears_the_exception_too(client):
    client.put("/api/global-mode/away-exceptions", json={"zone_ids": ["1"]})
    client.post("/api/global/override/away")
    client.post("/api/global/override/home")
    assert zone_mode("1") == "normal", "coming home returns every zone to its schedule"


@pytest.mark.asyncio
async def test_a_starting_away_period_applies_exceptions():
    """
    The away period is applied by a background loop, with no browser open. This
    is the case a UI-only implementation would silently fail.
    """
    config_persistence.save_away_exceptions(["1"])
    await server._apply_global_mode_internal("away", source="schedule")
    assert zone_mode("1") == "eco"
    assert zone_mode("2") == "away"


@pytest.mark.asyncio
async def test_an_ending_away_period_returns_everything_to_schedule():
    config_persistence.save_away_exceptions(["1"])
    await server._apply_global_mode_internal("away", source="schedule")
    await server._apply_global_mode_internal("home", source="schedule")
    assert zone_mode("1") == "normal"
    assert zone_mode("2") == "normal"


@pytest.mark.asyncio
async def test_no_exceptions_configured_changes_nothing():
    await server._apply_global_mode_internal("away", source="schedule")
    assert all(z["mode"] == "away" for z in DEMO_ZONES)


def test_setting_the_list_while_already_away_applies_it_immediately(client):
    """Otherwise the setting looks broken until the next trip."""
    client.post("/api/global/override/away")
    assert zone_mode("1") == "away"

    r = client.put("/api/global-mode/away-exceptions", json={"zone_ids": ["1"]})
    assert r.json()["applied_now"] == ["1"]
    assert zone_mode("1") == "eco"


def test_clearing_the_list_does_not_disturb_a_running_away(client):
    """Removing the exception should not force the zone anywhere new by itself."""
    client.put("/api/global-mode/away-exceptions", json={"zone_ids": ["1"]})
    client.post("/api/global/override/away")
    client.put("/api/global-mode/away-exceptions", json={"zone_ids": []})
    assert config_persistence.load_away_exceptions() == []
    assert zone_mode("1") == "eco", "it stays where it was until the next transition"


# ---------------------------------------------------------------------------
# Coming home again
# ---------------------------------------------------------------------------
# Found on real hardware during commissioning, and invisible until then. The
# away exception works by putting the room on a *zone* override, which outranks
# the global one -- that is the whole mechanism. Coming home sends
# create_override(NORMAL, GLOBAL), which cancels the global override and nothing
# else, so the zone override survived and the room held Eco for ever.
#
# Demo mode hid it: it used to blanket-assign every zone on a global change,
# which is tidier than the hub actually is. Demo now models the hub's ranking,
# so these tests fail if the release is removed.


class TestComingHomeReleasesTheRoom:
    def test_home_actually_frees_the_excluded_room(self, client):
        client.put("/api/global-mode/away-exceptions", json={"zone_ids": ["1"]})
        client.post("/api/global/override/away")
        assert zone_mode("1") == "eco"

        client.post("/api/global/override/home")
        assert zone_mode("1") == "normal", "the room must go back to its schedule"

    def test_comfort_reaches_the_excluded_room_too(self, client):
        """Away is the only mode the exception applies to."""
        client.put("/api/global-mode/away-exceptions", json={"zone_ids": ["1"]})
        client.post("/api/global/override/away")
        client.post("/api/global/override/comfort")
        assert zone_mode("1") == "comfort"

    def test_a_zone_override_outranks_a_global_one(self, client):
        """
        The hub's ranking, now modelled in demo. Proven on real hardware:
        zone Eco held while the other six rooms went Away.
        """
        client.post("/api/zones/1/override/comfort")
        client.post("/api/global/override/away")
        assert zone_mode("1") == "comfort", "zone override wins"
        assert zone_mode("2") == "away"

    def test_a_room_we_never_touched_is_left_alone(self, client):
        """
        Only zones this app actually overrode are released. A zone merely listed
        as an exception, with no away ever taken, must not be reset.
        """
        client.put("/api/global-mode/away-exceptions", json={"zone_ids": ["1"]})
        client.post("/api/global/override/comfort")
        assert zone_mode("1") == "comfort"

    def test_the_release_survives_a_restart(self, client):
        """
        A power blip halfway through a fortnight away must not strand the room
        on Eco, so what we overrode is written down, not just remembered.
        """
        client.put("/api/global-mode/away-exceptions", json={"zone_ids": ["1"]})
        client.post("/api/global/override/away")
        assert config_persistence.load_away_exceptions_applied() == ["1"]

        server._away_exception_zones_applied = set(
            config_persistence.load_away_exceptions_applied()
        )
        client.post("/api/global/override/home")
        assert zone_mode("1") == "normal"
        assert config_persistence.load_away_exceptions_applied() == []