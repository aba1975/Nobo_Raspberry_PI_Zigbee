"""Persisted demo sensor provider: contacts and room thermometers."""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sensor_persistence import load_simulated_sensors, save_simulated_sensors
from sensor_provider import (
    ContactSnapshot, ContactState, EventCallback, PairingStatus, SensorEvent,
    SensorEventKind, SensorKind, SensorNotFound, check_kind_change, valid_reading,
)

# What a new demo thermometer reads before anybody changes it: an ordinary
# heated room. Deliberately inside any sensible pair of thresholds, so adding
# one never raises a warning by itself.
DEMO_CLIMATE = {"temperature": 21.0, "humidity": 45.0, "pressure": 1013.0}

#: How often a simulated thermometer reports when nothing changes. A real
#: Aqara one sends a reading about once an hour even in a still room, so a
#: simulated one that only ever spoke when edited would go stale after three
#: hours and teach the demo that thermometers stop working.
SIMULATED_REPORT_SECONDS = 50 * 60


class SimulatedContactSensorProvider:
    def __init__(
        self,
        *,
        path: Optional[Path] = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ):
        self._path = path
        self._now = now
        self._id_factory = id_factory
        self._sensors: dict[str, ContactSnapshot] = {}
        self._callbacks: list[EventCallback] = []
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._sensors = {
            row["sensor_id"]: self._from_row(row)
            for row in load_simulated_sensors(self._path)
        }
        self._started = True

    async def stop(self) -> None:
        self._started = False

    def _ensure_started(self) -> None:
        if not self._started:
            raise RuntimeError("sensor provider is not started")

    async def list(self) -> list[ContactSnapshot]:
        self._ensure_started()
        self._report_readings()
        return sorted(self._sensors.values(), key=lambda item: item.sensor_id)

    def _report_readings(self) -> None:
        """Let every available thermometer send its periodic report.

        Lazily, when somebody looks, rather than on a timer: the automation
        always looks before a reading could go stale, because that moment is
        one of its deadlines. The same reading is repeated, as a real sensor
        in a room at a steady temperature does. Kept in memory only — the
        stored time moves with the next real change — so an idle demo does not
        write to the SD card every hour. A sensor set to unavailable stays
        silent, which is the point of being able to set it.
        """
        if not any(item.is_climate and item.available for item in self._sensors.values()):
            return
        now = self._aware_now()
        for sensor_id, item in tuple(self._sensors.items()):
            if (
                item.is_climate
                and item.available
                and (now - item.last_seen_at).total_seconds() >= SIMULATED_REPORT_SECONDS
            ):
                self._sensors[sensor_id] = self._replace(item, last_seen_at=now)

    async def routers(self) -> list:
        """There is no radio, so there is nothing relaying and nothing to say.

        Reported as none rather than omitted, so the interface takes the same
        path it does for a real network that happens to have no repeater in it.
        """
        return []

    async def create(
        self,
        name: str,
        zone_id: Optional[str] = None,
        kind: SensorKind | str = SensorKind.WINDOW,
    ) -> ContactSnapshot:
        self._ensure_started()
        name = self._valid_name(name)
        stamp = self._aware_now()
        sensor_id = self._id_factory()
        if not sensor_id or sensor_id in self._sensors:
            raise ValueError("id_factory returned an invalid or duplicate id")
        kind = self._kind(kind)
        climate = kind is SensorKind.CLIMATE
        snapshot = ContactSnapshot(
            sensor_id=sensor_id,
            provider_id=f"simulated:{sensor_id}",
            name=name,
            kind=kind,
            zone_id=self._zone(zone_id),
            # A thermometer has no contact, so it has no contact state either.
            state=ContactState.UNKNOWN if climate else ContactState.CLOSED,
            available=True,
            battery=100,
            link_quality=180,
            changed_at=stamp,
            last_seen_at=stamp,
            **(DEMO_CLIMATE if climate else {}),
        )
        previous = self._sensors.copy()
        self._sensors[sensor_id] = snapshot
        try:
            self._persist()
        except Exception:
            self._sensors = previous
            raise
        await self._emit(SensorEventKind.CREATED, snapshot)
        return snapshot

    async def pair(
        self,
        name: str,
        zone_id: Optional[str] = None,
        kind: SensorKind | str = SensorKind.WINDOW,
    ) -> ContactSnapshot:
        return await self.create(name, zone_id, kind)

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
        if kind is not None:
            kind = self._kind(kind)
            check_kind_change(current.kind, kind)
        updated = self._replace(
            current,
            name=self._valid_name(name) if name is not None else current.name,
            kind=kind if kind is not None else current.kind,
            zone_id=None if clear_zone else (
                self._zone(zone_id) if zone_id is not None else current.zone_id
            ),
        )
        return await self._store(updated)

    async def remove(self, sensor_id: str) -> None:
        current = self._get(sensor_id)
        del self._sensors[sensor_id]
        try:
            self._persist()
        except Exception:
            self._sensors[sensor_id] = current
            raise
        await self._emit(SensorEventKind.REMOVED, current, removed=True)

    async def simulate(
        self,
        sensor_id: str,
        *,
        state: Optional[ContactState | str] = None,
        available: Optional[bool] = None,
        battery: Optional[int] = None,
        clear_battery: bool = False,
        link_quality: Optional[int] = None,
        clear_link_quality: bool = False,
        temperature: Optional[float] = None,
        humidity: Optional[float] = None,
        pressure: Optional[float] = None,
    ) -> ContactSnapshot:
        current = self._get(sensor_id)
        readings = {
            "temperature": temperature, "humidity": humidity, "pressure": pressure,
        }
        sent = {name: value for name, value in readings.items() if value is not None}
        if sent and not current.is_climate:
            raise ValueError("Only a temperature sensor has room readings")
        if state is not None and current.is_climate:
            raise ValueError("A temperature sensor has no open or closed state")
        for name, value in sent.items():
            parsed = valid_reading(name, value)
            if parsed is None:
                raise ValueError(f"{name} is outside what the sensor can report")
            sent[name] = parsed
        if battery is not None and clear_battery:
            raise ValueError("battery and clear_battery cannot both be supplied")
        if link_quality is not None and clear_link_quality:
            raise ValueError(
                "link_quality and clear_link_quality cannot both be supplied"
            )
        if state is not None:
            try:
                state = ContactState(state)
            except ValueError as exc:
                raise ValueError("state must be open, closed, or unknown") from exc
        if available is not None and type(available) is not bool:
            raise ValueError("available must be a boolean")
        if battery is not None and (type(battery) is not int or not 0 <= battery <= 100):
            raise ValueError("battery must be from 0 to 100")
        if link_quality is not None and (
            type(link_quality) is not int or not 0 <= link_quality <= 255
        ):
            raise ValueError("link_quality must be from 0 to 255")
        now = self._aware_now()
        changed = state is not None and state != current.state
        updated = self._replace(
            current,
            state=state if state is not None else current.state,
            available=available if available is not None else current.available,
            battery=None if clear_battery else (
                battery if battery is not None else current.battery
            ),
            link_quality=None if clear_link_quality else (
                link_quality if link_quality is not None else current.link_quality
            ),
            changed_at=now if (changed or sent) else current.changed_at,
            last_seen_at=now,
            **sent,
        )
        return await self._store(updated)

    def pairing_status(self) -> PairingStatus:
        """There is no radio, so there is no window to wait at.

        Reported as unsupported rather than as permanently idle, so the
        interface offers the simulator's straight "create it" form instead of
        a progress display that would never move.
        """
        return PairingStatus(supported=False)

    def subscribe(self, callback: EventCallback):
        self._callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return unsubscribe

    async def _store(self, snapshot: ContactSnapshot) -> ContactSnapshot:
        previous = self._sensors.get(snapshot.sensor_id)
        self._sensors[snapshot.sensor_id] = snapshot
        try:
            self._persist()
        except Exception:
            if previous is None:
                del self._sensors[snapshot.sensor_id]
            else:
                self._sensors[snapshot.sensor_id] = previous
            raise
        await self._emit(SensorEventKind.UPDATED, snapshot)
        return snapshot

    async def _emit(
        self, kind: SensorEventKind, snapshot: ContactSnapshot, *, removed: bool = False
    ) -> None:
        event = SensorEvent(kind, snapshot.sensor_id, None if removed else snapshot)
        for callback in tuple(self._callbacks):
            result = callback(event)
            if inspect.isawaitable(result):
                await result

    def _persist(self) -> None:
        save_simulated_sensors(
            [self._to_row(item) for item in self._sensors.values()], self._path
        )

    def _get(self, sensor_id: str) -> ContactSnapshot:
        self._ensure_started()
        try:
            return self._sensors[sensor_id]
        except KeyError as exc:
            raise SensorNotFound(sensor_id) from exc

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("provider clock must return a timezone-aware datetime")
        return value

    @staticmethod
    def _valid_name(name: str) -> str:
        if not isinstance(name, str) or not (value := name.strip()) or len(value) > 80:
            raise ValueError("name must contain 1 to 80 characters")
        return value

    @staticmethod
    def _zone(zone_id: Optional[str]) -> Optional[str]:
        if zone_id is None:
            return None
        if not isinstance(zone_id, str) or not zone_id.strip():
            raise ValueError("zone_id must be a non-empty string or null")
        return zone_id

    @staticmethod
    def _kind(kind: SensorKind | str) -> SensorKind:
        try:
            return SensorKind(kind)
        except (TypeError, ValueError) as exc:
            raise ValueError("kind must be door, window or climate") from exc

    @staticmethod
    def _replace(item: ContactSnapshot, **changes) -> ContactSnapshot:
        values = item.__dict__.copy()
        values.update(changes)
        return ContactSnapshot(**values)

    @staticmethod
    def _to_row(item: ContactSnapshot) -> dict:
        row = item.__dict__.copy()
        row["state"] = item.state.value
        row["kind"] = item.kind.value
        row["changed_at"] = item.changed_at.isoformat()
        row["last_seen_at"] = item.last_seen_at.isoformat()
        return row

    @staticmethod
    def _from_row(row: dict) -> ContactSnapshot:
        values = dict(row)
        values["state"] = ContactState(values["state"])
        values["kind"] = SensorKind(values["kind"])
        values["changed_at"] = datetime.fromisoformat(values["changed_at"])
        values["last_seen_at"] = datetime.fromisoformat(values["last_seen_at"])
        if values["changed_at"].tzinfo is None or values["last_seen_at"].tzinfo is None:
            raise ValueError("persisted sensor timestamps must include a timezone")
        return ContactSnapshot(**values)
