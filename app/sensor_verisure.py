"""The alarm's own door, window and climate devices, used as sensors here.

A Verisure installation already has contacts on some doors and smoke detectors
that measure temperature. With the alarm integration on, a person can pick any
of them from a list and put it in a room, where it warns and changes the
heating exactly like a Zigbee sensor does. Nothing is added by itself: a
device is only a sensor here once somebody has chosen it.

They are not Zigbee sensors, and three differences are kept in view rather
than papered over:

* **They are polled**, once a minute with the alarm, so an open door is
  noticed up to a minute late. A reading the alarm has not refreshed for
  ``ALARM_STALE_SECONDS`` is not believed: the sensor reads *offline*.
* **They report no battery and no signal.** Verisure keeps those in its own
  app. The battery and sensor-silent alerts therefore never fire for them;
  a Verisure sensor that cannot be read is covered by "the alarm cannot be
  read" instead.
* **Zigbee comes first.** A Verisure contact can be marked as the backup for
  a Zigbee one on the same door: while the Zigbee sensor is reporting it is
  the one believed, and the Verisure contact only stands in while the Zigbee
  one is offline. A Verisure thermometer stands down in any room that has a
  fresh Zigbee thermometer. See ``apply_precedence``.

The choices — which device, its name, room, door or window, and what it backs
up — are kept in ``data/verisure_sensors.json``. The readings are never
stored; they are the alarm's latest.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from alarm_provider import AlarmDevice, AlarmReading
from sensor_provider import ContactSnapshot, ContactState, SensorKind

logger = logging.getLogger(__name__)

SOURCE = "verisure"
SENSOR_PREFIX = "verisure-"
SCHEMA_VERSION = 1
DATA_DIR = Path(__file__).resolve().parent / "data"
VERISURE_SENSORS_FILE = DATA_DIR / "verisure_sensors.json"
MAX_SENSORS = 64
NAME_MAX = 80

# Why a sensor is not being counted, for the interface.
STANDING_BY_ZIGBEE = "zigbee_reporting"
STANDING_BY_THERMOMETER = "zigbee_thermometer"


class InvalidVerisureSensor(ValueError):
    pass


@dataclass(frozen=True)
class VerisureSensor:
    """A device somebody has chosen to use as a sensor."""

    sensor_id: str
    device_id: str
    name: str
    kind: SensorKind
    zone_id: Optional[str] = None
    # The Zigbee contact on the same door or window. While that one reports,
    # this one does not count.
    backup_for: Optional[str] = None


def is_verisure_sensor(sensor_id: str) -> bool:
    return str(sensor_id).startswith(SENSOR_PREFIX)


def sensor_id_for(device_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", device_id.lower()).strip("-")
    return f"{SENSOR_PREFIX}{slug}"[:96]


def kind_allowed(device: AlarmDevice, kind: SensorKind) -> bool:
    return kind is SensorKind.CLIMATE if device.kind == "climate" else kind.is_contact


def default_kind(device: AlarmDevice) -> SensorKind:
    if device.kind == "climate":
        return SensorKind.CLIMATE
    model = (device.model or "").lower()
    return SensorKind.WINDOW if "window" in model and "door" not in model else SensorKind.DOOR


# --- persistence -------------------------------------------------------------

def _text(value: Any, where: str, *, required: bool = False) -> Optional[str]:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidVerisureSensor(f"{where} must be text")
    return value.strip()


def _parse(payload: Any) -> Dict[str, VerisureSensor]:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise InvalidVerisureSensor("unsupported or missing schema_version")
    items = payload.get("sensors")
    if not isinstance(items, list) or len(items) > MAX_SENSORS:
        raise InvalidVerisureSensor("sensors must be a list")
    out: Dict[str, VerisureSensor] = {}
    for item in items:
        if not isinstance(item, dict):
            raise InvalidVerisureSensor("sensor must be an object")
        device_id = _text(item.get("device_id"), "device_id", required=True)
        sensor = VerisureSensor(
            sensor_id=sensor_id_for(device_id),
            device_id=device_id,
            name=_text(item.get("name"), "name", required=True)[:NAME_MAX],
            kind=SensorKind(item.get("kind")),
            zone_id=_text(item.get("zone_id"), "zone_id"),
            backup_for=_text(item.get("backup_for"), "backup_for"),
        )
        if device_id.startswith("climate:") != (sensor.kind is SensorKind.CLIMATE):
            raise InvalidVerisureSensor("a climate device is a thermometer, and only it is")
        out[sensor.sensor_id] = sensor
    return out


def load(path: Optional[Path] = None) -> Dict[str, VerisureSensor]:
    target = path or VERISURE_SENSORS_FILE
    try:
        with target.open("r", encoding="utf-8") as handle:
            return _parse(json.load(handle))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, InvalidVerisureSensor, TypeError, ValueError) as exc:
        logger.warning("Invalid Verisure sensor data in %s: %s", target.name, exc)
        try:
            target.replace(target.with_suffix(".backup"))
        except OSError:
            pass
        return {}


def save(sensors: Mapping[str, VerisureSensor], path: Optional[Path] = None) -> None:
    target = path or VERISURE_SENSORS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "sensors": [
            {
                "device_id": item.device_id,
                "name": item.name,
                "kind": item.kind.value,
                "zone_id": item.zone_id,
                "backup_for": item.backup_for,
            }
            for item in sorted(sensors.values(), key=lambda s: s.sensor_id)
        ],
    }
    temporary = target.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(target)


# --- readings ----------------------------------------------------------------

def _stamp(value: Optional[str], fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def snapshots(
    sensors: Iterable[VerisureSensor],
    reading: Optional[AlarmReading],
    *,
    fresh: bool,
    zone_of: Optional[Mapping[str, Optional[str]]] = None,
    now: Optional[float] = None,
) -> List[ContactSnapshot]:
    """Each chosen device as a sensor, from the alarm's latest reading.

    ``fresh`` is whether that reading is recent enough to believe. A device
    missing from it — removed from the alarm, or the list not returned —
    reads offline rather than closed. A backup takes the room of the sensor
    it backs up, from ``zone_of``, so it cannot drift away from that door.
    """
    read_at = datetime.fromtimestamp(
        reading.read_at if reading is not None else (now or 0.0), timezone.utc
    )
    out: List[ContactSnapshot] = []
    for sensor in sensors:
        device = reading.device(sensor.device_id) if reading is not None else None
        zone_id = sensor.zone_id
        if sensor.backup_for and zone_of is not None and sensor.backup_for in zone_of:
            zone_id = zone_of[sensor.backup_for]
        if sensor.kind is SensorKind.CLIMATE:
            available = fresh and device is not None and device.temperature is not None
            # The climate device's own timestamp: a smoke detector reports
            # temperature perhaps hourly, and the thermometer rule must see
            # how old the number really is.
            seen = _stamp(device.reported_at, read_at) if device else read_at
            out.append(ContactSnapshot(
                sensor_id=sensor.sensor_id, provider_id=sensor.device_id, name=sensor.name,
                zone_id=zone_id, state=ContactState.UNKNOWN, available=available,
                battery=None, changed_at=seen, last_seen_at=seen, kind=sensor.kind,
                temperature=device.temperature if device else None,
                humidity=device.humidity if device else None,
                source=SOURCE,
            ))
            continue
        state = ContactState.UNKNOWN
        if device is not None and device.open is not None:
            state = ContactState.OPEN if device.open else ContactState.CLOSED
        available = fresh and device is not None and device.open is not None
        out.append(ContactSnapshot(
            sensor_id=sensor.sensor_id, provider_id=sensor.device_id, name=sensor.name,
            zone_id=zone_id, state=state, available=available, battery=None,
            changed_at=_stamp(device.reported_at, read_at) if device else read_at,
            # Heard from every time the alarm was read: the door said nothing
            # new, but the alarm vouched for it.
            last_seen_at=read_at, kind=sensor.kind, source=SOURCE,
        ))
    return out


# --- precedence --------------------------------------------------------------

@dataclass(frozen=True)
class Precedence:
    """What the automation should believe, and why the rest is set aside.

    ``counted`` is every sensor the rules see. ``standing_by`` maps a
    Verisure sensor that is not counted to the reason. ``stood_in_for`` maps
    an offline Zigbee sensor to the Verisure backup counting in its place.
    """

    counted: List[ContactSnapshot]
    standing_by: Dict[str, str]
    stood_in_for: Dict[str, str]


def apply_precedence(
    snapshots: Sequence[ContactSnapshot],
    sensors: Mapping[str, VerisureSensor],
    *,
    now: float,
    climate_stale_seconds: float,
) -> Precedence:
    primary = {item.sensor_id: item for item in snapshots if item.source != SOURCE}
    standing_by: Dict[str, str] = {}
    stood_in_for: Dict[str, str] = {}

    fresh_thermometer_zones = {
        str(item.zone_id) for item in primary.values()
        if item.is_climate and item.zone_id is not None and item.available
        and item.temperature is not None
        and now - item.last_seen_at.timestamp() <= climate_stale_seconds
    }
    for item in snapshots:
        if item.source != SOURCE:
            continue
        chosen = sensors.get(item.sensor_id)
        if item.is_climate:
            if item.zone_id is not None and str(item.zone_id) in fresh_thermometer_zones:
                standing_by[item.sensor_id] = STANDING_BY_THERMOMETER
            continue
        backed = primary.get(chosen.backup_for) if chosen and chosen.backup_for else None
        if backed is None or not backed.is_contact:
            continue
        if backed.available and backed.state is not ContactState.UNKNOWN:
            standing_by[item.sensor_id] = STANDING_BY_ZIGBEE
        elif item.available:
            stood_in_for[backed.sensor_id] = item.sensor_id
        else:
            # Neither can be believed. The Zigbee one stays counted, so the
            # room reads unknown exactly as it did before a backup existed.
            standing_by[item.sensor_id] = STANDING_BY_ZIGBEE

    counted = [
        item for item in snapshots
        if item.sensor_id not in standing_by and item.sensor_id not in stood_in_for
    ]
    return Precedence(counted=counted, standing_by=standing_by, stood_in_for=stood_in_for)


def catalogue(
    reading: Optional[AlarmReading], sensors: Mapping[str, VerisureSensor]
) -> List[Dict[str, Any]]:
    """Every device the alarm reported, and whether it is already a sensor."""
    chosen = {item.device_id: item for item in sensors.values()}
    out: List[Dict[str, Any]] = []
    for device in (reading.devices if reading is not None else ()):
        taken = chosen.get(device.device_id)
        out.append({
            "device_id": device.device_id,
            "kind": device.kind,
            "name": device.name,
            "model": device.model,
            "open": device.open,
            "temperature": device.temperature,
            "humidity": device.humidity,
            "reported_at": device.reported_at,
            "default_kind": default_kind(device).value,
            "sensor_id": taken.sensor_id if taken else None,
        })
    return sorted(out, key=lambda item: (item["kind"], item["name"].lower()))


def with_changes(sensor: VerisureSensor, **changes: Any) -> VerisureSensor:
    return replace(sensor, **changes)
