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

SCHEMA_VERSION = 3
DATA_DIR = Path(__file__).resolve().parent / "data"
SENSOR_SETTINGS_FILE = DATA_DIR / "sensor_settings.json"
SIMULATED_SENSORS_FILE = DATA_DIR / "simulated_contact_sensors.json"
SENSOR_AUTOMATION_STATE_FILE = DATA_DIR / "sensor_automation_state.json"


class ActionWhenOpen(str, Enum):
    NOTHING = "nothing"
    AWAY = "away"
    ECO = "eco"
    COMFORT = "comfort"
    SCHEDULE = "schedule"


@dataclass(frozen=True, init=False)
class ZoneSensorPolicy:
    warning_delay_seconds: int = 300
    action_when_open: ActionWhenOpen = ActionWhenOpen.NOTHING
    action_delay_seconds: int = 300
    override_all_modes: bool = False

    def __init__(
        self,
        warning_delay_seconds: int = 300,
        action_when_open: ActionWhenOpen | str = ActionWhenOpen.NOTHING,
        action_delay_seconds: int = 300,
        override_all_modes: bool = False,
        *,
        eco_enabled: Optional[bool] = None,
        eco_delay_seconds: Optional[int] = None,
    ):
        if eco_enabled is not None:
            action_when_open = (
                ActionWhenOpen.ECO if eco_enabled else ActionWhenOpen.NOTHING
            )
        if eco_delay_seconds is not None:
            action_delay_seconds = eco_delay_seconds
        object.__setattr__(self, "warning_delay_seconds", warning_delay_seconds)
        object.__setattr__(self, "action_when_open", ActionWhenOpen(action_when_open))
        object.__setattr__(self, "action_delay_seconds", action_delay_seconds)
        object.__setattr__(self, "override_all_modes", override_all_modes)

    @property
    def eco_enabled(self) -> bool:
        """Compatibility for integrations reading the original Eco-only policy."""
        return self.action_when_open is ActionWhenOpen.ECO

    @property
    def eco_delay_seconds(self) -> int:
        return self.action_delay_seconds


@dataclass(frozen=True)
class SensorSettings:
    enabled: bool = False
    provider: str = "simulated"
    zones: Dict[str, ZoneSensorPolicy] = field(default_factory=dict)


@dataclass(init=False)
class AutomationZoneState:
    open_started_at: Optional[float] = None
    warning_raised: bool = False
    owned_action: Optional[ActionWhenOpen] = None
    suppressed: bool = False
    owned_with_override: bool = False

    def __init__(
        self,
        open_started_at: Optional[float] = None,
        warning_raised: bool = False,
        owned_action: Optional[ActionWhenOpen | str] = None,
        suppressed: bool = False,
        owned_with_override: bool = False,
        *,
        eco_owned: Optional[bool] = None,
    ):
        if eco_owned is not None:
            owned_action = ActionWhenOpen.ECO if eco_owned else None
        self.open_started_at = open_started_at
        self.warning_raised = warning_raised
        self.owned_action = (
            ActionWhenOpen(owned_action) if owned_action is not None else None
        )
        self.owned_with_override = owned_with_override
        self.suppressed = suppressed

    @property
    def eco_owned(self) -> bool:
        """Compatibility alias for the original Eco-only ownership flag."""
        return self.owned_action is ActionWhenOpen.ECO

    @eco_owned.setter
    def eco_owned(self, value: bool) -> None:
        self.owned_action = ActionWhenOpen.ECO if value else None


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
    doc = _document(payload, (1, 2, SCHEMA_VERSION))
    enabled = _require_bool(doc.get("enabled"), "enabled")
    provider = doc.get("provider")
    if provider != "simulated":
        raise InvalidSensorData("provider must be 'simulated'")
    zones: Dict[str, ZoneSensorPolicy] = {}
    for zone_id, raw in _require_dict(doc.get("zones"), "zones").items():
        if not isinstance(zone_id, str) or not zone_id:
            raise InvalidSensorData("zone ids must be non-empty strings")
        item = _require_dict(raw, f"zones.{zone_id}")
        if doc["schema_version"] == 1:
            if set(item) != {"warning_delay_seconds", "eco_enabled", "eco_delay_seconds"}:
                raise InvalidSensorData(f"zones.{zone_id} has unexpected fields")
            action = (
                ActionWhenOpen.ECO
                if _require_bool(item["eco_enabled"], "eco_enabled")
                else ActionWhenOpen.NOTHING
            )
            action_delay = _delay(item["eco_delay_seconds"], "eco delay")
        elif doc["schema_version"] == 2:
            if set(item) != {
                "warning_delay_seconds", "action_when_open", "action_delay_seconds"
            }:
                raise InvalidSensorData(f"zones.{zone_id} has unexpected fields")
            try:
                action = ActionWhenOpen(item["action_when_open"])
            except (TypeError, ValueError) as exc:
                raise InvalidSensorData("invalid action_when_open") from exc
            action_delay = _delay(item["action_delay_seconds"], "action delay")
        else:
            if set(item) != {
                "warning_delay_seconds", "action_when_open",
                "action_delay_seconds", "override_all_modes",
            }:
                raise InvalidSensorData(f"zones.{zone_id} has unexpected fields")
            try:
                action = ActionWhenOpen(item["action_when_open"])
            except (TypeError, ValueError) as exc:
                raise InvalidSensorData("invalid action_when_open") from exc
            action_delay = _delay(item["action_delay_seconds"], "action delay")
        zones[zone_id] = ZoneSensorPolicy(
            warning_delay_seconds=_delay(item["warning_delay_seconds"], "warning delay"),
            action_when_open=action,
            action_delay_seconds=action_delay,
            override_all_modes=(
                _require_bool(item["override_all_modes"], "override_all_modes")
                if doc["schema_version"] == SCHEMA_VERSION
                else False
            ),
        )
    return SensorSettings(enabled=enabled, provider=provider, zones=zones)


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
    "battery", "changed_at", "last_seen_at", "kind",
}


