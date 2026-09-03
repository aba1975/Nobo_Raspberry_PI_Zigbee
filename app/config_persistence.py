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
import re
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
AWAY_EXCEPTIONS_FILE = DATA_DIR / "away_exceptions.json"
# Which exception zones are *currently* held on a zone-level Eco override.
#
# Separate from the configured list above, and deliberately so: the hub will not
# release a zone override by itself, so the only way a room can be freed after a
# restart mid-away is if we wrote down what we did to it.
AWAY_EXCEPTIONS_APPLIED_FILE = DATA_DIR / "away_exceptions_applied.json"
INTENDED_SETPOINTS_FILE = DATA_DIR / "intended_setpoints.json"
SITE_FILE = DATA_DIR / "site.json"

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


# ---------------------------------------------------------------------------
# Away exceptions  (zones kept on Eco while the rest of the house is Away)
# ---------------------------------------------------------------------------
#
# Nobø's Away state is a fixed 7 °C anti-frost temperature that cannot be
# configured — see AWAY_TEMPERATURE in server.py. That is too cold for some
# rooms: a cellar with water pipes, a room with an instrument or a plant, or a
# workshop. The only warmer setting a zone can hold is its own Eco temperature,
# which IS configurable per zone.
#
# So a zone can be listed here as an exception, and whenever the house goes
# Away — manually or because an away period started — those zones are put on
# Eco instead. This has to live on the server because the away period is
# applied by a background loop that runs whether or not a browser is open.

def save_away_exceptions(zone_ids: list) -> None:
    """Persist the list of zone ids kept on Eco during Away, atomically."""
    try:
        _atomic_write(AWAY_EXCEPTIONS_FILE, {"zone_ids": [str(z) for z in zone_ids]})
    except Exception as exc:
        logger.error("Failed to save away exceptions: %s", exc)
        raise


def load_away_exceptions() -> list:
    """
    Load the away exception zone ids from ``data/away_exceptions.json``.

    Returns an empty list when the file is missing or corrupt: no exception is
    always the safe reading, because it means the house behaves exactly the way
    Nobø's own Away does.
    """
    try:
        with AWAY_EXCEPTIONS_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.warning(
                "away_exceptions.json has unexpected format (expected dict, got %s) — using none",
                type(data).__name__,
            )
            return []
        ids = data.get("zone_ids", [])
        if not isinstance(ids, list):
            logger.warning("away_exceptions.json has a non-list zone_ids — using none")
            return []
        return [str(z) for z in ids]
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as exc:
        logger.warning("away_exceptions.json is corrupt: %s — backing up and using none", exc)
        _backup_corrupt(AWAY_EXCEPTIONS_FILE)
        return []


def save_away_exceptions_applied(zone_ids) -> None:
    """Record which exception zones are currently held on a zone override."""
    try:
        _atomic_write(
            AWAY_EXCEPTIONS_APPLIED_FILE,
            {"zone_ids": sorted(str(z) for z in zone_ids)},
        )
    except Exception as exc:
        logger.error("Failed to save applied away exceptions: %s", exc)


def load_away_exceptions_applied() -> list:
    """
    Load the zone ids currently held on an away-exception override.

    Returns an empty list when missing or corrupt. That is the safe reading:
    the worst case is a room that stays on Eco until the next away cycle, rather
    than an override being cancelled on a zone the user set deliberately.
    """
    try:
        with AWAY_EXCEPTIONS_APPLIED_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return []
        ids = data.get("zone_ids", [])
        if not isinstance(ids, list):
            return []
        return [str(z) for z in ids]
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as exc:
        logger.warning(
            "away_exceptions_applied.json is corrupt: %s — backing up and using none", exc
        )
        _backup_corrupt(AWAY_EXCEPTIONS_APPLIED_FILE)
        return []


# ---------------------------------------------------------------------------
# Intended setpoints
# ---------------------------------------------------------------------------
# The comfort and eco temperature this system means each zone to have. A Nobø
# thermostat with a dial rewrites the hub's value outright and the hub keeps no
# history, so this file is the only record that the room was ever meant to be
# something else. See app/setpoint_guard.py.

def save_intended_setpoints(intended: dict) -> None:
    """Persist the intended comfort/eco temperatures, atomically."""
    try:
        clean = {
            str(zone_id): {k: float(v) for k, v in fields.items() if v is not None}
            for zone_id, fields in (intended or {}).items()
        }
        _atomic_write(INTENDED_SETPOINTS_FILE, {"zones": clean})
    except Exception as exc:
        logger.error("Failed to save intended setpoints: %s", exc)


