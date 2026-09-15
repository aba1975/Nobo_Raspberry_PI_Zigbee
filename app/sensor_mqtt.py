"""An MQTT transport for the Zigbee2MQTT sensor provider.

The MQTT client library is imported lazily and on purpose.  Contact sensors are
optional, Zigbee is optional within that, and a Nobø-only installation should
neither need the dependency installed nor pay for importing it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_URL = "mqtt://127.0.0.1:1883"

# How long to wait before trying the broker again.  Zigbee2MQTT restarting, or
# a broker coming up after this application, are ordinary events rather than
# faults, so reconnecting quietly is the right behaviour.
RECONNECT_SECONDS = 5.0


class MqttUnavailable(RuntimeError):
    """The MQTT client library is not installed."""


class AiomqttTransport:
    """An ``MqttTransport`` backed by ``aiomqtt``."""

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        client_id: str = "nobo-web-control",
        reconnect_seconds: float = RECONNECT_SECONDS,
    ):
        self._url = url
        self._client_id = client_id
        self._reconnect_seconds = reconnect_seconds
        self._callback: Optional[Callable[[str, bytes], Awaitable[None]]] = None
        self._filters: list[str] = []
        self._client = None
        self._task: Optional[asyncio.Task] = None
        self._ready: Optional[asyncio.Event] = None
        self._closing = False

    def on_message(self, callback) -> None:
        self._callback = callback

    async def connect(self) -> None:
        self._closing = False
        self._ready = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="mqtt-sensor-client")
        # Do not block start-up on a broker that may not be up yet; the loop
        # keeps trying, and until it succeeds the provider simply reports no
        # sensors, which is honest.
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("MQTT broker at %s not ready yet; still trying", self._url)

    async def disconnect(self) -> None:
        self._closing = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def subscribe(self, topic: str) -> None:
        self._filters.append(topic)
        client = self._client
        if client is not None:
            await client.subscribe(topic)

    async def publish(self, topic: str, payload: str) -> None:
        client = self._client
        if client is None:
            raise MqttUnavailable("not connected to the MQTT broker")
        await client.publish(topic, payload.encode("utf-8"))

    async def _run(self) -> None:
        aiomqtt = _import_aiomqtt()
        parsed = urlparse(self._url)
        while not self._closing:
            try:
                async with aiomqtt.Client(
                    hostname=parsed.hostname or "127.0.0.1",
                    port=parsed.port or 1883,
                    username=parsed.username or None,
                    password=parsed.password or None,
                    identifier=self._client_id,
                ) as client:
                    self._client = client
                    for topic in list(self._filters):
                        await client.subscribe(topic)
                    if self._ready is not None:
                        self._ready.set()
                    async for message in client.messages:
                        if self._callback is not None:
                            await self._callback(
                                str(message.topic), bytes(message.payload or b"")
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Deliberately broad, and deliberately not fatal: the broker
                # being away must never take the heating control with it.
                logger.warning("MQTT connection to %s lost: %s", self._url, exc)
            finally:
                self._client = None
            if self._closing:
                break
            await asyncio.sleep(self._reconnect_seconds)


def _import_aiomqtt():
    try:
        import aiomqtt
    except ImportError as exc:
        raise MqttUnavailable(
            "The Zigbee sensor provider needs the 'aiomqtt' package. "
            "Install it with: pip install aiomqtt"
        ) from exc
    return aiomqtt
