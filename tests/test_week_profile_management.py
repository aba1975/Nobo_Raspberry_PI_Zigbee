"""
Week profiles as shared objects, which is what they actually are.

The official Nobø app treats a week profile as a thing you make, name and give
to rooms. This application hid them completely and showed "this zone's week", so
editing a schedule two rooms shared silently split one room onto a copy. Safe --
it can never reschedule a room you were not looking at -- but it quietly undoes
deliberate sharing, invents names like "Soverom 1. Etasje schedule", and leaves
orphaned profiles that nothing could delete.

The owner noticed the difference from the app and asked for the gap to be
closed. What is pinned down here:

  - a schedule says which zones use it;
  - a schedule can be renamed, because the invented names age badly -- a zone
    renamed to "Downstairs Bedrooms" left its schedule called "Soverom 1.
    Etasje schedule";
  - an unused schedule can be deleted, and one still in use cannot;
  - a zone can be pointed at a schedule that already exists, which is how two
    rooms come to share one rather than having two that happen to match;
  - saving a schedule can mean either "this zone only" or "everywhere this
    schedule is used", and the caller says which.
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("NOBO_DEMO", "true")

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import server


class TestWhoUsesASchedule:
    def test_a_schedule_used_by_nobody_can_go(self):
        assert server._week_profile_deletable("26", []) == (True, None)

    def test_a_schedule_in_use_names_the_zones(self):
        ok, why = server._week_profile_deletable(
            "21", [{"zone_id": "1", "name": "Large Bathroom"},
                   {"zone_id": "6", "name": "Small Bathroom"}])
        assert ok is False
        assert "Large Bathroom" in why and "Small Bathroom" in why
        assert "another schedule" in why, "should say what to do about it"

    @pytest.mark.parametrize("pid", sorted(server.UNDELETABLE_WEEK_PROFILES))
    def test_the_hubs_own_schedules_are_refused(self, pid):
        ok, why = server._week_profile_deletable(pid, [])
        assert ok is False
        assert "hub's own" in why

    def test_a_zone_with_no_name_falls_back_to_its_id(self):
        _, why = server._week_profile_deletable("22", [{"zone_id": "5", "name": ""}])
        assert "5" in why


class TestScheduleOrdering:
    def test_ids_sort_as_numbers(self):
        """The hub hands out 20, 21, 26 -- text ordering puts 20 before 3."""
        ids = ["26", "3", "20", "0", "21"]
        assert sorted(ids, key=server._profile_sort_key) == ["0", "3", "20", "21", "26"]

    def test_a_non_numeric_id_still_sorts(self):
        ids = ["10", "x", "2"]
        assert sorted(ids, key=server._profile_sort_key) == ["2", "10", "x"]


class TestWhichThingTheUserMeant:
    """
    ``apply_to`` on a schedule save. The default is unchanged, because every
    existing caller relies on it.
    """

    def _hub(self, shared: bool):
        hub = MagicMock()
        # The hub's command methods are coroutines; a plain MagicMock returns
        # something that cannot be awaited.
        hub.async_add_week_profile = AsyncMock()
        hub.async_update_week_profile = AsyncMock()
        hub.async_update_zone = AsyncMock()
        hub.zones = {
            "3": {"name": "Downstairs Bedrooms", "week_profile_id": "23"},
            "4": {"name": "Upstairs Bedrooms",
                  "week_profile_id": "23" if shared else "26"},
        }
        hub.week_profiles = {
            "23": {"name": "Soverom", "profile": ["00001"]},
            "26": {"name": "Own", "profile": ["00001"]},
        }
        return hub

    @pytest.mark.asyncio
    async def test_sharing_and_saying_nothing_splits_the_zone(self, monkeypatch):
        """The old behaviour, kept: a silent save must not touch the other zone."""
        hub = self._hub(shared=True)
        monkeypatch.setattr(server, "hub_command", _passthrough)
        monkeypatch.setattr(server, "wait_for_hub_state",
                            _fake_wait(hub, new_id="99"))
        monkeypatch.setattr(server, "_unique_week_profile_name", lambda h, n: n)
        pid = await server.apply_week_profile_to_zone(hub, "3", ["00001"])
        assert pid == "99", "should have been given a profile of its own"
        hub.async_add_week_profile.assert_called()
        hub.async_update_week_profile.assert_not_called()

    @pytest.mark.asyncio
    async def test_sharing_and_asking_for_everywhere_edits_in_place(self, monkeypatch):
        """The operation that had no way to be expressed before."""
        hub = self._hub(shared=True)
        monkeypatch.setattr(server, "hub_command", _passthrough)
        monkeypatch.setattr(server, "wait_for_hub_state", _fake_wait(hub))
        pid = await server.apply_week_profile_to_zone(
            hub, "3", ["00002"], apply_to="profile")
        assert pid == "23", "the shared schedule itself should have been edited"
        hub.async_update_week_profile.assert_called_once()
        hub.async_add_week_profile.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_zone_on_its_own_schedule_is_edited_in_place_either_way(self, monkeypatch):
        hub = self._hub(shared=False)
        hub.zones["3"]["week_profile_id"] = "26"
        hub.zones["4"]["week_profile_id"] = "23"
        monkeypatch.setattr(server, "hub_command", _passthrough)
        monkeypatch.setattr(server, "wait_for_hub_state", _fake_wait(hub))
        pid = await server.apply_week_profile_to_zone(hub, "3", ["00002"])
        assert pid == "26"
        hub.async_add_week_profile.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_factory_default_is_never_edited(self, monkeypatch):
        """
        Every zone starts on it and it has to keep meaning "the default", so
        even an explicit "change it everywhere" gets a copy instead.
        """
        hub = self._hub(shared=True)
        default = server.DEFAULT_WEEK_PROFILE_ID
        hub.week_profiles[default] = {"name": "Default", "profile": ["00001"]}
        hub.zones["3"]["week_profile_id"] = default
        hub.zones["4"]["week_profile_id"] = default
        monkeypatch.setattr(server, "hub_command", _passthrough)
        monkeypatch.setattr(server, "wait_for_hub_state", _fake_wait(hub, new_id="99"))
        monkeypatch.setattr(server, "_unique_week_profile_name", lambda h, n: n)
        pid = await server.apply_week_profile_to_zone(
            hub, "3", ["00002"], apply_to="profile")
        assert pid == "99"
        hub.async_update_week_profile.assert_not_called()


async def _passthrough(coro):
    """Stand in for hub_command, which sends and logs. Just await the call."""
    return await coro


def _fake_wait(hub, new_id=None):
    """Stand in for wait_for_hub_state, which polls a live hub."""
    async def wait(predicate, timeout=5.0, interval=0.1):
        if new_id is not None:
            hub.week_profiles[new_id] = {"name": "New", "profile": []}
        try:
            value = predicate()
        except Exception:
            return True
        return value if value else True
    return wait