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

    async def create(self, name: str, zone_id: Optional[str] = None) -> ContactSnapshot: ...

    async def pair(self, name: str, zone_id: Optional[str] = None) -> ContactSnapshot: ...

    async def update(
        self,
        sensor_id: str,
        *,
        name: Optional[str] = None,
        zone_id: Optional[str] = None,
        clear_zone: bool = False,
    ) -> ContactSnapshot: ...

    async def remove(self, sensor_id: str) -> None: ...

    def subscribe(self, callback: EventCallback) -> Unsubscribe: ...


def create_provider(name: str, *, demo_mode: bool, **kwargs) -> ContactSensorProvider:
    """Construct a provider without allowing simulation to masquerade as hardware."""
    if name != "simulated":
        raise ValueError(f"Unsupported sensor provider: {name}")
    if not demo_mode:
        raise RuntimeError("The simulated sensor provider is available only in demo mode")
    from sensor_simulated import SimulatedContactSensorProvider

    return SimulatedContactSensorProvider(**kwargs)
