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

from sensor_mqtt import AiomqttTransport
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