def _parse_sensors(payload: Any) -> list[dict]:
    doc = _document(payload, (1, 2, SCHEMA_VERSION))
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
        if row["kind"] not in ("door", "window"):
            raise InvalidSensorData("kind must be door or window")
        _require_bool(row["available"], "available")
        if row["battery"] is not None and (
            type(row["battery"]) is not int or not 0 <= row["battery"] <= 100
        ):
            raise InvalidSensorData("battery must be null or an integer from 0 to 100")
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


def _parse_automation(payload: Any) -> Dict[str, AutomationZoneState]:
    doc = _document(payload, (1, 2, SCHEMA_VERSION))
    result = {}
    for zone_id, raw in _require_dict(doc.get("zones"), "zones").items():
        if not isinstance(zone_id, str) or not zone_id:
            raise InvalidSensorData("zone ids must be non-empty strings")
        row = _require_dict(raw, f"zones.{zone_id}")
        if doc["schema_version"] == 1:
            if set(row) != {"open_started_at", "warning_raised", "eco_owned", "suppressed"}:
                raise InvalidSensorData(f"zones.{zone_id} has unexpected fields")
            owned_action = (
                ActionWhenOpen.ECO
                if _require_bool(row["eco_owned"], "eco_owned")
                else None
            )
        else:
            expected = {"open_started_at", "warning_raised", "owned_action", "suppressed"}
            if doc["schema_version"] == SCHEMA_VERSION:
                expected.add("owned_with_override")
            if set(row) != expected:
                raise InvalidSensorData(f"zones.{zone_id} has unexpected fields")
            try:
                owned_action = (
                    ActionWhenOpen(row["owned_action"])
                    if row["owned_action"] is not None
                    else None
                )
            except (TypeError, ValueError) as exc:
                raise InvalidSensorData("invalid owned_action") from exc
            if owned_action in (ActionWhenOpen.NOTHING, ActionWhenOpen.SCHEDULE):
                raise InvalidSensorData("owned_action must be away, eco, comfort, or null")
        stamp = row["open_started_at"]
        if stamp is not None and (type(stamp) not in (int, float) or stamp < 0):
            raise InvalidSensorData("open_started_at must be a non-negative number or null")
        result[zone_id] = AutomationZoneState(
            open_started_at=float(stamp) if stamp is not None else None,
            warning_raised=_require_bool(row["warning_raised"], "warning_raised"),
            owned_action=owned_action,
            owned_with_override=(
                _require_bool(row["owned_with_override"], "owned_with_override")
                if doc["schema_version"] == SCHEMA_VERSION
                # Earlier schemas had no colder-mode guard, so any live
                # ownership must be released unless the user explicitly opts
                # into the new Sensor override behavior.
                else owned_action is not None
            ),
            suppressed=_require_bool(row["suppressed"], "suppressed"),
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
