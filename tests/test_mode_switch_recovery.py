"""Switching between demo mode and a real hub must not get stuck.

The bug this covers: connecting to a real hub happens on a background thread
with a timeout. If the user switched back to demo mode before that thread gave
up, the thread's failure handler cleared ``hub_connected`` *after* demo mode had
already marked itself available. Demo mode was then permanently "not connected"
and every zone request answered ``503 Hub not connected`` until the service was
restarted.

Two defences are tested here:
  * a connection attempt carries the configuration generation it started under,
    and refuses to touch the globals if the configuration has changed since;
  * the reconnect loop repairs the flag if demo mode is ever left disconnected.
"""

import asyncio
import threading
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import server
from server import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def restore_globals():
    saved = (
        server.DEMO_MODE,
        server.NOBO_SERIAL,
        server.NOBO_IP,
        server.hub_connected,
        server.hub,
        server.hub_config_generation,
        server.HUB_CONFIG_SOURCE,
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
        server.hub_config_generation,
        server.HUB_CONFIG_SOURCE,
    ) = saved


class TestStaleConnectionCannotClobberState:
    def test_failed_attempt_from_an_old_config_is_ignored(self):
        """A doomed attempt must not mark the *new* configuration disconnected."""
        server.DEMO_MODE = False
        server.hub_connected = False
        started = threading.Event()
        release = threading.Event()

        def slow_and_doomed(*args, **kwargs):
            started.set()
            release.wait(timeout=10)
            raise OSError("no route to host")

        with patch.object(server.pynobo, "nobo", side_effect=slow_and_doomed):
            worker = threading.Thread(target=_swallow(server.connect_to_hub_sync))
            worker.start()
            assert started.wait(timeout=5), "connection attempt never started"

            # The user switches to demo mode while the attempt is still hanging.
            with server.connection_lock:
                server.hub_config_generation += 1
            server.DEMO_MODE = True
            server.hub_connected = True

            release.set()
            worker.join(timeout=10)

        assert server.hub_connected is True, (
            "a superseded connection failure cleared the connected flag"
        )

    def test_successful_attempt_from_an_old_config_is_discarded(self):
        """A hub that arrives after the user moved on is stopped, not adopted."""
        server.DEMO_MODE = False
        server.hub_connected = False
        server.hub = None
        started = threading.Event()
        release = threading.Event()
        stopped = threading.Event()

        class LateHub:
            def stop(self):
                stopped.set()

            def register_callback(self, cb):
                raise AssertionError("a superseded hub must not be wired up")

        def slow_success(*args, **kwargs):
            started.set()
            release.wait(timeout=10)
            return LateHub()

        with patch.object(server.pynobo, "nobo", side_effect=slow_success):
            worker = threading.Thread(target=_swallow(server.connect_to_hub_sync))
            worker.start()
            assert started.wait(timeout=5)

            with server.connection_lock:
                server.hub_config_generation += 1
            server.DEMO_MODE = True
            server.hub_connected = True

            release.set()
            worker.join(timeout=10)

        assert stopped.is_set(), "the superseded hub connection was left running"
        assert server.hub is None, "a superseded hub was adopted as the live one"
        assert server.hub_connected is True

    def test_current_config_failure_is_still_reported(self):
        """The guard must not hide a genuine failure of the current settings."""
        server.DEMO_MODE = False
        server.hub_connected = True

        with patch.object(server.pynobo, "nobo", side_effect=OSError("unreachable")):
            with pytest.raises(OSError):
                server.connect_to_hub_sync()

        assert server.hub_connected is False
        assert server.hub is None


class TestSwitchingBackToDemoWorks:
    def test_apply_hub_config_bumps_the_generation(self):
        server.DEMO_MODE = False
        before = server.hub_config_generation

        asyncio.run(server.apply_hub_config(True, "123456789012", "10.0.0.100"))

        assert server.hub_config_generation > before
        assert server.DEMO_MODE is True
        assert server.hub_connected is True, "demo mode must report itself available"

    def test_demo_mode_is_connected_after_a_failed_hub_attempt(self):
        """The end-to-end shape of the reported bug."""
        server.DEMO_MODE = True
        server.hub_connected = True

        with patch.object(server.pynobo, "nobo", side_effect=OSError("unreachable")):
            asyncio.run(server.apply_hub_config(False, "123123123123", "192.0.2.10"))
            # The background attempt is still running here; wait it out.
            if server.hub_thread is not None:
                server.hub_thread.join(timeout=15)
            assert server.hub_connected is False

            asyncio.run(server.apply_hub_config(True, "123123123123", "192.0.2.10"))

        assert server.DEMO_MODE is True
        assert server.hub_connected is True, (
            "switching back to demo mode left the app stuck on 'Hub not connected'"
        )

    def test_zones_are_served_again_after_switching_back(self, client):
        server.DEMO_MODE = True
        server.hub_connected = True
        # Keep in sync with TEST_SESSION_ID in conftest.py; importing from
        # conftest is fragile under pytest's import modes.
        client.cookies.set("session_id", "pytest-fixed-session-id")

        asyncio.run(server.apply_hub_config(True, "123456789012", "10.0.0.100"))

        response = client.get("/api/zones")
        assert response.status_code == 200
        assert len(response.json()["zones"]) > 0


class TestReconnectLoopRepairsDemoMode:
    def test_demo_branch_restores_the_connected_flag(self):
        """A stuck flag must heal itself rather than needing a restart."""
        server.DEMO_MODE = True
        server.hub_connected = False

        async def run_one_pass():
            # Let the loop run a single iteration, then stop it.
            task = asyncio.ensure_future(server.reconnect_loop())
            await asyncio.sleep(6)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run_one_pass())

        assert server.hub_connected is True


def _swallow(fn):
    """Run fn in a thread, ignoring the exception it deliberately raises."""

    def runner():
        try:
            fn()
        except Exception:
            pass

    return runner
