"""Provider-independent contact-sensor types.

Provider identifiers are deliberately opaque.  Code outside a provider should
only use the stable ``sensor_id`` assigned by the application.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Awaitable, Callable, Optional, Protocol, Sequence, Union


class ContactState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class SensorKind(str, Enum):
    """Physical opening represented by a contact sensor."""

    DOOR = "door"
    WINDOW = "window"


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


class SensorEventKind(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    REMOVED = "removed"


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

    def subscribe(self, callback: EventCallback) -> Unsubscribe: ...


def create_provider(name: str, *, demo_mode: bool, **kwargs) -> ContactSensorProvider:
    """Construct a provider without allowing simulation to masquerade as hardware."""
    if name == "simulated":
        if not demo_mode:
            raise RuntimeError(
                "The simulated sensor provider is available only in demo mode"
            )
        from sensor_simulated import SimulatedContactSensorProvider

        return SimulatedContactSensorProvider(**kwargs)

    if name == "zigbee2mqtt":
        # Deliberately not gated on demo mode.  That gate exists so a simulator
        # cannot pretend to be hardware, which says nothing about real sensors
        # running beside a simulated hub — the arrangement used to test Zigbee
        # equipment without touching a building's heating.
        import os

        from sensor_mqtt import DEFAULT_URL, AiomqttTransport
        from sensor_persistence import load_zigbee_metadata, save_zigbee_metadata
        from sensor_zigbee2mqtt import Zigbee2MqttContactSensorProvider

        url = kwargs.pop("url", None) or os.environ.get("NOBO_MQTT_URL", DEFAULT_URL)
        base_topic = kwargs.pop("base_topic", None) or os.environ.get(
            "NOBO_MQTT_BASE_TOPIC", "zigbee2mqtt"
        )
        transport = kwargs.pop("transport", None) or AiomqttTransport(url)
        kwargs.setdefault("load_metadata", load_zigbee_metadata)
        kwargs.setdefault("save_metadata", save_zigbee_metadata)
        return Zigbee2MqttContactSensorProvider(
            transport=transport, base_topic=base_topic, **kwargs
        )

    raise ValueError(f"Unsupported sensor provider: {name}")
