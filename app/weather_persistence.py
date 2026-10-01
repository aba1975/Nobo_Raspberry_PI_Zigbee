"""Stores for the optional weather station integration.

Each file is written atomically, with the helpers the alarm uses:

* ``weather_settings.json`` — whether the integration is on, where it reads
  from, and the outdoor temperature worth a warning.
* ``weather_state.json`` — which warnings are raised, so a restart neither
  repeats one nor forgets to say it has cleared.
* ``simulated_weather.json`` — the demo station's readings.
* ``weather_pressure_history.json`` and ``weather_outdoor_history.json`` — a
  day of the station's air pressure and the outdoor temperature.
* ``netatmo/account.json`` — the Netatmo app's client secret and the tokens
  Netatmo hands back. Secrets: the directory is 0700, the file 0600, a damaged
  copy is deleted rather than kept, and ``scripts/backup.sh`` leaves the
  directory out. It never holds the Netatmo password, which this application
  never sees.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from alarm_persistence import _atomic_write
from weather_provider import MODULE_KINDS, default_simulated_state

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DATA_DIR = Path(__file__).resolve().parent / "data"
WEATHER_SETTINGS_FILE = DATA_DIR / "weather_settings.json"
WEATHER_STATE_FILE = DATA_DIR / "weather_state.json"
SIMULATED_WEATHER_FILE = DATA_DIR / "simulated_weather.json"
PRESSURE_HISTORY_FILE = DATA_DIR / "weather_pressure_history.json"
OUTDOOR_HISTORY_FILE = DATA_DIR / "weather_outdoor_history.json"
NETATMO_DIR = DATA_DIR / "netatmo"
NETATMO_ACCOUNT_FILE = NETATMO_DIR / "account.json"

PROVIDERS = frozenset({"simulated", "netatmo"})
# The coldest and mildest outdoor warning that can be chosen.
COLD_LIMITS = (-40.0, 10.0)
DEFAULT_COLD_BELOW = -15.0
MAX_MODULES = 16


class InvalidWeatherData(ValueError):
    pass


@dataclass(frozen=True)
class WeatherSettings:
    enabled: bool = False
    provider: str = "simulated"
    # Below this outside, the "very cold outside" warning is raised.
    outdoor_cold_below: float = DEFAULT_COLD_BELOW


@dataclass(frozen=True)
class WeatherLedger:
    connection_raised: bool = False
    cold_raised: bool = False
    # Module ids whose low battery has been reported.
    battery_low: Tuple[str, ...] = field(default_factory=tuple)


def _document(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise InvalidWeatherData("document must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise InvalidWeatherData("unsupported or missing schema_version")
    return payload


def _load(path: Path, default: Any, parse, keep_invalid: bool = True):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return parse(json.load(handle))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, InvalidWeatherData, TypeError, ValueError) as exc:
        logger.warning("Invalid weather data in %s: %s", path.name, type(exc).__name__)
        if keep_invalid:
            try:
                path.replace(path.with_suffix(".backup"))
            except OSError:
                pass
        else:
            path.unlink(missing_ok=True)
        return default


def _number(value: Any, where: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidWeatherData(f"{where} must be a number")
    return float(value)


def parse_cold_below(value: Any) -> float:
    number = _number(value, "outdoor_cold_below")
    if number is None or not COLD_LIMITS[0] <= number <= COLD_LIMITS[1]:
        raise InvalidWeatherData(
            f"outdoor_cold_below must be between {COLD_LIMITS[0]:.0f} and {COLD_LIMITS[1]:.0f} °C"
        )
    return round(number * 2) / 2


def parse_settings(payload: Any) -> WeatherSettings:
    doc = _document(payload)
    enabled = doc.get("enabled", False)
    if not isinstance(enabled, bool):
        raise InvalidWeatherData("enabled must be a boolean")
    provider = doc.get("provider", "simulated")
    if provider not in PROVIDERS:
        raise InvalidWeatherData("unknown provider")
    cold = parse_cold_below(doc.get("outdoor_cold_below", DEFAULT_COLD_BELOW))
    return WeatherSettings(enabled=enabled, provider=provider, outdoor_cold_below=cold)


def load_settings(path: Optional[Path] = None) -> WeatherSettings:
    return _load(path or WEATHER_SETTINGS_FILE, WeatherSettings(), parse_settings)


def save_settings(settings: WeatherSettings, path: Optional[Path] = None) -> None:
    _atomic_write(path or WEATHER_SETTINGS_FILE,
                  {"schema_version": SCHEMA_VERSION, **asdict(settings)})


def _parse_ledger(payload: Any) -> WeatherLedger:
    doc = _document(payload)
    flags = {}
    for name in ("connection_raised", "cold_raised"):
        value = doc.get(name, False)
        if not isinstance(value, bool):
            raise InvalidWeatherData(f"{name} must be a boolean")
        flags[name] = value
    low = doc.get("battery_low", [])
    if not isinstance(low, list) or not all(isinstance(item, str) for item in low):
        raise InvalidWeatherData("battery_low must be a list of module ids")
    return WeatherLedger(battery_low=tuple(sorted(set(low))), **flags)


def load_ledger(path: Optional[Path] = None) -> WeatherLedger:
    return _load(path or WEATHER_STATE_FILE, WeatherLedger(), _parse_ledger)


def save_ledger(ledger: WeatherLedger, path: Optional[Path] = None) -> None:
    payload = asdict(ledger)
    payload["battery_low"] = list(ledger.battery_low)
    _atomic_write(path or WEATHER_STATE_FILE, {"schema_version": SCHEMA_VERSION, **payload})


def _parse_simulated(payload: Any) -> Dict[str, Any]:
    doc = _document(payload)
    name = doc.get("station_name")
    if not isinstance(name, str) or not name.strip():
        raise InvalidWeatherData("station_name must be text")
    modules = doc.get("modules")
    if not isinstance(modules, list) or not modules or len(modules) > MAX_MODULES:
        raise InvalidWeatherData("modules must be a short non-empty list")
    parsed: List[Dict[str, Any]] = []
    for item in modules:
        if not isinstance(item, dict) or not isinstance(item.get("module_id"), str):
            raise InvalidWeatherData("module must have a module_id")
        if item.get("kind") not in MODULE_KINDS:
            raise InvalidWeatherData("unknown module kind")
        if not isinstance(item.get("name"), str):
            raise InvalidWeatherData("module must have a name")
        reachable = item.get("reachable", True)
        if not isinstance(reachable, bool):
            raise InvalidWeatherData("reachable must be a boolean")
        battery = _number(item.get("battery"), "battery")
        co2 = _number(item.get("co2"), "co2")
        parsed.append({
            "module_id": item["module_id"],
            "kind": item["kind"],
            "name": item["name"],
            "temperature": _number(item.get("temperature"), "temperature"),
            "humidity": _number(item.get("humidity"), "humidity"),
            "co2": int(co2) if co2 is not None else None,
            "pressure": _number(item.get("pressure"), "pressure"),
            "min_temperature": _number(item.get("min_temperature"), "min_temperature"),
            "max_temperature": _number(item.get("max_temperature"), "max_temperature"),
            "battery": int(battery) if battery is not None else None,
            "reachable": reachable,
            "lost_at": _number(item.get("lost_at"), "lost_at"),
        })
    return {"station_name": name, "modules": parsed}


def load_simulated(path: Optional[Path] = None) -> Dict[str, Any]:
    return _load(path or SIMULATED_WEATHER_FILE, default_simulated_state(), _parse_simulated)


def save_simulated(state: Mapping[str, Any], path: Optional[Path] = None) -> None:
    _atomic_write(path or SIMULATED_WEATHER_FILE, {"schema_version": SCHEMA_VERSION, **state})


# --- the station's own history ---------------------------------------------

def _load_history(path: Path) -> Dict[str, list]:
    def parse(payload: Any) -> Dict[str, list]:
        doc = _document(payload)
        zones = doc.get("series")
        if not isinstance(zones, dict) or not all(
            isinstance(key, str) and isinstance(rows, list) for key, rows in zones.items()
        ):
            raise InvalidWeatherData("series must map names to lists")
        return zones

    return _load(path, {}, parse)


def _save_history(path: Path, series: Mapping[str, list]) -> None:
    _atomic_write(path, {"schema_version": SCHEMA_VERSION, "series": dict(series)})


def load_pressure_history() -> Dict[str, list]:
    return _load_history(PRESSURE_HISTORY_FILE)


def save_pressure_history(series: Mapping[str, list]) -> None:
    _save_history(PRESSURE_HISTORY_FILE, series)


def load_outdoor_history() -> Dict[str, list]:
    return _load_history(OUTDOOR_HISTORY_FILE)


def save_outdoor_history(series: Mapping[str, list]) -> None:
    _save_history(OUTDOOR_HISTORY_FILE, series)


def clear_history() -> None:
    for path in (PRESSURE_HISTORY_FILE, OUTDOOR_HISTORY_FILE):
        path.unlink(missing_ok=True)


# --- the Netatmo account: secrets ------------------------------------------

def secure_dir(path: Optional[Path] = None) -> Path:
    """The private directory for Netatmo's file, created 0700."""
    directory = path or NETATMO_DIR
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


