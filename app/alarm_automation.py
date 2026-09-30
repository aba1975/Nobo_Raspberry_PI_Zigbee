"""What the alarm means for the heating and for open windows. Pure decisions.

Nothing here reads a file, a clock or the network, so every rule can be tested
by handing it a reading and looking at the answer.

The heating rule is built around one idea: the alarm may only undo what the
alarm did. Arming puts the house on Away and records that the alarm owns that
Away. Disarming lifts it only if the alarm still owns it — nobody has pressed
anything since, and the house is still on Away. A holiday set in the app, an
away period, or somebody coming home early and choosing Comfort all take
ownership away, and from then on the alarm leaves the house alone until it is
next armed.

Each arming is recognised by its state and time together, so a restart that
reads "armed away since 08:02" again knows it has already acted on it — and in
particular does not put back an Away somebody lifted by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from alarm_persistence import AlarmLedger, AlarmSettings
from alarm_provider import AlarmReading, LockReading

# The value of ``global_mode_source`` while the alarm owns the house's Away.
SOURCE = "alarm"

# Ordered by how certain it is that nobody is inside.
LEAVING_REASONS = ("armed_away", "armed_home", "locked_outside")


@dataclass(frozen=True)
class Leaving:
    reason: str
    lock_name: Optional[str] = None
    since: Optional[str] = None


@dataclass(frozen=True)
class HeatingDecision:
    """``action`` is "away", "home" or None. ``ledger`` is what to keep once
    the action has succeeded; if it fails, the old ledger is kept, and the
    same decision is reached again on the next reading."""

    action: Optional[str]
    ledger: AlarmLedger


def lock_is_outside(lock: LockReading, settings: AlarmSettings) -> bool:
    """Whether a lock is locked in a way that means somebody left.

    Only the thumb turn is proof of somebody inside. An unrecognised method
    counts as outside, because the cost of being wrong that way is one warning
    about a window; the other way it is a silent window left open.
    """
    if lock.locked is not True:
        return False
    method = (lock.method or "").lower()
    if method == "thumb":
        return False
    if method == "auto":
        return settings.autolock_counts_as_leaving
    return True


def leaving(reading: Optional[AlarmReading], settings: AlarmSettings) -> Optional[Leaving]:
    """Why open doors and windows now matter, or None if they do not."""
    if reading is None:
        return None
    if reading.arm_state == "armed_away" and settings.warn_when_armed_away:
        return Leaving("armed_away", since=reading.arm_changed_at)
    if reading.arm_state == "armed_home" and settings.warn_when_armed_home:
        return Leaving("armed_home", since=reading.arm_changed_at)
    if settings.warn_when_locked_outside:
        for lock in reading.locks:
            if lock_is_outside(lock, settings):
                return Leaving("locked_outside", lock_name=lock.name, since=lock.changed_at)
    return None


def event_id(reading: AlarmReading) -> str:
    return f"{reading.arm_state}@{reading.arm_changed_at or ''}"


def wants_away(arm_state: Optional[str], settings: AlarmSettings) -> bool:
    return (
        (arm_state == "armed_away" and settings.away_when_armed_away)
        or (arm_state == "armed_home" and settings.away_when_armed_home)
    )


def decide_heating(
    ledger: AlarmLedger,
    reading: Optional[AlarmReading],
    settings: AlarmSettings,
    *,
    global_mode: Optional[str],
    global_source: str,
    hub_connected: bool,
    may_act: bool,
) -> HeatingDecision:
    """What, if anything, the alarm should do to the house's global mode.

    ``global_mode`` is the global override in force ("away", "eco", ...) or
    None. ``global_source`` is who set it, as far as this application knows.
    """
    if not hub_connected or reading is None or reading.arm_state is None:
        # Nothing is decided on a guess: without the hub the current mode is
        # unknown, and without a reading the alarm's is.
        return HeatingDecision(None, ledger)

    # Somebody else has set the global mode since the alarm did. It is theirs.
    owns = ledger.owns_away and global_source == SOURCE
    event = event_id(reading)
    if event == ledger.handled_event:
        return HeatingDecision(None, replace(ledger, owns_away=owns))

    handled = replace(ledger, handled_event=event)
    if wants_away(reading.arm_state, settings):
        if not may_act or global_mode == "away":
            # Already away — a holiday, or Away pressed before leaving. That
            # Away belongs to whoever set it, so disarming will not lift it.
            return HeatingDecision(None, replace(handled, owns_away=owns))
        return HeatingDecision("away", replace(handled, owns_away=True))

    if owns and global_mode == "away":
        return HeatingDecision("home", replace(handled, owns_away=False))
    return HeatingDecision(None, replace(handled, owns_away=False))
