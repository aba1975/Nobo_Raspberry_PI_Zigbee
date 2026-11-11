#!/usr/bin/env python3
"""Turn a floor-plan *template* into the house.json a Pi accepts.

A floor plan (docs/HOUSE.md) points at things by id: a room at its zone id, a
sensor at its sensor id. Those ids belong to one installation and are only
known on its Pi. A *template* is the same plan written with names instead:

    rooms:      "zone": "Kitchen"            instead of "zone_id": "9"
    heaters:    keyed by heater name or by its twelve-digit serial
    sensors:    keyed by sensor name or by its id

This script signs in to a Pi, reads its rooms, heaters and sensors, swaps
every name for its id, checks the result with the application's own
validator, and writes it out. It refuses rather than guesses: a name the Pi
does not have, or has twice, stops it with a list of what to fix.

It only reads, unless asked to upload with --upload, which asks first. It
needs nothing beyond Python 3.8 and runs on the Pi itself or on any machine
that can reach it.

    python3 scripts/make_house.py --template cabin.template.json
    python3 scripts/make_house.py --template https://gist.githubusercontent.com/... \\
        --pi https://cabin.local --insecure --upload

The template may be a file or an https URL, such as a secret gist's raw
link, so the plan of a real building never has to be in this repository.
"""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

APP_DIR = Path(__file__).resolve().parent.parent / "app"
TEMPLATE_VERSION = 1
SERIAL_RE = re.compile(r"^\d{12}$")


class TemplateError(Exception):
    """The template cannot be resolved; the message says what to fix."""


def _norm(name: Any) -> str:
    # The hub sends names with U+00A0 for spaces; people type ordinary ones.
    return " ".join(str(name).replace("\u00a0", " ").split()).casefold()


def _index(items: List[dict]) -> Dict[str, List[dict]]:
    found: Dict[str, List[dict]] = {}
    for item in items:
        found.setdefault(_norm(item.get("name", "")), []).append(item)
    return found


