"""Provider-independent contact-sensor types.

Provider identifiers are deliberately opaque.  Code outside a provider should
only use the stable ``sensor_id`` assigned by the application.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Awaitable, Callable, Optional, Protocol, Sequence, Union


class SensorNotFound(KeyError):
    """No sensor with that id.

    Defined here, beside the contract, because every provider raises it and
    ``server.py`` catches it to answer 404. A provider that defines its own
    class of the same name is not caught, and the handler returns 500 instead
    — which is how a successful pairing used to end in "Internal Server Error".
    """


class ProviderUnavailable(RuntimeError):
    """The provider cannot honour this request at the moment.

    Answered as 503. Distinct from :class:`SensorNotFound`, which is a 404,
    and from a programming error, which is a 500 and should stay one.
    """


class ContactState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class SensorKind(str, Enum):
    """Physical opening represented by a contact sensor."""

    DOOR = "door"
    WINDOW = "window"


@dataclass(frozen=True)
class RouterInfo:
    """A mains-powered device that relays for others.

    Not a sensor and never treated as one, but worth surfacing: a network of a
    coordinator and nothing but battery sensors has no mesh in it, and the only
    way to see that from the interface is to be told what is repeating.
    """

    router_id: str
    name: str
    description: Optional[str] = None
    vendor: Optional[str] = None
    model: Optional[str] = None


@dataclass(frozen=True)
class ContactSnapshot:
    sensor_id: str
    provider_id: str
    name: str
    zone_id: Optional[str]
    state: ContactState
    available: bool
    battery: Optional[int]
    changed_at: datetime
    last_seen_at: datetime
    kind: SensorKind = SensorKind.WINDOW
    #: Zigbee link quality (LQI) from the most recent report, 0 to 255, or
    #: None if the device has not been heard from since this started.  It is
    #: the quality of the *last hop* — for a device reporting through a
    #: repeater it describes that leg, not the distance to the coordinator.
    link_quality: Optional[int] = None


class SensorEventKind(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    REMOVED = "removed"
    PAIRING = "pairing"


class PairingOutcome(str, Enum):
    """How a pairing attempt ended."""

    JOINED = "joined"
    IGNORED = "ignored"      # something joined, but it is not a contact sensor
    ROUTER = "router"        # a mains device joined: no sensor, but it repeats
    FAILED = "failed"        # it tried to join and the interview did not finish
    CANCELLED = "cancelled"  # a person closed the window
    EXPIRED = "expired"      # the window closed with nothing having joined


@dataclass(frozen=True)
class PairingStatus:
    """What the pairing window is doing, for the benefit of the interface.

    Deliberately explicit about the difference between *nothing has happened
    yet* and *nothing is going to*: a person holding a button on a battery
    device needs to know whether to keep waiting, and a silent window that has
    quietly expired is the most confusing possible answer.
    """

    supported: bool = False
    active: bool = False
    seconds_remaining: Optional[int] = None
    outcome: Optional[PairingOutcome] = None
    sensor_id: Optional[str] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class SensorEvent:
    kind: SensorEventKind
    sensor_id: str
    snapshot: Optional[ContactSnapshot]


EventCallback = Callable[[SensorEvent], Union[None, Awaitable[None]]]
Unsubscribe = Callable[[], None]


class ContactSensorProvider(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def list(self) -> Sequence[ContactSnapshot]: ...

    async def routers(self) -> Sequence[RouterInfo]:
        """Mains-powered devices relaying for others.  Empty is a valid answer,
        and an important one: it means there is no mesh, only spokes."""
        return ()

    async def create(
        self,
        name: str,
        zone_id: Optional[str] = None,
        kind: SensorKind = SensorKind.WINDOW,
    ) -> ContactSnapshot: ...

    async def pair(
        self,
        name: str,
        zone_id: Optional[str] = None,
        kind: SensorKind = SensorKind.WINDOW,
    ) -> ContactSnapshot: ...

    async def update(
        self,
        sensor_id: str,
        *,
        name: Optional[str] = None,
        kind: Optional[SensorKind] = None,
        zone_id: Optional[str] = None,
        clear_zone: bool = False,
    ) -> ContactSnapshot: ...

    async def remove(self, sensor_id: str) -> None: ...

    def pairing_status(self) -> PairingStatus: ...

    def subscribe(self, callback: EventCallback) -> Unsubscribe: ...


def zigbee2mqtt_endpoint() -> tuple[str, str]:
    """Where Zigbee2MQTT is expected to be, as (broker url, base topic).

    Shared by the provider and by the pre-flight probe so the two cannot drift
    apart: a probe that checked a different broker from the one the provider
    goes on to use would be worse than no probe at all.
    """
    import os

    from sensor_mqtt import DEFAULT_URL

    return (
        os.environ.get("NOBO_MQTT_URL", DEFAULT_URL),
        os.environ.get("NOBO_MQTT_BASE_TOPIC", "zigbee2mqtt"),
    )


def create_provider(name: str, **kwargs) -> ContactSensorProvider:
    """Construct the named provider.

    Neither is tied to whether the *hub* is simulated. Demo sensors beside a
    real hub are how somebody tries the feature before a Zigbee stick arrives,
    and real sensors beside a demo hub are how Zigbee equipment is tested
    without touching a building's heating. What stops invented contacts from
    reaching real heaters is the automation, which stands down for them; see
    ``BlockReason.DEMO_SENSORS``.
    """
    if name == "simulated":
        from sensor_simulated import SimulatedContactSensorProvider

        return SimulatedContactSensorProvider(**kwargs)

    if name == "zigbee2mqtt":
        from sensor_mqtt import AiomqttTransport
        from sensor_persistence import load_zigbee_metadata, save_zigbee_metadata
        from sensor_zigbee2mqtt import Zigbee2MqttContactSensorProvider

        default_url, default_topic = zigbee2mqtt_endpoint()
        url = kwargs.pop("url", None) or default_url
        base_topic = kwargs.pop("base_topic", None) or default_topic
        transport = kwargs.pop("transport", None) or AiomqttTransport(url)
        kwargs.setdefault("load_metadata", load_zigbee_metadata)
        kwargs.setdefault("save_metadata", save_zigbee_metadata)
        return Zigbee2MqttContactSensorProvider(
            transport=transport, base_topic=base_topic, **kwargs
        )

    raise ValueError(f"Unsupported sensor provider: {name}")
