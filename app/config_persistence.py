"""
config_persistence.py — Demo mode and server state persistence for Nobø Web Control.

Provides atomic file-based persistence for:
- DEMO_ZONES       → data/demo_zones.json
- demo_schedules   → data/demo_schedules.json
- server_state     → data/server_state.json  (global_mode_source, …)
- hub_config       → data/hub_config.json    (demo_mode, serial, ip)

Uses the same atomic-write pattern (write to .tmp then rename) as
away_schedule.py and auth.py to prevent corruption on abrupt termination.

Storage paths are module-level variables so tests can redirect them via
monkeypatch (same pattern as away_schedule.DATA_DIR / SCHEDULE_FILE).
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Storage paths (can be overridden in tests via monkeypatching)
# ---------------------------------------------------------------------------
# Resolved from this file's location rather than the working directory, so the
# same files are used no matter where the app or the tests are started from
# (QA defect D-03). In the container this is still /app/data, which is where the
# nobo-data volume is mounted.
DATA_DIR = Path(__file__).resolve().parent / "data"
DEMO_ZONES_FILE = DATA_DIR / "demo_zones.json"
DEMO_SCHEDULES_FILE = DATA_DIR / "demo_schedules.json"
SERVER_STATE_FILE = DATA_DIR / "server_state.json"
HUB_CONFIG_FILE = DATA_DIR / "hub_config.json"
ZONE_ICONS_FILE = DATA_DIR / "zone_icons.json"

# Default server state values
_DEFAULT_SERVER_STATE: dict = {"global_mode_source": "manual"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _backup_corrupt(path: Path) -> None:
    """Rename a corrupt JSON file to .backup so the next load starts fresh."""
    try:
        backup = path.with_suffix(".backup")
        path.rename(backup)
        logger.info("Backed up corrupt config file to %s", backup)
    except Exception as exc:
        logger.warning("Could not back up corrupt file %s: %s", path, exc)


def _atomic_write(path: Path, data: object) -> None:
    """Write *data* as indented JSON to *path* atomically (write to .tmp, then rename)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Demo zones
# ---------------------------------------------------------------------------

def save_demo_zones(zones: list) -> None:
    """Persist *zones* list to ``data/demo_zones.json`` atomically."""
    try:
        _atomic_write(DEMO_ZONES_FILE, zones)
    except Exception as exc:
        logger.error("Failed to save demo zones: %s", exc)


def load_demo_zones() -> Optional[list]:
    """
    Load demo zones from ``data/demo_zones.json``.

    Returns:
        ``list`` on success.
        ``None`` when the file does not exist (caller should use hardcoded defaults).
        ``None`` when the file is corrupt (backed up as .backup; caller should use defaults).
    """
    try:
        with DEMO_ZONES_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            logger.warning(
                "demo_zones.json has unexpected format (expected list, got %s) — using defaults",
                type(data).__name__,
            )
            return None
        return data
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        logger.warning("demo_zones.json is corrupt: %s — backing up and using defaults", exc)
        _backup_corrupt(DEMO_ZONES_FILE)
        return None


# ---------------------------------------------------------------------------
# Demo schedules
# ---------------------------------------------------------------------------

def save_demo_schedules(schedules: dict) -> None:
    """Persist *schedules* dict to ``data/demo_schedules.json`` atomically."""
    try:
        _atomic_write(DEMO_SCHEDULES_FILE, schedules)
    except Exception as exc:
        logger.error("Failed to save demo schedules: %s", exc)


def load_demo_schedules() -> dict:
    """
    Load demo schedules from ``data/demo_schedules.json``.

    Returns:
        ``dict`` on success.
        Empty ``dict`` when the file does not exist or is corrupt (backed up as .backup).
    """
    try:
        with DEMO_SCHEDULES_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.warning(
                "demo_schedules.json has unexpected format (expected dict, got %s) — using empty dict",
                type(data).__name__,
            )
            return {}
        return data
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        logger.warning("demo_schedules.json is corrupt: %s — backing up and using empty dict", exc)
        _backup_corrupt(DEMO_SCHEDULES_FILE)
        return {}