def load_intended_setpoints() -> dict:
    """
    Load the intended comfort/eco temperatures from ``data/intended_setpoints.json``.

    Returns an empty dict when missing or corrupt. The guard then re-adopts
    whatever the hub currently reports, which loses the warning but never
    invents one.
    """
    try:
        with INTENDED_SETPOINTS_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        zones = data.get("zones", {})
        if not isinstance(zones, dict):
            return {}
        out = {}
        for zone_id, fields in zones.items():
            if not isinstance(fields, dict):
                continue
            picked = {}
            for k, v in fields.items():
                try:
                    picked[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
            if picked:
                out[str(zone_id)] = picked
        return out
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        logger.warning(
            "intended_setpoints.json is corrupt: %s — backing up and using none", exc
        )
        _backup_corrupt(INTENDED_SETPOINTS_FILE)
        return {}


# ---------------------------------------------------------------------------
# Site identity and regional format
# ---------------------------------------------------------------------------
# What the household calls this place: a nickname, a street address, "The flat".
# Purely cosmetic — the hub neither knows nor cares — but it is the difference
# between an app that belongs to you and one that calls your home "the cabin".
#
# ``show_on_login`` exists because the sign-in page is served to anyone who can
# reach the Pi, before any password is asked for. A nickname there is harmless;
# a street address is an address given away to whoever is on the network. That
# is the user's call to make, not ours, so it is a setting rather than an
# assumption. It defaults to on, because a name nobody chose to hide is a name
# they wanted shown.
#
# ``locale`` decides how dates are written: the order of day and month, and the
# language of day and month names. It is stored per installation rather than
# read from each browser, because a household should not see "Sun 30 Aug" on
# one device and "søn. 30. aug." on another.
#
# Two things it deliberately does NOT control:
#
#   * The clock. Times are always 24-hour. The hub's own week profiles are
#     "HHMM" strings and its handshake is "yyyyMMddHHmmss", so 24-hour is the
#     protocol's own format; offering a 12-hour display would invent an
#     ambiguity the system does not have.
#   * The temperature unit. ``API_Nobo.pdf`` states plainly that "temperatures
#     are in celsius". Fahrenheit is not a unit the hub can be asked for, so a
#     setting would be a lie — the number would be wrong or the conversion
#     would be ours to get wrong.

SITE_NAME_MAX = 40

# Empty means "follow the browser". Anything else is a BCP 47 tag passed
# straight to Intl.DateTimeFormat, so this list is a convenience rather than a
# limit — the check below only rejects strings that cannot be a language tag.
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,8}(-[A-Za-z0-9]{2,8})*$")
LOCALE_MAX = 35

_DEFAULT_SITE: dict = {"name": "", "show_on_login": True, "locale": ""}


def _clean_locale(value: object) -> str:
    """Normalise a locale tag, or return "" for "follow the browser".

    Anything that is not shaped like a language tag becomes "" rather than an
    error: the consequence is dates in the browser's own format, which is a
    reasonable outcome and not worth blocking a settings save over.
    """
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()[:LOCALE_MAX]
    if not cleaned:
        return ""
    return cleaned if _LOCALE_RE.match(cleaned) else ""


def _clean_site_name(value: object) -> str:
    """Normalise a site name: trimmed, single-line, length-capped.

    Control characters are stripped rather than rejected. This value is
    interpolated into page titles and the sign-in page, so a newline or a stray
    escape sequence is not worth an error message to a user who almost
    certainly pasted it by accident.
    """
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned.strip()[:SITE_NAME_MAX]


def save_site(site: dict) -> None:
    """Persist the site identity atomically."""
    try:
        _atomic_write(
            SITE_FILE,
            {
                "name": _clean_site_name(site.get("name", "")),
                "show_on_login": bool(site.get("show_on_login", True)),
                "locale": _clean_locale(site.get("locale", "")),
            },
        )
    except Exception as exc:
        logger.error("Failed to save site settings: %s", exc)
        raise


def load_site() -> dict:
    """
    Load the site identity from ``data/site.json``.

    Falls back to the unnamed default whenever the file is missing or unusable.
    An unnamed site is the shipped behaviour, so a corrupt file costs the user
    their chosen name until they set it again — never a broken page.
    """
    try:
        with SITE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.warning(
                "site.json has unexpected format (expected dict, got %s) — using the default",
                type(data).__name__,
            )
            return dict(_DEFAULT_SITE)
        return {
            "name": _clean_site_name(data.get("name", "")),
            "show_on_login": bool(data.get("show_on_login", True)),
            # Absent in files written before this setting existed, which is the
            # same as "follow the browser".
            "locale": _clean_locale(data.get("locale", "")),
        }
    except FileNotFoundError:
        return dict(_DEFAULT_SITE)
    except json.JSONDecodeError as exc:
        logger.warning("site.json is corrupt: %s — backing up and using the default", exc)
        _backup_corrupt(SITE_FILE)
        return dict(_DEFAULT_SITE)
