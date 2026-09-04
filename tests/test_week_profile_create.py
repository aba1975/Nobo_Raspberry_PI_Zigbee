"""
Creating a schedule, and refusing to pretend an edit landed.

Two findings from the owner using the schedule management built in the previous
commits.

**There was no way to make one.** The list could be renamed, edited and deleted
but never added to, so a schedule only came into existence as a side effect:
editing a shared one split a copy off. That is a strange way to get the thing
you actually wanted, and it means you cannot have a schedule without first
disturbing a zone.

**And an edit could silently do nothing.** The hub accepts U02 for its own
built-in schedules and then ignores it -- no error, no change. Established on
real hardware against profile 0: renaming it and rescheduling it both reported
success and left it exactly as it was. Reporting success there is worse than
refusing, because the user believes a week they typed has been saved.
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("NOBO_DEMO", "true")

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import server


DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def all_day(mode):
    return {d: [{"start": "00:00", "end": "24:00", "mode": mode}] for d in DAYS}


class TestTheHubsOwnSchedulesAreOffLimits:
    @pytest.mark.parametrize("pid", sorted(server.UNDELETABLE_WEEK_PROFILES))
    def test_they_are_marked_uneditable(self, pid):
        ok, why = server._week_profile_editable(pid)
        assert ok is False
        assert "make a new schedule" in why, "should say what to do instead"

    def test_an_ordinary_schedule_is_editable(self):
        assert server._week_profile_editable("26") == (True, None)

    def test_the_two_reasons_are_different(self):
        """
        Deleting and editing fail for the same profiles but not for the same
        reason, and a user reading "cannot be deleted" while trying to edit
        learns nothing.
        """
        _, no_delete = server._week_profile_deletable("0", [])
        _, no_edit = server._week_profile_editable("0")
        assert no_delete != no_edit


class TestOffIsStillUnderstood:
    """
    Off is no longer offered as a choice -- in a cabin it means no frost
    protection at all, and the hub refuses it as an override anyway. But it is
    valid in a week profile (verified on hardware: wire status 4), and a
    schedule made in the official app may contain it, so it must still be read
    and written rather than rejected.
    """

    def test_off_is_a_valid_mode_on_the_way_in(self):
        assert "off" in server.VALID_SCHEDULE_MODES

    def test_off_survives_a_round_trip(self):
        week = {d: [{"start": "00:00", "end": "12:00", "mode": "off"},
                    {"start": "12:00", "end": "24:00", "mode": "comfort"}] for d in DAYS}
        entries = server.schedule_to_week_profile(
            server.ScheduleUpdate(schedule=week).schedule)
        assert entries[0] == "00004", "status 4 is Off on the wire"
        back = server.week_profile_to_schedule(entries)
        assert back["monday"][0]["mode"] == "off"


class TestCreatingASchedule:
    def _hub(self):
        hub = MagicMock()
        hub.week_profiles = {"23": {"name": "Bedrooms", "profile": ["00001"]}}
        hub.zones = {}
        hub.async_add_week_profile = AsyncMock()
        return hub

    @pytest.mark.asyncio
    async def test_a_new_schedule_gets_the_hubs_id(self, monkeypatch):
        hub = self._hub()
        monkeypatch.setattr(server, "DEMO_MODE", False)
        monkeypatch.setattr(server, "hub", hub)
        monkeypatch.setattr(server, "hub_connected", True)
        monkeypatch.setattr(server, "hub_command", _passthrough)
        monkeypatch.setattr(server, "broadcast_zone_update", AsyncMock())

        async def wait(predicate, timeout=5.0, interval=0.1):
            hub.week_profiles["44"] = {"name": "Weekends", "profile": ["00001"]}
            return predicate()

        monkeypatch.setattr(server, "wait_for_hub_state", wait)

        body = server.WeekProfileCreate(name="Weekends", schedule=all_day("comfort"))
        result = await server.create_week_profile(body)
        assert result["profile_id"] == "44", "the hub assigns the id, not the client"
        hub.async_add_week_profile.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_hub_that_never_reports_it_is_not_called_success(self, monkeypatch):
        hub = self._hub()
        monkeypatch.setattr(server, "DEMO_MODE", False)
        monkeypatch.setattr(server, "hub", hub)
        monkeypatch.setattr(server, "hub_connected", True)
        monkeypatch.setattr(server, "hub_command", _passthrough)

        async def wait(predicate, timeout=5.0, interval=0.1):
            return set()

        monkeypatch.setattr(server, "wait_for_hub_state", wait)

        body = server.WeekProfileCreate(name="Weekends", schedule=all_day("comfort"))
        with pytest.raises(server.HTTPException) as exc:
            await server.create_week_profile(body)
        assert exc.value.status_code == 502

    @pytest.mark.asyncio
    async def test_an_empty_name_is_refused(self, monkeypatch):
        monkeypatch.setattr(server, "hub_connected", True)
        body = server.WeekProfileCreate(name="   ", schedule=all_day("comfort"))
        with pytest.raises(server.HTTPException) as exc:
            await server.create_week_profile(body)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_a_week_with_a_gap_is_refused(self, monkeypatch):
        monkeypatch.setattr(server, "hub_connected", True)
        gappy = {d: [{"start": "00:00", "end": "12:00", "mode": "comfort"}] for d in DAYS}
        body = server.WeekProfileCreate(name="Half", schedule=gappy)
        with pytest.raises(server.HTTPException) as exc:
            await server.create_week_profile(body)
        assert exc.value.status_code == 400
        assert "24:00" in exc.value.detail


async def _passthrough(coro):
    return await coro