def resolve(template: dict, zones: List[dict], devices: List[dict],
            sensors: Optional[List[dict]], allow_missing: bool = False) -> Tuple[dict, List[str]]:
    """Return (house.json, notes) for ``template``, or raise TemplateError.

    ``zones``, ``devices`` and ``sensors`` are as GET /api/zones, /api/devices
    and /api/sensors return them. ``sensors`` is None when the Pi has the
    sensor feature off; a template that places sensors then cannot be used.

    With ``allow_missing``, a heater or sensor the Pi does not have is left
    out and noted instead of stopping everything: a device renamed since the
    template was written is then simply placed by hand afterwards. Rooms are
    never let through, because a room on the wrong zone shows the wrong
    heating.
    """
    if not isinstance(template, dict) or template.get("template") != TEMPLATE_VERSION:
        raise TemplateError(f'not a floor-plan template: it needs "template": {TEMPLATE_VERSION}')
    # Shapes first, so a mistake reads as a message rather than a traceback.
    rooms_in = template.get("rooms", [])
    placements_in = template.get("placements") or {}
    if not isinstance(rooms_in, list) or not all(isinstance(r, dict) for r in rooms_in):
        raise TemplateError('  - "rooms" must be a list of rooms')
    if not isinstance(placements_in, dict) or not all(
            isinstance(placements_in.get(kind) or {}, dict) for kind in ("heaters", "sensors")):
        raise TemplateError('  - "placements" must hold "heaters" and "sensors", each keyed by device')
    problems: List[str] = []
    notes: List[str] = []
    missing: List[str] = []

    zones_by_name = _index(zones)
    zone_ids = {str(z["zone_id"]) for z in zones}
    rooms = []
    for room in rooms_in:
        room = dict(room)
        if "zone" in room:
            name = room.pop("zone")
            if "zone_id" in room:
                problems.append(f'room {room.get("id")}: give "zone" or "zone_id", not both')
                continue
            found = zones_by_name.get(_norm(name), [])
            if len(found) == 1:
                room["zone_id"] = str(found[0]["zone_id"])
            elif not found:
                problems.append(f'room {room.get("id")}: the Pi has no room called "{name}"')
            else:
                problems.append(f'room {room.get("id")}: the Pi has {len(found)} rooms called "{name}"')
        elif room.get("zone_id") is not None and str(room["zone_id"]) not in zone_ids:
            problems.append(f'room {room.get("id")}: the Pi has no zone {room["zone_id"]}')
        rooms.append(room)

    placements = placements_in
    heaters_by_name = _index(devices)
    serials = {str(d["serial"]) for d in devices}
    heaters: Dict[str, Any] = {}
    for key, place in (placements.get("heaters") or {}).items():
        compact = str(key).replace(" ", "")
        if SERIAL_RE.match(compact):
            if compact not in serials:
                missing.append(f"heater {key}: the Pi has no heater with that serial")
                continue
            serial = compact
        else:
            found = heaters_by_name.get(_norm(key), [])
            if len(found) > 1:
                problems.append(f'heater "{key}": the Pi has {len(found)} heaters called that; use its serial')
                continue
            if not found:
                missing.append(f'heater "{key}": the Pi has no heater called that')
                continue
            serial = str(found[0]["serial"])
        if serial in heaters:
            problems.append(f"heater {key}: placed twice")
            continue
        heaters[serial] = place

    placed_sensors: Dict[str, Any] = {}
    wanted = placements.get("sensors") or {}
    if wanted and sensors is None:
        problems.append("the template places sensors, but sensors are off on this Pi")
    elif wanted:
        by_name = _index(sensors)
        ids = {str(s["sensor_id"]) for s in sensors}
        for key, place in wanted.items():
            if str(key) in ids:
                sensor_id = str(key)
            else:
                found = by_name.get(_norm(key), [])
                # A Verisure sensor standing by behind a Zigbee one may share
                # its name; the one that counts is the one to place.
                if len(found) > 1:
                    found = [s for s in found if s.get("counts") is not False] or found
                if len(found) > 1:
                    problems.append(f'sensor "{key}": the Pi has {len(found)} sensors called that; use its id')
                    continue
                if not found:
                    missing.append(f'sensor "{key}": the Pi has no sensor called that')
                    continue
                sensor_id = str(found[0]["sensor_id"])
            if sensor_id in placed_sensors:
                problems.append(f'sensor "{key}": placed twice')
                continue
            placed_sensors[sensor_id] = place

    if allow_missing:
        notes.extend(f"left out, {text}" for text in missing)
    else:
        problems.extend(missing)
    if problems:
        raise TemplateError("\n".join(f"  - {p}" for p in problems))

    house = {"schema": 1}
    for key in ("levels", "walls"):
        house[key] = template.get(key, [])
    house["rooms"] = rooms
    house["fixtures"] = template.get("fixtures", [])
    house["placements"] = {"heaters": heaters, "sensors": placed_sensors}

    placed_rooms = {r.get("zone_id") for r in rooms}
    for zone in zones:
        if str(zone["zone_id"]) not in placed_rooms:
            notes.append(f'room "{zone.get("name")}" is not on the plan')
    for device in devices:
        if str(device["serial"]) not in heaters:
            notes.append(f'heater "{device.get("name")}" is not placed yet')
    for sensor in sensors or []:
        if str(sensor["sensor_id"]) not in placed_sensors and sensor.get("counts") is not False:
            notes.append(f'sensor "{sensor.get("name")}" is not placed yet')
    return house, notes


def check(house: dict) -> dict:
    """Validate with the application's own rules, exactly as the Pi will."""
    sys.path.insert(0, str(APP_DIR))
    try:
        import house_model  # noqa: E402
    finally:
        sys.path.pop(0)
    try:
        return house_model.validate(house)
    except house_model.HouseModelError as exc:
        raise TemplateError(f"  - the Pi would refuse it: {exc}") from None


# --- Talking to the Pi -------------------------------------------------------

