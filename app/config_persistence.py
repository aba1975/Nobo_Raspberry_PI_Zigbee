"""
config_persistence.py — Demo mode and server state persistence for Nobø Web Control.

Provides atomic file-based persistence for:
- DEMO_ZONES       → data/demo_zones.json
- demo_schedules   → data/demo_schedules.json
- server_state     → data/server_state.json  (global_mode_source, …)

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
DATA_DIR = Path("data")
DEMO_ZONES_FILE = DATA_DIR / "demo_zones.json"
DEMO_SCHEDULES_FILE = DATA_DIR / "demo_schedules.json"
SERVER_STATE_FILE = DATA_DIR / "server_state.json"

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
