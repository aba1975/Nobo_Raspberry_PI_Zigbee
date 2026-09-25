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
    """What a sensor watches.

    ``door`` and ``window`` are contacts, and which of the two is a person's
    choice. ``climate`` is a room thermometer — temperature, humidity and
    usually air pressure — and is decided by the hardware, not by anybody: a
    thermometer cannot be relabelled a window, and a contact cannot be turned
    into a thermometer.
    """

    DOOR = "door"
    WINDOW = "window"
    CLIMATE = "climate"

    @property
    def is_contact(self) -> bool:
        return self is not SensorKind.CLIMATE


CONTACT_KINDS = frozenset({SensorKind.DOOR, SensorKind.WINDOW})

# What a room thermometer can plausibly report. The Aqara WSDCGQ11LM is
# specified from -20 to 50 °C and 300 to 1100 hPa; the bounds are a little
# wider so a genuine reading is never thrown away, and narrow enough that a
# corrupted one is.
READING_LIMITS = {
    "temperature": (-40.0, 80.0),
    "humidity": (0.0, 100.0),
    "pressure": (300.0, 1100.0),
}


def valid_reading(name: str, value) -> Optional[float]:
    """A climate reading rounded to one decimal, or None if it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    low, high = READING_LIMITS[name]
    if value != value or not low <= value <= high:  # NaN compares unequal
        return None
    return round(float(value), 1)


def check_kind_change(current: "SensorKind", wanted: "SensorKind") -> None:
    """Refuse turning a contact into a thermometer or the other way round.

    Door and window are the same hardware with a different label, so that
    change is a person's to make. A thermometer is a different device.
    """
    if current.is_contact != wanted.is_contact:
        raise ValueError(
            "A temperature sensor cannot become a door or window sensor, "
            "or the other way round"
        )


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
    #: Room readings from a ``climate`` sensor: °C, % relative humidity and
    #: hPa. Always None for a contact. Each is whatever the device last sent,
    #: and ``last_seen_at`` says how old that is — a stale reading is still
    #: carried here, and it is the automation that decides not to believe it.
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    pressure: Optional[float] = None

    @property
    def is_contact(self) -> bool:
        return self.kind.is_contact

    @property
    def is_climate(self) -> bool:
        return self.kind is SensorKind.CLIMATE


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
