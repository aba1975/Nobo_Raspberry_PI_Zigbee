"""Stores for the optional alarm integration.

Four files, each written atomically:

* ``alarm_settings.json`` — whether the integration is on and what it may do.
* ``alarm_state.json`` — which arm event has been handled and whether the Away
  now in force was put there by the alarm. Without it a restart would either
  re-apply Away the user had already overridden, or forget to lift the Away it
  set.
* ``simulated_alarm.json`` — the demo alarm's state.
* ``verisure/session.json`` — the Verisure session. This one holds secrets and
  is treated differently: its directory is 0700, the file is 0600, it never
  holds the Verisure password, and ``scripts/backup.sh`` leaves it out.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from alarm_provider import ARM_STATES, default_simulated_state

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DATA_DIR = Path(__file__).resolve().parent / "data"
ALARM_SETTINGS_FILE = DATA_DIR / "alarm_settings.json"
ALARM_STATE_FILE = DATA_DIR / "alarm_state.json"
SIMULATED_ALARM_FILE = DATA_DIR / "simulated_alarm.json"
VERISURE_DIR = DATA_DIR / "verisure"
VERISURE_SESSION_FILE = VERISURE_DIR / "session.json"

PROVIDERS = frozenset({"simulated", "verisure"})


class InvalidAlarmData(ValueError):
    pass


@dataclass(frozen=True)
class AlarmSettings:
    enabled: bool = False
    provider: str = "simulated"
    # Heating: armed away means nobody is there. Armed home ("skallsikring")
    # usually means people asleep inside, so it is off by default.
    away_when_armed_away: bool = True
    away_when_armed_home: bool = False
    # Warnings about open doors and windows.
    warn_when_armed_away: bool = True
    warn_when_armed_home: bool = True
    warn_when_locked_outside: bool = True
    # A Doorman with auto-lock relocks itself after every closing, including
    # somebody walking in, so an auto-lock does not mean anybody left.
    autolock_counts_as_leaving: bool = False


@dataclass(frozen=True)
class AlarmLedger:
    handled_event: Optional[str] = None
    owns_away: bool = False
    left_open_raised: bool = False
    connection_raised: bool = False


_SETTINGS_FIELDS = frozenset(AlarmSettings.__dataclass_fields__)
_LEDGER_FIELDS = frozenset(AlarmLedger.__dataclass_fields__)


def _document(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise InvalidAlarmData("document must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise InvalidAlarmData("unsupported or missing schema_version")
    return payload


def _atomic_write(path: Path, payload: object, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(temporary, flags, mode if mode is not None else 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    if mode is not None:
        # os.open honours the umask, so the mode is set again explicitly.
        os.chmod(temporary, mode)
    temporary.replace(path)


def _load(path: Path, default: Any, parse, keep_invalid: bool = True):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return parse(json.load(handle))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, InvalidAlarmData, TypeError, ValueError) as exc:
        logger.warning("Invalid alarm data in %s: %s", path.name, type(exc).__name__)
        if keep_invalid:
            try:
                path.replace(path.with_suffix(".backup"))
            except OSError:
                pass
        else:
            path.unlink(missing_ok=True)
        return default


def _booleans(payload: Mapping[str, Any], names: frozenset, where: str) -> Dict[str, bool]:
    out: Dict[str, bool] = {}
    for name in names:
        if name not in payload:
            continue
        if not isinstance(payload[name], bool):
            raise InvalidAlarmData(f"{where}.{name} must be a boolean")
        out[name] = payload[name]
    return out


def parse_settings(payload: Any) -> AlarmSettings:
    doc = _document(payload)
    provider = doc.get("provider", "simulated")
    if provider not in PROVIDERS:
        raise InvalidAlarmData("unknown provider")
    flags = _booleans(doc, _SETTINGS_FIELDS - {"provider"}, "settings")
    return AlarmSettings(provider=provider, **flags)


def load_settings(path: Optional[Path] = None) -> AlarmSettings:
    return _load(path or ALARM_SETTINGS_FILE, AlarmSettings(), parse_settings)


def save_settings(settings: AlarmSettings, path: Optional[Path] = None) -> None:
    _atomic_write(path or ALARM_SETTINGS_FILE, {"schema_version": SCHEMA_VERSION, **asdict(settings)})


def _parse_ledger(payload: Any) -> AlarmLedger:
    doc = _document(payload)
    handled = doc.get("handled_event")
    if handled is not None and not isinstance(handled, str):
        raise InvalidAlarmData("handled_event must be a string")
    flags = _booleans(doc, _LEDGER_FIELDS - {"handled_event"}, "state")
    return AlarmLedger(handled_event=handled, **flags)


def load_ledger(path: Optional[Path] = None) -> AlarmLedger:
    return _load(path or ALARM_STATE_FILE, AlarmLedger(), _parse_ledger)


def save_ledger(ledger: AlarmLedger, path: Optional[Path] = None) -> None:
    _atomic_write(path or ALARM_STATE_FILE, {"schema_version": SCHEMA_VERSION, **asdict(ledger)})


def _optional_text(value: Any, where: str) -> Optional[str]:
    if value is not None and not isinstance(value, str):
        raise InvalidAlarmData(f"{where} must be a string")
    return value


def _parse_simulated(payload: Any) -> Dict[str, Any]:
    doc = _document(payload)
    arm_state = doc.get("arm_state")
    if arm_state not in ARM_STATES:
        raise InvalidAlarmData("unknown arm_state")
    locks = doc.get("locks")
    if not isinstance(locks, list) or not locks:
        raise InvalidAlarmData("locks must be a non-empty list")
    parsed = []
    for lock in locks:
        if not isinstance(lock, dict) or not isinstance(lock.get("lock_id"), str):
            raise InvalidAlarmData("lock must have a lock_id")
        locked = lock.get("locked")
        if locked is not None and not isinstance(locked, bool):
            raise InvalidAlarmData("locked must be a boolean")
        parsed.append({
            "lock_id": lock["lock_id"],
            "name": _optional_text(lock.get("name"), "name") or lock["lock_id"],
            "locked": locked,
            "method": _optional_text(lock.get("method"), "method"),
            "changed_at": _optional_text(lock.get("changed_at"), "changed_at"),
        })
    return {
        "arm_state": arm_state,
        "arm_changed_at": _optional_text(doc.get("arm_changed_at"), "arm_changed_at"),
        "locks": parsed,
    }


def load_simulated(path: Optional[Path] = None) -> Dict[str, Any]:
    return _load(path or SIMULATED_ALARM_FILE, default_simulated_state(), _parse_simulated)


def save_simulated(state: Mapping[str, Any], path: Optional[Path] = None) -> None:
    _atomic_write(path or SIMULATED_ALARM_FILE, {"schema_version": SCHEMA_VERSION, **state})


# --- the Verisure session: secrets ---------------------------------------

def secure_dir(path: Optional[Path] = None) -> Path:
    """The private directory for Verisure's files, created 0700."""
    directory = path or VERISURE_DIR
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def _parse_session(payload: Any) -> Dict[str, Any]:
    doc = _document(payload)
    email = doc.get("email")
    cookies = doc.get("cookies")
    if not isinstance(email, str) or not email:
        raise InvalidAlarmData("email missing")
    if not isinstance(cookies, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in cookies.items()
    ):
        raise InvalidAlarmData("cookies must map names to values")
    installations = doc.get("installations", [])
    if not isinstance(installations, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("giid"), str)
        and isinstance(item.get("alias", ""), str)
        for item in installations
    ):
        raise InvalidAlarmData("installations malformed")
    refreshed = doc.get("refreshed_at", 0)
    if not isinstance(refreshed, (int, float)):
        raise InvalidAlarmData("refreshed_at must be a number")
    return {
        "email": email,
        "cookies": dict(cookies),
        "trust_token": _optional_text(doc.get("trust_token"), "trust_token"),
        "giid": _optional_text(doc.get("giid"), "giid"),
        "installations": [
            {"giid": item["giid"], "alias": item.get("alias", "")} for item in installations
        ],
        "refreshed_at": float(refreshed),
    }


def load_session(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    # A damaged session is deleted rather than kept as a .backup: it holds
    # tokens, and a stray copy of them is exactly what must not exist.
    return _load(path or VERISURE_SESSION_FILE, None, _parse_session, keep_invalid=False)


def save_session(session: Mapping[str, Any], path: Optional[Path] = None) -> None:
    target = path or VERISURE_SESSION_FILE
    secure_dir(target.parent)
    _atomic_write(target, {"schema_version": SCHEMA_VERSION, **session}, mode=0o600)


def delete_session(path: Optional[Path] = None) -> None:
    target = path or VERISURE_SESSION_FILE
    target.unlink(missing_ok=True)
    target.with_suffix(".tmp").unlink(missing_ok=True)
