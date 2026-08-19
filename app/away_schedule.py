"""
away_schedule.py — Away schedule persistence and helpers for Nobø Web Control

Provides:
- File-based schedule store (data/away_schedule.json)
- Schedule validation (ISO-8601 datetimes, end > start)
- Pure helper: is_schedule_active(schedule, now)
- load/save/clear helpers
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Storage paths (can be overridden in tests via monkeypatching)
# ---------------------------------------------------------------------------
# Resolved from this file's location, not the working directory (QA defect D-03).
DATA_DIR = Path(__file__).resolve().parent / "data"
SCHEDULE_FILE = DATA_DIR / "away_schedule.json"

# Default/empty schedule
_EMPTY_SCHEDULE = {"enabled": False, "start_at": None, "end_at": None}


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def load_schedule() -> dict:
    """Load schedule from file; return empty/disabled schedule on error or absence."""
    try:
        with SCHEDULE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # Ensure all required keys are present
        return {
            "enabled": bool(data.get("enabled", False)),
            "start_at": data.get("start_at") or None,
            "end_at": data.get("end_at") or None,
        }
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_EMPTY_SCHEDULE)


def save_schedule(schedule: dict) -> None:
    """Persist schedule to file atomically."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SCHEDULE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(schedule, f, indent=2)
    tmp.replace(SCHEDULE_FILE)


def clear_schedule() -> dict:
    """Reset schedule to disabled/empty and persist."""
    empty = dict(_EMPTY_SCHEDULE)
    save_schedule(empty)
    return empty


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def parse_iso_dt(value: str) -> Optional[datetime]:
    """
    Parse an ISO-8601 datetime string and return a timezone-aware datetime.
    Returns None if parsing fails.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # Treat naive datetimes as UTC
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def validate_schedule(
    enabled: bool,
    start_at: Optional[str],
    end_at: Optional[str],
    now: Optional[datetime] = None,
) -> tuple:
    """
    Validate schedule fields.
    Returns (is_valid: bool, error_message: str | None).
    """
    if not enabled:
        # When disabling we don't require dates
        return True, None

    if not start_at or not end_at:
        return False, "start_at and end_at are required when enabling a schedule"

    start_dt = parse_iso_dt(start_at)
    if start_dt is None:
        return False, f"Invalid start_at datetime: {start_at!r}"

    end_dt = parse_iso_dt(end_at)
    if end_dt is None:
        return False, f"Invalid end_at datetime: {end_at!r}"

    if end_dt <= start_dt:
        return False, "end_at must be strictly after start_at"

    # A schedule that has already ended is saved as "enabled", shows up in the
    # UI as an upcoming holiday and then never does anything, which reads as a
    # broken feature. Usually it means the year was typed wrong.
    if now is None:
        now = datetime.now(timezone.utc)
    if end_dt <= now:
        return False, (
            "The end of the away period is already in the past, so the schedule "
            "would never run. Check the date and try again."
        )

    return True, None


# ---------------------------------------------------------------------------
# Schedule state helpers
# ---------------------------------------------------------------------------

def is_schedule_active(schedule: dict, now: Optional[datetime] = None) -> bool:
    """
    Return True if the schedule is enabled and *now* falls within [start_at, end_at).
    *now* defaults to datetime.now(timezone.utc) if not provided.
    """
    if not schedule.get("enabled"):
        return False

    if now is None:
        now = datetime.now(timezone.utc)

    start_dt = parse_iso_dt(schedule.get("start_at"))
    end_dt = parse_iso_dt(schedule.get("end_at"))

    if start_dt is None or end_dt is None:
        return False

    return start_dt <= now < end_dt


def is_schedule_expired(schedule: dict, now: Optional[datetime] = None) -> bool:
    """Return True if the schedule is enabled but end_at is in the past."""
    if not schedule.get("enabled"):
        return False

    if now is None:
        now = datetime.now(timezone.utc)

    end_dt = parse_iso_dt(schedule.get("end_at"))
    if end_dt is None:
        return False

    return now >= end_dt