_ACCOUNT_TEXT = ("client_id", "client_secret")
_ACCOUNT_OPTIONAL_TEXT = ("access_token", "refresh_token")


def _parse_account(payload: Any) -> Dict[str, Any]:
    doc = _document(payload)
    out: Dict[str, Any] = {}
    for name in _ACCOUNT_TEXT:
        value = doc.get(name)
        if not isinstance(value, str) or not value:
            raise InvalidWeatherData(f"{name} missing")
        out[name] = value
    for name in _ACCOUNT_OPTIONAL_TEXT:
        value = doc.get(name)
        if value is not None and (not isinstance(value, str) or not value):
            raise InvalidWeatherData(f"{name} must be text")
        out[name] = value
    for name in ("expires_at", "connected_at"):
        out[name] = _number(doc.get(name), name)
    return out


def load_account(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    # A damaged file is deleted rather than kept as a .backup: it holds
    # tokens, and a stray copy of them is exactly what must not exist.
    return _load(path or NETATMO_ACCOUNT_FILE, None, _parse_account, keep_invalid=False)


def save_account(account: Mapping[str, Any], path: Optional[Path] = None) -> None:
    target = path or NETATMO_ACCOUNT_FILE
    secure_dir(target.parent)
    _atomic_write(target, {"schema_version": SCHEMA_VERSION, **account}, mode=0o600)


def delete_account(path: Optional[Path] = None) -> None:
    target = path or NETATMO_ACCOUNT_FILE
    target.unlink(missing_ok=True)
    target.with_suffix(".tmp").unlink(missing_ok=True)
