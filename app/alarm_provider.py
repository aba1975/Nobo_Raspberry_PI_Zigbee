"""What an alarm system can tell this application, and a demo one to try it on.

An alarm is read, never driven. The contract below has no way to arm, disarm,
lock or unlock anything, and that is the point of it: this is a heating
controller, and the worst it may do with an alarm it has misunderstood is
leave the heating where it was. See docs/ALARM.md.

Two providers implement it:

* ``SimulatedAlarm`` — demo mode only. Its state is set by hand from Settings
  and kept in ``data/simulated_alarm.json``, so a demo survives a restart.
* ``VerisureAlarm`` in ``alarm_verisure`` — the user's real Verisure account,
  read through Verisure's unofficial cloud API.

Both report the same three facts: how the alarm is armed, when that last
changed, and each smart lock's state and *how* it was last locked. The last
one is what separates "locked from outside" (somebody left) from "locked with
the thumb turn" (somebody is inside for the night).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Tuple

ARM_STATES = ("disarmed", "armed_home", "armed_away")

# How a Yale Doorman reports being locked. Verisure's API sends these in
# capitals; they are kept lower-case here. ``thumb`` is the turn on the inside
# of the door and is the only method that proves somebody is indoors. ``auto``
# is the lock re-locking itself after the door closes, which happens whether
# the person who shut it is inside or out, so it is a setting.
LOCK_METHODS = ("thumb", "auto", "code", "star", "remote", "tag", "key")

# The simulated lock can be locked from either side. "Outside" uses the keypad
# code, which is the ordinary way to lock a Doorman on the way out.
SIMULATED_LOCK_ACTIONS = {
    "outside": (True, "code"),
    "inside": (True, "thumb"),
    "unlocked": (False, None),
}


@dataclass(frozen=True)
class LockReading:
    lock_id: str
    name: str
    locked: Optional[bool]
    method: Optional[str]
    changed_at: Optional[str]


@dataclass(frozen=True)
class AlarmReading:
    """One consistent look at the alarm. ``arm_state`` is None when unknown."""

    arm_state: Optional[str]
    arm_changed_at: Optional[str]
    locks: Tuple[LockReading, ...] = ()
    read_at: float = field(default_factory=time.time)


class AlarmUnavailable(Exception):
    """The alarm could not be read, for a reason the interface can show.

    ``kind`` is one of ``signed_out``, ``unreachable``, ``rate_limited`` or
    ``not_configured``. The message is always one of ours, never text from the
    alarm company's servers, which can carry account details.
    """

    def __init__(self, kind: str, message: str, retry_after: float = 60.0):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class AlarmProvider(Protocol):
    name: str

    async def read(self) -> AlarmReading:
        ...


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_simulated_state() -> Dict[str, Any]:
    return {
        "arm_state": "disarmed",
        "arm_changed_at": None,
        "locks": [{
            "lock_id": "front-door",
            "name": "Front door",
            "locked": False,
            "method": None,
            "changed_at": None,
        }],
    }


class SimulatedAlarm:
    """A demo alarm and front-door lock, set by hand from Settings."""

    name = "simulated"

    def __init__(
        self,
        load: Callable[[], Dict[str, Any]],
        save: Callable[[Mapping[str, Any]], None],
    ) -> None:
        self._save = save
        self._state = load()

    async def read(self) -> AlarmReading:
        return AlarmReading(
            arm_state=self._state["arm_state"],
            arm_changed_at=self._state["arm_changed_at"],
            locks=tuple(LockReading(**lock) for lock in self._state["locks"]),
        )

    def set_arm_state(self, arm_state: str) -> None:
        if arm_state not in ARM_STATES:
            raise ValueError(f"Unknown arm state: {arm_state}")
        if arm_state == self._state["arm_state"]:
            return
        self._state = {**self._state, "arm_state": arm_state, "arm_changed_at": _now_iso()}
        self._save(self._state)

    def set_lock(self, lock_id: str, action: str) -> None:
        if action not in SIMULATED_LOCK_ACTIONS:
            raise ValueError(f"Unknown lock action: {action}")
        locked, method = SIMULATED_LOCK_ACTIONS[action]
        locks: List[Dict[str, Any]] = []
        found = False
        for lock in self._state["locks"]:
            if lock["lock_id"] == lock_id:
                found = True
                lock = {**lock, "locked": locked, "method": method, "changed_at": _now_iso()}
            locks.append(lock)
        if not found:
            raise KeyError(lock_id)
        self._state = {**self._state, "locks": locks}
        self._save(self._state)
