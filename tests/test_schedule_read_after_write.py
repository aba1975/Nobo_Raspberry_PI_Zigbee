"""Saving a schedule must be visible the moment the call returns.

The bug this covers produced a failure in roughly one full test run in three,
always in an unrelated-looking place, and it took two wrong diagnoses before it
was pinned down.

``apply_week_profile_to_zone`` sends its commands to the hub and the hub echoes
the result back asynchronously; pynobo's dictionaries only update when that echo
arrives. The function waited for the *new profile* to appear — but not for the
zone to start pointing at it, and not for an in-place edit to land at all. So it
returned, the endpoint answered ``200``, and a client that immediately re-read
the schedule got the factory default the zone had been sharing a moment earlier.

For the user that is a schedule that looks like it did not save. In the test
suite it was ``test_a_written_schedule_reads_back_identically``, which is not
where the bug was — that test simply did the one thing no other test in its file
did, and read back without waiting.

These tests do not depend on how fast the machine is. A real hub's echo is
simulated with a delay, so the race is forced rather than hoped for: without the
waits every test in the first class below fails, every time.
"""

import asyncio

import pytest

import server


ENTRIES = ["00001", "06000", "22001"]
OTHER_ENTRIES = ["00000", "12001"]


class EchoingHub:
    """A hub whose changes land after a delay, the way a real one's do.

    pynobo does not apply a command locally when it is sent. It sends, and the
    hub's echo updates the dictionaries later, on the receive task. Everything
    here models exactly that and nothing else.
    """

    def __init__(self, delay=0.25):
        self.delay = delay
        self.zones = {
            "1": {"name": "Kitchen", "week_profile_id": "1"},
            "2": {"name": "Hallway", "week_profile_id": "1"},
        }
        self.week_profiles = {
            "1": {"week_profile_id": "1", "name": "Default", "profile": ["00000"]},
        }
        self._next_id = 20
        self.pending = []

    def _later(self, fn):
        async def land():
            await asyncio.sleep(self.delay)
            fn()
        self.pending.append(asyncio.ensure_future(land()))

    async def async_add_week_profile(self, name, entries):
        new_id = str(self._next_id)
        self._next_id += 1

        def apply():
            self.week_profiles[new_id] = {
                "week_profile_id": new_id, "name": name, "profile": list(entries),
            }
        self._later(apply)

    async def async_update_week_profile(self, profile_id, name, entries):
        def apply():
            self.week_profiles[profile_id] = {
                "week_profile_id": profile_id, "name": name, "profile": list(entries),
            }
        self._later(apply)

    async def async_update_zone(self, zone_id, week_profile_id=None, **kwargs):
        def apply():
            if week_profile_id is not None:
                self.zones[zone_id]["week_profile_id"] = week_profile_id
        self._later(apply)

    async def settle(self):
        if self.pending:
            await asyncio.gather(*self.pending)
            self.pending.clear()


@pytest.fixture(autouse=True)
def command_runs_inline(monkeypatch):
    """Run hub commands on the test's own loop.

    ``hub_command`` normally hands the coroutine to the dedicated hub loop via
    a worker thread. That machinery is not what is under test here, and running
    it would only add a second source of timing.
    """
    async def inline(coro, timeout=None):
        return await coro

    monkeypatch.setattr(server, "hub_command", inline)


class TestTheChangeIsVisibleWhenTheCallReturns:
    @pytest.mark.asyncio
    async def test_a_new_profile_is_attached_to_the_zone_before_returning(self):
        """The copy-on-write path: the zone shares the factory profile."""
        hub = EchoingHub()

        new_id = await server.apply_week_profile_to_zone(hub, "1", ENTRIES)

        assert hub.zones["1"]["week_profile_id"] == new_id, (
            "the call returned while the zone still pointed at the old profile, "
            "so reading the schedule back gives the one it just replaced"
        )
        await hub.settle()

    @pytest.mark.asyncio
    async def test_reading_back_immediately_gives_what_was_written(self):
        """The whole point, expressed as the caller sees it."""
        hub = EchoingHub()

        new_id = await server.apply_week_profile_to_zone(hub, "1", ENTRIES)

        profile_id = hub.zones["1"]["week_profile_id"]
        assert list(hub.week_profiles[profile_id]["profile"]) == ENTRIES
        assert profile_id == new_id
        await hub.settle()

    @pytest.mark.asyncio
    async def test_an_in_place_edit_has_landed_before_returning(self):
        """The edit path: the zone already owns its profile.

        This one had no wait at all, so it was the more certainly broken of the
        two — it just happened not to be what the suite tripped over.
        """
        hub = EchoingHub()
        hub.week_profiles["7"] = {
            "week_profile_id": "7", "name": "Kitchen schedule", "profile": list(OTHER_ENTRIES),
        }
        hub.zones["1"]["week_profile_id"] = "7"

        returned = await server.apply_week_profile_to_zone(hub, "1", ENTRIES)

        assert returned == "7", "an owned profile should be edited in place"
        assert list(hub.week_profiles["7"]["profile"]) == ENTRIES, (
            "the call returned before the edit had landed"
        )
        await hub.settle()


class TestTheSharingRulesStillHold:
    """The waits must not have changed what the function actually does."""

    @pytest.mark.asyncio
    async def test_a_shared_profile_is_never_edited_in_place(self):
        hub = EchoingHub()

        new_id = await server.apply_week_profile_to_zone(hub, "1", ENTRIES)

        assert new_id != "1"
        assert hub.zones["2"]["week_profile_id"] == "1", "the other zone was moved"
        assert hub.week_profiles["1"]["profile"] == ["00000"], "the factory profile was edited"
        await hub.settle()

    @pytest.mark.asyncio
    async def test_the_factory_profile_is_copied_even_when_only_one_zone_uses_it(self):
        """Profile 1 is the factory default and is never edited, exclusive or not."""
        hub = EchoingHub()
        del hub.zones["2"]

        new_id = await server.apply_week_profile_to_zone(hub, "1", ENTRIES)

        assert new_id != "1"
        assert hub.week_profiles["1"]["profile"] == ["00000"]
        await hub.settle()


class TestAHubThatNeverAnswers:
    @pytest.mark.asyncio
    async def test_a_missing_profile_is_reported_not_guessed(self):
        """If the hub never confirms the new profile, say so."""
        hub = EchoingHub()

        async def silence(name, entries):
            return None

        hub.async_add_week_profile = silence

        with pytest.raises(server.HTTPException) as exc:
            await server.apply_week_profile_to_zone(hub, "1", ENTRIES)
        assert exc.value.status_code == 502

    @pytest.mark.asyncio
    async def test_a_zone_that_never_attaches_does_not_hang_for_ever(self):
        """The wait is bounded. A hub that goes quiet must not block the caller.

        Returning a schedule that has not been confirmed is bad; never
        returning at all is worse, because it would tie up the request thread.
        """
        hub = EchoingHub()

        async def silence(zone_id, week_profile_id=None, **kwargs):
            return None

        hub.async_update_zone = silence

        new_id = await asyncio.wait_for(
            server.apply_week_profile_to_zone(hub, "1", ENTRIES), timeout=15
        )
        assert new_id is not None
        await hub.settle()