class Pi:
    """A signed-in session with one Pi. Reads, and writes only in upload()."""

    def __init__(self, base: str, insecure: bool = False, cacert: Optional[str] = None):
        self.base = base.rstrip("/")
        context = None
        if self.base.startswith("https://"):
            context = ssl.create_default_context(cafile=cacert)
            if insecure:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=context),
        )

    def request(self, method: str, path: str, body: Optional[bytes] = None,
                content_type: Optional[str] = None) -> Tuple[int, Any]:
        req = urllib.request.Request(self.base + path, data=body, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with self.opener.open(req, timeout=30) as response:
                raw = response.read()
                try:
                    return response.status, json.loads(raw or b"null")
                except ValueError:
                    raise TemplateError(f"  - {path} did not answer with JSON: is --pi the right address?") from None
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("detail")
            except Exception:
                detail = None
            return exc.code, {"detail": detail or exc.reason}

    def login(self, username: str, password: str) -> dict:
        form = urllib.parse.urlencode({"username": username, "password": password}).encode()
        status, body = self.request("POST", "/auth/login", form, "application/x-www-form-urlencoded")
        if status != 200:
            raise TemplateError(f"  - could not sign in: {body.get('detail') if body else status}")
        return body

    def get(self, path: str, allow: Tuple[int, ...] = ()) -> Any:
        status, body = self.request("GET", path)
        if status in allow:
            return None
        if status != 200:
            raise TemplateError(f"  - GET {path} answered {status}: {body.get('detail') if body else ''}")
        return body

    def upload(self, house: dict) -> dict:
        status, body = self.request("PUT", "/api/house", json.dumps(house).encode(), "application/json")
        if status != 200:
            raise TemplateError(f"  - the Pi refused the floor plan ({status}): {body.get('detail')}")
        return body

    def logout(self) -> None:
        try:
            self.request("POST", "/auth/logout", b"")
        except Exception:
            pass


def read_pi(pi: Pi) -> Tuple[List[dict], List[dict], Optional[List[dict]]]:
    def unwrap(body: Any, key: str) -> Any:
        return body.get(key, []) if isinstance(body, dict) else body

    zones = unwrap(pi.get("/api/zones"), "zones")
    devices = unwrap(pi.get("/api/devices"), "devices")
    sensors = pi.get("/api/sensors", allow=(404, 409, 503))
    if sensors is not None:
        sensors = unwrap(sensors, "sensors")
    return zones, devices, sensors


def load_template(source: str) -> dict:
    if source.startswith("http://"):
        raise TemplateError("  - fetch the template over https, not http")
    if source.startswith("https://"):
        with urllib.request.urlopen(source, timeout=30) as response:
            text = response.read().decode("utf-8")
    else:
        text = Path(source).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise TemplateError(f"  - the template is not valid JSON: {exc}") from None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--template", required=True, help="template file, or an https URL")
    parser.add_argument("--pi", default="http://127.0.0.1:8000",
                        help="the Pi's address (default: http://127.0.0.1:8000, the Pi itself)")
    parser.add_argument("--user", default="admin", help="an administrator's username (default: admin)")
    parser.add_argument("--insecure", action="store_true",
                        help="accept the Pi's own HTTPS certificate without checking it")
    parser.add_argument("--cacert", help="the Pi's root certificate, to check HTTPS properly")
    parser.add_argument("-o", "--output", default="house.json", help="where to write (default: house.json)")
    parser.add_argument("--allow-missing", action="store_true",
                        help="leave out a heater or sensor the Pi does not have, instead of stopping")
    parser.add_argument("--upload", action="store_true", help="also load it on the Pi, after asking")
    parser.add_argument("--yes", action="store_true", help="with --upload, do not ask")
    args = parser.parse_args(argv)

    try:
        template = load_template(args.template)
        password = os.environ.get("NOBO_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")
        pi = Pi(args.pi, insecure=args.insecure, cacert=args.cacert)
        me = pi.login(args.user, password)
        try:
            if args.upload and (me or {}).get("role") != "admin":
                raise TemplateError(f"  - {args.user} is not an administrator, so cannot load a floor plan")
            zones, devices, sensors = read_pi(pi)
            house, notes = resolve(template, zones, devices, sensors, allow_missing=args.allow_missing)
            house = check(house)
            Path(args.output).write_text(json.dumps(house, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            placed = house["placements"]
            print(f"Wrote {args.output}: {len(house['rooms'])} rooms, {len(placed['heaters'])} heaters "
                  f"and {len(placed['sensors'])} sensors placed.")
            for note in notes:
                print(f"  note: {note}")
            if args.upload:
                if not args.yes:
                    answer = input(f"Load it on {args.pi}, replacing the floor plan there? [y/N] ")
                    if answer.strip().lower() not in ("y", "yes"):
                        print("Not loaded. The file is written; you can load it under Settings → House View.")
                        return 0
                try:
                    result = pi.upload(house)
                except (TemplateError, OSError) as exc:
                    # A timeout may come after the Pi has saved it: say so, not "nothing".
                    print(f"{args.output} is written, but loading it on the Pi failed or did not answer:\n"
                          f"{exc}\nCheck Settings → House View to see which plan the Pi has.", file=sys.stderr)
                    return 1
                dropped = result.get("dropped") or {}
                lost = len(dropped.get("heaters", [])) + len(dropped.get("sensors", []))
                print(f"Loaded on {args.pi}." + (f" {lost} earlier placements no longer fit." if lost else ""))
        finally:
            pi.logout()
    except TemplateError as exc:
        print(f"Nothing written:\n{exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        # OSError covers the network (URLError); ValueError a file that is not UTF-8.
        print(f"Nothing written: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
