"""
Tests for whether a global mode actually reaches a zone.

Commissioning found this the hard way. A zone put on Eco by hand kept that Eco
through every press of Home and Away, because a zone-level override outranks the
global one on the hub and nothing ever released it. The front page said the
house was Home; one room was quietly holding Eco, and would have gone on holding
it all winter. In a cabin that is not a cosmetic bug — it is how pipes freeze.

The hub already has the concept this needs: ``override_allowed``, field 6 of the
zone record, the same checkbox the Nobø app shows. So the behaviour pinned down
here is:

  - a zone that follows the global mode has its own override released when a
    global mode is chosen, so the mode actually takes effect;
  - a zone told to ignore global modes keeps exactly what it was on, because
    that is the entire point of turning the flag off;
  - the flag is readable and writable, and writing it does not clobber the
    zone's name — both live in the same U00 record;
  - the scheduler's automatic Away and Home behave identically to the buttons,
    since an away period is precisely when nobody is watching.
"""

import copy
import os
import sys
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import config_persistence
import server

TEST_SESSION_ID = "pytest-fixed-session-id"


# Everything below reaches the app through the ``server`` module rather than
# through names imported from it. Two test modules call importlib.reload(server),
# which rebinds the module's globals while leaving the already-imported names in
# other modules pointing at the previous objects. A module-level
# ``from server import DEMO_ZONES`` therefore ends up inspecting a list the
# request handlers no longer use, and the tests quietly assert against a copy
# that nothing writes to.


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config_persistence, "AWAY_EXCEPTIONS_FILE",
                        tmp_path / "away_exceptions.json")
    monkeypatch.setattr(config_persistence, "save_demo_zones", lambda zones: None)
    monkeypatch.setattr(config_persistence, "save_server_state", lambda state: None)
    monkeypatch.setattr(config_persistence, "save_away_exceptions_applied", lambda ids: None)
    yield


@pytest.fixture(autouse=True)
def reset_demo_state():
    original = copy.deepcopy(server.DEMO_ZONES)
    original_overrides = set(server.DEMO_ZONE_OVERRIDES)
    yield
    server.DEMO_ZONES[:] = copy.deepcopy(original)
    server.DEMO_ZONE_OVERRIDES.clear()
    server.DEMO_ZONE_OVERRIDES.update(original_overrides)


@pytest.fixture
def client():
    c = TestClient(server.app)
    c.cookies.set("session_id", TEST_SESSION_ID)
    return c


def zone_mode(zone_id):
    return next(z['mode'] for z in server.DEMO_ZONES if str(z['zone_id']) == str(zone_id))


def zone_payload(client, zone_id):
    zones = client.get("/api/zones").json()["zones"]
    return next(z for z in zones if str(z["zone_id"]) == str(zone_id))


# ---------------------------------------------------------------------------
# The defect itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("global_mode, expected", [
    ("home", "normal"),
    ("comfort", "comfort"),
    ("away", "away"),
])
def test_global_mode_releases_a_zone_set_by_hand(client, global_mode, expected):
    """A zone set by hand must not sit out the global mode that follows it."""
    client.post("/api/zones/1/override/eco")
    assert zone_mode("1") == "eco"

    client.post(f"/api/global/override/{global_mode}")

    assert zone_mode("1") == expected, (
        f"zone 1 was left on {zone_mode('1')} after the house was told {global_mode}"
    )


def test_release_is_reported_in_the_response(client):
    client.post("/api/zones/1/override/eco")
    body = client.post("/api/global/override/home").json()
    assert "1" in body["zone_overrides_released"]


def test_a_zone_told_to_ignore_global_modes_keeps_its_own(client):
    """The flag has to actually mean something, or it is just decoration."""
    client.put("/api/zones/1", json={"follow_global_mode": False})
    client.post("/api/zones/1/override/eco")

    client.post("/api/global/override/comfort")

    assert zone_mode("1") == "eco"
    assert "1" not in client.post("/api/global/override/home").json()["zone_overrides_released"]


def test_other_zones_still_follow_when_one_opts_out(client):
    """One independent zone must not stop the rest of the house."""
    client.put("/api/zones/1", json={"follow_global_mode": False})
    client.post("/api/zones/1/override/eco")
    client.post("/api/zones/2/override/eco")

    client.post("/api/global/override/comfort")

    assert zone_mode("1") == "eco"
    assert zone_mode("2") == "comfort"


# ---------------------------------------------------------------------------
# What the interface is told
# ---------------------------------------------------------------------------

def test_zones_report_both_facts(client):
    zone = zone_payload(client, "1")
    assert zone["follows_global_mode"] is True
    assert zone["has_zone_override"] is False

    client.post("/api/zones/1/override/eco")
    assert zone_payload(client, "1")["has_zone_override"] is True

    client.post("/api/global/override/home")
    assert zone_payload(client, "1")["has_zone_override"] is False


def test_flag_round_trips(client):
    client.put("/api/zones/1", json={"follow_global_mode": False})
    assert zone_payload(client, "1")["follows_global_mode"] is False

    client.put("/api/zones/1", json={"follow_global_mode": True})
    assert zone_payload(client, "1")["follows_global_mode"] is True


