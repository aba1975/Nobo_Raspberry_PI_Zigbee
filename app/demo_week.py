"""Week profiles for demo mode, modelled the way the hub keeps them.

A Nobø hub does not store "this zone's week". It stores a set of *week
profiles*, each with a name and a seven-day schedule, and every zone points at
one of them. Several zones pointing at the same profile is not a coincidence to
be tidied away — it is how the official app expects a house to be set up, and
it is why editing one room's week has to think about whether it would quietly
reschedule the others.

Demo mode used to keep a flat ``{zone_id: schedule}`` map instead, with every
profile operation stubbed out to return success and store nothing. Creating a
schedule in Settings reported "Schedule added" and changed nothing; the list
showed one built-in profile that claimed every zone used it and could not be
edited; and saving a zone's week through "save as a new schedule" wrote a
profile that never existed. This module exists so demo answers those questions
the same way the hardware does — which, as ``CLAUDE.md`` records, is the whole
point of demo mode being faithful rather than forgiving.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

# The profile every zone starts on. As on the hub, it is never edited in place
# and never deleted: it has to keep meaning "the default".
DEFAULT_PROFILE_ID = "1"


class WeekProfileError(ValueError):
    """A request that cannot be honoured, with a message fit to show a user."""


def _clone(schedule: Mapping[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(dict(schedule))


class DemoWeekProfiles:
    """The simulated hub's week profiles and which zone follows which.

    Deliberately free of FastAPI and of the demo house itself: it is handed
    zone ids and names and returns plain data, so the rules can be tested
    without a server.
    """

    def __init__(
        self,
        profiles: Optional[Mapping[str, Mapping[str, Any]]] = None,
        assignments: Optional[Mapping[str, str]] = None,
        *,
        default_schedule: Optional[Mapping[str, Any]] = None,
    ):
        self.default_schedule = _clone(default_schedule or {})
        self.profiles: Dict[str, Dict[str, Any]] = {
            str(pid): {
                "name": str(row.get("name") or f"Schedule {pid}"),
                "schedule": _clone(row.get("schedule") or self.default_schedule),
            }
            for pid, row in (profiles or {}).items()
        }
        if DEFAULT_PROFILE_ID not in self.profiles:
            self.profiles[DEFAULT_PROFILE_ID] = {
                "name": "Default",
                "schedule": _clone(self.default_schedule),
            }
        self.assignments: Dict[str, str] = {
            str(zone_id): str(pid)
            for zone_id, pid in (assignments or {}).items()
            if str(pid) in self.profiles
        }

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def profile_id_for(self, zone_id: str) -> str:
        return self.assignments.get(str(zone_id), DEFAULT_PROFILE_ID)

    def schedule_for(self, zone_id: str) -> Dict[str, Any]:
        return _clone(self.profiles[self.profile_id_for(zone_id)]["schedule"])

    def name_for(self, zone_id: str) -> str:
        return self.profiles[self.profile_id_for(zone_id)]["name"]

    def users(self, profile_id: str) -> List[str]:
        """Zone ids following *profile_id*, in the order the house lists them."""
        return [
            zone_id for zone_id in self.assignments
            if self.assignments[zone_id] == str(profile_id)
        ]

    def as_per_zone_schedules(self) -> Dict[str, Any]:
        """The flat ``{zone_id: schedule}`` view the rest of the app reads.

        Derived, never authoritative. It is kept and persisted because the
        week-schedule lookup, the away logic and several tests all read it, and
        because a file called ``demo_schedules.json`` going stale would be a
        trap for whoever looked at it next.
        """
        return {
            zone_id: self.schedule_for(zone_id) for zone_id in self.assignments
        }

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def sync_zones(self, zone_ids: Sequence[str]) -> bool:
        """Give new zones the default and forget zones that have gone."""
        wanted = [str(zone_id) for zone_id in zone_ids]
        changed = False
        for zone_id in wanted:
            if zone_id not in self.assignments:
                self.assignments[zone_id] = DEFAULT_PROFILE_ID
                changed = True
        for zone_id in list(self.assignments):
            if zone_id not in wanted:
                del self.assignments[zone_id]
                changed = True
        # Keep the order the house is in, so ``used_by`` reads naturally.
        if changed:
            self.assignments = {
                zone_id: self.assignments[zone_id] for zone_id in wanted
            }
        return changed

    def unique_name(self, wanted: str) -> str:
        """A name no other profile is using, numbered like the hub path does."""
        taken = {row["name"] for row in self.profiles.values()}
        if wanted not in taken:
            return wanted
        stem = re.sub(r"\s+\d+$", "", wanted)
        index = 2
        while f"{stem} {index}" in taken:
            index += 1
        return f"{stem} {index}"

    def _next_id(self) -> str:
        used = {int(pid) for pid in self.profiles if pid.isdigit()}
        candidate = 2
        while candidate in used:
            candidate += 1
        return str(candidate)

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def create(self, name: str, schedule: Mapping[str, Any]) -> str:
        profile_id = self._next_id()
        self.profiles[profile_id] = {
            "name": self.unique_name(name.strip()),
            "schedule": _clone(schedule),
        }
        return profile_id

    def update(
        self,
        profile_id: str,
        *,
        name: Optional[str] = None,
        schedule: Optional[Mapping[str, Any]] = None,
    ) -> None:
        profile_id = str(profile_id)
        if profile_id not in self.profiles:
            raise WeekProfileError("That schedule no longer exists.")
        if profile_id == DEFAULT_PROFILE_ID:
            # Same rule as the hub path: every zone starts on this one, so it
            # has to keep meaning "the default".
            raise WeekProfileError("The built-in schedule cannot be changed.")
        if name is not None:
            wanted = name.strip()
            if not wanted:
                raise WeekProfileError("Schedule name cannot be empty")
            if wanted != self.profiles[profile_id]["name"]:
                self.profiles[profile_id]["name"] = self.unique_name(wanted)
        if schedule is not None:
            self.profiles[profile_id]["schedule"] = _clone(schedule)

    def delete(self, profile_id: str) -> None:
        profile_id = str(profile_id)
        if profile_id not in self.profiles:
            raise WeekProfileError("That schedule no longer exists.")
        if profile_id == DEFAULT_PROFILE_ID:
            raise WeekProfileError("The built-in schedule cannot be deleted.")
        users = self.users(profile_id)
        if users:
            raise WeekProfileError(
                f"{len(users)} {'zone is' if len(users) == 1 else 'zones are'} "
                "still following this schedule."
            )
        del self.profiles[profile_id]

    def assign(self, zone_id: str, profile_id: str) -> None:
        profile_id = str(profile_id)
        if profile_id not in self.profiles:
            raise WeekProfileError("That schedule no longer exists.")
        self.assignments[str(zone_id)] = profile_id

    def apply_to_zone(
        self,
        zone_id: str,
        schedule: Mapping[str, Any],
        *,
        zone_name: str,
        apply_to: Optional[str] = None,
    ) -> str:
        """Save a week for one zone, and say which profile ended up holding it.

        The same rule as the hub path, and for the same reason. A profile this
        zone shares with others is copied rather than changed underneath them,
        unless the caller explicitly asked to change the schedule itself with
        ``apply_to="profile"``. The default profile is never edited either way.
        """
        profile_id = self.profile_id_for(zone_id)
        exclusive = self.users(profile_id) == [str(zone_id)]
        share = apply_to == "profile"

        if (exclusive or share) and profile_id != DEFAULT_PROFILE_ID:
            self.profiles[profile_id]["schedule"] = _clone(schedule)
            return profile_id

        new_id = self.create(f"{zone_name} schedule", schedule)
        self.assignments[str(zone_id)] = new_id
        return new_id

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profiles": {
                pid: {"name": row["name"], "schedule": row["schedule"]}
                for pid, row in self.profiles.items()
            },
            "zones": dict(self.assignments),
        }

    @classmethod
    def from_dict(
        cls,
        data: Optional[Mapping[str, Any]],
        *,
        default_schedule: Mapping[str, Any],
    ) -> "DemoWeekProfiles":
        data = data or {}
        return cls(
            data.get("profiles") if isinstance(data.get("profiles"), dict) else None,
            data.get("zones") if isinstance(data.get("zones"), dict) else None,
            default_schedule=default_schedule,
        )

    @classmethod
    def migrated(
        cls,
        per_zone_schedules: Mapping[str, Any],
        *,
        default_schedule: Mapping[str, Any],
        zone_name: Callable[[str], str],
    ) -> "DemoWeekProfiles":
        """Build the profile set from the flat per-zone map demo mode used to keep.

        A zone whose week matched the default simply follows the default. A
        zone that had been edited gets a profile of its own, named after the
        room, which is what it would have had if profiles had been modelled
        properly at the time.
        """
        model = cls(default_schedule=default_schedule)
        for zone_id, schedule in per_zone_schedules.items():
            zone_id = str(zone_id)
            if not isinstance(schedule, dict) or not schedule:
                continue
            if schedule == model.profiles[DEFAULT_PROFILE_ID]["schedule"]:
                model.assignments[zone_id] = DEFAULT_PROFILE_ID
                continue
            model.assignments[zone_id] = model.create(
                f"{zone_name(zone_id)} schedule", schedule
            )
        return model
