"""Telling the hub the time again when the clocks change.

The hub keeps its own clock and takes it from ``HELLO`` alone. It then runs the
week profile itself, in wall clock, so an offset change that is never announced
leaves every scheduled switch an hour out until something forces a fresh
handshake.

The dangerous half of this is not the detection, it is the cure: it displaces a
healthy hub client on purpose, which is otherwise exactly the mistake that once
left two orphaned sockets holding both of the hub's LAN slots. These therefore
test the connection accounting as hard as the arithmetic.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import server


@pytest.fixture(autouse=True)
def hub_state():
    """Pretend to be a real installation with a live hub."""
    before = (
        server.DEMO_MODE, server.hub, server.hub_connected,
        server.hub_tap, server.hub_clock_offset,
    )
    server.DEMO_MODE = False
    server.hub = None
    server.hub_connected = False
    server.hub_tap = None
    server.hub_clock_offset = None
    yield
    (
        server.DEMO_MODE, server.hub, server.hub_connected,
        server.hub_tap, server.hub_clock_offset,
    ) = before


def at_offset(hours: float):
    """Run as though the machine's UTC offset were this.

    A real aware datetime, not a stub with one method: the resync logs as it
    goes, and the log stamps the time, so anything thinner just fails on its
    way past.
    """
    zone = timezone(timedelta(hours=hours))
    return patch.object(server, "local_now", lambda: datetime.now(zone))


def run(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


# -- when it should do nothing ---------------------------------------------


def test_nothing_happens_before_the_first_connection():
    """No handshake has been sent, so there is no offset to have changed."""
    with at_offset(2):
        assert run(server.resync_hub_clock_if_season_changed()) is False


def test_nothing_happens_when_the_offset_is_unchanged():
    server.hub_clock_offset = timedelta(hours=2)
    with at_offset(2), patch.object(server, "connect_to_hub") as connect:
        assert run(server.resync_hub_clock_if_season_changed()) is False
    connect.assert_not_called()


def test_nothing_happens_in_demo_mode():
    """There is no hub to tell, and nothing to displace."""
    server.DEMO_MODE = True
    server.hub_clock_offset = timedelta(hours=1)
    with at_offset(2), patch.object(server, "connect_to_hub") as connect:
        assert run(server.resync_hub_clock_if_season_changed()) is False
    connect.assert_not_called()


# -- when it should act ----------------------------------------------------


@pytest.mark.parametrize(
    "was, becomes, label",
    [
        (1, 2, "March, the one that matters: Comfort would start an hour late"),
        (2, 1, "October: Comfort an hour early, which only costs electricity"),
    ],
)
def test_a_seasonal_change_says_hello_again(was, becomes, label):
    server.hub_clock_offset = timedelta(hours=was)
    calls = []

    async def fake_connect(force=False):
        calls.append(force)

    with at_offset(becomes), patch.object(server, "connect_to_hub", fake_connect):
        assert run(server.resync_hub_clock_if_season_changed()) is True

    # Forced on purpose: without it the attempt is skipped as a duplicate,
    # because the existing client is perfectly healthy. That is the whole point.
    assert calls == [True], label


def test_the_new_offset_is_claimed_before_the_attempt():
    """A failing reconnect must not retry every five seconds until spring."""
    server.hub_clock_offset = timedelta(hours=1)

    async def fails(force=False):
        raise OSError("hub unreachable")

    with at_offset(2), patch.object(server, "connect_to_hub", fails):
        with pytest.raises(OSError):
            run(server.resync_hub_clock_if_season_changed())

    assert server.hub_clock_offset == timedelta(hours=2)
    # And a second pass now finds nothing to do, leaving the hub's own reboot
    # as the backstop it always was.
    with at_offset(2), patch.object(server, "connect_to_hub") as connect:
        assert run(server.resync_hub_clock_if_season_changed()) is False
    connect.assert_not_called()


def test_a_failed_resync_does_not_kill_the_reconnect_loop():
    import inspect

    source = inspect.getsource(server.reconnect_loop)
    assert "resync_hub_clock_if_season_changed()" in source
    # A dead reconnect loop costs the heating entirely; a missed resync costs
    # an hour of schedule twice a year.
    resync = source[source.index("resync_hub_clock_if_season_changed()"):]
    assert "except Exception" in resync


# -- the offset the handshake actually carried ------------------------------


def test_the_offset_recorded_is_the_one_the_handshake_used():
    """Recorded beside the connection, not at some other moment.

    pynobo puts the wall-clock time into HELLO as it starts, so the offset that
    matters is the one in force then — not the one when the loop later happens
    to look.
    """
    import inspect

    source = inspect.getsource(server.connect_to_hub_sync)
    assert "handshake_offset = local_now().utcoffset()" in source
    assert source.index("handshake_offset =") < source.index("new_hub.start()")
    assert "hub_clock_offset = handshake_offset" in source


def test_a_forced_attempt_is_not_skipped_as_a_duplicate():
    import inspect

    source = inspect.getsource(server.connect_to_hub_sync)
    assert "hub is not None and hub_connected and not force" in source


# -- the dangerous half: it must not leak a connection ----------------------


class TestForcingDoesNotLeak:
    """A forced reconnect replaces a *healthy* client, so the old one has to be
    shut down rather than abandoned. Two orphans hold both of the hub's LAN
    slots and lock the owner out of their own heating."""

    def _client(self, name):
        client = MagicMock(name=name)
        client.stop = MagicMock()
        return client

    def _connect(self, force):
        made = []

        def factory(*args, **kwargs):
            client = self._client(f"hub{len(made)}")
            made.append(client)
            return client

        with patch.object(server.pynobo, "nobo", side_effect=factory), \
             patch.object(server, "HubProtocolTap", MagicMock()), \
             patch.object(server, "hub_loop") as loop, \
             patch.object(server, "stop_hub_client") as stop:
            loop.start = MagicMock()
            loop.run = MagicMock()
            thread = threading.Thread(
                target=server.connect_to_hub_sync, args=(force,)
            )
            thread.start()
            thread.join(timeout=10)
            return made, stop

    def test_a_forced_reconnect_stops_the_client_it_replaces(self):
        existing = self._client("existing")
        server.hub = existing
        server.hub_connected = True

        made, stop = self._connect(force=True)

        assert made, "a forced attempt must actually connect"
        assert server.hub is made[0]
        stop.assert_called_once()
        assert stop.call_args[0][0] is existing

    def test_an_unforced_reconnect_still_leaves_the_healthy_one_alone(self):
        existing = self._client("existing")
        server.hub = existing
        server.hub_connected = True

        made, stop = self._connect(force=False)

        assert made == [], "a duplicate attempt must not open a second socket"
        assert server.hub is existing
        stop.assert_not_called()
