"""
Tests for the setpoint guard — noticing a temperature changed outside this app.

Written after a real hardware finding. A Nobø thermostat with a dial does not
create an override when somebody turns it: it rewrites the zone's comfort or eco
temperature on the hub, permanently, and the hub keeps no record of the old
value. Turning the Gang NTB-2R produced exactly one change::

    20:00:13  z5 Gang: comfort: 17.0 -> 21.0

with ``active_override_id`` still ``-1``, and Comfort, Eco and Normal afterwards
all left it at 21.0. So there is nothing to cancel and nothing to read back —
the only way to know the room was meant to be 17 is to have written it down.

Two behaviours are pinned down here:

  - the room is *flagged* when the hub disagrees with what we intended, so it can
    be shown without anybody being emailed;
  - a global mode change *restores* the intended value first, because choosing
    Comfort or Away is the owner saying "use my settings".

What is deliberately not claimed: which app or dial made the change. The
official Nobø app rewrites the same field in the same way.
"""

import os
import sys

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import config_persistence
import server
import setpoint_guard as sg
from server import app, DEMO_ZONES

TEST_SESSION_ID = "pytest-fixed-session-id"


@pytest.fixture(autouse=True)
def isolated_guard(tmp_path, monkeypatch):
    """A guard with nothing remembered, and nothing written to the real data dir."""
    monkeypatch.setattr(config_persistence, "INTENDED_SETPOINTS_FILE",
                        tmp_path / "intended_setpoints.json", raising=False)
    monkeypatch.setattr(config_persistence, "save_demo_zones", lambda zones: None)
    monkeypatch.setattr(config_persistence, "save_server_state", lambda state: None)
    fresh = sg.SetpointGuard(save_fn=config_persistence.save_intended_setpoints)
    monkeypatch.setattr(server, "setpoint_guard", fresh)
    yield fresh


@pytest.fixture
def client():
    c = TestClient(app)
    c.cookies.set("session_id", TEST_SESSION_ID)
    return c


def zone(zone_id="1"):
    return next(z for z in DEMO_ZONES if str(z["zone_id"]) == zone_id)


def zones_list(client):
    body = client.get("/api/zones").json()
    return body if isinstance(body, list) else body.get("zones", [])


def api_zone(client, zone_id):
    return next(z for z in zones_list(client) if str(z["zone_id"]) == str(zone_id))


def adjustable_zone_id(client):
    """
    A room whose devices actually take a set point.

    Not every zone does — some demo rooms are on models where the temperature is
    set on the device itself — and picking one by number would tie these tests to
    the shape of the demo house.
    """
    for z in zones_list(client):
        if z.get("supports_temp_adjust"):
            return str(z["zone_id"])
    raise AssertionError("no demo zone accepts a set point")


# ---------------------------------------------------------------------------
# The guard on its own
# ---------------------------------------------------------------------------

class TestNoticingTheDifference:
    def test_a_zone_we_have_never_set_is_adopted_not_flagged(self):
        g = sg.SetpointGuard()
        z = {"zone_id": "1", "comfort_temperature": 21.0, "eco_temperature": 17.0}
        g.observe([z])
        assert g.drift(z) == {}, "first sight is the truth, not a change"
        assert g.intended_for("1", "comfort") == 21.0

    def test_the_hub_disagreeing_is_drift(self):
        g = sg.SetpointGuard()
        g.observe([{"zone_id": "1", "comfort_temperature": 17.0, "eco_temperature": 15.0}])
        moved = g.drift({"zone_id": "1", "comfort_temperature": 21.0, "eco_temperature": 15.0})
        assert moved == {"comfort": {"intended": 17.0, "actual": 21.0}}

    def test_our_own_write_is_not_drift(self):
        g = sg.SetpointGuard()
        g.observe([{"zone_id": "1", "comfort_temperature": 17.0, "eco_temperature": 15.0}])
        g.remember("1", "comfort", 21)
        assert g.drift({"zone_id": "1", "comfort_temperature": 21.0,
                        "eco_temperature": 15.0}) == {}

    def test_the_hub_lagging_our_write_is_not_reported(self):
        """
        A command and its push are not atomic. Reporting the gap between them
        would make the warning flicker on every adjustment.
        """
        clock = [1000.0]
        g = sg.SetpointGuard(now_fn=lambda: clock[0])
        g.observe([{"zone_id": "1", "comfort_temperature": 17.0, "eco_temperature": 15.0}])
        g.remember("1", "comfort", 21)
        stale = {"zone_id": "1", "comfort_temperature": 17.0, "eco_temperature": 15.0}
        assert g.drift(stale) == {}, "still settling"

        clock[0] += sg.SETTLE_SECONDS + 1
        assert g.drift(stale) == {"comfort": {"intended": 21.0, "actual": 17.0}}

    def test_the_hub_sending_strings_is_handled(self):
        """The Nobø protocol is text on the wire; pynobo keeps it that way."""
        g = sg.SetpointGuard()
        g.observe([{"zone_id": "1", "comfort_temperature": "17", "eco_temperature": "15"}])
        assert g.intended_for("1", "comfort") == 17.0
        assert g.drift({"zone_id": "1", "comfort_temperature": "21",
                        "eco_temperature": "15"}) == {"comfort": {"intended": 17.0,
                                                                  "actual": 21.0}}

    def test_accepting_stops_the_warning(self):
        g = sg.SetpointGuard()
        z_before = {"zone_id": "1", "comfort_temperature": 17.0, "eco_temperature": 15.0}
        z_after = {"zone_id": "1", "comfort_temperature": 21.0, "eco_temperature": 15.0}
        g.observe([z_before])
        assert g.drift(z_after)
        g.accept("1")
        g.observe([z_after])
        assert g.drift(z_after) == {}

    def test_an_intention_outlives_a_restart(self):
        """
        The point of persisting: a dial turned while the Pi was off is still
        visible when it comes back, because the intention is on disk and the
        hub's value is not what it was.
        """
        saved = {}
        g = sg.SetpointGuard(save_fn=lambda d: saved.update({"v": dict(d)}))
        g.observe([{"zone_id": "1", "comfort_temperature": 17.0, "eco_temperature": 15.0}])

        reborn = sg.SetpointGuard(intended={k: dict(v) for k, v in saved["v"].items()})
        moved = reborn.drift({"zone_id": "1", "comfort_temperature": 21.0,
                              "eco_temperature": 15.0})
        assert moved == {"comfort": {"intended": 17.0, "actual": 21.0}}

    def test_away_is_not_guarded(self):
        """Away is a fixed 7 °C on the hub and cannot be set, so it cannot drift."""
        assert "away" not in sg.GUARDED_FIELDS


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------

