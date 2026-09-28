"""The sensors across a hub outage.

Found by pulling the main breaker, 28 September 2026. The Pi came back before
the hub did, so the first evaluation ran while the hub was still unreachable.
The rooms come from the hub, so there were none — and the engine read "no
rooms" as "every room was deleted" and threw away each room's open timers and
raised warnings. Nothing woke it when the hub did connect, so a door left open
stayed unwarned and its rule idle until some sensor next moved.

The hub drops its connection by itself every eighteen hours or so, so this was
not only a power-cut problem.
"""

import asyncio
import threading
from unittest.mock import patch

import pytest

import server
from tests.test_connection_leak import FakeHub, restore_globals  # noqa: F401
from tests.test_sensor_api import (  # noqa: F401 - fixtures are used by name
    add_sensor, client, enable, isolated_sensor_service,
)


def _zone_one_state():
    return server.sensor_automation.states.get("1")


def test_an_outage_keeps_open_timers_and_warnings(client, monkeypatch):
    enable(client, warning=0)
    sensor = add_sensor(client, "Door")
    client.post(f"/api/sensors/{sensor['sensor_id']}/simulate", json={"state": "open"})
    before = _zone_one_state()
    assert before.warning_raised is True
    opened = before.open_started_at

    monkeypatch.setattr(server, "hub_connected", False)
    assert client.portal.call(server.evaluate_sensor_automation) is None
    state = _zone_one_state()
    assert state is not None, "the hub going away made the engine forget the room"
    assert state.warning_raised is True
    assert state.open_started_at == opened

    monkeypatch.setattr(server, "hub_connected", True)
    result = client.portal.call(server.evaluate_sensor_automation)
    assert result is not None
    assert result.zones["1"].warning_raised is True
    # The same cycle carries on; it did not begin again at reconnection.
    assert _zone_one_state().open_started_at == opened


def test_an_outage_leaves_no_deadline_to_spin_on(client, monkeypatch):
    enable(client, warning=0)
    add_sensor(client, "Door")
    monkeypatch.setattr(server, "hub_connected", False)
    client.portal.call(server.evaluate_sensor_automation)
    assert server.sensor_alert_deadline is None


@pytest.fixture
def woken(monkeypatch):
    """A real event loop standing in for the web server's, and a record of
    whether the sensors were asked to look again on it."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    rang = threading.Event()

    async def no_broadcast():
        return None

    monkeypatch.setattr(server, "main_event_loop", loop)
    monkeypatch.setattr(server, "wake_sensor_automation", rang.set)
    monkeypatch.setattr(server, "broadcast_zone_update", no_broadcast)
    yield rang
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


def test_connecting_to_the_hub_wakes_the_sensors(woken):
    server.DEMO_MODE = False
    server.hub_connected = False
    server.hub = None
    with patch.object(server.pynobo, "nobo", side_effect=lambda *a, **k: FakeHub([], "hub")):
        server.connect_to_hub_sync()
    assert server.hub_connected is True
    assert woken.wait(timeout=5), "the sensors were left waiting for rooms after the hub connected"


def test_a_push_from_the_hub_wakes_the_sensors(woken):
    """A mode chosen in the Nobø app arrives as a push, and whether a sensor
    rule may act depends on what the room is running."""
    server.hub_update_callback(None)
    assert woken.wait(timeout=5)
