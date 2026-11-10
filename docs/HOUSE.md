# The house view

A 3D cutaway of the building on the front page, showing where every heater
and sensor is. It is **optional, per user**: until someone turns it on under
Settings, their front page stays exactly as it was.

This document is the design. It is being delivered in stages:

| Stage | What | State |
| --- | --- | --- |
| 1 | The floor-plan model, its API, each user's display choice, a demo building | **This change** |
| 2 | The Dollhouse view in Cabin: vendored three.js, live updates, room sheet | Planned |
| 3 | Placing heaters and sensors from a top-down plan (admin) | Planned |
| 4 | Loading the real building's plan on the Pi | Planned. The plan is never committed |

## What it must never do

- **Change the heating.** A placement says where a device is *drawn*. Which
  room a sensor warns for, which heating it changes and its rules all stay in
  the sensor files. Nothing in `house_model.py` is read by anything that heats,
  warns or writes to the hub. A test checks that loading and placing leave the
  zones untouched and push nothing to other browsers.
- **Say a heater is on.** The hub does not report output. A heater's colour
  means its room's *mode*.
- **Show a temperature nothing measured.** "Actual" appears only where a
  thermometer exists, exactly as on the room cards.
- **Leave the Pi.** The plan is the owner's building. It is kept in `data/`,
  served only behind sign-in, never shown on `/login`, never opened by a
  display key, and never committed. The repository is public, and an
  architect's drawing carries a name and a plot number.

## The floor plan: `data/house.json`

One JSON file per installation. Demo mode keeps its own, `data/house_demo.json`,
so switching to a real hub never shows the demo building. Until demo mode has
one, the generic `app/demo_house.json` stands in. That file is committed and
has nothing to do with any real building.

Geometry is in metres on the plan: `x` east, `y` north. Heights are measured
from the floor of the level.

```jsonc
{
  "schema": 1,
  "levels":   [{ "id": "ground", "name": "Ground floor", "elevation": 0, "height": 2.5 }],
  "walls":    [{ "id": "s", "level": "ground", "a": [0, 0], "b": [12, 0],
                 "thickness": 0.15, "exterior": true,
                 "openings": [
                   { "code": "D1", "kind": "door",   "at": 4.05, "width": 0.9, "height": 2.0, "swing": "out" },
                   { "code": "W1", "kind": "window", "at": 0.9,  "width": 1.2, "sill": 0.9, "height": 1.2 },
                   {               "kind": "gap",    "at": 7.0,  "width": 1.5 } ] }],
  "rooms":    [{ "id": "hallway", "level": "ground", "zone_id": "3",
                 "polygon": [[3, 0], [6, 0], [6, 4], [3, 4]] },
               { "id": "sauna", "level": "ground", "name": "Sauna", "polygon": [] }],
  "fixtures": [{ "level": "ground", "box": [3.15, 0.6, 3.95, 3.6], "height": 2.7, "label": "Stairs" }],
  "placements": {
    "heaters": { "160004028114": { "wall": "n", "at": 4.1, "length": 1.0 },
                 "000000016249": { "floor": "hallway" },
                 "234000012006": { "point": [4.5, 4.15, 1.5], "level": "ground" } },
    "sensors": { "0x00158d008c8bc54f": { "opening": "W7", "sash": "left" },
                 "0x00158d000223b97c": { "point": [6.2, 10.1, 1.5], "level": "ground" },
                 "verisure-contact-3spc-jgfp": { "outside": true } }
  }
}
```

**Rooms point at hub zones by `zone_id`.** The name shown is always the zone's
own, so renaming a room in the app renames it in the house view. `name` is
only for a room with no zone (a sauna, a store), so it is not blank. Several
rooms may share one zone.

**Openings have codes** — `W7`, `D2` — because that is what people say out
loud, and it is what a contact is placed on. A door or window must have one;
an open doorway (`gap`) need not.

**Heaters are keyed by their twelve-digit serial**, the one the zone list
carries, and placed in one of three ways:

- on a wall, `at` metres from its start, `length` long;
- as a whole floor (`floor`), for floor heating: an NTB-2R heats its room's
  floor, so the floor is the heater;
- at a `point`, for a control panel.

**Sensors are keyed by their sensor id** and placed in one of three ways:

- on an `opening`, with a `sash` (`left`, `right` as seen from inside, or
  `whole`). A door has no sashes. When the contact reports open, that door or
  sash is drawn open;
- at a `point`, for a thermometer or smoke detector;
- `outside`: deliberately not drawn, for something in another building. This
  stops it being listed as unplaced forever.

### What is checked

`house_model.validate` checks the whole file before anything is saved, and
saves the cleaned copy it checked:

- known fields only, schema `1`, no more than 256 KB;
- every number finite and within 100 m;
- ids unique; every reference (level, wall, room, opening) exists;
- a wall at least 5 cm long, and an opening or heater that fits on its wall;
- a room outline of 3–64 corners that has area and does not cross itself;
- text collapsed to single spaces, with no control characters.

A saved file that fails these checks is left where it is, for someone to fix,
and the house view is simply unavailable. Nothing else depends on it.

## API

| Endpoint | Who | Purpose |
| --- | --- | --- |
| `GET /api/house` | signed in | The plan, where it came from (`file` or `demo`), and `status`: devices still `unplaced`, and `stale` placements for devices or rooms' zones that no longer exist. A list is `null`, never empty, while it is not known: heaters and zones while the hub is not connected, sensors while the sensor feature is off or has listed nothing yet. A Verisure sensor standing by behind a Zigbee one exists but is never listed as unplaced. 404 when there is no plan |
| `PUT /api/house` | admin | Load or replace the plan. Placements in the upload are used; earlier placements that still fit are kept, up to the limit of 500, and the rest are listed in `dropped`. 400 says what is wrong; 413 if too large |
| `DELETE /api/house` | admin | Remove the plan |
| `PUT /api/house/placements/{heaters\|sensors}/{id}` | admin | Place one device. It must exist: 404 otherwise, 503 while its list is not known |
| `DELETE /api/house/placements/{heaters\|sensors}/{id}` | admin | Unplace one device, including one that no longer exists |
| `GET` / `PUT /api/me/preferences` | signed in, own user only | `{"rooms_view": "list" \| "house"}`. Default `list` |

Writes under `/api/house` and `/api/me/` push nothing to other browsers: they
change no zone. Every change to the plan is read, changed and saved with
nothing awaited in between, so two changes sent at once both land, and a
placement sent while the plan is being replaced cannot write the old plan back.
Whatever is saved is validated whole first, so it always loads again.

With `NOBO_ALLOW_ANON_API=true`, `GET /api/house` is open like the rest of
`/api/`. Changing the plan still needs an administrator's session, and
`/api/me/preferences` always needs a session, because without one there is no
"me".

## Each user's choice: `data/user_prefs.json`

Kept on the Pi rather than in the browser, so it follows its owner to every
phone. It is keyed by username: a rename moves it, and deleting the user removes
it. Only display choices may ever live here. Two people in one house must
never get different heating from the same tap.

## Backups

`backup.sh` copies `data/` whole, so `house.json`, `house_demo.json` and
`user_prefs.json` are included. Neither holds a secret, and redoing the
placements takes time.
