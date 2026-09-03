"""
setpoint_guard.py — remembering the temperatures this system intends.

A Nobø thermostat with a dial does not create an override when somebody turns
it. It rewrites the zone's comfort or eco temperature on the hub, permanently,
and the hub keeps no record of what the value used to be.

That was proved on real hardware rather than assumed. Turning the dial on the
Gang NTB-2R produced exactly one change on the hub::

    20:00:13  z5 Gang: comfort: 17.0 -> 21.0

``active_override_id`` stayed ``-1``, the mode stayed ``normal``, and Comfort,
Eco and Normal afterwards all left the value at 21.0. There is no override to
cancel and nothing in the protocol that remembers 17.0 ever existed.

So this file remembers. Every temperature this system sets is written down as
the intended value, and any difference between that and what the hub reports is
a change that came from somewhere else.

What it cannot do is say *where* from. The official Nobø app rewrites the same
field in the same way, so "somebody turned the dial" and "somebody used the Nobø
app" are indistinguishable from here. The wording throughout says only that the
change did not come from this system, which is the whole of what is known.

Why the state is derived rather than accumulated
------------------------------------------------
There is no flag to set and clear. Drift is recomputed from the intended value
and the hub's current value every time anyone asks, so a flag cannot be left
behind by a missed message, a restart, or a change made while the Pi was off.
The only stored thing is the intention itself.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# The fields a dial can move. Away is a fixed 7 °C on the hub and cannot be set.
GUARDED_FIELDS = ("comfort", "eco")

# How long after our own write the hub is allowed to still be reporting the old
# value without that counting as somebody else's change. The hub echoes quickly,
# but a command and its push are not atomic, and a warning that flickers on
# every adjustment would be worse than no warning at all.
SETTLE_SECONDS = 25


def _as_float(value: Any) -> Optional[float]:
    """The hub sends temperatures as text; a missing one may be None or ''."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class SetpointGuard:
    """
    The intended comfort and eco temperature for every zone.

    ``save_fn`` persists the intentions; it is injected so the class can be
    tested with no filesystem.
    """

    save_fn: Optional[Callable[[Dict[str, Dict[str, float]]], None]] = None
    now_fn: Callable[[], float] = time.time
    intended: Dict[str, Dict[str, float]] = field(default_factory=dict)
    _written_at: Dict[str, float] = field(default_factory=dict)

    # -- what we meant -------------------------------------------------------

    def remember(self, zone_id: Any, which: str, value: Any) -> None:
        """
        This system set this temperature, so this is now what it intends.

        Called before the hub command goes out, for the same reason the
        notification watcher records writes first: the push can arrive before
        the call returns.
        """
        if which not in GUARDED_FIELDS:
            return
        temp = _as_float(value)
        if temp is None:
            return
        zone_id = str(zone_id)
        self.intended.setdefault(zone_id, {})[which] = temp
        self._written_at[f"{zone_id}:{which}"] = self.now_fn()
        self._save()

    def seed(self, zone_id: Any, which: str, value: Any) -> None:
        """
        Adopt the hub's value as the intention, for a zone never set from here.

        Only ever fills a blank. A zone we already have an intention for is left
        alone, which is what makes a change during a restart still visible: the
        intention survives on disk while the hub's value has moved on.
        """
        if which not in GUARDED_FIELDS:
            return
        zone_id = str(zone_id)
        if self.intended.get(zone_id, {}).get(which) is not None:
            return
        temp = _as_float(value)
        if temp is None:
            return
        self.intended.setdefault(zone_id, {})[which] = temp
        self._save()

    def accept(self, zone_id: Any, which: Optional[str] = None) -> None:
        """Stop warning: whatever the hub says is now what we intend."""
        zone_id = str(zone_id)
        if which is None:
            self.intended.pop(zone_id, None)
        else:
            self.intended.get(zone_id, {}).pop(which, None)
        self._save()

    def forget_zone(self, zone_id: Any) -> None:
        """A deleted zone should not keep an opinion."""
        if self.intended.pop(str(zone_id), None) is not None:
            self._save()

    def intended_for(self, zone_id: Any, which: str) -> Optional[float]:
        return self.intended.get(str(zone_id), {}).get(which)

    # -- what actually happened ---------------------------------------------

    def _settling(self, zone_id: str, which: str) -> bool:
        stamp = self._written_at.get(f"{zone_id}:{which}")
        return stamp is not None and (self.now_fn() - stamp) < SETTLE_SECONDS

    def drift(self, zone: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        """
        Where this zone's temperatures differ from what we intended.

        Returns ``{"comfort": {"intended": 17.0, "actual": 21.0}}`` for each
        field that has moved, or an empty dict when the house agrees with us.
        """
        zone_id = str(zone.get("zone_id"))
        result: Dict[str, Dict[str, float]] = {}
        for which in GUARDED_FIELDS:
            want = self.intended_for(zone_id, which)
            if want is None:
                continue
            got = _as_float(zone.get(f"{which}_temperature"))
            if got is None or got == want:
                continue
            if self._settling(zone_id, which):
                continue
            result[which] = {"intended": want, "actual": got}
        return result

    def observe(self, zones) -> None:
        """Adopt an intention for any zone we have never had one for."""
        for zone in zones or []:
            zone_id = zone.get("zone_id")
            if zone_id is None:
                continue
            for which in GUARDED_FIELDS:
                self.seed(zone_id, which, zone.get(f"{which}_temperature"))

    def restore_targets(self, zones) -> Dict[str, Dict[str, float]]:
        """
        What would have to be written to put the house back as intended.

        ``{zone_id: {"comfort": 17.0}}``. Used both by the restore button and by
        a global mode change, which is the moment the owner is telling the whole
        house what to do and expects their own settings to be the ones applied.
        """
        targets: Dict[str, Dict[str, float]] = {}
        for zone in zones or []:
            moved = self.drift(zone)
            if moved:
                targets[str(zone.get("zone_id"))] = {
                    which: values["intended"] for which, values in moved.items()
                }
        return targets

    # -- persistence ---------------------------------------------------------

    def _save(self) -> None:
        if self.save_fn is None:
            return
        try:
            self.save_fn(self.intended)
        except Exception as exc:
            logger.error("Could not persist intended setpoints: %s", exc)
