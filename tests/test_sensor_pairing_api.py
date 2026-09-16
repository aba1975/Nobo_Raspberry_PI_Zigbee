"""The pairing window over HTTP, and choosing which provider runs.

Pairing is the one part of this feature where somebody is standing at a door
with a battery device in their hand, so every outcome has to be distinguishable
over the API — and in particular "nothing has happened yet" has to be
distinguishable from "nothing is going to".
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

import sensor_persistence
import server
from sensor_automation import SensorAutomation
from sensor_persistence import SensorSettings
from sensor_provider import PairingOutcome, PairingStatus
from sensor_zigbee2mqtt import ProviderUnavailable, Zigbee2MqttContactSensorProvider
from tests.fake_zigbee2mqtt import (
    FakeBroker, FakeTransport, FakeZigbee2Mqtt, contact_device,
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    original_settings = server.sensor_settings
    original_automation = server.sensor_automation
    original_zones = copy.deepcopy(server.DEMO_ZONES)
    server.sensor_settings = SensorSettings()
    server.sensor_provider = None
    server.sensor_unsubscribe = None
    server.sensor_snapshots = []
    server.sensor_zone_aggregates = {}
    server.sensor_wakeup = None
    server.sensor_automation = SensorAutomation(
        states={},
        save=sensor_persistence.save_automation_state,
        commands=server.SensorHeatingCommands(),
    )
    yield
    server.DEMO_ZONES[:] = original_zones
    server.sensor_settings = original_settings
    server.sensor_automation = original_automation
    server.sensor_provider = None
    server.sensor_unsubscribe = None
    server.sensor_snapshots = []
    server.sensor_zone_aggregates = {}


@pytest.fixture
def client():
    with TestClient(server.app) as test_client:
        test_client.cookies.set("session_id", "pytest-fixed-session-id")
        yield test_client


def enable(client, provider="simulated"):
    response = client.put(
        "/api/sensors/settings", json={"enabled": True, "provider": provider}
    )
    assert response.status_code == 200, response.text
    return response.json()


# -- choosing a provider ---------------------------------------------------


def test_the_settings_say_which_providers_exist(client):
    body = enable(client)

    assert body["provider"] == "simulated"
    assert set(body["providers"]) == set(sensor_persistence.PROVIDERS)


def test_an_unknown_provider_is_refused(client):
    response = client.put(
        "/api/sensors/settings", json={"enabled": True, "provider": "hue"}
    )

    assert response.status_code == 400
    assert "provider must be one of" in response.json()["detail"]


def test_the_provider_is_remembered(client):
    enable(client)

    assert sensor_persistence.load_sensor_settings().provider == "simulated"


def test_omitting_the_provider_keeps_the_current_one(client):
    enable(client)

    response = client.put("/api/sensors/settings", json={"enabled": True})

    assert response.json()["provider"] == "simulated"


# -- the window, with a simulator behind it --------------------------------


def test_the_simulator_reports_no_pairing_window(client):
    body = enable(client)

    # Unsupported, not merely inactive: the interface offers the simulator's
    # create form rather than a progress display that would never move.
    assert body["pairing"]["supported"] is False
    assert body["pairing"]["active"] is False


def test_opening_a_window_on_the_simulator_is_refused(client):
    enable(client)

    response = client.post("/api/sensors/pairing", json={"seconds": 60})

    # 501 is this codebase's answer for "this build cannot do that", and the
    # Classic capability map keys off it.
    assert response.status_code == 501
    assert "simulated sensor" in response.json()["detail"].lower()


# -- the window, with a radio behind it ------------------------------------


@pytest.fixture
def zigbee(monkeypatch):
    """Install a Zigbee provider driven by the Zigbee2MQTT fake."""
    broker = FakeBroker()
    z2m = FakeZigbee2Mqtt(broker)
    provider = Zigbee2MqttContactSensorProvider(
        transport=FakeTransport(broker),
        load_metadata=lambda: {},
        save_metadata=lambda _data: None,
    )
    monkeypatch.setattr(
        server, "create_provider", lambda *args, **kwargs: provider
    )
    return provider, z2m


def test_a_radio_reports_a_real_window(client, zigbee):
    provider, z2m = zigbee
    body = enable(client, provider="zigbee2mqtt")

    assert body["pairing"]["supported"] is True
    assert body["pairing"]["active"] is False


def test_opening_and_closing_a_window(client, zigbee):
    provider, z2m = zigbee
    enable(client, provider="zigbee2mqtt")
    client.portal.call(z2m.go_online)

    opened = client.post("/api/sensors/pairing", json={"seconds": 120})
    assert opened.status_code == 200, opened.text
    assert opened.json()["active"] is True
    assert opened.json()["seconds_remaining"] == 120

    closed = client.delete("/api/sensors/pairing")
    assert closed.status_code == 200
    assert closed.json()["active"] is False
    assert closed.json()["outcome"] == PairingOutcome.CANCELLED.value


def test_a_window_longer_than_zigbee_allows_is_refused(client, zigbee):
    enable(client, provider="zigbee2mqtt")

    response = client.post("/api/sensors/pairing", json={"seconds": 3600})

    assert response.status_code == 422


def test_pairing_needs_the_bridge(client, zigbee):
    enable(client, provider="zigbee2mqtt")
    # Zigbee2MQTT never came up.

    response = client.post("/api/sensors/pairing", json={"seconds": 60})

    assert response.status_code == 503
    assert "not connected" in response.json()["detail"].lower()


def test_the_outcome_is_readable_while_waiting(client, zigbee):
    provider, z2m = zigbee
    enable(client, provider="zigbee2mqtt")
    client.portal.call(z2m.go_online)
    client.post("/api/sensors/pairing", json={"seconds": 120})

    status = client.get("/api/sensors/pairing")

    assert status.status_code == 200
    assert status.json()["active"] is True
    assert status.json()["outcome"] is None


# -- auth ------------------------------------------------------------------


def test_pairing_needs_a_session(client):
    enable(client)
    cookies = dict(client.cookies)
    client.cookies.clear()
    try:
        for call in (
            lambda: client.post("/api/sensors/pairing", json={"seconds": 60}),
            lambda: client.delete("/api/sensors/pairing"),
            lambda: client.get("/api/sensors/pairing"),
        ):
            assert call().status_code in (302, 401, 403)
    finally:
        client.cookies.update(cookies)


# -- what the review found -------------------------------------------------


def test_editing_a_sensor_the_provider_does_not_hold_is_404_not_500(client, zigbee):
    """Reachable straight off a successful pairing.

    The sheet reports "joined" from bridge/event, which can arrive before
    bridge/devices has registered the device — so Save can name an id the
    provider does not yet hold. A 500 there reads as a crash at the end of a
    pairing that actually worked.
    """
    enable(client, provider="zigbee2mqtt")
    missing = "0x00158d000000dead"

    assert client.put(f"/api/sensors/{missing}",
                      json={"name": "Kitchen window"}).status_code == 404
    assert client.delete(f"/api/sensors/{missing}").status_code == 404


def test_a_broker_that_has_gone_is_503_not_500(client, zigbee, monkeypatch):
    from sensor_mqtt import MqttUnavailable

    provider, z2m = zigbee
    enable(client, provider="zigbee2mqtt")
    client.portal.call(z2m.go_online)

    async def gone(*_args, **_kwargs):
        raise MqttUnavailable("not connected to the MQTT broker")

    monkeypatch.setattr(provider._transport, "publish", gone)

    response = client.post("/api/sensors/pairing", json={"seconds": 60})

    # Unavailable, not broken: the caller should be told to try again, and the
    # log should not fill with tracebacks every time the broker restarts.
    assert response.status_code == 503


# -- what the user found on the hardware -----------------------------------


def test_a_real_sensors_battery_cannot_be_typed_in(client, zigbee):
    """The hub is simulated here and the sensors are not.

    Gating the simulator's controls on demo mode conflated the two, so a real
    Aqara offered an editable battery percentage — a number the hardware never
    reported.
    """
    provider, z2m = zigbee
    body = enable(client, provider="zigbee2mqtt")

    assert body["simulated"] is False

    response = client.post(
        f"/api/sensors/{'0x00158d008c8bc4f2'}/simulate", json={"battery": 50}
    )
    assert response.status_code == 501
    assert "report their own state" in response.json()["detail"]


def test_a_simulated_sensor_can_still_be_driven(client):
    body = enable(client)
    assert body["simulated"] is True

    created = client.post(
        "/api/sensors", json={"name": "Kitchen window", "zone_id": "5"}
    )
    assert created.status_code == 200, created.text
    sensor_id = created.json()["sensor_id"]

    response = client.post(f"/api/sensors/{sensor_id}/simulate", json={"battery": 50})
    assert response.status_code == 200
    assert response.json()["battery"] == 50


def test_a_sensor_that_will_not_leave_is_reported_not_silently_kept(client, zigbee):
    """What the user hit: Delete appeared to work and the sensor stayed.

    Zigbee2MQTT asks the device to leave first, and a battery contact sensor
    is asleep almost all of the time.
    """
    provider, z2m = zigbee
    enable(client, provider="zigbee2mqtt")
    client.portal.call(z2m.go_online)
    client.portal.call(z2m.add_device, contact_device("0x00158d008c8bc4f2"))
    z2m.refuse_removal = True

    response = client.delete("/api/sensors/0x00158d008c8bc4f2")

    assert response.status_code == 409
    assert "rejoin later" in response.json()["detail"]
    assert len(client.get("/api/sensors").json()["sensors"]) == 1


def test_a_forced_removal_gets_rid_of_it(client, zigbee):
    provider, z2m = zigbee
    enable(client, provider="zigbee2mqtt")
    client.portal.call(z2m.go_online)
    client.portal.call(z2m.add_device, contact_device("0x00158d008c8bc4f2"))
    z2m.refuse_removal = True

    response = client.delete("/api/sensors/0x00158d008c8bc4f2?force=true")

    assert response.status_code == 200
    assert client.get("/api/sensors").json()["sensors"] == []
