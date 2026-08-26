"""Connecting to the hub must never leak a client.

The bug this covers was found against real hardware. Switching the running
server from demo mode to a real hub does two things in quick succession: it
clears ``hub_connected`` and then starts a connection attempt. The reconnect
loop wakes every five seconds, and if it happened to look in between, it saw a
disconnected hub and started an attempt of its own. Both attempts carried the
same configuration generation, so the generation guard let both through. Both
succeeded, the second assigned itself to ``server.hub``, and the first was
simply forgotten — still holding an open socket, still running its keep-alive
task. Nothing ever closed it, and because the keep-alive kept answering, the
hub never timed it out either.

That matters more than an ordinary leak. ``API_Nobo.pdf`` section 5.8 says the
hub accepts **two** LAN connections. A couple of orphans and the user cannot
reach their own heating from anywhere on the network, with no error to explain
why — the handshake has no "busy" reject code, so a hub at its limit simply
stops answering.

Observed on a live hub: after one switch to production mode, ``ss -tnp`` showed
two sockets to port 27779 where there should have been one.
"""

import threading

from unittest.mock import patch

import pytest

import server


@pytest.fixture(autouse=True)
def restore_globals():
    saved = (
        server.DEMO_MODE,
        server.NOBO_SERIAL,
        server.NOBO_IP,
        server.hub_connected,
        server.hub,
        server.hub_tap,
        server.hub_config_generation,
    )
    yield
    thread = server.hub_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=15)
    (
        server.DEMO_MODE,
        server.NOBO_SERIAL,
        server.NOBO_IP,
        server.hub_connected,
        server.hub,
        server.hub_tap,
        server.hub_config_generation,
    ) = saved


class FakeHub:
    """A pynobo stand-in that records whether it was shut down."""

    def __init__(self, registry, name):
        self.name = name
        self.stopped = threading.Event()
        self.callback_registered = False
        registry.append(self)

    # HubProtocolTap.attach replaces this attribute.
    @staticmethod
    def response_handler(response):
        return None

    async def start(self):
        return None

    async def stop(self):
        self.stopped.set()

    def register_callback(self, cb):
        self.callback_registered = True


def _swallow(fn):
    def runner():
        try:
            fn()
        except Exception:
            pass

    return runner


class TestConcurrentAttemptsDoNotLeak:
    def test_a_second_attempt_does_not_open_a_second_connection(self):
        """The classic race: a config change and the reconnect loop overlap.

        Whatever the interleaving, the hub must end up with exactly one client
        that is still open.
        """
        server.DEMO_MODE = False
        server.hub_connected = False
        server.hub = None
        created = []

        def factory(*args, **kwargs):
            return FakeHub(created, f"hub{len(created)}")

        with patch.object(server.pynobo, "nobo", side_effect=factory):
            workers = [
                threading.Thread(target=_swallow(server.connect_to_hub_sync))
                for _ in range(2)
            ]
            for w in workers:
                w.start()
            for w in workers:
                w.join(timeout=15)
                assert not w.is_alive(), "a connection attempt hung"

        live = [h for h in created if not h.stopped.is_set()]
        assert len(live) == 1, (
            f"{len(live)} hub connections left open by two overlapping attempts; "
            "each orphan holds one of the hub's two LAN slots for ever"
        )
        assert server.hub is live[0], "the live client is not the one in use"
        assert server.hub_connected is True

    def test_many_overlapping_attempts_still_leave_one(self):
        """Backoff can stack up several attempts. None of them may be orphaned."""
        server.DEMO_MODE = False
        server.hub_connected = False
        server.hub = None
        created = []

        def factory(*args, **kwargs):
            return FakeHub(created, f"hub{len(created)}")

        with patch.object(server.pynobo, "nobo", side_effect=factory):
            workers = [
                threading.Thread(target=_swallow(server.connect_to_hub_sync))
                for _ in range(6)
            ]
            for w in workers:
                w.start()
            for w in workers:
                w.join(timeout=20)
                assert not w.is_alive()

        live = [h for h in created if not h.stopped.is_set()]
        assert len(live) == 1, f"{len(live)} connections left open by six attempts"


class TestReplacedClientIsClosed:
    def test_reconnecting_over_a_dead_client_closes_it(self):
        """A dropped socket leaves the old client installed; replacing it must close it.

        pynobo does not tear itself down when the connection dies, so without
        this the old client's socket lingers half-open on the hub and the
        reconnect quietly costs a slot instead of reusing one.
        """
        server.DEMO_MODE = False
        created = []
        stale = FakeHub(created, "stale")

        server.hub = stale
        # The socket died, so the flag is down but the object is still installed.
        server.hub_connected = False

        def factory(*args, **kwargs):
            return FakeHub(created, "fresh")

        with patch.object(server.pynobo, "nobo", side_effect=factory):
            server.connect_to_hub_sync()

        assert stale.stopped.is_set(), (
            "the replaced hub client was abandoned rather than closed"
        )
        assert server.hub is not stale
        assert server.hub.stopped.is_set() is False
        assert server.hub_connected is True

    def test_the_replacement_is_wired_up_for_updates(self):
        """Closing the old client must not cost us the new one's callback."""
        server.DEMO_MODE = False
        created = []
        server.hub = FakeHub(created, "stale")
        server.hub_connected = False

        with patch.object(
            server.pynobo, "nobo", side_effect=lambda *a, **k: FakeHub(created, "fresh")
        ):
            server.connect_to_hub_sync()

        assert server.hub.callback_registered, (
            "the new hub connection was never registered for push updates"
        )


