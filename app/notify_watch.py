"""
notify_watch.py — working out what actually happened, from what the hub says.

The hub does not tell us *why* anything changed. It pushes a new picture of the
world and leaves us to work out the story. This module holds that reasoning,
deliberately separated from both the server and the sending, so it can be
tested against a sequence of snapshots with no hub, no network and no clock.

Four things are detected here.

**Somebody changed something, and it was not us.** The Nobø override struct has
no source field, so this is done by elimination: every write this application
makes is recorded first, and a change that arrives without a matching record
came from somewhere else — the official Nobø app, another browser, or the hub
itself expiring an override. We can say *that* it was not us; we can never say
which of the others it was, and the wording is careful not to pretend otherwise.

**A weekly schedule event started.** ``schedule_mode`` is what the week profile
says right now, so a change in it while the zone follows its schedule is a
scheduled switch rather than somebody interfering.

**A room got cold.** The frost alarm, and the reason the feature exists.

**A thermostat went quiet.** ``Y02`` reports a temperature of ``N/A`` once the
hub's stored value goes stale, which the server turns into ``None``. A room that
*used to* report a temperature and has stopped has almost certainly had its
thermostat switched off or lost power. Rooms that never reported one — a dial
only R80, say — are ignored, because for them ``None`` is simply normal.

The last two are deliberately slow. A room takes hours to freeze, so waiting
half an hour before crying about it costs nothing and avoids alerting on a door
propped open while the car is unloaded.
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

# A room must climb this far back above the threshold before we call it
# recovered. Without it, a room hovering exactly on the line would alternate
# between alarm and all-clear all night.
COLD_HYSTERESIS_C = 1.0

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


def _fmt_temp(value: Optional[float]) -> str:
    return "no reading" if value is None else f"{value:.1f}°C"


@dataclass
class _ZoneMemory:
    """What we last saw for one zone, and how long any problem has been true."""
    mode: Optional[str] = None
    schedule_mode: Optional[str] = None
    comfort: Optional[float] = None
    eco: Optional[float] = None
    temperature: Optional[float] = None
    # None until the room has ever reported a temperature. This is what keeps
    # dial-only rooms, which never report, out of the silent-sensor alarm.
    ever_reported: bool = False
    last_reading_at: float = 0.0
    cold_since: float = 0.0
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
            cold_threshold = float(settings.get("cold_threshold_c", 5.0))
            cold_for = float(settings.get("cold_for_minutes", 30)) * 60
            silent_after = float(settings.get("silent_after_minutes", 180)) * 60
        except Exception:
            cold_threshold, cold_for, silent_after = 5.0, 1800.0, 10800.0

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

            # -- temperature-driven conditions ------------------------------
            if temp is not None:
                mem.ever_reported = True
                mem.last_reading_at = now
                mem.temperature = temp
                self._check_silent(zone_id, name, mem, silent_after, now, reporting=True)
                self._check_cold(zone_id, name, mem, temp, zone, cold_threshold, cold_for, now)
            else:
                # No reading. Either this room never had a sensor, or its
                # sensor has stopped talking - only the latter is news.
                if mem.ever_reported:
                    self._check_silent(zone_id, name, mem, silent_after, now, reporting=False)
                # A room we cannot measure cannot be called cold.
                self._clear_cold(zone_id, name, mem)

        # A zone that vanished should not keep an alarm raised forever.
        for zone_id, mem in list(self._zones.items()):
            if not mem.seen:
                self.notifier.set_condition("room_cold", f"room_cold:{zone_id}", False)
                self.notifier.set_condition("sensor_silent", f"sensor_silent:{zone_id}", False)
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

    def _check_cold(self, zone_id: str, name: str, mem: _ZoneMemory, temp: float,
                    zone: Dict[str, Any], threshold: float, cold_for: float, now: float) -> None:
        key = f"room_cold:{zone_id}"
        if temp < threshold:
            if not mem.cold_since:
                mem.cold_since = now
            if (now - mem.cold_since) < cold_for and not self.notifier.is_raised(key):
                return
            expected = self._expected_temp(zone)
            detail = f"\nIt is set to {_label(zone.get('current_mode'))}"
            if expected is not None:
                detail += f", which should hold about {expected:.0f}°C"
            detail += ".\n"
            self.notifier.set_condition(
                "room_cold", key, True,
                subject=f"{name} is cold: {temp:.1f}°C",
                body=(
                    f"{name} has been below {threshold:.0f}°C for "
                    f"{int((now - mem.cold_since) / 60)} minutes.\n"
                    f"It is now {temp:.1f}°C.\n"
                    f"{detail}\n"
                    "Something is stopping this room from heating. The usual causes are a\n"
                    "thermostat switched off at the wall, a tripped breaker, a window left\n"
                    "open, or a heater that has failed.\n\n"
                    "If there is water in this part of the building, it is worth acting on."
                ),
                severity="critical",
                recovery_subject=f"{name} is warming up again",
                recovery_body=f"{name} is back above {threshold:.0f}°C. It is now {temp:.1f}°C.",
            )
        elif temp >= threshold + COLD_HYSTERESIS_C:
            mem.cold_since = 0.0
            self._clear_cold(zone_id, name, mem, temp=temp, threshold=threshold)

    def _clear_cold(self, zone_id: str, name: str, mem: _ZoneMemory,
                    temp: Optional[float] = None, threshold: Optional[float] = None) -> None:
        key = f"room_cold:{zone_id}"
        if not self.notifier.is_raised(key):
            return
        if temp is None:
            # Cleared because the reading vanished, not because it warmed up.
            self.notifier.set_condition("room_cold", key, False)
            return
        self.notifier.set_condition(
            "room_cold", key, False,
            recovery_subject=f"{name} is warming up again",
            recovery_body=f"{name} is back above {threshold:.0f}°C. It is now {temp:.1f}°C.",
        )

    def _check_silent(self, zone_id: str, name: str, mem: _ZoneMemory,
                      silent_after: float, now: float, reporting: bool) -> None:
        key = f"sensor_silent:{zone_id}"
        if reporting:
            self.notifier.set_condition(
                "sensor_silent", key, False,
                recovery_subject=f"{name} is reporting its temperature again",
                recovery_body=(
                    f"The thermostat in {name} is sending readings again. "
                    f"It reports {_fmt_temp(mem.temperature)}."
                ),
            )
            return

        # Never reported since we started watching: we have no idea how long it
        # has been quiet, so wait for the timer to run from start-up instead.
        reference = mem.last_reading_at or self._started_at
        if (now - reference) < silent_after:
            return
        self.notifier.set_condition(
            "sensor_silent", key, True,
            subject=f"{name} has stopped reporting its temperature",
            body=(
                f"The hub has had no temperature from {name} for over "
                f"{int(silent_after / 60)} minutes.\n\n"
                "The hub reports a temperature as unavailable once its stored value goes\n"
                "stale, which is what happens when a thermostat is switched off at the\n"
                "wall, loses power, or drops off the radio.\n\n"
                "Worth checking, because a room with no thermostat reporting is also a\n"
                "room nothing is watching for frost.\n\n"
                f"The last reading was {_fmt_temp(mem.temperature)}."
            ),
            severity="warning",
            recovery_subject=f"{name} is reporting its temperature again",
            recovery_body=f"The thermostat in {name} is sending readings again.",
        )

    @staticmethod
    def _expected_temp(zone: Dict[str, Any]) -> Optional[float]:
        mode = zone.get("current_mode")
        if mode == "normal":
            mode = zone.get("schedule_mode")
        return {
            "comfort": _as_float(zone.get("comfort_temperature")),
            "eco": _as_float(zone.get("eco_temperature")),
            "away": _as_float(zone.get("away_temperature")),
        }.get(mode)


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        # This is the Y02 "N/A" path, and it is not an error: the hub is
        # telling us the value went stale.
        return None
