"""The real MQTT transport, against a real broker on a real socket.

``test_sensor_zigbee2mqtt.py`` proves the topic contract is understood but
talks through an in-memory transport, so ``sensor_mqtt.py`` — the part that
opens a socket — is never exercised there.  These tests close that gap with the
genuine ``aiomqtt`` client connecting to ``tests/mini_mqtt_broker.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio

from sensor_mqtt import AiomqttTransport, probe_zigbee2mqtt
from sensor_provider import ContactState
from sensor_zigbee2mqtt import Zigbee2MqttContactSensorProvider
from tests.fake_zigbee2mqtt import contact_device
from tests.mini_mqtt_broker import MiniMqttBroker

pytest.importorskip("aiomqtt")

ADDRESS = "0x00158d008c8bc4f2"


async def _settle(times: int = 8) -> None:
    for _ in range(times):
        await asyncio.sleep(0.02)


class Recorder:
    """Collects what the transport delivers."""

    def __init__(self):
        self.messages: list[tuple[str, bytes]] = []

    async def __call__(self, topic: str, payload: bytes) -> None:
        self.messages.append((topic, payload))

    @property
    def topics(self) -> list[str]:
        return [topic for topic, _payload in self.messages]


@pytest_asyncio.fixture
async def broker():
    server = MiniMqttBroker()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_the_transport_connects_and_receives(broker):
    received = Recorder()
    transport = AiomqttTransport(broker.url)
    transport.on_message(received)

    await transport.connect()
    await transport.subscribe("zigbee2mqtt/#")
    await _settle()
    await broker.publish("zigbee2mqtt/kitchen", '{"contact":true}')
    await _settle()
    await transport.disconnect()

    assert ("zigbee2mqtt/kitchen", b'{"contact":true}') in received.messages


@pytest.mark.asyncio
async def test_the_transport_publishes(broker):
    transport = AiomqttTransport(broker.url)
    transport.on_message(Recorder())

    await transport.connect()
    await _settle()
    await transport.publish("zigbee2mqtt/bridge/request/permit_join",
                            json.dumps({"time": 254}))
    await _settle()
    await transport.disconnect()

    assert broker.received == [
        ("zigbee2mqtt/bridge/request/permit_join", b'{"time": 254}')
    ]


@pytest.mark.asyncio
async def test_retained_messages_arrive_on_subscribe(broker):
    """A restart must get current state without waiting for something to move."""
    await broker.publish("zigbee2mqtt/bridge/state", '{"state":"online"}')
    received = Recorder()
    transport = AiomqttTransport(broker.url)
    transport.on_message(received)

    await transport.connect()
    await transport.subscribe("zigbee2mqtt/bridge/state")
    await _settle()
    await transport.disconnect()

    assert "zigbee2mqtt/bridge/state" in received.topics


@pytest.mark.asyncio
async def test_a_dropped_connection_is_re_established(broker):
    received = Recorder()
    transport = AiomqttTransport(broker.url, reconnect_seconds=0.05)
    transport.on_message(received)

    await transport.connect()
    await transport.subscribe("zigbee2mqtt/#")
    await _settle()

    # Zigbee2MQTT restarting, or a broker coming back, is an ordinary event.
    await broker.drop_connections()
    for _ in range(60):
        await asyncio.sleep(0.05)
        await broker.publish("zigbee2mqtt/kitchen", '{"contact":false}')
        if "zigbee2mqtt/kitchen" in received.topics:
            break

    await transport.disconnect()
    assert "zigbee2mqtt/kitchen" in received.topics


@pytest.mark.asyncio
async def test_a_missing_broker_does_not_stop_start_up(broker):
    """The heating must come up even if the broker never does."""
    url = broker.url
    await broker.stop()
    transport = AiomqttTransport(url, reconnect_seconds=0.05)
    transport.on_message(Recorder())

    provider = Zigbee2MqttContactSensorProvider(
        transport=transport,
        load_metadata=lambda: {},
        save_metadata=lambda _data: None,
    )
    await asyncio.wait_for(provider.start(), timeout=8)

    # Started, with nothing to report, which is the honest answer.
    assert await provider.list() == []
    await provider.stop()


@pytest.mark.asyncio
async def test_end_to_end_over_a_real_socket(broker):
    """Real client, real socket, and the payloads a real Aqara sent."""
    transport = AiomqttTransport(broker.url)
    provider = Zigbee2MqttContactSensorProvider(
        transport=transport,
        load_metadata=lambda: {},
        save_metadata=lambda _data: None,
    )
    await provider.start()
    await _settle()

    await broker.publish("zigbee2mqtt/bridge/state", '{"state":"online"}')
    await broker.publish(
        "zigbee2mqtt/bridge/devices", json.dumps([contact_device(ADDRESS)])
    )
    await _settle()
    await broker.publish(
        f"zigbee2mqtt/{ADDRESS}", '{"contact":false,"linkquality":105}'
    )
    await _settle()

    sensors = await provider.list()
    assert [item.sensor_id for item in sensors] == [ADDRESS]
    assert sensors[0].state is ContactState.OPEN
    assert sensors[0].available is True
    assert sensors[0].battery is None

    await broker.publish(
        f"zigbee2mqtt/{ADDRESS}", '{"contact":true,"linkquality":98}'
    )
    await _settle()
    assert (await provider.list())[0].state is ContactState.CLOSED

    # And the request path, over the same socket.
    await provider.begin_pairing(254)
    await _settle()
    assert (
        "zigbee2mqtt/bridge/request/permit_join",
        b'{"time": 254}',
    ) in broker.received

    await provider.stop()


# ---------------------------------------------------------------------------
# Asking whether there is a Zigbee2MQTT to switch to
# ---------------------------------------------------------------------------
#
# The adapter is passed to the zigbee2mqtt container, not to this process, so
# "is the dongle plugged in?" cannot be answered here at all. What can be
# answered is whether the stack that owns the radio is alive - which is the
# same question in every way that matters, because a stick in a Pi with
# Zigbee2MQTT stopped is as useless as no stick.


@pytest.mark.asyncio
async def test_a_running_zigbee2mqtt_is_recognised(broker):
    await broker.publish("zigbee2mqtt/bridge/state", json.dumps({"state": "online"}))
    probe = await probe_zigbee2mqtt(broker.url, timeout=5)
    assert probe.usable
    assert probe.broker_reachable and probe.bridge_online


@pytest.mark.asyncio
async def test_a_broker_with_no_zigbee2mqtt_behind_it_is_not_usable(broker):
    """The case this exists for: Mosquitto up, Zigbee2MQTT never started.

    Nothing is retained on bridge/state, so the probe waits and then has to
    say so. Reporting the broker as reachable is the useful half of the
    answer - it tells the reader the fault is the Zigbee container, not the
    network.
    """
    probe = await probe_zigbee2mqtt(broker.url, timeout=1)
    assert not probe.usable
    assert probe.broker_reachable
    assert not probe.bridge_online
    assert "Zigbee2MQTT" in probe.detail


@pytest.mark.asyncio
async def test_zigbee2mqtt_having_stopped_is_not_usable(broker):
    """Its last will leaves "offline" retained, which must not read as ready."""
    await broker.publish("zigbee2mqtt/bridge/state", json.dumps({"state": "offline"}))
    probe = await probe_zigbee2mqtt(broker.url, timeout=5)
    assert not probe.usable
    assert probe.broker_reachable
    assert not probe.bridge_online


@pytest.mark.asyncio
async def test_no_broker_at_all_is_reported_as_such():
    """Port 1 is reserved and nothing listens on it, so this is a refusal
    rather than a timeout - the shape an installation with no Zigbee profile
    running actually has."""
    probe = await probe_zigbee2mqtt("mqtt://127.0.0.1:1", timeout=5)
    assert not probe.usable
    assert not probe.broker_reachable
    assert not probe.bridge_online


@pytest.mark.asyncio
async def test_a_custom_base_topic_is_honoured(broker):
    """NOBO_MQTT_BASE_TOPIC moves every topic, and a probe that ignored it
    would declare a perfectly good installation unusable."""
    await broker.publish("attic/bridge/state", json.dumps({"state": "online"}))
    assert (await probe_zigbee2mqtt(broker.url, base_topic="attic", timeout=5)).usable
    assert not (await probe_zigbee2mqtt(broker.url, timeout=1)).usable


@pytest.mark.asyncio
async def test_a_payload_the_provider_could_not_read_is_not_green_lit(broker):
    """The provider json-decodes bridge/state and ignores anything else. A
    probe that accepted a bare "online" would wave through a broker publishing
    something the provider itself cannot act on."""
    await broker.publish("zigbee2mqtt/bridge/state", "online")
    probe = await probe_zigbee2mqtt(broker.url, timeout=1)
    assert not probe.usable
    assert probe.broker_reachable


def test_the_probe_and_the_provider_read_the_same_environment():
    """A probe that checked a different broker from the one the provider then
    used would be worse than no probe at all."""
    import os

    from sensor_provider import zigbee2mqtt_endpoint

    previous = (os.environ.get("NOBO_MQTT_URL"), os.environ.get("NOBO_MQTT_BASE_TOPIC"))
    try:
        os.environ["NOBO_MQTT_URL"] = "mqtt://example.invalid:1884"
        os.environ["NOBO_MQTT_BASE_TOPIC"] = "attic"
        assert zigbee2mqtt_endpoint() == ("mqtt://example.invalid:1884", "attic")
    finally:
        for key, value in zip(("NOBO_MQTT_URL", "NOBO_MQTT_BASE_TOPIC"), previous):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
