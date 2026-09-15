"""Zigbee2MQTT contact-sensor provider.

Translates Zigbee2MQTT's topics into the provider-independent snapshots the
rest of the application consumes.  Nothing outside this module knows that MQTT
exists.

Two decisions are worth stating because getting either wrong is quiet rather
than loud:

*Identity is the IEEE address.*  Not the friendly name, which a user may change
in Zigbee2MQTT, and not a generated id, which would not survive re-pairing.  A
sensor removed and paired again returns to the room it was already assigned to.

*Zigbee2MQTT's ``contact: true`` means CLOSED.*  It reports whether the magnet
is in contact, not whether the opening is.  Inverting it would make the whole
feature backwards while looking entirely plausible, so it is asserted by test.

This application owns the display name, the door/window type and the zone.
Zigbee2MQTT is infrastructure and keeps whatever friendly name it assigned,
which by default is the IEEE address.  Deliberately not renaming devices there
avoids a second source of truth for the one field a user edits most, and avoids
a rename round-trip that can fail halfway.
"""

from __future__ import annotations

import inspect
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional, Protocol

from sensor_provider import (
    ContactSnapshot, ContactState, EventCallback, PairingOutcome, PairingStatus,
    ProviderUnavailable, SensorEvent, SensorEventKind, SensorKind, SensorNotFound,
)

DEFAULT_BASE_TOPIC = "zigbee2mqtt"

# Zigbee caps a join window at 254 seconds; anything longer is the radio
# silently clamping rather than the value being honoured.
MAX_PERMIT_JOIN_SECONDS = 254

_IEEE = re.compile(r"^0x[0-9a-f]{16}$")


class MqttTransport(Protocol):
    """The only part of MQTT this module depends on.

    Kept this narrow so the provider can be tested against a Zigbee2MQTT fake
    without a broker.  The MQTT wire protocol itself is the client library's
    responsibility; what the tests exercise is the topic contract, which is the
    part this application can get wrong.
    """

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def subscribe(self, topic: str) -> None: ...

    async def publish(self, topic: str, payload: str) -> None: ...

    def on_message(
        self, callback: Callable[[str, bytes], Awaitable[None]]
    ) -> None: ...


