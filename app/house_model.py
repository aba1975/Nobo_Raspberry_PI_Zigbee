"""
house_model.py — the floor plan behind the optional house view.

The house view draws the building in 3D and shows where each heater and
sensor is. Everything it needs that the hub and the sensor registry do not
already know lives in one file:

- ``data/house.json``      the real building, written by an administrator
- ``data/house_demo.json`` the same, while the application is in demo mode
- ``app/demo_house.json``  the generic demo building, used until demo mode
                           has one of its own

Geometry is metres in a plan coordinate system: ``x`` east, ``y`` north, and
heights measured from the level's floor. Every wall is a straight segment
with its openings measured along it from ``a``.

What this file deliberately does **not** hold:

- **Names that the app already knows.** A room points at a hub zone by
  ``zone_id``; the name shown is always the zone's own. ``name`` here only
  labels a room with no zone (a sauna, a store) so it is not blank.
- **Behaviour.** A placement says where a device *is drawn*. Which room a
  sensor warns for, which heating it changes and what its rules are stay in
  the sensor files. Nothing in this module is ever read by anything that
  heats, warns or writes to the hub.
- **Heater keys from ``hub.components``.** Heaters are keyed by the twelve
  digit component serial, which is what the zone list carries (rule 3 in
  CLAUDE.md is about not echoing pynobo's component dict back to the hub;
  this module never writes to the hub at all, but uses the same key anyway).

The file is the user's own building, possibly with their address in mind, so
it is never committed and only ever served behind sign-in.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent / "data"
DEMO_TEMPLATE_FILE = Path(__file__).resolve().parent / "demo_house.json"
HOUSE_FILENAME = "house.json"
DEMO_HOUSE_FILENAME = "house_demo.json"

SCHEMA_VERSION = 1

# A generous ceiling for one cabin: the real one is about 25 KB.
MAX_BYTES = 256 * 1024
MAX_COORD = 100.0
MAX_LEVELS = 4
MAX_WALLS = 500
MAX_OPENINGS_PER_WALL = 20
MAX_ROOMS = 100
MAX_POLYGON_POINTS = 64
MAX_FIXTURES = 200
MAX_PLACEMENTS = 500
MAX_NAME = 60
MAX_LABEL = 40

ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
SENSOR_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
HEATER_SERIAL_RE = re.compile(r"^\d{12}$")
# The opening code is what people say out loud ("W7", "D2"), so it is short.
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,12}$")

OPENING_KINDS = ("door", "window", "gap")
SASHES = ("left", "right", "whole")
SWINGS = ("in", "out")
PLACEMENT_KINDS = ("heaters", "sensors")

# A wall is drawn to the nearest centimetre, so a heater or opening that ends
# a few millimetres past the end of its wall is a rounding error, not a fault.
_TOLERANCE = 0.02


class HouseModelError(ValueError):
    """The floor plan, or a change to it, is not acceptable. ``str()`` says why."""


# ---------------------------------------------------------------------------
# Field checks
# ---------------------------------------------------------------------------

def _fail(where: str, message: str) -> None:
    raise HouseModelError(f"{where}: {message}" if where else message)


def _only_keys(value: Any, where: str, required: Iterable[str], optional: Iterable[str] = ()) -> dict:
    if not isinstance(value, dict):
        _fail(where, "must be an object")
    required, optional = tuple(required), tuple(optional)
    missing = [key for key in required if key not in value]
    if missing:
        _fail(where, f"missing {', '.join(missing)}")
    unknown = sorted(set(value) - set(required) - set(optional))
    if unknown:
        _fail(where, f"unknown field {', '.join(unknown)}")
    return value


def _number(value: Any, where: str, low: float = -MAX_COORD, high: float = MAX_COORD) -> float:
    # bool is an int in Python; true is not a coordinate.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(where, "must be a number")
    number = float(value)
    if not math.isfinite(number):
        _fail(where, "must be a finite number")
    if number < low or number > high:
        _fail(where, f"must be between {low:g} and {high:g}")
    return round(number, 4)


def _point(value: Any, where: str, dims: int = 2) -> List[float]:
    if not isinstance(value, list) or len(value) != dims:
        _fail(where, f"must be a list of {dims} numbers")
    return [_number(item, f"{where}[{index}]") for index, item in enumerate(value)]


def _identifier(value: Any, where: str, pattern: re.Pattern = ID_RE) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        _fail(where, "must be a short identifier (letters, digits, _ . : -)")
    return value


def _text(value: Any, where: str, limit: int) -> str:
    if not isinstance(value, str):
        _fail(where, "must be text")
    text = " ".join(value.split())
    if any(ord(ch) < 32 for ch in text):
        _fail(where, "must not contain control characters")
    if len(text) > limit:
        _fail(where, f"must be at most {limit} characters")
    return text


def _choice(value: Any, where: str, choices: Tuple[str, ...]) -> str:
    if value not in choices:
        _fail(where, f"must be one of {', '.join(choices)}")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        _fail(where, "must be true or false")
    return value


def _list(value: Any, where: str, limit: int) -> list:
    if not isinstance(value, list):
        _fail(where, "must be a list")
    if len(value) > limit:
        _fail(where, f"must have at most {limit} entries")
    return value


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _length(a: List[float], b: List[float]) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def polygon_area(points: List[List[float]]) -> float:
    """Signed area by the shoelace formula; positive when anticlockwise."""
    total = 0.0
    for index, (x0, y0) in enumerate(points):
        x1, y1 = points[(index + 1) % len(points)]
        total += x0 * y1 - x1 * y0
    return total / 2.0


def _orientation(p, q, r) -> int:
    value = (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])
    if abs(value) < 1e-12:
        return 0
    return 1 if value > 0 else 2


def _on_segment(p, q, r) -> bool:
    return (min(p[0], r[0]) - 1e-12 <= q[0] <= max(p[0], r[0]) + 1e-12
            and min(p[1], r[1]) - 1e-12 <= q[1] <= max(p[1], r[1]) + 1e-12)


def _segments_cross(p1, q1, p2, q2) -> bool:
    o1, o2 = _orientation(p1, q1, p2), _orientation(p1, q1, q2)
    o3, o4 = _orientation(p2, q2, p1), _orientation(p2, q2, q1)
    if o1 != o2 and o3 != o4:
        return True
    return ((o1 == 0 and _on_segment(p1, p2, q1)) or (o2 == 0 and _on_segment(p1, q2, q1))
            or (o3 == 0 and _on_segment(p2, p1, q2)) or (o4 == 0 and _on_segment(p2, q1, q2)))


def polygon_is_simple(points: List[List[float]]) -> bool:
    """True when no two non-adjacent edges touch, i.e. the outline does not cross itself."""
    count = len(points)
    if len({(x, y) for x, y in points}) != count:
        return False
    edges = [(points[i], points[(i + 1) % count]) for i in range(count)]
    for i in range(count):
        for j in range(i + 1, count):
            if j == i + 1 or (i == 0 and j == count - 1):
                continue
            if _segments_cross(*edges[i], *edges[j]):
                return False
    return True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_levels(raw: Any) -> List[dict]:
    levels = _list(raw, "levels", MAX_LEVELS)
    if not levels:
        _fail("levels", "needs at least one level")
    result, seen = [], set()
    for index, level in enumerate(levels):
        where = f"levels[{index}]"
        _only_keys(level, where, ("id", "elevation", "height"), ("name",))
        level_id = _identifier(level["id"], f"{where}.id")
        if level_id in seen:
            _fail(f"{where}.id", f"'{level_id}' is used twice")
        seen.add(level_id)
        item = {
            "id": level_id,
            "elevation": _number(level["elevation"], f"{where}.elevation", 0, MAX_COORD),
            "height": _number(level["height"], f"{where}.height", 0.5, 20),
        }
        if "name" in level:
            item["name"] = _text(level["name"], f"{where}.name", MAX_NAME)
        result.append(item)
    return result


def _validate_opening(raw: Any, where: str, wall_length: float) -> dict:
    _only_keys(raw, where, ("kind", "at", "width"), ("code", "sill", "height", "swing"))
    kind = _choice(raw["kind"], f"{where}.kind", OPENING_KINDS)
    at = _number(raw["at"], f"{where}.at", 0, MAX_COORD)
    width = _number(raw["width"], f"{where}.width", 0.1, 20)
    if at + width > wall_length + _TOLERANCE:
        _fail(where, f"runs past the end of its wall ({at + width:.2f} m on a {wall_length:.2f} m wall)")
    item = {"kind": kind, "at": at, "width": width,
            "sill": _number(raw.get("sill", 0), f"{where}.sill", 0, 20),
            "height": _number(raw.get("height", 2.0), f"{where}.height", 0.1, 20)}
    if "code" in raw:
        item["code"] = _identifier(raw["code"], f"{where}.code", CODE_RE)
    elif kind != "gap":
        # Contacts are placed on a door or window by its code, so one without
        # a code could never carry a sensor.
        _fail(where, "a door or window needs a code, such as W1 or D1")
    if "swing" in raw:
        if kind != "door":
            _fail(f"{where}.swing", "only a door swings")
        item["swing"] = _choice(raw["swing"], f"{where}.swing", SWINGS)
    return item


def _validate_walls(raw: Any, levels: Dict[str, dict]) -> List[dict]:
    walls = _list(raw, "walls", MAX_WALLS)
    result, seen_ids, seen_codes = [], set(), set()
    for index, wall in enumerate(walls):
        where = f"walls[{index}]"
        _only_keys(wall, where, ("id", "level", "a", "b"), ("thickness", "exterior", "openings"))
        wall_id = _identifier(wall["id"], f"{where}.id")
        if wall_id in seen_ids:
            _fail(f"{where}.id", f"'{wall_id}' is used twice")
        seen_ids.add(wall_id)
        level = _identifier(wall["level"], f"{where}.level")
        if level not in levels:
            _fail(f"{where}.level", f"no level called '{level}'")
        a, b = _point(wall["a"], f"{where}.a"), _point(wall["b"], f"{where}.b")
        length = _length(a, b)
        if length < 0.05:
            _fail(where, "a wall must be at least 5 cm long")
        openings = []
        for o_index, opening in enumerate(_list(wall.get("openings", []), f"{where}.openings",
                                                MAX_OPENINGS_PER_WALL)):
            item = _validate_opening(opening, f"{where}.openings[{o_index}]", length)
            code = item.get("code")
            if code:
                if code in seen_codes:
                    _fail(f"{where}.openings[{o_index}].code", f"'{code}' is used twice")
                seen_codes.add(code)
            openings.append(item)
        result.append({
            "id": wall_id, "level": level, "a": a, "b": b,
            "thickness": _number(wall.get("thickness", 0.1), f"{where}.thickness", 0.01, 1),
            "exterior": _bool(wall.get("exterior", False), f"{where}.exterior"),
            "openings": openings,
        })
    return result


def _validate_rooms(raw: Any, levels: Dict[str, dict]) -> List[dict]:
    rooms = _list(raw, "rooms", MAX_ROOMS)
    result, seen = [], set()
    for index, room in enumerate(rooms):
        where = f"rooms[{index}]"
        _only_keys(room, where, ("id", "level", "polygon"), ("zone_id", "name"))
        room_id = _identifier(room["id"], f"{where}.id")
        if room_id in seen:
            _fail(f"{where}.id", f"'{room_id}' is used twice")
        seen.add(room_id)
        level = _identifier(room["level"], f"{where}.level")
        if level not in levels:
            _fail(f"{where}.level", f"no level called '{level}'")
        polygon = _list(room["polygon"], f"{where}.polygon", MAX_POLYGON_POINTS)
        if len(polygon) < 3:
            _fail(f"{where}.polygon", "needs at least three corners")
        points = [_point(p, f"{where}.polygon[{i}]") for i, p in enumerate(polygon)]
        if abs(polygon_area(points)) < 0.05:
            _fail(f"{where}.polygon", "has no area")
        if not polygon_is_simple(points):
            _fail(f"{where}.polygon", "crosses itself")
        zone_id = room.get("zone_id")
        if zone_id is not None:
            # The hub's ids are small integers; the API carries them as text.
            if isinstance(zone_id, int) and not isinstance(zone_id, bool):
                zone_id = str(zone_id)
            zone_id = _identifier(zone_id, f"{where}.zone_id")
        item = {"id": room_id, "level": level, "zone_id": zone_id, "polygon": points}
        if "name" in room:
            item["name"] = _text(room["name"], f"{where}.name", MAX_NAME)
        result.append(item)
    return result


def _validate_fixtures(raw: Any, levels: Dict[str, dict]) -> List[dict]:
    fixtures = _list(raw, "fixtures", MAX_FIXTURES)
    result = []
    for index, fixture in enumerate(fixtures):
        where = f"fixtures[{index}]"
        _only_keys(fixture, where, ("level", "box", "height"), ("label",))
        level = _identifier(fixture["level"], f"{where}.level")
        if level not in levels:
            _fail(f"{where}.level", f"no level called '{level}'")
        box = fixture["box"]
        if not isinstance(box, list) or len(box) != 4:
            _fail(f"{where}.box", "must be [x0, y0, x1, y1]")
        x0, y0, x1, y1 = (_number(v, f"{where}.box[{i}]") for i, v in enumerate(box))
        if x1 <= x0 or y1 <= y0:
            _fail(f"{where}.box", "must run from the lower to the higher corner")
        item = {"level": level, "box": [x0, y0, x1, y1],
                "height": _number(fixture["height"], f"{where}.height", 0.01, 10)}
        if "label" in fixture:
            item["label"] = _text(fixture["label"], f"{where}.label", MAX_LABEL)
        result.append(item)
    return result


class _Index:
    """What a placement may point at, looked up by id."""

    def __init__(self, levels, walls, rooms):
        self.levels = {level["id"]: level for level in levels}
        self.walls = {wall["id"]: wall for wall in walls}
        self.rooms = {room["id"]: room for room in rooms}
        self.openings = {}
        for wall in walls:
            for opening in wall["openings"]:
                if opening.get("code"):
                    self.openings[opening["code"]] = (wall, opening)


def _validate_placement(kind: str, device_id: str, raw: Any, index: _Index) -> dict:
    where = f"placements.{kind}.{device_id}"
    if not isinstance(raw, dict):
        _fail(where, "must be an object")

    if raw.get("outside") is not None:
        # Deliberately not drawn: a woodshed, a separate building. It stops the
        # device being listed as unplaced for ever.
        _only_keys(raw, where, ("outside",))
        if raw["outside"] is not True:
            _fail(f"{where}.outside", "must be true")
        return {"outside": True}

    if "point" in raw:
        _only_keys(raw, where, ("point", "level"))
        level = _identifier(raw["level"], f"{where}.level")
        if level not in index.levels:
            _fail(f"{where}.level", f"no level called '{level}'")
        x, y, z = _point(raw["point"], f"{where}.point", 3)
        if z < 0 or z > index.levels[level]["height"] + 2:
            _fail(f"{where}.point", "height is outside the level")
        return {"point": [x, y, z], "level": level}

    if kind == "heaters":
        if "floor" in raw:
            # Floor heating: an NTB-2R heats the whole floor of its room.
            _only_keys(raw, where, ("floor",))
            room = _identifier(raw["floor"], f"{where}.floor")
            if room not in index.rooms:
                _fail(f"{where}.floor", f"no room called '{room}'")
            return {"floor": room}
        _only_keys(raw, where, ("wall", "at", "length"))
        wall_id = _identifier(raw["wall"], f"{where}.wall")
        wall = index.walls.get(wall_id)
        if wall is None:
            _fail(f"{where}.wall", f"no wall called '{wall_id}'")
        at = _number(raw["at"], f"{where}.at", 0, MAX_COORD)
        length = _number(raw["length"], f"{where}.length", 0.1, 10)
        wall_length = _length(wall["a"], wall["b"])
        if at + length > wall_length + _TOLERANCE:
            _fail(where, f"runs past the end of wall '{wall_id}'")
        return {"wall": wall_id, "at": at, "length": length}

    _only_keys(raw, where, ("opening",), ("sash",))
    code = _identifier(raw["opening"], f"{where}.opening", CODE_RE)
    found = index.openings.get(code)
    if found is None:
        _fail(f"{where}.opening", f"no door or window called '{code}'")
    item = {"opening": code, "sash": _choice(raw.get("sash", "whole"), f"{where}.sash", SASHES)}
    if found[1]["kind"] == "door" and item["sash"] != "whole":
        _fail(f"{where}.sash", "a door has no sashes")
    return item


def _device_key(kind: str, device_id: Any) -> str:
    where = f"placements.{kind}"
    if kind == "heaters":
        if not isinstance(device_id, str) or not HEATER_SERIAL_RE.match(device_id):
            _fail(where, f"'{device_id}' is not a twelve-digit heater serial")
        return device_id
    return _identifier(device_id, where, SENSOR_ID_RE)


def _validate_placements(raw: Any, index: _Index) -> Dict[str, Dict[str, dict]]:
    if raw is None:
        raw = {}
    _only_keys(raw, "placements", (), PLACEMENT_KINDS)
    result: Dict[str, Dict[str, dict]] = {}
    total = 0
    for kind in PLACEMENT_KINDS:
        entries = raw.get(kind, {})
        if not isinstance(entries, dict):
            _fail(f"placements.{kind}", "must be an object keyed by device")
        total += len(entries)
        result[kind] = {
            _device_key(kind, device_id): _validate_placement(kind, device_id, value, index)
            for device_id, value in entries.items()
        }
    if total > MAX_PLACEMENTS:
        _fail("placements", f"must have at most {MAX_PLACEMENTS} entries")
    return result


def validate(raw: Any) -> dict:
    """Check a whole floor plan and return a clean copy, or raise HouseModelError.

    The copy holds only known fields, with numbers rounded to a tenth of a
    millimetre and text collapsed to single spaces, so what is saved is
    exactly what was checked.
    """
    _only_keys(raw, "", ("schema", "levels", "walls", "rooms"), ("fixtures", "placements"))
    if raw["schema"] != SCHEMA_VERSION:
        _fail("schema", f"must be {SCHEMA_VERSION}")
    levels = _validate_levels(raw["levels"])
    level_map = {level["id"]: level for level in levels}
    walls = _validate_walls(raw["walls"], level_map)
    rooms = _validate_rooms(raw["rooms"], level_map)
    fixtures = _validate_fixtures(raw.get("fixtures", []), level_map)
    placements = _validate_placements(raw.get("placements"), _Index(levels, walls, rooms))
    model = {"schema": SCHEMA_VERSION, "levels": levels, "walls": walls, "rooms": rooms,
             "fixtures": fixtures, "placements": placements}
    if len(json.dumps(model)) > MAX_BYTES:
        _fail("", f"the floor plan is larger than {MAX_BYTES // 1024} KB")
    return model


def parse(body: bytes) -> dict:
    """Validate a floor plan as uploaded: size first, then JSON, then content."""
    if len(body) > MAX_BYTES:
        raise HouseModelError(f"the floor plan is larger than {MAX_BYTES // 1024} KB")
    try:
        raw = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HouseModelError(f"not valid JSON: {exc}") from None
    return validate(raw)


# ---------------------------------------------------------------------------
# Changing a saved plan
# ---------------------------------------------------------------------------

def replace_geometry(new_model: dict, old_model: Optional[dict]) -> Tuple[dict, Dict[str, List[str]]]:
    """Take a new plan, keeping earlier placements wherever they still fit.

    Placements in the uploaded file win. Every other device placed before
    keeps its place if what it points at still exists in the new plan;
    the rest are returned, so the caller can say what was dropped rather
    than lose them silently.
    """
    model = copy.deepcopy(new_model)
    dropped: Dict[str, List[str]] = {kind: [] for kind in PLACEMENT_KINDS}
    if not old_model:
        return model, dropped
    index = _Index(model["levels"], model["walls"], model["rooms"])
    total = sum(len(model["placements"][kind]) for kind in PLACEMENT_KINDS)
    for kind in PLACEMENT_KINDS:
        for device_id, placement in old_model.get("placements", {}).get(kind, {}).items():
            if device_id in model["placements"][kind]:
                continue
            if total >= MAX_PLACEMENTS:
                dropped[kind].append(device_id)
                continue
            try:
                model["placements"][kind][device_id] = _validate_placement(
                    kind, device_id, placement, index)
                total += 1
            except HouseModelError:
                dropped[kind].append(device_id)
    # Checked whole once more, so nothing is saved that would not load again.
    return validate(model), dropped


def set_placement(model: dict, kind: str, device_id: str, placement: Any) -> dict:
    """Return a copy of *model* with one device placed."""
    if kind not in PLACEMENT_KINDS:
        raise HouseModelError(f"kind must be one of {', '.join(PLACEMENT_KINDS)}")
    key = _device_key(kind, device_id)
    index = _Index(model["levels"], model["walls"], model["rooms"])
    clean = _validate_placement(kind, key, placement, index)
    updated = copy.deepcopy(model)
    updated["placements"][kind][key] = clean
    # Checked whole, so the count and size limits hold for what is saved.
    return validate(updated)


def remove_placement(model: dict, kind: str, device_id: str) -> Tuple[dict, bool]:
    """Return a copy of *model* without that device's placement, and whether it had one."""
    if kind not in PLACEMENT_KINDS:
        raise HouseModelError(f"kind must be one of {', '.join(PLACEMENT_KINDS)}")
    updated = copy.deepcopy(model)
    existed = updated["placements"][kind].pop(device_id, None) is not None
    return updated, existed