def test_renaming_a_zone_leaves_the_flag_alone(client):
    """Name and flag share one hub record, so one must not overwrite the other."""
    client.put("/api/zones/1", json={"follow_global_mode": False})
    client.put("/api/zones/1", json={"name": "Pump House"})

    zone = zone_payload(client, "1")
    assert zone["name"] == "Pump House"
    assert zone["follows_global_mode"] is False


def test_setting_the_flag_leaves_the_name_alone(client):
    client.put("/api/zones/1", json={"name": "Pump House"})
    client.put("/api/zones/1", json={"follow_global_mode": False})

    assert zone_payload(client, "1")["name"] == "Pump House"


# ---------------------------------------------------------------------------
# The unattended path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scheduled_away_also_releases(client):
    """An away period is exactly when nobody is there to notice a stuck zone."""
    client.post("/api/zones/1/override/comfort")

    await server._apply_global_mode_internal("away", source="schedule")

    assert zone_mode("1") == "away"


@pytest.mark.asyncio
async def test_scheduled_away_respects_the_flag(client):
    client.put("/api/zones/1", json={"follow_global_mode": False})
    client.post("/api/zones/1/override/comfort")

    await server._apply_global_mode_internal("away", source="schedule")

    assert zone_mode("1") == "comfort"


# ---------------------------------------------------------------------------
# The real-hub path
#
# Demo mode models the hub, but it is not the hub. These drive the release
# against a stand-in and assert on the commands that would go down the wire.
# ---------------------------------------------------------------------------

def _stub_hub(zone_flags, overridden):
    """A hub with the given zones and a zone-level override on some of them."""
    hub = MagicMock()
    hub.zones = {
        zone_id: {"zone_id": zone_id, "name": f"Zone {zone_id}",
                  "override_allowed": allowed}
        for zone_id, allowed in zone_flags.items()
    }
    hub.overrides = {
        f"o{zone_id}": {"override_id": f"o{zone_id}", "mode": "2", "type": "0",
                        "end_time": "-1", "start_time": "-1",
                        "target_type": "1", "target_id": zone_id}
        for zone_id in overridden
    }
    # hub_command is what awaits the coroutine, so the hub method itself is a
    # plain mock — an AsyncMock here would leave coroutines unawaited.
    hub.async_create_override = MagicMock(return_value=None)
    return hub


@pytest.mark.asyncio
async def test_real_hub_releases_only_the_zones_that_follow(monkeypatch):
    hub = _stub_hub({"1": "1", "2": "0", "3": "1"}, overridden=["1", "2", "3"])
    monkeypatch.setattr(server, "DEMO_MODE", False)
    monkeypatch.setattr(server, "hub", hub)
    monkeypatch.setattr(server, "hub_command", AsyncMock())

    released = await server._release_zone_overrides_for_global("home")

    assert released == ["1", "3"]
    targets = [call.args[3] for call in hub.async_create_override.call_args_list]
    assert targets == ["1", "3"]
    modes = {call.args[0] for call in hub.async_create_override.call_args_list}
    assert modes == {"0"}, "a release must be an override in NORMAL mode"


@pytest.mark.asyncio
async def test_real_hub_leaves_unoverridden_zones_alone(monkeypatch):
    """No point sending a cancel to a zone that is not holding anything."""
    hub = _stub_hub({"1": "1", "2": "1"}, overridden=["1"])
    monkeypatch.setattr(server, "DEMO_MODE", False)
    monkeypatch.setattr(server, "hub", hub)
    monkeypatch.setattr(server, "hub_command", AsyncMock())

    released = await server._release_zone_overrides_for_global("home")

    assert released == ["1"]


@pytest.mark.asyncio
async def test_one_failing_zone_does_not_strand_the_others(monkeypatch):
    hub = _stub_hub({"1": "1", "2": "1"}, overridden=["1", "2"])
    monkeypatch.setattr(server, "DEMO_MODE", False)
    monkeypatch.setattr(server, "hub", hub)

    calls = []

    async def flaky(coro):
        calls.append(coro)
        if len(calls) == 1:
            raise RuntimeError("hub said no")

    monkeypatch.setattr(server, "hub_command", flaky)

    released = await server._release_zone_overrides_for_global("home")

    assert released == ["2"]


# ---------------------------------------------------------------------------
# Reading the flag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("1", True),
    ("0", False),
    (None, True),      # absent means allowed, which is the hub's own default
    ("", True),        # and anything unrecognised errs towards following
])
def test_zone_follows_global_mode_reading(raw, expected):
    zone = {} if raw is None else {"override_allowed": raw}
    assert server.zone_follows_global_mode(zone) is expected


def test_normal_overrides_are_not_a_hold():
    """Mode 0 is the hub's way of saying 'no override', not a held zone."""
    hub = MagicMock()
    hub.overrides = {
        "a": {"mode": "0", "target_type": "1", "target_id": "1"},
        "b": {"mode": "2", "target_type": "1", "target_id": "2"},
        "c": {"mode": "2", "target_type": "0", "target_id": "-1"},
    }
    assert server._zones_with_own_override(hub) == {"2"}
