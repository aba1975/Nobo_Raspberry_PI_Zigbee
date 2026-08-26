"""
notify_watch.py — working out what actually happened, from what the hub says.

The hub does not tell us *why* anything changed. It pushes a new picture of the
world and leaves us to work out the story. This module holds that reasoning,
deliberately separated from both the server and the sending, so it can be
tested against a sequence of snapshots with no hub, no network and no clock.

Three things are detected here, and all three work on ordinary Nobø hardware —
which is the constraint that shaped this file.

**Somebody changed something, and it was not us.** The Nobø override struct has
no source field, so this is done by elimination: every write this application
makes is recorded first, and a change that arrives without a matching record
came from somewhere else — the official Nobø app, another browser, or the hub
itself expiring an override. We can say *that* it was not us; we can never say
which of the others it was, and the wording is careful not to pretend otherwise.

**A weekly schedule event started.** ``schedule_mode`` is what the week profile
says right now, so a change in it while the zone follows its schedule is a
scheduled switch rather than somebody interfering.

**A room has been left switched off.** Off is below Away: no heating at all, not
even anti-frost. This is the only frost-related thing that can be seen on a
house with no thermometers, because it is a fact about the configuration rather
than about the building.

What is deliberately not here
-----------------------------
Cold rooms, silent thermostats and heaters that cannot keep up were all
implemented and then removed. Every one needs a measured room temperature; only
the SW4 reports one, and it is no longer sold. They are not commented out or
left switched off, because an alert that cannot fire is worse than none — the
owner believes the cabin is watched when nothing is watching it.

Nor is there any way to see a heater that has lost power. The radio to a
receiver is one-way, with no acknowledgement and no keep-alive, and the
component ``Status`` field is "not yet implemented, always 0". The keep-alive in
this system runs between the Pi and the hub and nowhere else; see
``notifications.py``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# How long a write we made stays "ours". Generous enough to cover the hub
# echoing a change back slowly, short enough that a genuinely separate change a
# minute later is not wrongly credited to us.
LOCAL_WRITE_TTL_SECONDS = 120

MODE_LABELS = {
    "comfort": "Comfort",
    "eco": "Eco",
    "away": "Away",
    "off": "Off",
    "normal": "its weekly schedule",
}


def _label(mode: Optional[str]) -> str:
    if not mode:
        return "unknown"
    return MODE_LABELS.get(mode, str(mode))


@dataclass
class _ZoneMemory:
    """What we last saw for one zone, and how long any problem has been true."""
    mode: Optional[str] = None
    schedule_mode: Optional[str] = None
    comfort: Optional[float] = None
    eco: Optional[float] = None
    # Kept only so an alert can quote a reading if one happens to exist.
    temperature: Optional[float] = None
    off_since: float = 0.0
    seen: bool = True


@dataclass
class ZoneWatcher:
    """Turns a stream of zone snapshots into notifications."""

    notifier: Any
    now_fn: Callable[[], float] = time.time
    _zones: Dict[str, _ZoneMemory] = field(default_factory=dict)
    _local_writes: Dict[str, float] = field(default_factory=dict)
    _started_at: float = 0.0
    _primed: bool = False

    def __post_init__(self):
        self._started_at = self.now_fn()

    # -- knowing what we did ------------------------------------------------

    @staticmethod
    def _write_key(zone_id: str, field_name: str, value: Any) -> str:
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return f"{zone_id}:{field_name}:{value}"

    def record_local_write(self, zone_id: str, field_name: str, value: Any) -> None:
        """
        Remember that *we* just set this, so the echo is not reported as somebody else.

        ``zone_id`` may be ``"*"`` for a change applied to every zone at once,
        which is what a global mode does.
        """
        now = self.now_fn()
        self._local_writes[self._write_key(str(zone_id), field_name, value)] = now
        # Opportunistic cleanup; this dict is tiny and only grows on writes.
        for key, stamp in list(self._local_writes.items()):
            if now - stamp > LOCAL_WRITE_TTL_SECONDS:
                self._local_writes.pop(key, None)

    def _was_ours(self, zone_id: str, field_name: str, value: Any) -> bool:
        """True if we caused this change; consumes the record so it counts once."""
        now = self.now_fn()
        for key in (self._write_key(str(zone_id), field_name, value),
                    self._write_key("*", field_name, value)):
            stamp = self._local_writes.get(key)
            if stamp is not None and (now - stamp) <= LOCAL_WRITE_TTL_SECONDS:
                # A global write is left in place: it explains one change per
                # zone, not just the first zone we happen to look at.
                if not key.startswith("*:"):
                    self._local_writes.pop(key, None)
                return True
        return False

    # -- the main entry point ----------------------------------------------

    def observe(self, zones: List[Dict[str, Any]]) -> None:
        """
        Take a fresh picture of every zone and report anything worth reporting.

        The first call only records: at start-up everything looks like it just
        changed, and mailing the owner a summary of the entire house every time
        the service restarts would be intolerable.
        """
        try:
            settings = self.notifier.settings
            off_for = float(settings.get("off_for_hours", 24)) * 3600
        except Exception:
            off_for = 86400.0

        now = self.now_fn()
        first_pass = not self._primed

        for mem in self._zones.values():
            mem.seen = False

        for zone in zones or []:
            zone_id = str(zone.get("zone_id"))
            name = zone.get("name") or f"Zone {zone_id}"
            mem = self._zones.get(zone_id)
            if mem is None:
                mem = _ZoneMemory()
                self._zones[zone_id] = mem
                new_zone = True
            else:
                new_zone = False
            mem.seen = True

            mode = zone.get("current_mode")
            schedule_mode = zone.get("schedule_mode")
            comfort = _as_float(zone.get("comfort_temperature"))
            eco = _as_float(zone.get("eco_temperature"))
            temp = _as_float(zone.get("current_temperature"))

            if not first_pass and not new_zone:
                self._check_mode(zone_id, name, mem, mode)
                self._check_setpoint(zone_id, name, mem, "comfort", mem.comfort, comfort)
                self._check_setpoint(zone_id, name, mem, "eco", mem.eco, eco)
                self._check_schedule(zone_id, name, mem, mode, schedule_mode)

            mem.mode = mode
            mem.schedule_mode = schedule_mode
            mem.comfort = comfort
            mem.eco = eco

            # -- switched off ------------------------------------------------
            # The only frost-related thing visible on this hardware, because it
            # is a fact about the configuration rather than about the building.
            self._check_off(zone_id, name, mem, zone, off_for, now)

            # A reading is kept if one happens to arrive, purely so an alert can
            # quote it. Nothing depends on it, because almost nothing reports it.
            if temp is not None:
                mem.temperature = temp

        # A zone that vanished should not keep an alarm raised forever.
        for zone_id, mem in list(self._zones.items()):
            if not mem.seen:
                self.notifier.set_condition("zone_off", f"zone_off:{zone_id}", False)
                self._zones.pop(zone_id, None)

        self._primed = True

    # -- individual checks --------------------------------------------------

    def _check_mode(self, zone_id: str, name: str, mem: _ZoneMemory, mode: Optional[str]) -> None:
        if mode == mem.mode or mode is None or mem.mode is None:
            return
        if self._was_ours(zone_id, "mode", mode):
            return
        self.notifier.notify(
            "changed_elsewhere",
            f"{name} was changed to {_label(mode)}",
            f"{name} is now set to {_label(mode)}, and it was not changed from here.\n\n"
            f"It was {_label(mem.mode)} before.\n\n"
            "That means somebody used the Nobø app, another browser, or the change came\n"
            "from the hub itself — for example an override reaching its end time.\n\n"
            "The hub does not record which app made a change, so this is as much as\n"
            "can honestly be said.",
            severity="info",
            key=f"changed_mode:{zone_id}",
        )

    def _check_setpoint(self, zone_id: str, name: str, mem: _ZoneMemory,
                        which: str, before: Optional[float], after: Optional[float]) -> None:
        if before is None or after is None or before == after:
            return
        if self._was_ours(zone_id, which, after):
            return
        self.notifier.notify(
            "changed_elsewhere",
            f"{name}: {which} temperature changed to {after:.0f}°C",
            f"The {which} temperature for {name} changed from {before:.0f}°C to {after:.0f}°C,\n"
            "and it was not changed from here.\n\n"
            "Somebody used the Nobø app or another browser. The hub does not record\n"
            "which app made a change, so this is as much as can honestly be said.",
            severity="info",
            key=f"changed_{which}:{zone_id}",
        )

    def _check_schedule(self, zone_id: str, name: str, mem: _ZoneMemory,
                        mode: Optional[str], schedule_mode: Optional[str]) -> None:
        if mode != "normal" or schedule_mode is None or schedule_mode == mem.schedule_mode:
            return
        if mem.schedule_mode is None:
            return
        self.notifier.notify(
            "schedule_event",
            f"{name} switched to {_label(schedule_mode)}",
            f"{name} moved from {_label(mem.schedule_mode)} to {_label(schedule_mode)},\n"
            "because that is what its weekly schedule says for this time.",
            severity="info",
            key=f"schedule:{zone_id}",
        )

    def _check_off(self, zone_id: str, name: str, mem: _ZoneMemory,
                   zone: Dict[str, Any], off_for: float, now: float) -> None:
        """
        A room the schedule is holding Off, for long enough to be a mistake.

        This is the one frost-related thing that can be seen on a house with no
        thermometers anywhere, because it is a fact about the *configuration*
        rather than about the building. It does not catch a thermostat switched
        off at the wall — nothing can, the hub is never told — but it does catch
        the same mistake made in software, which is the half that is visible.
        """
        key = f"zone_off:{zone_id}"
        is_off = zone.get("current_mode") == "normal" and zone.get("schedule_mode") == "off"

        if not is_off:
            mem.off_since = 0.0
            self.notifier.set_condition(
                "zone_off", key, False,
                recovery_subject=f"{name} is heating again",
                recovery_body=f"{name} is no longer switched off by its schedule.",
            )
            return

        if not mem.off_since:
            mem.off_since = now
        if (now - mem.off_since) < off_for and not self.notifier.is_raised(key):
            return

        hours = int((now - mem.off_since) / 3600)
        self.notifier.set_condition(
            "zone_off", key, True,
            subject=f"{name} has been switched off for {hours} hours",
            body=(
                f"{name} is set to Off in its weekly schedule and has been for about\n"
                f"{hours} hours.\n\n"
                "Off means no heating at all — not even the 7°C anti-frost that Away\n"
                "gives you. If there is water anywhere in this room, that is a risk.\n\n"
                "If this is deliberate, you can turn this alert off under Settings."
            ),
            severity="critical",
            recovery_subject=f"{name} is heating again",
            recovery_body=f"{name} is no longer switched off by its schedule.",
        )


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        # This is the Y02 "N/A" path, and it is not an error: the hub is
        # telling us the value went stale.
        return None
