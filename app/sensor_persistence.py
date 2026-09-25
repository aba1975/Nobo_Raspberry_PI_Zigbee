"""Strict atomic JSON stores for the isolated sensor subsystem."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 5
DATA_DIR = Path(__file__).resolve().parent / "data"
SENSOR_SETTINGS_FILE = DATA_DIR / "sensor_settings.json"
SIMULATED_SENSORS_FILE = DATA_DIR / "simulated_contact_sensors.json"
SENSOR_AUTOMATION_STATE_FILE = DATA_DIR / "sensor_automation_state.json"
ZIGBEE_METADATA_FILE = DATA_DIR / "zigbee_sensor_metadata.json"

# ``simulated`` exists only in demo mode; ``zigbee2mqtt`` talks to real
# hardware through a Zigbee2MQTT bridge.  See docs/SENSORS.md.
PROVIDERS = frozenset({"simulated", "zigbee2mqtt"})


class ActionWhenOpen(str, Enum):
    NOTHING = "nothing"
    AWAY = "away"
    ECO = "eco"
    COMFORT = "comfort"
    SCHEDULE = "schedule"


# The actions that make the automation hold a zone override until every contact
# closes again, and therefore the only values ``owned_action`` may take.
# ``SCHEDULE`` cancels a hold rather than taking one, so it owns nothing.
HOLD_ACTIONS = frozenset({
    ActionWhenOpen.AWAY,
    ActionWhenOpen.ECO,
    ActionWhenOpen.COMFORT,
})

# What a temperature rule may do. A room that is too warm can only be made
# colder, and one that is too cold only warmer: offering Comfort for "too warm"
# would be offering to make it worse.
TOO_WARM_ACTIONS = frozenset({
    ActionWhenOpen.NOTHING, ActionWhenOpen.ECO, ActionWhenOpen.AWAY,
})
TOO_COLD_ACTIONS = frozenset({
    ActionWhenOpen.NOTHING, ActionWhenOpen.ECO, ActionWhenOpen.COMFORT,
})

# The bounds a threshold may be set within, and the smallest gap between a
# maximum and a minimum. The gap has to be wider than twice the hysteresis in
# ``sensor_automation`` or one room could be too warm and too cold at once.
THRESHOLD_LIMITS = (0.0, 40.0)
THRESHOLD_MIN_GAP = 1.0


class HoldReason(str, Enum):
    """Which rule put the automation's override on a zone.

    A zone has one zone override, so the automation keeps one ledger for it,
    and this says whose it is. It matters when a rule stops wanting the hold:
    a window closing must not release an override the temperature rule still
    needs, and a room cooling down must not release one an open window does.
    """

    OPEN = "open"
    TOO_WARM = "too_warm"
    TOO_COLD = "too_cold"


class ClimateCondition(str, Enum):
    TOO_WARM = "too_warm"
    TOO_COLD = "too_cold"


def check_climate_policy(
    temperature_max: Optional[float],
    action_when_too_warm: "ActionWhenOpen",
    temperature_min: Optional[float],
    action_when_too_cold: "ActionWhenOpen",
) -> None:
    """Raise ValueError unless these temperature rules make sense together."""
    low, high = THRESHOLD_LIMITS
    for label, value in (("maximum", temperature_max), ("minimum", temperature_min)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"The {label} temperature must be a number")
        if not low <= value <= high:
            raise ValueError(
                f"The {label} temperature must be from {low:g} to {high:g} °C"
            )
    if ActionWhenOpen(action_when_too_warm) not in TOO_WARM_ACTIONS:
        raise ValueError("Too warm can only warn, set Eco or set Away")
    if ActionWhenOpen(action_when_too_cold) not in TOO_COLD_ACTIONS:
        raise ValueError("Too cold can only warn, set Eco or set Comfort")
    if (
        temperature_max is not None
        and temperature_min is not None
        and temperature_max - temperature_min < THRESHOLD_MIN_GAP
    ):
        raise ValueError(
            f"The maximum must be at least {THRESHOLD_MIN_GAP:g} °C above the minimum"
        )


@dataclass(frozen=True)
class ZoneSensorPolicy:
    """What one zone should do about a contact of its own that stays open.

    ``override_all_modes`` is the escape hatch from the warmth ordering in
    ``sensor_automation``: with it off the action may only make the room
    colder, with it on the action wins until something else takes the zone.
    """

    warning_delay_seconds: int = 300
    action_when_open: ActionWhenOpen = ActionWhenOpen.NOTHING
    action_delay_seconds: int = 300
    override_all_modes: bool = False
    # Temperature rules, read from the zone's climate sensors. None switches a
    # threshold off; an action of NOTHING still warns.
    temperature_max: Optional[float] = None
    action_when_too_warm: ActionWhenOpen = ActionWhenOpen.NOTHING
    temperature_min: Optional[float] = None
    action_when_too_cold: ActionWhenOpen = ActionWhenOpen.NOTHING

    def __post_init__(self):
        for name in ("action_when_open", "action_when_too_warm", "action_when_too_cold"):
            object.__setattr__(self, name, ActionWhenOpen(getattr(self, name)))
        for name in ("temperature_max", "temperature_min"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                object.__setattr__(self, name, round(float(value), 1))
        check_climate_policy(
            self.temperature_max, self.action_when_too_warm,
            self.temperature_min, self.action_when_too_cold,
        )

    @property
    def has_climate_rule(self) -> bool:
        return self.temperature_max is not None or self.temperature_min is not None


@dataclass(frozen=True)
class SensorSettings:
    enabled: bool = False
    provider: str = "simulated"
    zones: Dict[str, ZoneSensorPolicy] = field(default_factory=dict)


@dataclass
class AutomationZoneState:
    """What this automation is in the middle of, for one zone.

    Three facts and no more: when the room was first opened, whether its
    left-open warning has been raised, and which override — if any — this
    automation put there and is therefore entitled to take away.

    Deliberately nothing about who has "taken over". Which mode should be
    running is worked out from what is true now, not from the order in which
    people pressed things, so there is nothing else to remember.
    """

    open_started_at: Optional[float] = None
    warning_raised: bool = False
    owned_action: Optional[ActionWhenOpen] = None
    # Which rule that override belongs to. Meaningless while nothing is owned,
    # and kept as OPEN then so a file never carries a reason for nothing.
    owned_reason: HoldReason = HoldReason.OPEN
    # Whether the room is currently outside its temperature thresholds, and
    # since when. Persisted for the same reason ``warning_raised`` is: a
    # restart must not announce a condition that was already announced.
    climate_condition: Optional[ClimateCondition] = None
    climate_since: Optional[float] = None

    def __post_init__(self):
        if self.owned_action is not None:
            self.owned_action = ActionWhenOpen(self.owned_action)
        self.owned_reason = HoldReason(self.owned_reason)
        if self.climate_condition is not None:
            self.climate_condition = ClimateCondition(self.climate_condition)


class InvalidSensorData(ValueError):
    pass


def _require_dict(value: Any, where: str) -> dict:
    if type(value) is not dict:
        raise InvalidSensorData(f"{where} must be an object")
    return value


def _require_bool(value: Any, where: str) -> bool:
    if type(value) is not bool:
        raise InvalidSensorData(f"{where} must be a boolean")
    return value


def _delay(value: Any, where: str) -> int:
    if type(value) is not int or not 0 <= value <= 86400:
        raise InvalidSensorData(f"{where} must be an integer from 0 to 86400")
    return value


def _action(value: Any) -> ActionWhenOpen:
    try:
        return ActionWhenOpen(value)
    except (TypeError, ValueError) as exc:
        raise InvalidSensorData(f"invalid action {value!r}") from exc


def _exact_fields(value: Any, expected: frozenset, where: str) -> dict:
    """A row has to carry exactly the keys its schema version defines.

    Strict on purpose. A stray key is either a hand-edit or a version this
    build does not understand, and both are better refused loudly here than
    silently dropped on the next write.
    """
    row = _require_dict(value, where)
    if set(row) != set(expected):
        raise InvalidSensorData(f"{where} has unexpected fields")
    return row


# What each stored schema version calls a zone policy. v1 only offered Eco as a
# plain on/off; v2 introduced the choice of action; v3 added the escape hatch
# from the warmth ordering.
_POLICY_FIELDS = {
    1: frozenset({"warning_delay_seconds", "eco_enabled", "eco_delay_seconds"}),
    2: frozenset({"warning_delay_seconds", "action_when_open", "action_delay_seconds"}),
    3: frozenset({
        "warning_delay_seconds", "action_when_open",
        "action_delay_seconds", "override_all_modes",
    }),
    4: frozenset({
        "warning_delay_seconds", "action_when_open",
        "action_delay_seconds", "override_all_modes",
    }),
    5: frozenset({
        "warning_delay_seconds", "action_when_open",
        "action_delay_seconds", "override_all_modes",
        "temperature_max", "action_when_too_warm",
        "temperature_min", "action_when_too_cold",
    }),
}

# The same, for what the automation had in flight when it was last saved. The
# suppression flags v1-v3 carried are read and discarded: the rule no longer
# remembers who last touched a room, it re-decides from what is true now.
_AUTOMATION_FIELDS = {
    1: frozenset({"open_started_at", "warning_raised", "eco_owned", "suppressed"}),
    2: frozenset({"open_started_at", "warning_raised", "owned_action", "suppressed"}),
    3: frozenset({
        "open_started_at", "warning_raised", "owned_action",
        "suppressed", "owned_with_override",
    }),
    4: frozenset({"open_started_at", "warning_raised", "owned_action"}),
    5: frozenset({
        "open_started_at", "warning_raised", "owned_action",
        "owned_reason", "climate_condition", "climate_since",
    }),
}

# Every version this build can read. Older files migrate on the next save.
_READABLE = (1, 2, 3, 4, SCHEMA_VERSION)


def _document(payload: Any, versions: tuple[int, ...] = (SCHEMA_VERSION,)) -> dict:
    doc = _require_dict(payload, "document")
    if doc.get("schema_version") not in versions:
        raise InvalidSensorData("unsupported or missing schema_version")
    return doc


def _atomic_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def _backup_invalid(path: Path) -> None:
    backup = path.with_suffix(".backup")
    try:
        path.replace(backup)
    except OSError as exc:
        logger.warning("Could not back up invalid sensor data %s: %s", path, exc)


def _load(path: Path, default: Any, validator):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return validator(json.load(handle))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, InvalidSensorData, TypeError, ValueError) as exc:
        logger.warning("Invalid sensor data in %s: %s", path, exc)
        _backup_invalid(path)
        return default


def _parse_settings(payload: Any) -> SensorSettings:
    doc = _document(payload, _READABLE)
    enabled = _require_bool(doc.get("enabled"), "enabled")
    provider = doc.get("provider")
    if provider not in PROVIDERS:
        raise InvalidSensorData(
            "provider must be one of: " + ", ".join(sorted(PROVIDERS))
        )
    version = doc["schema_version"]
    zones: Dict[str, ZoneSensorPolicy] = {}
    for zone_id, raw in _require_dict(doc.get("zones"), "zones").items():
        if not isinstance(zone_id, str) or not zone_id:
            raise InvalidSensorData("zone ids must be non-empty strings")
        item = _exact_fields(raw, _POLICY_FIELDS[version], f"zones.{zone_id}")
        if version == 1:
            # v1 only ever offered Eco, as a plain on/off.
            action = (
                ActionWhenOpen.ECO
                if _require_bool(item["eco_enabled"], "eco_enabled")
                else ActionWhenOpen.NOTHING
            )
            action_delay = _delay(item["eco_delay_seconds"], "eco delay")
        else:
            action = _action(item["action_when_open"])
            action_delay = _delay(item["action_delay_seconds"], "action delay")
        climate = {}
        if version >= 5:
            climate = {
                "temperature_max": _threshold(item["temperature_max"], "temperature_max"),
                "action_when_too_warm": _action(item["action_when_too_warm"]),
                "temperature_min": _threshold(item["temperature_min"], "temperature_min"),
                "action_when_too_cold": _action(item["action_when_too_cold"]),
            }
        try:
            zones[zone_id] = _policy(item, version, action, action_delay, climate)
        except ValueError as exc:
            raise InvalidSensorData(f"zones.{zone_id}: {exc}") from exc
    return SensorSettings(enabled=enabled, provider=provider, zones=zones)


def _threshold(value: Any, where: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidSensorData(f"{where} must be a number or null")
    return float(value)


def _policy(item: dict, version: int, action, action_delay, climate: dict) -> ZoneSensorPolicy:
    return ZoneSensorPolicy(
        warning_delay_seconds=_delay(item["warning_delay_seconds"], "warning delay"),
        action_when_open=action,
        action_delay_seconds=action_delay,
        # Rows written before the warmth ordering existed say nothing about
        # wanting past it, so they migrate with the escape hatch shut.
        override_all_modes=(
            _require_bool(item["override_all_modes"], "override_all_modes")
            if version >= 3
            else False
        ),
        **climate,
    )


def load_sensor_settings(path: Optional[Path] = None) -> SensorSettings:
    return _load(path or SENSOR_SETTINGS_FILE, SensorSettings(), _parse_settings)


def save_sensor_settings(settings: SensorSettings, path: Optional[Path] = None) -> None:
    if not isinstance(settings, SensorSettings):
        raise TypeError("settings must be SensorSettings")
    payload = asdict(settings)
    payload["schema_version"] = SCHEMA_VERSION
    _parse_settings(payload)
    _atomic_write(path or SENSOR_SETTINGS_FILE, payload)


_SENSOR_FIELDS = {
    "sensor_id", "provider_id", "name", "zone_id", "state", "available",
    "battery", "changed_at", "last_seen_at", "kind", "link_quality",
    "temperature", "humidity", "pressure",
}

_SENSOR_KINDS = ("door", "window", "climate")
_READINGS = ("temperature", "humidity", "pressure")


def _climate_reading(value: Any, name: str, where: str) -> Optional[float]:
    """A stored climate reading, validated against what hardware can report."""
    from sensor_provider import READING_LIMITS

    if value is None:
        return None
    low, high = READING_LIMITS[name]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not low <= value <= high
    ):
        raise InvalidSensorData(f"{where} must be null or a number from {low:g} to {high:g}")
    return float(value)


def _parse_sensors(payload: Any) -> list[dict]:
    doc = _document(payload, _READABLE)
    rows = doc.get("sensors")
    if type(rows) is not list:
        raise InvalidSensorData("sensors must be an array")
    result, ids = [], set()
    for index, raw in enumerate(rows):
        row = _require_dict(raw, f"sensors[{index}]")
        if doc["schema_version"] == 1 and "kind" not in row:
            # Existing deployments predominantly modelled windows.  Defaulting
            # those records to window preserves them without guessing by name.
            row = {**row, "kind": "window"}
        if "link_quality" not in row:
            # Written before signal strength was recorded.  Absent means "not
            # measured", which is exactly what the live value means too.
            row = {**row, "link_quality": None}
        # Written before climate sensors existed: a contact has no readings.
        row = {**{name: None for name in _READINGS}, **row}
        if set(row) != _SENSOR_FIELDS:
            raise InvalidSensorData(f"sensors[{index}] has unexpected fields")
        for key in ("sensor_id", "provider_id", "name", "changed_at", "last_seen_at"):
            if not isinstance(row[key], str) or not row[key]:
                raise InvalidSensorData(f"sensors[{index}].{key} must be a non-empty string")
        for key in ("changed_at", "last_seen_at"):
            try:
                parsed = datetime.fromisoformat(row[key])
            except ValueError as exc:
                raise InvalidSensorData(f"sensors[{index}].{key} must be ISO-8601") from exc
            if parsed.tzinfo is None:
                raise InvalidSensorData(f"sensors[{index}].{key} must include a timezone")
        if row["sensor_id"] in ids:
            raise InvalidSensorData("duplicate sensor_id")
        ids.add(row["sensor_id"])
        if row["zone_id"] is not None and not isinstance(row["zone_id"], str):
            raise InvalidSensorData("zone_id must be a string or null")
        if row["state"] not in ("open", "closed", "unknown"):
            raise InvalidSensorData("invalid contact state")
        if row["kind"] not in _SENSOR_KINDS:
            raise InvalidSensorData("kind must be door, window or climate")
        for name in _READINGS:
            if row["kind"] != "climate" and row[name] is not None:
                raise InvalidSensorData(f"sensors[{index}]: a contact has no {name}")
            row[name] = _climate_reading(row[name], name, f"sensors[{index}].{name}")
        _require_bool(row["available"], "available")
        if row["battery"] is not None and (
            type(row["battery"]) is not int or not 0 <= row["battery"] <= 100
        ):
            raise InvalidSensorData("battery must be null or an integer from 0 to 100")
        if row["link_quality"] is not None and (
            type(row["link_quality"]) is not int or not 0 <= row["link_quality"] <= 255
        ):
            raise InvalidSensorData(
                "link_quality must be null or an integer from 0 to 255"
            )
        result.append(dict(row))
    return result


def load_simulated_sensors(path: Optional[Path] = None) -> list[dict]:
    return _load(path or SIMULATED_SENSORS_FILE, [], _parse_sensors)


def save_simulated_sensors(sensors: list[Mapping[str, Any]], path: Optional[Path] = None) -> None:
    payload = {"schema_version": SCHEMA_VERSION, "sensors": [dict(row) for row in sensors]}
    parsed = _parse_sensors(payload)
    _atomic_write(path or SIMULATED_SENSORS_FILE, {
        "schema_version": SCHEMA_VERSION, "sensors": parsed,
    })


def load_zigbee_metadata(path: Optional[Path] = None) -> dict:
    """What this application knows about a Zigbee sensor that the mesh does not.

    Name, door/window type and room are choices a person made; none of them can
    be read off the hardware.  Keyed by IEEE address so they survive a device
    being renamed in Zigbee2MQTT, or removed and paired again.
    """
    return _load(path or ZIGBEE_METADATA_FILE, {}, _parse_zigbee_metadata)


def save_zigbee_metadata(metadata: Mapping[str, Any], path: Optional[Path] = None) -> None:
    parsed = _parse_zigbee_metadata(
        {"schema_version": SCHEMA_VERSION, "sensors": dict(metadata)}
    )
    _atomic_write(path or ZIGBEE_METADATA_FILE, {
        "schema_version": SCHEMA_VERSION, "sensors": parsed,
    })


def _parse_zigbee_metadata(payload: Any) -> Dict[str, dict]:
    # v4 is the same shape less the climate readings, which are filled below.
    doc = _document(payload, (4, SCHEMA_VERSION))
    result: Dict[str, dict] = {}
    for address, raw in _require_dict(doc.get("sensors"), "sensors").items():
        if not isinstance(address, str) or not address:
            raise InvalidSensorData("sensor ids must be non-empty strings")
        # The shape has grown twice, so missing keys are filled rather than
        # refused: an installation written before last_seen or the readings
        # were kept would otherwise lose every name and room assignment on the
        # next start. Unknown keys are still an error.
        raw = _require_dict(raw, f"sensors.{address}")
        row = {
            "last_seen": None, "battery": None, "link_quality": None,
            **{name: None for name in _READINGS},
            **raw,
        }
        row = _exact_fields(
            row,
            ("name", "kind", "zone_id", "last_seen", "battery", "link_quality",
             *_READINGS),
            f"sensors.{address}",
        )
        name = row["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise InvalidSensorData(f"sensors.{address}.name must be 1 to 80 characters")
        if row["kind"] not in _SENSOR_KINDS:
            raise InvalidSensorData(
                f"sensors.{address}.kind must be door, window or climate"
            )
        zone_id = row["zone_id"]
        if zone_id is not None and (not isinstance(zone_id, str) or not zone_id.strip()):
            raise InvalidSensorData(
                f"sensors.{address}.zone_id must be a non-empty string or null"
            )
        last_seen = row["last_seen"]
        if last_seen is not None:
            if not isinstance(last_seen, str):
                raise InvalidSensorData(
                    f"sensors.{address}.last_seen must be a timestamp or null"
                )
            try:
                parsed = datetime.fromisoformat(last_seen)
            except ValueError as exc:
                raise InvalidSensorData(
                    f"sensors.{address}.last_seen is not a valid timestamp"
                ) from exc
            if parsed.tzinfo is None:
                raise InvalidSensorData(
                    f"sensors.{address}.last_seen must include a timezone"
                )
        result[address] = {
            "name": name,
            "kind": row["kind"],
            "zone_id": zone_id,
            "last_seen": last_seen,
            "battery": _reading(row["battery"], 100, f"sensors.{address}.battery"),
            "link_quality": _reading(
                row["link_quality"], 255, f"sensors.{address}.link_quality"
            ),
            **{
                name: _climate_reading(row[name], name, f"sensors.{address}.{name}")
                for name in _READINGS
            },
        }
    return result


def _reading(value: Any, ceiling: int, where: str) -> Optional[int]:
    """A whole-number hardware reading carried across a restart, or nothing.

    Battery and signal strength both arrive only when the device feels like
    speaking, which for a sleeping contact sensor can be many hours.  Throwing
    the last one away on every restart left both blank for most of a day after
    each update, so they are kept — honestly, because the interface shows how
    long ago the sensor was last heard from.
    """
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= ceiling:
        raise InvalidSensorData(f"{where} must be null or an integer from 0 to {ceiling}")
    return value


def _parse_automation(payload: Any) -> Dict[str, AutomationZoneState]:
    doc = _document(payload, _READABLE)
    version = doc["schema_version"]
    result = {}
    for zone_id, raw in _require_dict(doc.get("zones"), "zones").items():
        if not isinstance(zone_id, str) or not zone_id:
            raise InvalidSensorData("zone ids must be non-empty strings")
        row = _exact_fields(raw, _AUTOMATION_FIELDS[version], f"zones.{zone_id}")
        if version == 1:
            owned_action = (
                ActionWhenOpen.ECO
                if _require_bool(row["eco_owned"], "eco_owned")
                else None
            )
        else:
            owned_action = (
                _action(row["owned_action"])
                if row["owned_action"] is not None
                else None
            )
            if owned_action is not None and owned_action not in HOLD_ACTIONS:
                raise InvalidSensorData(
                    "owned_action must be away, eco, comfort, or null"
                )
        stamp = row["open_started_at"]
        if stamp is not None and (type(stamp) not in (int, float) or stamp < 0):
            raise InvalidSensorData("open_started_at must be a non-negative number or null")
        # Before v5 the only thing that could own an override was an open
        # contact, so that is whose every older hold is.
        reason, condition, since = HoldReason.OPEN, None, None
        if version >= 5:
            try:
                reason = HoldReason(row["owned_reason"])
                condition = (
                    ClimateCondition(row["climate_condition"])
                    if row["climate_condition"] is not None else None
                )
            except (TypeError, ValueError) as exc:
                raise InvalidSensorData(f"zones.{zone_id}: {exc}") from exc
            since = row["climate_since"]
            if since is not None and (type(since) not in (int, float) or since < 0):
                raise InvalidSensorData("climate_since must be a non-negative number or null")
        result[zone_id] = AutomationZoneState(
            open_started_at=float(stamp) if stamp is not None else None,
            warning_raised=_require_bool(row["warning_raised"], "warning_raised"),
            owned_action=owned_action,
            owned_reason=reason if owned_action is not None else HoldReason.OPEN,
            climate_condition=condition,
            climate_since=float(since) if since is not None else None,
        )
    return result


def load_automation_state(path: Optional[Path] = None) -> Dict[str, AutomationZoneState]:
    return _load(path or SENSOR_AUTOMATION_STATE_FILE, {}, _parse_automation)


def save_automation_state(
    states: Mapping[str, AutomationZoneState], path: Optional[Path] = None
) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "zones": {str(zone_id): asdict(state) for zone_id, state in states.items()},
    }
    _parse_automation(payload)
    _atomic_write(path or SENSOR_AUTOMATION_STATE_FILE, payload)
