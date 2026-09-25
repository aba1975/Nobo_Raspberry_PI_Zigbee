"""The last twenty-four hours of each room's climate.

Kept as one bucket per clock hour holding the lowest and highest temperature
and humidity seen in it, which is all "coldest overnight" and a small chart
need, and bounds the file at 24 rows per room however often a sensor reports.

The value recorded is the zone reading the rule itself used — fresh,
available thermometers averaged — at the time of its newest reading, so the
history says what the room card said and when it was measured, not when this
process happened to look.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

HOUR = 3600
HISTORY_HOURS = 24


def _hour(at: float) -> int:
    return int(at // HOUR) * HOUR


def _lower(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None:
        return b
    return a if b is None else min(a, b)


def _higher(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None:
        return b
    return a if b is None else max(a, b)


class ClimateHistory:
    def __init__(
        self,
        *,
        zones: Optional[Mapping[str, list]] = None,
        save: Optional[Callable[[Mapping[str, list]], None]] = None,
    ):
        self._zones: Dict[str, Dict[int, dict]] = {
            str(zone_id): {row["start"]: dict(row) for row in rows}
            for zone_id, rows in (zones or {}).items()
        }
        self._save_fn = save

    def _window_start(self, now: float) -> int:
        return _hour(now) - (HISTORY_HOURS - 1) * HOUR

    def record(
        self,
        readings: Mapping[str, tuple[float, Optional[float], Optional[float]]],
        now: float,
    ) -> bool:
        """Fold ``{zone_id: (measured_at, temperature, humidity)}`` in.

        Saves only when a bucket actually changed — a new hour, a new lowest
        or a new highest — so a room that reads the same all afternoon costs
        one write an hour, not one per evaluation.
        """
        start = self._window_start(now)
        changed = False
        for zone_id, rows in list(self._zones.items()):
            kept = {hour: row for hour, row in rows.items() if hour >= start}
            if len(kept) != len(rows):
                changed = True
                if kept:
                    self._zones[zone_id] = kept
                else:
                    del self._zones[zone_id]
        for zone_id, (at, temperature, humidity) in readings.items():
            if temperature is None and humidity is None:
                continue
            hour = _hour(at)
            if hour < start or hour > _hour(now):
                continue
            rows = self._zones.setdefault(str(zone_id), {})
            row = rows.get(hour) or {
                "start": hour, "t_min": None, "t_max": None, "h_min": None, "h_max": None,
            }
            updated = {
                "start": hour,
                "t_min": _lower(row["t_min"], temperature),
                "t_max": _higher(row["t_max"], temperature),
                "h_min": _lower(row["h_min"], humidity),
                "h_max": _higher(row["h_max"], humidity),
            }
            if updated != rows.get(hour):
                rows[hour] = updated
                changed = True
        if changed:
            self._save()
        return changed

    def summary(self, zone_id: str, now: float) -> Optional[dict]:
        """The last 24 hours for one room, oldest hour first, or None."""
        start = self._window_start(now)
        rows = sorted(
            (row for hour, row in self._zones.get(str(zone_id), {}).items() if hour >= start),
            key=lambda row: row["start"],
        )
        if not rows:
            return None

        def extreme(pick, key):
            values = [row[key] for row in rows if row[key] is not None]
            return pick(values) if values else None

        return {
            "hours": [dict(row) for row in rows],
            "temperature_min": extreme(min, "t_min"),
            "temperature_max": extreme(max, "t_max"),
            "humidity_min": extreme(min, "h_min"),
            "humidity_max": extreme(max, "h_max"),
            "window_start": start,
        }

    def _save(self) -> None:
        if self._save_fn is None:
            return
        payload = {
            zone_id: [rows[hour] for hour in sorted(rows)]
            for zone_id, rows in self._zones.items()
        }
        try:
            self._save_fn(payload)
        except OSError as exc:
            # History is a convenience. A full card must never stop the rule.
            logger.warning("Could not save climate history: %s", exc)