class TestAlreadyConnectedIsLeftAlone:
    def test_a_duplicate_attempt_is_skipped_entirely(self):
        """If a healthy connection is already installed, do not build another."""
        server.DEMO_MODE = False
        created = []
        existing = FakeHub(created, "existing")
        server.hub = existing
        server.hub_connected = True

        with patch.object(
            server.pynobo, "nobo", side_effect=lambda *a, **k: FakeHub(created, "extra")
        ):
            server.connect_to_hub_sync()

        assert len(created) == 1, "a redundant hub connection was opened"
        assert server.hub is existing, "a healthy connection was replaced"
        assert not existing.stopped.is_set(), "a healthy connection was closed"


class TestSupersededAttemptStillDiscarded:
    def test_the_generation_guard_survives_the_new_locking(self):
        """The older defence must keep working alongside the new one."""
        server.DEMO_MODE = False
        server.hub_connected = False
        server.hub = None
        created = []
        started = threading.Event()
        release = threading.Event()

        def slow(*args, **kwargs):
            started.set()
            release.wait(timeout=10)
            return FakeHub(created, "late")

        with patch.object(server.pynobo, "nobo", side_effect=slow):
            worker = threading.Thread(target=_swallow(server.connect_to_hub_sync))
            worker.start()
            assert started.wait(timeout=5)

            with server.connection_lock:
                server.hub_config_generation += 1
            server.DEMO_MODE = True
            server.hub_connected = True

            release.set()
            worker.join(timeout=15)

        assert created[0].stopped.is_set(), "a superseded connection was left open"
        assert server.hub is None


class TestALateFailureCannotDisconnectALiveHub:
    """The bug behind a once-in-three-runs failure across the whole suite.

    A connection attempt against an unreachable address does not fail quickly —
    it sits in a TCP timeout for up to thirty seconds on a background thread,
    long after the request that started it returned. Meanwhile the application
    carries on, and something else establishes a working connection.

    When the doomed attempt finally gives up, its error handler used to clear
    ``hub``, ``hub_tap`` and ``hub_connected`` — disconnecting a hub it had
    never owned. Every subsequent request answered ``503 Hub not connected``
    until something reconnected.

    The existing generation guard cannot catch this. Generation only moves when
    the *configuration* changes, and here it has not: the user set one hub, it
    was unreachable, and a connection to that same configuration succeeded by
    another route. Both attempts carry the same generation, so the guard waves
    the failure through.

    In the test suite this showed up as an unrelated test failing at random,
    because ``test_hub_config`` deliberately points the app at ``192.0.2.10``
    (TEST-NET-1, guaranteed unroutable) and leaves the attempt timing out while
    later tests run against a fake hub. In a cabin it shows up as heating that
    reports itself offline while it is demonstrably online.
    """

    def test_a_doomed_attempt_does_not_clear_a_connection_made_beside_it(self):
        server.DEMO_MODE = False
        server.hub = None
        server.hub_connected = False

        started = threading.Event()
        release = threading.Event()
        created = []

        def doomed(*args, **kwargs):
            started.set()
            release.wait(timeout=10)
            raise OSError("no route to host")

        with patch.object(server.pynobo, "nobo", side_effect=doomed):
            worker = threading.Thread(target=_swallow(server.connect_to_hub_sync))
            worker.start()
            assert started.wait(timeout=5), "the doomed attempt never started"

            # While it hangs, a working connection is established by another
            # route — the fixture in test_real_hub_endpoints does exactly this.
            live = FakeHub(created, "live")
            with server.connection_lock:
                server.hub = live
                server.hub_connected = True

            release.set()
            worker.join(timeout=15)
            assert not worker.is_alive()

        assert server.hub is live, (
            "a failed attempt disconnected a hub it never owned"
        )
        assert server.hub_connected is True, (
            "a failed attempt reported the system offline while it was online"
        )
        assert not live.stopped.is_set(), "the live connection was stopped"

    def test_the_tap_belonging_to_the_live_hub_survives(self):
        """`hub_tap` is cleared on the same path and holds the raw wire rows.

        Losing it silently breaks device editing, because component updates
        would then be rebuilt from pynobo's mangled copy.
        """
        server.DEMO_MODE = False
        server.hub = None
        server.hub_connected = False

        started = threading.Event()
        release = threading.Event()

        def doomed(*args, **kwargs):
            started.set()
            release.wait(timeout=10)
            raise OSError("no route to host")

        with patch.object(server.pynobo, "nobo", side_effect=doomed):
            worker = threading.Thread(target=_swallow(server.connect_to_hub_sync))
            worker.start()
            assert started.wait(timeout=5)

            live_tap = server.HubProtocolTap()
            with server.connection_lock:
                server.hub = FakeHub([], "live")
                server.hub_tap = live_tap
                server.hub_connected = True

            release.set()
            worker.join(timeout=15)

        assert server.hub_tap is live_tap, "the live hub's protocol tap was discarded"

    def test_a_genuine_failure_with_nothing_connected_is_still_reported(self):
        """The guard must not turn into "never report a failure".

        If nothing is connected, a failure is the whole truth and the UI needs
        to hear it.
        """
        server.DEMO_MODE = False
        server.hub = None
        server.hub_connected = True

        with patch.object(server.pynobo, "nobo", side_effect=OSError("unreachable")):
            with pytest.raises(OSError):
                server.connect_to_hub_sync()

        assert server.hub_connected is False
        assert server.hub is None
