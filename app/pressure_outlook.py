"""Air pressure over the last day, and what its last three hours say.

A barometer's absolute reading says little on its own: the sensor measures
pressure where it stands, not at sea level, so a cabin at 350 m reads about
40 hPa below the number a weather report quotes. What a barometer *is* good
for is its change over the last three hours, the "barometric tendency"
weather services have used for a century, and that needs no altitude.

Readings are treated as holding until the next one. A thermometer reports
when something moves and otherwise about once an hour, so a steady afternoon
leaves long gaps, and the value before a gap is still the value. The
pressure three hours ago is therefore the last reading at or before that
moment, provided the sensor was not silent for longer than a reading stays
fresh.

The outlook is worked out for the whole house, not per room. Pressure is the
same in every room, and two sensors disagreeing by a tenth near a boundary
must not tell two rooms different weather. Each room's change is taken from
its own series first and only then averaged, because two sensors of the same
model can differ by more than a hectopascal in absolute terms but agree on
how fast it is moving.

It is a rough local guide from one instrument, and is worded as one. It is
not a forecast.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

#: One stored sample per slot; later readings in a slot replace the first.
SAMPLE_SECONDS = 600
KEEP_SECONDS = 24 * 3600
TENDENCY_SECONDS = 3 * 3600


class Tendency(str, Enum):
    STORM = "storm"
    FALLING_FAST = "falling_fast"
    FALLING = "falling"
    STEADY = "steady"
    RISING = "rising"
    RISING_FAST = "rising_fast"


def classify(change: float) -> Tendency:
    """The three-hour change in hPa, in the bands weather services use.

    Under 1.6 either way is steady; 1.6–3.5 is a change; 3.6–6.0 is a quick
    one; beyond 6 falling is the drop that comes before a gale.
    """
    change = round(change, 1)
    if change <= -6.1:
        return Tendency.STORM
    if change <= -3.6:
        return Tendency.FALLING_FAST
    if change <= -1.6:
        return Tendency.FALLING
    if change >= 3.6:
        return Tendency.RISING_FAST
    if change >= 1.6:
        return Tendency.RISING
    return Tendency.STEADY


def _slot(at: float) -> int:
    return int(at // SAMPLE_SECONDS)


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


class PressureHistory:
    def __init__(
        self,
        *,
        zones: Optional[Mapping[str, list]] = None,
        save: Optional[Callable[[Mapping[str, list]], None]] = None,
    ):
        self._zones: Dict[str, List[List[float]]] = {
            str(zone_id): [[int(at), float(value)] for at, value in rows]
            for zone_id, rows in (zones or {}).items()
            if rows
        }
        self._save_fn = save

    def record(self, readings: Mapping[str, tuple[float, float]], now: float) -> bool:
        """Fold ``{zone_id: (measured_at, pressure)}`` in.

        Saves only when a slot is added or dropped, so a sensor reporting
        every minute costs one write per ten minutes, not one per report.
        A later reading in the same slot replaces the earlier one in memory
        and reaches the file with the next slot.
        """
        cutoff = now - KEEP_SECONDS
        added = False
        for zone_id in list(self._zones):
            kept = [row for row in self._zones[zone_id] if row[0] >= cutoff]
            if len(kept) != len(self._zones[zone_id]):
                added = True
                if kept:
                    self._zones[zone_id] = kept
                else:
                    del self._zones[zone_id]
        for zone_id, (at, value) in readings.items():
            if value is None or at is None or at < cutoff or at > now:
                continue
            rows = self._zones.setdefault(str(zone_id), [])
            sample = [int(at), float(value)]
            if rows and sample[0] < rows[-1][0]:
                continue
            if rows and _slot(rows[-1][0]) == _slot(sample[0]):
                rows[-1] = sample
            else:
                rows.append(sample)
                added = True
        if added:
            self._save()
        return added

    def hourly(self, zone_id: str, now: float) -> List[List[float]]:
        """The last reading of each hour, oldest first, for a small chart."""
        last: Dict[int, List[float]] = {}
        for at, value in self._zones.get(str(zone_id), ()):
            if at >= now - KEEP_SECONDS:
                last[int(at // 3600)] = [at, value]
        return [last[hour] for hour in sorted(last)]

    def change(self, zone_id: str, now: float, fresh_seconds: float) -> Optional[float]:
        """How far this room's pressure has moved in three hours, or None."""
        rows = self._zones.get(str(zone_id)) or []
        if not rows or now - rows[-1][0] > fresh_seconds:
            return None
        target = now - TENDENCY_SECONDS
        before = [row for row in rows if row[0] <= target]
        if not before or target - before[-1][0] > fresh_seconds:
            return None
        return rows[-1][1] - before[-1][1]

    def outlook(self, now: float, fresh_seconds: float) -> Optional[dict]:
        """The house's pressure tendency, or when it will first be known.

        None when no room has a fresh pressure reading at all.
        """
        changes = []
        ready = []
        for zone_id, rows in self._zones.items():
            if not rows or now - rows[-1][0] > fresh_seconds:
                continue
            moved = self.change(zone_id, now, fresh_seconds)
            if moved is not None:
                changes.append(moved)
            else:
                ready.append(rows[0][0] + TENDENCY_SECONDS)
        if changes:
            change = round(sum(changes) / len(changes), 1)
            return {
                "tendency": classify(change).value,
                "change_3h": change + 0.0,
                "rooms": len(changes),
                "ready_at": None,
            }
        if not ready:
            return None
        # A room whose three hours are already up but which has a gap in
        # them has no honest time to promise, so none is given.
        upcoming = [at for at in ready if at > now]
        return {
            "tendency": None,
            "change_3h": None,
            "rooms": 0,
            "ready_at": _iso(min(upcoming)) if upcoming else None,
        }

    def _save(self) -> None:
        if self._save_fn is None:
            return
        payload = {zone_id: [list(row) for row in rows] for zone_id, rows in self._zones.items()}
        try:
            self._save_fn(payload)
        except OSError as exc:
            # A convenience, like the temperature history: a full card must
            # never stop the rule that is reading these sensors.
            logger.warning("Could not save pressure history: %s", exc)
