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

They also report the alarm's own door and window contacts and the temperature
its smoke detectors and sirens measure. Those are *readings of devices*, not
alarm events, and ``sensor_verisure`` turns the ones a person has chosen into
sensors beside the Zigbee ones. See docs/SENSORS.md.
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


# The two kinds of alarm device this application can read. A door or window
# contact reports open or closed; a climate device — a smoke detector, a
# siren, a water detector — reports the temperature where it hangs, and
# sometimes humidity.
DEVICE_KINDS = ("contact", "climate")


@dataclass(frozen=True)
class AlarmDevice:
    """One alarm device as the alarm last described it.

    ``device_id`` is ``<kind>:<the alarm's own label>``: one physical device
    can appear in both lists, and each list is a different sensor here.
    ``open`` is None when the alarm did not say. ``reported_at`` is when the
    *device* last reported, which for a climate device can be an hour old.
    """

    device_id: str
    kind: str
    name: str
    model: Optional[str] = None
    open: Optional[bool] = None
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    reported_at: Optional[str] = None


@dataclass(frozen=True)
class AlarmReading:
    """One consistent look at the alarm. ``arm_state`` is None when unknown."""

    arm_state: Optional[str]
    arm_changed_at: Optional[str]
    locks: Tuple[LockReading, ...] = ()
    read_at: float = field(default_factory=time.time)
    devices: Tuple[AlarmDevice, ...] = ()

    def device(self, device_id: str) -> Optional[AlarmDevice]:
        return next((item for item in self.devices if item.device_id == device_id), None)


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


def default_simulated_devices() -> List[Dict[str, Any]]:
    """The demo alarm's own sensors, mirroring a typical Verisure house: door
    contacts at the entrances and an outbuilding, and smoke detectors that
    happen to measure temperature."""
    def contact(label: str, name: str) -> Dict[str, Any]:
        return {"device_id": f"contact:{label}", "kind": "contact", "name": name,
                "model": "Door/window", "open": False, "temperature": None,
                "humidity": None, "reported_at": None}

    def climate(label: str, name: str, temperature: float, humidity: Optional[float]) -> Dict[str, Any]:
        return {"device_id": f"climate:{label}", "kind": "climate", "name": name,
                "model": "Smoke detector", "open": None, "temperature": temperature,
                "humidity": humidity, "reported_at": None}

    return [
        contact("DEMO 0001", "Front door"),
        contact("DEMO 0002", "Patio door"),
        contact("DEMO 0003", "Tech room"),
        contact("DEMO 0004", "Woodshed"),
        climate("DEMO 0101", "Hallway", 19.5, 41.0),
        climate("DEMO 0102", "Living room", 21.0, None),
    ]


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
        "devices": default_simulated_devices(),
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
            devices=tuple(AlarmDevice(**device) for device in self._state.get("devices", [])),
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

    def set_lock_method(self, lock_id: str, method: str) -> None:
        """Lock it with any method the real lock can report, so a mapping of
        methods to inside and outside can be tried without a Doorman."""
        if method not in LOCK_METHODS:
            raise ValueError(f"Unknown lock method: {method}")
        if not any(lock["lock_id"] == lock_id for lock in self._state["locks"]):
            raise KeyError(lock_id)
        self._state = {**self._state, "locks": [
            {**lock, "locked": True, "method": method, "changed_at": _now_iso()}
            if lock["lock_id"] == lock_id else lock
            for lock in self._state["locks"]
        ]}
        self._save(self._state)

    def set_device(
        self,
        device_id: str,
        *,
        open: Optional[bool] = None,
        temperature: Optional[float] = None,
    ) -> None:
        devices: List[Dict[str, Any]] = []
        found = False
        for device in self._state.get("devices", []):
            if device["device_id"] == device_id:
                found = True
                changes: Dict[str, Any] = {}
                if open is not None:
                    if device["kind"] != "contact":
                        raise ValueError("Only a door or window contact opens")
                    changes["open"] = open
                if temperature is not None:
                    if device["kind"] != "climate":
                        raise ValueError("Only a climate device has a temperature")
                    if not -40 <= temperature <= 80:
                        raise ValueError("Temperature must be between -40 and 80 °C")
                    changes["temperature"] = round(float(temperature), 1)
                if changes:
                    device = {**device, **changes, "reported_at": _now_iso()}
            devices.append(device)
        if not found:
            raise KeyError(device_id)
        self._state = {**self._state, "devices": devices}
        self._save(self._state)
