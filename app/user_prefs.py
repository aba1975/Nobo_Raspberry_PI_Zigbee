"""
user_prefs.py — choices that belong to one signed-in person, not to the house.

Kept on the Pi rather than in the browser, so a choice follows its owner to
every phone and survives a cleared browser. It is keyed by username, so a
rename moves it and deleting the user removes it.

Only display choices live here. Nothing that changes the heating may: two
people in the same house must never get different heating from the same tap.

``data/user_prefs.json``::

    {"schema": 1, "users": {"anders": {"rooms_view": "house"}}}
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent / "data"
PREFS_FILENAME = "user_prefs.json"
SCHEMA_VERSION = 1

# Each preference with its allowed values; the first is the default, and the
# default is always what the application looked like before the choice existed.
CHOICES: Dict[str, tuple] = {
    # "list" is the front page as it has always been. "house" replaces the
    # room cards with the house view, once a floor plan is loaded.
    "rooms_view": ("list", "house"),
}

DEFAULTS = {key: values[0] for key, values in CHOICES.items()}


class PreferenceError(ValueError):
    """A preference or its value is not one this application knows."""


def _path() -> Path:
    return DATA_DIR / PREFS_FILENAME


def _load_all() -> Dict[str, Dict[str, str]]:
    try:
        with _path().open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        # Losing a display choice costs one tap; refusing to start costs the heating.
        logger.warning("user_prefs.json could not be read (%s); using defaults", exc)
        return {}
    users = raw.get("users") if isinstance(raw, dict) else None
    if not isinstance(users, dict):
        return {}
    clean: Dict[str, Dict[str, str]] = {}
    for username, prefs in users.items():
        if not isinstance(username, str) or not isinstance(prefs, dict):
            continue
        kept = {key: value for key, value in prefs.items()
                if key in CHOICES and value in CHOICES[key]}
        if kept:
            clean[username] = kept
    return clean


def _save_all(users: Dict[str, Dict[str, str]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _path()
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump({"schema": SCHEMA_VERSION, "users": users}, fh, indent=2, sort_keys=True)
    tmp.replace(path)


def get(username: str) -> Dict[str, str]:
    """Every preference for *username*, with defaults filled in."""
    return {**DEFAULTS, **_load_all().get(username, {})}


def update(username: str, changes: Any) -> Dict[str, str]:
    """Apply *changes* (a dict) for *username* and return the full result.

    The whole change is checked before anything is written, so a request with
    one bad value changes nothing.
    """
    if not isinstance(changes, dict) or not changes:
        raise PreferenceError("Send at least one preference to change")
    for key, value in changes.items():
        if key not in CHOICES:
            raise PreferenceError(f"Unknown preference '{key}'")
        if value not in CHOICES[key]:
            raise PreferenceError(f"'{key}' must be one of {', '.join(CHOICES[key])}")
    users = _load_all()
    mine = {**users.get(username, {}), **changes}
    # Only what differs from the default is stored, so the file stays a list
    # of people who chose something.
    mine = {key: value for key, value in mine.items() if value != DEFAULTS[key]}
    if mine:
        users[username] = mine
    else:
        users.pop(username, None)
    _save_all(users)
    return {**DEFAULTS, **mine}


def rename_user(old: str, new: str) -> None:
    users = _load_all()
    if old in users:
        users[new] = users.pop(old)
        _save_all(users)


def remove_user(username: str) -> None:
    users = _load_all()
    if users.pop(username, None) is not None:
        _save_all(users)
