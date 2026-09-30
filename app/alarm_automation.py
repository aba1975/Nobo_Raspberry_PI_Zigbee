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

The front-door lock can do the same, if asked to: locked from outside may put
the house on Eco or Away, and locked from inside may put it on Eco for the
night. The alarm outranks the lock — armed away is more certain than a locked
door — and whatever either set is owned the same way and released the same
way. Away is never swapped for Eco: that would warm a house somebody chose
to leave on Away.

Each situation is recognised by what caused it and when, so a restart that
reads "armed away since 08:02" again knows it has already acted on it — and in
particular does not put back an Away somebody lifted by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Tuple

from alarm_persistence import AlarmLedger, AlarmSettings
from alarm_provider import AlarmReading, LockReading

# The value of ``global_mode_source`` while the alarm owns the house's Away.
SOURCE = "alarm"

# Ordered by how certain it is that nobody is inside. Locked from inside is
# not leaving at all, but it is a moment worth checking the windows.
LEAVING_REASONS = ("armed_away", "armed_home", "locked_outside", "locked_inside")


@dataclass(frozen=True)
class Leaving:
    reason: str
    lock_name: Optional[str] = None
    since: Optional[str] = None


@dataclass(frozen=True)
class HeatingDecision:
    """``action`` is "away", "eco", "home" or None. ``ledger`` is what to keep
    once the action has succeeded; if it fails, the old ledger is kept, and the
    same decision is reached again on the next reading. ``reason`` says what
    asked for it, for the log."""

    action: Optional[str]
    ledger: AlarmLedger
    reason: Optional[str] = None


def lock_side(lock: LockReading, settings: AlarmSettings) -> Optional[str]:
    """"inside", "outside", or None when the lock is not locked.

    Only the thumb turn is proof of somebody inside by default. An
    unrecognised method counts as outside, because the cost of being wrong
    that way is one warning about a window; the other way it is a silent
    window left open. The user can say otherwise per method in Settings.
    """
    if lock.locked is not True:
        return None
    return settings.lock_side(lock.method)


def lock_is_outside(lock: LockReading, settings: AlarmSettings) -> bool:
    """Whether a lock is locked in a way that means somebody left."""
    return lock_side(lock, settings) == "outside"


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
    if settings.warn_when_locked_inside:
        for lock in reading.locks:
            if lock_side(lock, settings) == "inside":
                return Leaving("locked_inside", lock_name=lock.name, since=lock.changed_at)
    return None


def event_id(reading: AlarmReading) -> str:
    return f"{reading.arm_state}@{reading.arm_changed_at or ''}"


def wants_away(arm_state: Optional[str], settings: AlarmSettings) -> bool:
    return (
        (arm_state == "armed_away" and settings.away_when_armed_away)
        or (arm_state == "armed_home" and settings.away_when_armed_home)
    )


def heating_intent(
    reading: AlarmReading, settings: AlarmSettings
) -> Tuple[str, Optional[str], Optional[str]]:
    """(cause, the global mode wanted or None, a reason for the log).

    The cause names the situation and when it began. An armed alarm keeps the
    same cause the alarm always used, so a ledger written before the lock
    could act still recognises the arming it already handled.
    """
    if wants_away(reading.arm_state, settings):
        return event_id(reading), "away", reading.arm_state
    for side, choice in (
        ("outside", settings.heating_when_locked_outside),
        ("inside", settings.heating_when_locked_inside),
    ):
        if choice == "none":
            continue
        for lock in reading.locks:
            if lock_side(lock, settings) == side:
                cause = f"lock:{lock.lock_id}:{side}@{lock.changed_at or ''}"
                return cause, choice, f"locked_{side}"
    return event_id(reading), None, None


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

    # Somebody else has set the global mode since the alarm did, or it has
    # been changed on the hub. It is theirs.
    owned = (
        ledger.owned_mode
        if ledger.owned_mode is not None
        and global_source == SOURCE
        and global_mode == ledger.owned_mode
        else None
    )
    cause, wanted, reason = heating_intent(reading, settings)
    if cause == ledger.handled_event:
        return HeatingDecision(None, replace(ledger, owned_mode=owned))

    handled = replace(ledger, handled_event=cause)
    if wanted is not None:
        if global_mode == wanted:
            # Already there. If the alarm put it there it still owns it — the
            # lock's Away carries on as the alarm's Away. Otherwise it belongs
            # to whoever set it — a holiday, or Away pressed before leaving —
            # and will not be lifted by disarming.
            return HeatingDecision(None, replace(handled, owned_mode=owned))
        if not may_act:
            return HeatingDecision(None, replace(handled, owned_mode=owned))
        if global_mode == "away" and owned is None:
            # Somebody else's Away is colder than anything the alarm or the
            # lock would choose, and it stays.
            return HeatingDecision(None, replace(handled, owned_mode=None))
        return HeatingDecision(wanted, replace(handled, owned_mode=wanted), reason)

    if owned is not None:
        return HeatingDecision("home", replace(handled, owned_mode=None), "released")
    return HeatingDecision(None, replace(handled, owned_mode=None))