class TestTheRoomShowsTheWarning:
    def test_a_quiet_house_flags_nothing(self, client):
        for z in zones_list(client):
            assert z["setpoint_changed_outside"] is None

    def test_a_change_made_elsewhere_is_flagged_on_the_room(self, client):
        zid = adjustable_zone_id(client)          # adopts the current house
        was = float(zone(zid)["comfort_temp"])
        zone(zid)["comfort_temp"] = was + 4       # somebody turns the dial

        assert api_zone(client, zid)["setpoint_changed_outside"] == {
            "comfort": {"intended": was, "actual": was + 4}
        }

    def test_setting_it_from_here_is_not_flagged(self, client):
        zid = adjustable_zone_id(client)
        was = float(zone(zid)["comfort_temp"])
        r = client.post(f"/api/zones/{zid}/temperature", json={"comfort": was - 2})
        assert r.status_code == 200, r.text
        assert api_zone(client, zid)["setpoint_changed_outside"] is None

    def test_restoring_puts_the_room_back(self, client):
        zid = adjustable_zone_id(client)
        was = float(zone(zid)["comfort_temp"])
        zone(zid)["comfort_temp"] = was + 4

        r = client.post(f"/api/zones/{zid}/restore-setpoints")
        assert r.status_code == 200, r.text
        assert r.json()["restored"][0]["comfort"] == was
        assert zone(zid)["comfort_temp"] == was
        assert api_zone(client, zid)["setpoint_changed_outside"] is None

    def test_restoring_a_room_that_has_not_moved_says_so(self, client):
        zid = adjustable_zone_id(client)
        r = client.post(f"/api/zones/{zid}/restore-setpoints")
        assert r.status_code == 400
        assert "already matches" in r.json()["detail"]

    def test_accepting_keeps_the_new_value(self, client):
        zid = adjustable_zone_id(client)
        was = float(zone(zid)["comfort_temp"])
        zone(zid)["comfort_temp"] = was + 4
        assert api_zone(client, zid)["setpoint_changed_outside"]

        r = client.post(f"/api/zones/{zid}/accept-setpoints")
        assert r.status_code == 200
        assert api_zone(client, zid)["setpoint_changed_outside"] is None
        assert zone(zid)["comfort_temp"] == was + 4, "accepted, not reverted"


class TestAGlobalModeUsesOurSettings:
    """
    The owner's second requirement, and the reason the guard is not only a
    warning: choosing Comfort, Eco, Away or the schedule means "use my
    settings", and the hub has no memory of what those were.
    """

    @pytest.mark.parametrize("mode", ["comfort", "eco", "away", "home"])
    def test_a_global_mode_restores_the_setpoint_first(self, client, mode):
        zid = adjustable_zone_id(client)
        was = float(zone(zid)["comfort_temp"])
        zone(zid)["comfort_temp"] = was + 4

        r = client.post(f"/api/global/override/{mode}")
        assert r.status_code == 200, r.text
        assert r.json()["setpoints_restored"], "the drift should have been put right"
        assert zone(zid)["comfort_temp"] == was
        assert api_zone(client, zid)["setpoint_changed_outside"] is None

    def test_a_global_mode_on_a_quiet_house_writes_nothing(self, client):
        adjustable_zone_id(client)
        r = client.post("/api/global/override/comfort")
        assert r.json()["setpoints_restored"] == []

    @pytest.mark.asyncio
    async def test_the_schedule_restores_it_too(self, client):
        """No browser is open when an away period starts."""
        zid = adjustable_zone_id(client)
        was = float(zone(zid)["comfort_temp"])
        zone(zid)["comfort_temp"] = was + 4

        await server._apply_global_mode_internal("away", source="schedule")
        assert zone(zid)["comfort_temp"] == was

    def test_an_accepted_value_is_not_undone_by_a_global_mode(self, client):
        """Accepting means it is ours now, so there is nothing to restore."""
        zid = adjustable_zone_id(client)
        was = float(zone(zid)["comfort_temp"])
        zone(zid)["comfort_temp"] = was + 4
        client.post(f"/api/zones/{zid}/accept-setpoints")

        client.post("/api/global/override/comfort")
        assert zone(zid)["comfort_temp"] == was + 4