# ---------------------------------------------------------------------------
# Zone icons
# ---------------------------------------------------------------------------
# The hub has no concept of a zone icon, so it is this app's own setting and is
# kept here for both demo mode and a real hub. Keyed by zone id.

def save_zone_icons(icons: dict) -> None:
    """Persist *icons* dict to ``data/zone_icons.json`` atomically."""
    try:
        _atomic_write(ZONE_ICONS_FILE, icons)
    except Exception as exc:
        logger.error("Failed to save zone icons: %s", exc)


def load_zone_icons() -> dict:
    """
    Load zone icons from ``data/zone_icons.json``.

    Returns:
        ``dict`` mapping zone id to icon name.
        Empty ``dict`` when the file does not exist or is corrupt (backed up as .backup).
    """
    try:
        with ZONE_ICONS_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.warning(
                "zone_icons.json has unexpected format (expected dict, got %s) — using empty dict",
                type(data).__name__,
            )
            return {}
        return {str(k): str(v) for k, v in data.items()}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        logger.warning("zone_icons.json is corrupt: %s — backing up and using empty dict", exc)
        _backup_corrupt(ZONE_ICONS_FILE)
        return {}


# ---------------------------------------------------------------------------
# Server state  (global_mode_source, …)
# ---------------------------------------------------------------------------

def save_server_state(state: dict) -> None:
    """Persist *state* dict to ``data/server_state.json`` atomically."""
    try:
        _atomic_write(SERVER_STATE_FILE, state)
    except Exception as exc:
        logger.error("Failed to save server state: %s", exc)


def load_server_state() -> dict:
    """
    Load server state from ``data/server_state.json``.

    Returns a dict merged with defaults so callers can always rely on
    all expected keys being present.  Returns defaults when the file does
    not exist or is corrupt (backed up as .backup).
    """
    defaults = dict(_DEFAULT_SERVER_STATE)
    try:
        with SERVER_STATE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.warning(
                "server_state.json has unexpected format (expected dict, got %s) — using defaults",
                type(data).__name__,
            )
            return defaults
        # Merge: start from defaults so any newly-added keys are present
        merged = dict(defaults)
        merged.update(data)
        return merged
    except FileNotFoundError:
        return defaults
    except json.JSONDecodeError as exc:
        logger.warning("server_state.json is corrupt: %s — backing up and using defaults", exc)
        _backup_corrupt(SERVER_STATE_FILE)
        return defaults


# ---------------------------------------------------------------------------
# Hub connection config  (demo_mode, serial, ip)
# ---------------------------------------------------------------------------

def save_hub_config(config: dict) -> None:
    """Persist the hub connection *config* dict to ``data/hub_config.json`` atomically.

    Expected keys: ``demo_mode`` (bool), ``serial`` (str), ``ip`` (str).
    """
    try:
        _atomic_write(HUB_CONFIG_FILE, config)
    except Exception as exc:
        logger.error("Failed to save hub config: %s", exc)
        raise


def load_hub_config() -> Optional[dict]:
    """
    Load the hub connection config from ``data/hub_config.json``.

    This file is written when the user changes the hub settings from the web
    interface. When present it takes precedence over the NOBO_* environment
    variables, which is what makes the setting survive restarts and reboots.

    Returns:
        ``dict`` with at least ``demo_mode``/``serial``/``ip`` on success.
        ``None`` when the file does not exist or is corrupt (caller should fall
        back to the environment variables).
    """
    try:
        with HUB_CONFIG_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.warning(
                "hub_config.json has unexpected format (expected dict, got %s) — using environment",
                type(data).__name__,
            )
            return None
        return data
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        logger.warning("hub_config.json is corrupt: %s — backing up and using environment", exc)
        _backup_corrupt(HUB_CONFIG_FILE)
        return None
