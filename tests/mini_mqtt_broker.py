"""A minimal MQTT 3.1.1 broker, so the real transport can be tested.

``fake_zigbee2mqtt.py`` proves the topic contract is understood, but it speaks
to the provider through an in-memory transport, which means ``sensor_mqtt.py``
— the part that actually opens a socket — is never exercised by it.  This is
the missing half: a real broker on a real port, with the genuine ``aiomqtt``
client connecting to it, in the spirit of ``fake_hub.py``.

Only what the transport uses is implemented: CONNECT, SUBSCRIBE, PUBLISH at
QoS 0, PINGREQ and DISCONNECT.  It is a test fixture, not a broker.
"""

from __future__ import annotations

import asyncio
from typing import Optional

CONNECT = 1
CONNACK = 2
PUBLISH = 3
SUBSCRIBE = 8
SUBACK = 9
PINGREQ = 12
PINGRESP = 13
DISCONNECT = 14


def _encode_length(length: int) -> bytes:
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length:
            byte |= 0x80
        out.append(byte)
        if not length:
            return bytes(out)


def _encode_string(value: str) -> bytes:
    data = value.encode("utf-8")
    return len(data).to_bytes(2, "big") + data


def topic_matches(filter_: str, topic: str) -> bool:
    parts = filter_.split("/")
    actual = topic.split("/")
    for index, part in enumerate(parts):
        if part == "#":
            return True
        if index >= len(actual):
            return False
        if part != "+" and part != actual[index]:
            return False
    return len(parts) == len(actual)


class _Session:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.filters: list[str] = []


class MiniMqttBroker:
    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self._host = host
        self._port = port
        self._server: Optional[asyncio.AbstractServer] = None
        self._sessions: list[_Session] = []
        self._retained: dict[str, bytes] = {}
        self.received: list[tuple[str, bytes]] = []

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    @property
    def url(self) -> str:
        return f"mqtt://{self._host}:{self.port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._serve, self._host, self._port
        )

    async def stop(self) -> None:
        for session in list(self._sessions):
            session.writer.close()
        self._sessions.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def drop_connections(self) -> None:
        """Cut every client, to exercise the transport's reconnect."""
        for session in list(self._sessions):
            session.writer.close()
        self._sessions.clear()

    async def publish(self, topic: str, payload: str, retain: bool = True) -> None:
        data = payload.encode("utf-8")
        if retain:
            self._retained[topic] = data
        for session in list(self._sessions):
            if any(topic_matches(item, topic) for item in session.filters):
                await self._send_publish(session, topic, data)

    # -- wire --------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        session = _Session(writer)
        try:
            while True:
                header = await reader.readexactly(1)
                kind = header[0] >> 4
                length = await self._read_length(reader)
                body = await reader.readexactly(length) if length else b""

                if kind == CONNECT:
                    writer.write(bytes([CONNACK << 4, 2, 0, 0]))
                    await writer.drain()
                    self._sessions.append(session)
                elif kind == SUBSCRIBE:
                    await self._on_subscribe(session, body)
                elif kind == PUBLISH:
                    self._on_publish(header[0], body)
                elif kind == PINGREQ:
                    writer.write(bytes([PINGRESP << 4, 0]))
                    await writer.drain()
                elif kind == DISCONNECT:
                    break
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            if session in self._sessions:
                self._sessions.remove(session)
            writer.close()

    @staticmethod
    async def _read_length(reader: asyncio.StreamReader) -> int:
        multiplier = 1
        value = 0
        while True:
            byte = (await reader.readexactly(1))[0]
            value += (byte & 127) * multiplier
            if not byte & 0x80:
                return value
            multiplier *= 128

    async def _on_subscribe(self, session: _Session, body: bytes) -> None:
        packet_id = int.from_bytes(body[:2], "big")
        offset = 2
        granted = bytearray()
        added: list[str] = []
        while offset < len(body):
            size = int.from_bytes(body[offset:offset + 2], "big")
            offset += 2
            topic = body[offset:offset + size].decode("utf-8")
            offset += size + 1  # skip the requested QoS
            session.filters.append(topic)
            added.append(topic)
            granted.append(0)

        payload = packet_id.to_bytes(2, "big") + bytes(granted)
        session.writer.write(
            bytes([SUBACK << 4]) + _encode_length(len(payload)) + payload
        )
        await session.writer.drain()

        for topic, data in list(self._retained.items()):
            if any(topic_matches(item, topic) for item in added):
                await self._send_publish(session, topic, data)

    def _on_publish(self, flags: int, body: bytes) -> None:
        size = int.from_bytes(body[:2], "big")
        topic = body[2:2 + size].decode("utf-8")
        offset = 2 + size
        if (flags >> 1) & 0x03:  # QoS > 0 carries a packet id
            offset += 2
        self.received.append((topic, body[offset:]))

    async def _send_publish(self, session: _Session, topic: str, data: bytes) -> None:
        payload = _encode_string(topic) + data
        try:
            session.writer.write(
                bytes([PUBLISH << 4]) + _encode_length(len(payload)) + payload
            )
            await session.writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
