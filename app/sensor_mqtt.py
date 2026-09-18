"""An MQTT transport for the Zigbee2MQTT sensor provider.

The MQTT client library is imported lazily and on purpose.  Contact sensors are
optional, Zigbee is optional within that, and a Nobø-only installation should
neither need the dependency installed nor pay for importing it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

from sensor_provider import ProviderUnavailable

try:
    import aiomqtt
except ImportError as _exc:  # pragma: no cover - depends on the environment
    aiomqtt = None
    _IMPORT_ERROR = _exc
else:
    _IMPORT_ERROR = None

logger = logging.getLogger(__name__)

DEFAULT_URL = "mqtt://127.0.0.1:1883"

# How long to wait before trying the broker again.  Zigbee2MQTT restarting, or
# a broker coming up after this application, are ordinary events rather than
# faults, so reconnecting quietly is the right behaviour.
RECONNECT_SECONDS = 5.0

# How long to give the broker when asking whether Zigbee2MQTT is there at all.
# bridge/state is retained, so a running Zigbee2MQTT answers within a round
# trip; this is a budget for a slow Pi, not for waiting on anything to start.
PROBE_TIMEOUT_SECONDS = 4.0


@dataclass(frozen=True)
class Zigbee2MqttProbe:
    """What a short look at the broker could establish.

    Three outcomes worth telling apart, because they need different things
    doing: no broker at all (the Zigbee profile is not running), a broker with
    no Zigbee2MQTT behind it (the container is down, or has never started), and
    a Zigbee2MQTT that is there and answering.
    """

    broker_reachable: bool
    bridge_online: bool
    detail: str

    @property
    def usable(self) -> bool:
        return self.broker_reachable and self.bridge_online


async def probe_zigbee2mqtt(
    url: str = DEFAULT_URL,
    *,
    base_topic: str = "zigbee2mqtt",
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> Zigbee2MqttProbe:
    """Ask, briefly, whether there is a Zigbee2MQTT worth switching to.

    This is the closest thing to "is the dongle plugged in?" that this process
    can honestly answer. The adapter is passed to the *zigbee2mqtt* container,
    not to this one, so there is no device node here to look at — and a stick
    plugged into a Pi where Zigbee2MQTT is not running is exactly as useless as
    no stick at all. What can be established is whether the stack that owns the
    radio is alive, which is the question actually being asked.

    Never raises: every failure is an answer, and the caller turns it into a
    sentence somebody can act on.
    """
    if aiomqtt is None:
        return Zigbee2MqttProbe(
            False, False,
            "The MQTT client library is not installed in this image, so real "
            "Zigbee sensors cannot be used.",
        )

    parsed = urlparse(url)
    topic = f"{base_topic}/bridge/state"
    connected = asyncio.Event()

    async def look() -> bool:
        async with aiomqtt.Client(
            hostname=parsed.hostname or "127.0.0.1",
            port=parsed.port or 1883,
            username=parsed.username or None,
            password=parsed.password or None,
            identifier="nobo-web-control-probe",
        ) as client:
            connected.set()
            await client.subscribe(topic)
            async for message in client.messages:
                # Retained, so a Zigbee2MQTT that is running - or one that ran
                # and left its will behind - answers immediately.
                try:
                    document = json.loads(bytes(message.payload or b"").decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # Read exactly as the provider reads it. Being more lenient
                    # here would green-light a broker publishing something the
                    # provider itself cannot understand.
                    continue
                if isinstance(document, dict):
                    return document.get("state") == "online"
            return False

    task = asyncio.create_task(look(), name="zigbee2mqtt-probe")
    try:
        online = await asyncio.wait_for(task, timeout=timeout)
    except asyncio.TimeoutError:
        if connected.is_set():
            return Zigbee2MqttProbe(
                True, False,
                "The MQTT broker is running, but Zigbee2MQTT has never "
                "announced itself on it. It is most likely not started — check "
                "that COMPOSE_PROFILES includes 'zigbee' and that the adapter "
                "path in NOBO_ZIGBEE_ADAPTER exists.",
            )
        return Zigbee2MqttProbe(
            False, False,
            f"Nothing answered at {url}. The Zigbee stack does not appear to "
            "be running on this Pi.",
        )
    except Exception as exc:  # noqa: BLE001 - every failure is a diagnosis
        logger.info("Zigbee2MQTT probe of %s failed: %s", url, exc)
        return Zigbee2MqttProbe(
            False, False,
            f"No MQTT broker answered at {url} ({exc}). The Zigbee stack does "
            "not appear to be running on this Pi.",
        )

    if online:
        return Zigbee2MqttProbe(True, True, "Zigbee2MQTT is running and reachable.")
    return Zigbee2MqttProbe(
        True, False,
        "The MQTT broker is running, but Zigbee2MQTT reports itself offline. "
        "It has stopped, or cannot open the adapter.",
    )


class MqttUnavailable(ProviderUnavailable):
    """The MQTT layer cannot serve this request.

    Deliberately a subclass of ``ProviderUnavailable``: the handlers answer 503
    for that, and a separate hierarchy here meant a broker that had gone away
    produced a 500 instead.
    """


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
        self._on_connection_lost: Optional[Callable[[], Awaitable[None]]] = None

    def on_message(self, callback) -> None:
        self._callback = callback

    def on_connection_lost(self, callback) -> None:
        """Told when the socket goes, which the broker's own will cannot say.

        Zigbee2MQTT publishes a last will, so the *bridge* stopping is visible.
        A broker that dies, or a network that goes, delivers no will at all —
        and without this the last known sensor states would be presented as
        current indefinitely.
        """
        self._on_connection_lost = callback

    async def connect(self) -> None:
        # Checked before the task starts. Left to the task, a missing package
        # killed it on its first pass while connect() went on to log "still
        # trying", and nothing was.
        _require_aiomqtt()
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
        parsed = urlparse(self._url)
        was_connected = False
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
                    was_connected = True
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
            if was_connected and self._on_connection_lost is not None:
                was_connected = False
                try:
                    await self._on_connection_lost()
                except Exception:
                    logger.exception("MQTT disconnect handler failed")
            if self._closing:
                break
            await asyncio.sleep(self._reconnect_seconds)


def _require_aiomqtt():
    if aiomqtt is None:
        raise MqttUnavailable(
            "The Zigbee sensor provider needs the 'aiomqtt' package. "
            "Install it with: pip install aiomqtt"
        ) from _IMPORT_ERROR
    return aiomqtt