class Zigbee2MqttContactSensorProvider:
    def __init__(
        self,
        *,
        transport: MqttTransport,
        base_topic: str = DEFAULT_BASE_TOPIC,
        load_metadata: Optional[Callable[[], dict]] = None,
        save_metadata: Optional[Callable[[dict], None]] = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._transport = transport
        self._base = base_topic.rstrip("/")
        self._now = now
        self._load_metadata = load_metadata or (lambda: {})
        self._save_metadata = save_metadata or (lambda _data: None)

        self._sensors: dict[str, ContactSnapshot] = {}
        self._metadata: dict[str, dict] = {}
        # Zigbee2MQTT addresses devices by friendly name on the wire, so a
        # topic has to be resolved back to the address that identifies them.
        self._topic_names: dict[str, str] = {}
        self._callbacks: list[EventCallback] = []
        self._started = False
        self._bridge_online = False
        self._pairing_until: Optional[datetime] = None
        self._pairing_outcome: Optional[PairingOutcome] = None
        self._pairing_sensor_id: Optional[str] = None
        self._pairing_detail: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        self._metadata = self._load_metadata() or {}
        self._transport.on_message(self._handle_message)
        lost = getattr(self._transport, "on_connection_lost", None)
        if callable(lost):
            lost(self._connection_lost)
        await self._transport.connect()
        # One wildcard rather than a filter per shape. Zigbee2MQTT allows "/"
        # in a friendly name and publishes it as a nested topic, which no
        # single-level filter matches — and subscribing for those as they are
        # discovered would mean calling subscribe() from inside the message
        # handler, which deadlocks the real client: it waits for the SUBACK on
        # the very loop that would deliver it.
        await self._transport.subscribe(f"{self._base}/#")
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._bridge_online = False
        await self._transport.disconnect()

    async def _connection_lost(self) -> None:
        """The broker went, so nothing we hold is known to be current.

        Zigbee2MQTT's last will covers the bridge stopping. It cannot cover the
        broker itself dying, and without this the sensors would keep being
        presented as reachable — the front page would go on asserting that
        every window is shut, and the away-and-open warning would quietly stop
        working, with nothing on screen to say why.
        """
        await self._on_bridge_state({"state": "offline"})

    def _ensure_started(self) -> None:
        if not self._started:
            raise RuntimeError("sensor provider is not started")

    # -- provider contract -------------------------------------------------

    async def list(self) -> list[ContactSnapshot]:
        self._ensure_started()
        return sorted(self._sensors.values(), key=lambda item: item.sensor_id)

    async def create(
        self,
        name: str,
        zone_id: Optional[str] = None,
        kind: SensorKind | str = SensorKind.WINDOW,
    ) -> ContactSnapshot:
        raise ProviderUnavailable(
            "Zigbee sensors cannot be created, only paired with real hardware"
        )

    async def pair(
        self,
        name: str,
        zone_id: Optional[str] = None,
        kind: SensorKind | str = SensorKind.WINDOW,
    ) -> ContactSnapshot:
        """Not supported: a real join is not instantaneous.

        Callers open a window with :meth:`begin_pairing` and wait for a
        ``CREATED`` event, which is also the better order — until a device has
        joined there is nothing to name.
        """
        raise ProviderUnavailable(
            "Zigbee pairing is asynchronous; use begin_pairing and wait for the "
            "device to join"
        )

    async def begin_pairing(self, seconds: int = MAX_PERMIT_JOIN_SECONDS) -> int:
        self._ensure_started()
        if type(seconds) is not int or not 1 <= seconds <= MAX_PERMIT_JOIN_SECONDS:
            raise ValueError(
                f"seconds must be from 1 to {MAX_PERMIT_JOIN_SECONDS}"
            )
        self._require_bridge()
        await self._request("permit_join", {"time": seconds})
        self._pairing_until = self._aware_now() + timedelta(seconds=seconds)
        self._pairing_outcome = None
        self._pairing_sensor_id = None
        self._pairing_detail = None
        await self._announce_pairing()
        return seconds

    async def cancel_pairing(self) -> None:
        self._ensure_started()
        self._require_bridge()
        await self._request("permit_join", {"time": 0})
        await self._end_pairing(PairingOutcome.CANCELLED)

    def pairing_status(self) -> PairingStatus:
        """Derived, never stored — this must stay free of side effects.

        It is called twice per automation pass and the two results compared to
        decide whether anything is worth broadcasting. A version of this that
        settled the expiry *as* it was read made the first call do the
        transition, so the comparison saw no change and the window closing was
        never announced: somebody would watch "3s left" and then nothing.
        """
        remaining = self._pairing_seconds_remaining()
        expired = self._pairing_until is not None and remaining == 0
        active = self._pairing_until is not None and not expired
        outcome = self._pairing_outcome
        if outcome is None and expired:
            # Reported rather than left silent. A join window that simply goes
            # quiet leaves a person pressing a button at a radio that stopped
            # listening minutes ago.
            outcome = PairingOutcome.EXPIRED
        return PairingStatus(
            supported=True,
            active=active,
            seconds_remaining=remaining if active else None,
            outcome=outcome,
            sensor_id=self._pairing_sensor_id,
            detail=self._pairing_detail,
        )

    def _pairing_seconds_remaining(self) -> Optional[int]:
        if self._pairing_until is None:
            return None
        # Rounded up, so a countdown never reads zero while there is still
        # time to press a button, and asking for 120 does not immediately
        # display 119.
        left = (self._pairing_until - self._aware_now()).total_seconds()
        return max(0, math.ceil(left))

    async def _end_pairing(
        self,
        outcome: PairingOutcome,
        *,
        sensor_id: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        self._pairing_until = None
        self._pairing_outcome = outcome
        self._pairing_sensor_id = sensor_id
        self._pairing_detail = detail
        await self._announce_pairing()

    async def _announce_pairing(self) -> None:
        await self._dispatch(
            SensorEvent(SensorEventKind.PAIRING, self._pairing_sensor_id or "", None)
        )

    async def update(
        self,
        sensor_id: str,
        *,
        name: Optional[str] = None,
        kind: Optional[SensorKind | str] = None,
        zone_id: Optional[str] = None,
        clear_zone: bool = False,
    ) -> ContactSnapshot:
        current = self._get(sensor_id)
        if zone_id is not None and clear_zone:
            raise ValueError("zone_id and clear_zone cannot both be supplied")
        updated = _replace(
            current,
            name=_valid_name(name) if name is not None else current.name,
            kind=_valid_kind(kind) if kind is not None else current.kind,
            zone_id=None if clear_zone else (
                _valid_zone(zone_id) if zone_id is not None else current.zone_id
            ),
        )
        self._remember(updated)
        self._sensors[sensor_id] = updated
        await self._emit(SensorEventKind.UPDATED, updated)
        return updated

    async def remove(self, sensor_id: str) -> None:
        current = self._get(sensor_id)
        self._require_bridge()
        await self._request("device/remove", {"id": current.sensor_id})
        # The registry is not edited here.  Zigbee2MQTT confirms the removal
        # with a bridge event, and treating the request as the outcome would
        # lose a sensor from the UI that is still on the mesh.

    def subscribe(self, callback: EventCallback):
        self._callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return unsubscribe

    # -- incoming ----------------------------------------------------------

    async def _handle_message(self, topic: str, payload: bytes) -> None:
        if not topic.startswith(f"{self._base}/"):
            return
        rest = topic[len(self._base) + 1:]
        if rest.startswith("bridge/") and rest not in (
            "bridge/state", "bridge/devices", "bridge/event"
        ):
            return
        try:
            document = json.loads(payload.decode("utf-8")) if payload else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return

        if rest == "bridge/state":
            await self._on_bridge_state(document)
        elif rest == "bridge/devices":
            await self._on_devices(document)
        elif rest == "bridge/event":
            await self._on_event(document)
        elif rest.endswith("/availability") and (
            address := self._address_for(rest[: -len("/availability")])
        ):
            await self._on_availability(address, document)
        elif (address := self._address_for(rest)) is not None:
            await self._on_state(address, document)

    def _address_for(self, name: str) -> Optional[str]:
        """Resolve a topic segment back to the address that identifies a sensor.

        Zigbee2MQTT allows ``/`` in a friendly name and publishes it as a
        nested topic, so a device called "kitchen/window" reports on
        ``zigbee2mqtt/kitchen/window``. Matching the known names rather than
        assuming one level is what keeps such a device from being registered
        and then never heard from — which would leave its zone unable to settle
        and its heating override held for ever.
        """
        known = self._topic_names.get(name)
        if known is not None:
            return known
        # A device can report before its bridge/devices entry arrives; the
        # default friendly name is the address itself.
        return name if name in self._sensors else None

    async def _on_bridge_state(self, document) -> None:
        state = (document or {}).get("state") if isinstance(document, dict) else None
        online = state == "online"
        if online == self._bridge_online:
            return
        self._bridge_online = online
        if online:
            return
        # Zigbee2MQTT itself is gone, so nothing it reported is current any
        # more.  Saying so is the honest answer, and the UI already draws
        # "offline" distinctly from "closed".
        for sensor_id, snapshot in list(self._sensors.items()):
            if snapshot.available:
                await self._store(_replace(snapshot, available=False))

    async def _on_devices(self, document) -> None:
        if not isinstance(document, list):
            return
        seen: set[str] = set()
        self._topic_names = {}
        for entry in document:
            if not isinstance(entry, dict):
                continue
            address = entry.get("ieee_address")
            if not _is_address(address) or not _is_contact_sensor(entry):
                continue
            seen.add(address)
            friendly = entry.get("friendly_name") or address
            self._topic_names[friendly] = address
            if address not in self._sensors:
                await self._store(self._new_snapshot(address, entry))

        for address in list(self._sensors):
            if address not in seen:
                await self._forget(address)

    async def _on_event(self, document) -> None:
        if not isinstance(document, dict):
            return
        data = document.get("data")
        if not isinstance(data, dict):
            return
        kind = document.get("type")
        address = data.get("ieee_address")
        if not _is_address(address):
            return

        if kind == "device_leave":
            await self._forget(address)
            return

        if kind != "device_interview" or self._pairing_until is None:
            return

        status = data.get("status")
        if status == "failed":
            await self._end_pairing(
                PairingOutcome.FAILED,
                sensor_id=address,
                detail="The device started joining but the interview did not finish",
            )
        elif status == "successful":
            if _is_contact_sensor(data):
                await self._end_pairing(PairingOutcome.JOINED, sensor_id=address)
            else:
                # A repeater or plug is a perfectly good thing to have joined;
                # it is simply not a contact sensor, and saying so beats
                # leaving the window apparently unanswered.
                await self._end_pairing(
                    PairingOutcome.IGNORED,
                    sensor_id=address,
                    detail=_describe(data) or "That device is not a contact sensor",
                )

    async def _on_availability(self, address: str, document) -> None:
        snapshot = self._sensors.get(address)
        if snapshot is None:
            return
        state = document.get("state") if isinstance(document, dict) else document
        available = state == "online"
        if available == snapshot.available:
            return
        await self._store(_replace(snapshot, available=available))

    async def _on_state(self, address: str, document) -> None:
        if not isinstance(document, dict):
            return
        snapshot = self._sensors.get(address)
        if snapshot is None:
            return

        state = _contact_state(document.get("contact"), snapshot.state)
        battery = _battery(document.get("battery"), snapshot.battery)
        stamp = self._aware_now()
        changed = state != snapshot.state
        updated = _replace(
            snapshot,
            state=state,
            battery=battery,
            # A report is proof the device is reachable, whatever the
            # availability topic last said.
            available=True,
            changed_at=stamp if changed else snapshot.changed_at,
            last_seen_at=stamp,
        )
        await self._store(updated)

    # -- registry ----------------------------------------------------------

    def _new_snapshot(self, address: str, entry: dict) -> ContactSnapshot:
        meta = self._metadata.get(address) or {}
        stamp = self._aware_now()
        return ContactSnapshot(
            sensor_id=address,
            provider_id=f"zigbee2mqtt:{address}",
            name=meta.get("name") or _default_name(entry, address),
            kind=_valid_kind(meta.get("kind") or SensorKind.WINDOW),
            zone_id=meta.get("zone_id"),
            state=ContactState.UNKNOWN,
            available=False,
            battery=None,
            changed_at=stamp,
            last_seen_at=stamp,
        )

    async def _store(self, snapshot: ContactSnapshot) -> ContactSnapshot:
        created = snapshot.sensor_id not in self._sensors
        self._sensors[snapshot.sensor_id] = snapshot
        if created:
            self._remember(snapshot)
        await self._emit(
            SensorEventKind.CREATED if created else SensorEventKind.UPDATED,
            snapshot,
        )
        return snapshot

    async def _forget(self, address: str) -> None:
        if self._sensors.pop(address, None) is None:
            return
        # The metadata is kept deliberately.  A sensor that is re-paired, or
        # that drops off while its battery is changed, comes back to the room
        # and name it already had rather than arriving anonymous.
        await self._emit_removed(address)

    def _remember(self, snapshot: ContactSnapshot) -> None:
        self._metadata[snapshot.sensor_id] = {
            "name": snapshot.name,
            "kind": snapshot.kind.value,
            "zone_id": snapshot.zone_id,
        }
        self._save_metadata(self._metadata)

    def _get(self, sensor_id: str) -> ContactSnapshot:
        self._ensure_started()
        try:
            return self._sensors[sensor_id]
        except KeyError as exc:
            raise SensorNotFound(sensor_id) from exc

    def _require_bridge(self) -> None:
        if not self._bridge_online:
            raise ProviderUnavailable("Zigbee2MQTT is not connected")

    async def _request(self, path: str, body: dict) -> None:
        await self._transport.publish(
            f"{self._base}/bridge/request/{path}", json.dumps(body)
        )

    async def _emit(
        self, kind: SensorEventKind, snapshot: ContactSnapshot
    ) -> None:
        await self._dispatch(SensorEvent(kind, snapshot.sensor_id, snapshot))

    async def _emit_removed(self, sensor_id: str) -> None:
        await self._dispatch(
            SensorEvent(SensorEventKind.REMOVED, sensor_id, None)
        )

    async def _dispatch(self, event: SensorEvent) -> None:
        for callback in tuple(self._callbacks):
            result = callback(event)
            if inspect.isawaitable(result):
                await result

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("provider clock must return a timezone-aware datetime")
        return value


# -- payload translation ---------------------------------------------------


def _contact_state(value, previous: ContactState) -> ContactState:
    """Zigbee2MQTT reports whether the magnet is in contact, not the opening."""
    if value is True:
        return ContactState.CLOSED
    if value is False:
        return ContactState.OPEN
    return previous


def _battery(value, previous: Optional[int]) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return previous
    return max(0, min(100, round(value)))


def _is_address(value) -> bool:
    return isinstance(value, str) and bool(_IEEE.match(value.lower()))


def _is_contact_sensor(entry: dict) -> bool:
    definition = entry.get("definition")
    if not isinstance(definition, dict):
        return False
    return any(
        isinstance(expose, dict) and expose.get("property") == "contact"
        for expose in _exposes(definition)
    )


def _exposes(definition: dict) -> list:
    found: list = []
    for expose in definition.get("exposes") or []:
        if not isinstance(expose, dict):
            continue
        found.append(expose)
        # Composite exposes nest the interesting properties one level down.
        found.extend(
            item for item in (expose.get("features") or []) if isinstance(item, dict)
        )
    return found


def _describe(entry: dict) -> Optional[str]:
    definition = entry.get("definition")
    if not isinstance(definition, dict):
        return None
    vendor = definition.get("vendor")
    description = definition.get("description") or definition.get("model")
    parts = [item for item in (vendor, description) if isinstance(item, str) and item]
    return " ".join(parts)[:120] or None


def _default_name(entry: dict, address: str) -> str:
    definition = entry.get("definition")
    if isinstance(definition, dict):
        description = definition.get("description") or definition.get("model")
        if isinstance(description, str) and description.strip():
            return description.strip()[:80]
    return address


def _valid_name(name: str) -> str:
    if not isinstance(name, str) or not (value := name.strip()) or len(value) > 80:
        raise ValueError("name must contain 1 to 80 characters")
    return value


def _valid_zone(zone_id: Optional[str]) -> Optional[str]:
    if zone_id is None:
        return None
    if not isinstance(zone_id, str) or not zone_id.strip():
        raise ValueError("zone_id must be a non-empty string or null")
    return zone_id


def _valid_kind(kind: SensorKind | str) -> SensorKind:
    try:
        return SensorKind(kind)
    except (TypeError, ValueError) as exc:
        raise ValueError("kind must be door or window") from exc


def _replace(item: ContactSnapshot, **changes) -> ContactSnapshot:
    values = item.__dict__.copy()
    values.update(changes)
    return ContactSnapshot(**values)
