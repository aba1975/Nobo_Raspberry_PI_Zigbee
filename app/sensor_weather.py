"""The weather station's indoor modules, used as room thermometers here.

With the weather integration on, the base station and each indoor module can
be picked from a list and put in a room, where it reads the temperature and
humidity exactly like a Zigbee thermometer does — the room's actual
temperature, its limits, its near-freezing warning and its day of history. The
outdoor module is not offered: outside is not a room, and it has its own
place on the front page.

Three differences from a Zigbee thermometer are kept in view:

* **They are polled**, every few minutes with the station, and a module
  reports every five minutes or so. A reading the station has not refreshed
  for ``WEATHER_STALE_SECONDS`` reads *offline*, as does a module the
  station says it can no longer hear.
* **Their battery and radio are the station's.** The battery is shown, and a
  low one is the weather station's own alert, not the sensor alert; a module
  that goes quiet is covered by the station's connection.
* **Zigbee comes first, then the weather station, then Verisure.** A module
  stands by in any room that has a fresh Zigbee thermometer, and a Verisure
  smoke detector stands by in any room with a fresh module. See
  ``apply_precedence`` here and in ``sensor_verisure``.

The choices — which module, its name and room — are kept in
``data/weather_sensors.json``. The readings are never stored here.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from sensor_provider import ContactSnapshot, ContactState, SensorKind
from weather_provider import ROOM_KINDS, WeatherReading

logger = logging.getLogger(__name__)

SOURCE = "netatmo"
SENSOR_PREFIX = "netatmo-"
SCHEMA_VERSION = 1
DATA_DIR = Path(__file__).resolve().parent / "data"
WEATHER_SENSORS_FILE = DATA_DIR / "weather_sensors.json"
MAX_SENSORS = 16
NAME_MAX = 80

STANDING_BY_THERMOMETER = "zigbee_thermometer"
# Sources ranked below the weather station: it stands by for none of them.
LOWER_SOURCES = frozenset({"verisure"})


class InvalidWeatherSensor(ValueError):
    pass


@dataclass(frozen=True)
class WeatherSensor:
    sensor_id: str
    module_id: str
    name: str
    zone_id: Optional[str] = None

    kind = SensorKind.CLIMATE


def is_weather_sensor(sensor_id: str) -> bool:
    return str(sensor_id).startswith(SENSOR_PREFIX)


def sensor_id_for(module_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", module_id.lower()).strip("-")
    return f"{SENSOR_PREFIX}{slug}"[:96]


def _text(value: Any, where: str, *, required: bool = False) -> Optional[str]:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidWeatherSensor(f"{where} must be text")
    return value.strip()


def _parse(payload: Any) -> Dict[str, WeatherSensor]:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise InvalidWeatherSensor("unsupported or missing schema_version")
    items = payload.get("sensors")
    if not isinstance(items, list) or len(items) > MAX_SENSORS:
        raise InvalidWeatherSensor("sensors must be a list")
    out: Dict[str, WeatherSensor] = {}
    for item in items:
        if not isinstance(item, dict):
            raise InvalidWeatherSensor("sensor must be an object")
        module_id = _text(item.get("module_id"), "module_id", required=True)
        sensor = WeatherSensor(
            sensor_id=sensor_id_for(module_id),
            module_id=module_id,
            name=_text(item.get("name"), "name", required=True)[:NAME_MAX],
            zone_id=_text(item.get("zone_id"), "zone_id"),
        )
        out[sensor.sensor_id] = sensor
    return out


def load(path: Optional[Path] = None) -> Dict[str, WeatherSensor]:
    target = path or WEATHER_SENSORS_FILE
    try:
        with target.open("r", encoding="utf-8") as handle:
            return _parse(json.load(handle))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, InvalidWeatherSensor, TypeError, ValueError) as exc:
        logger.warning("Invalid weather sensor data in %s: %s", target.name, exc)
        try:
            target.replace(target.with_suffix(".backup"))
        except OSError:
            pass
        return {}


def save(sensors: Mapping[str, WeatherSensor], path: Optional[Path] = None) -> None:
    target = path or WEATHER_SENSORS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "sensors": [
            {"module_id": item.module_id, "name": item.name, "zone_id": item.zone_id}
            for item in sorted(sensors.values(), key=lambda s: s.sensor_id)
        ],
    }
    temporary = target.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(target)


def snapshots(
    sensors: Iterable[WeatherSensor],
    reading: Optional[WeatherReading],
    *,
    fresh: bool,
    stale_seconds: float,
    now: float,
) -> List[ContactSnapshot]:
    """Each chosen module as a thermometer, from the station's latest reading.

    ``fresh`` is whether that reading is recent enough to believe at all. A
    module missing from it, unreachable, or whose own measurement is older
    than ``stale_seconds`` reads offline rather than keeping its last number.
    """
    out: List[ContactSnapshot] = []
    for sensor in sensors:
        module = reading.module(sensor.module_id) if reading is not None else None
        measured = module.reported_at if module is not None else None
        seen_at = measured if measured is not None else (reading.read_at if reading else now)
        seen = datetime.fromtimestamp(seen_at, timezone.utc)
        available = (
            fresh and module is not None and module.reachable
            and module.temperature is not None
            and measured is not None and now - measured <= stale_seconds
        )
        out.append(ContactSnapshot(
            sensor_id=sensor.sensor_id, provider_id=sensor.module_id, name=sensor.name,
            zone_id=sensor.zone_id, state=ContactState.UNKNOWN, available=available,
            battery=module.battery if module is not None else None,
            changed_at=seen, last_seen_at=seen, kind=SensorKind.CLIMATE,
            temperature=module.temperature if module is not None else None,
            humidity=module.humidity if module is not None else None,
            pressure=module.pressure if module is not None else None,
            source=SOURCE,
        ))
    return out


def standing_by(
    snapshots: Sequence[ContactSnapshot], *, now: float, climate_stale_seconds: float,
) -> Dict[str, str]:
    """Modules set aside because a Zigbee thermometer is reading their room."""
    fresh_zones = {
        str(item.zone_id) for item in snapshots
        if item.source != SOURCE and item.source not in LOWER_SOURCES
        and item.is_climate and item.zone_id is not None and item.available
        and item.temperature is not None
        and now - item.last_seen_at.timestamp() <= climate_stale_seconds
    }
    return {
        item.sensor_id: STANDING_BY_THERMOMETER
        for item in snapshots
        if item.source == SOURCE and item.zone_id is not None and str(item.zone_id) in fresh_zones
    }


def catalogue(
    reading: Optional[WeatherReading], sensors: Mapping[str, WeatherSensor]
) -> List[Dict[str, Any]]:
    """Every indoor module the station reported, and whether it is already a sensor."""
    chosen = {item.module_id: item for item in sensors.values()}
    out: List[Dict[str, Any]] = []
    for module in (reading.modules if reading is not None else ()):
        if module.kind not in ROOM_KINDS:
            continue
        taken = chosen.get(module.module_id)
        out.append({
            "module_id": module.module_id,
            "kind": module.kind,
            "name": module.name,
            "temperature": module.temperature,
            "humidity": module.humidity,
            "co2": module.co2,
            "battery": module.battery,
            "reachable": module.reachable,
            "sensor_id": taken.sensor_id if taken else None,
        })
    return sorted(out, key=lambda item: (item["kind"] != "base", item["name"].lower()))


def with_changes(sensor: WeatherSensor, **changes: Any) -> WeatherSensor:
    return replace(sensor, **changes)