def placement_status(model: dict, heater_serials: Optional[Iterable[str]],
                     sensor_ids: Optional[Iterable[str]],
                     standby_sensor_ids: Iterable[str] = ()) -> Dict[str, Dict[str, Optional[List[str]]]]:
    """Devices with no place yet, and places for devices that no longer exist.

    Both lists are for the person placing things. Neither changes anything:
    a stale placement is simply not drawn.

    A kind passed as None is *unknown* — the hub is away, or the sensors have
    not been listed yet — and both of its lists are None rather than empty.
    An empty list would call every placed device gone, and the next "remove
    stale placements" would delete good ones.

    ``standby_sensor_ids`` exist but need no place of their own: a Verisure
    contact standing by behind a Zigbee one is the same door.
    """
    known = {"heaters": heater_serials, "sensors": sensor_ids}
    placed = model.get("placements", {})
    status: Dict[str, Dict[str, Optional[List[str]]]] = {"unplaced": {}, "stale": {}}
    for kind in PLACEMENT_KINDS:
        if known[kind] is None:
            status["unplaced"][kind] = status["stale"][kind] = None
            continue
        wanted = list(dict.fromkeys(known[kind]))
        here = placed.get(kind, {})
        status["unplaced"][kind] = [device for device in wanted if device not in here]
        present = set(wanted) | (set(standby_sensor_ids) if kind == "sensors" else set())
        status["stale"][kind] = sorted(device for device in here if device not in present)
    return status


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def house_file(demo: bool) -> Path:
    """Demo mode keeps its own plan, so switching to a real hub never shows the demo house."""
    return DATA_DIR / (DEMO_HOUSE_FILENAME if demo else HOUSE_FILENAME)


def _read(path: Path, what: str) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("%s could not be read (%s); the house view is unavailable", what, exc)
        return None
    try:
        return validate(raw)
    except HouseModelError as exc:
        # Kept, not renamed: an administrator may want to fix it by hand, and
        # the house view is optional, so its absence harms nothing.
        logger.warning("%s is not a valid floor plan (%s); the house view is unavailable", what, exc)
        return None


def load(demo: bool) -> Tuple[Optional[dict], Optional[str]]:
    """The saved plan and where it came from: ``"file"``, ``"demo"`` or ``None``."""
    path = house_file(demo)
    model = _read(path, path.name)
    if model is not None:
        return model, "file"
    if demo and not path.exists():
        template = _read(DEMO_TEMPLATE_FILE, DEMO_TEMPLATE_FILE.name)
        if template is not None:
            return template, "demo"
    return None, None


def save(model: dict, demo: bool) -> None:
    """Write the plan atomically: to a temporary file first, then renamed over."""
    path = house_file(demo)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(model, fh, indent=1)
    tmp.replace(path)


def delete(demo: bool) -> bool:
    path = house_file(demo)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
