# The house view

A 3D cutaway of the building on the front page, showing where every heater
and sensor is. It is **optional, per user**: until someone turns it on under
Settings, their front page stays exactly as it was.

This document is the design. It is being delivered in stages:

| Stage | What | State |
| --- | --- | --- |
| 1 | The floor-plan model, its API, each user's display choice, a demo building | Done |
| 2 | The Dollhouse view in Cabin: vendored three.js, live updates, room sheet | Done |
| 3 | Placing heaters and sensors from a top-down plan (admin) | Done |
| 4 | Loading a real building's plan on its Pi (`scripts/make_house.py`) | **This change**. The plan itself is never committed |

## The view

**Turning it on.** Settings → House View → *Rooms on the front page* →
**House**. That is this user's choice alone, kept on the Pi, so it follows
them to every phone. Everyone else keeps the list. On the front page a
List/House switch beside **Rooms** changes only what is on screen now; the
lasting choice stays the one in Settings. An administrator loads or removes
the plan on the same card.

**What it draws.** The *Dollhouse*: the roof off and the walls cut at 1.35 m,
one floor at a time, seen from above at an angle. It can be turned, tilted
and pinched, and opened full screen.

- Each room's floor is washed in its mode's colour (Comfort, Eco, Away, Off)
  and labelled with the zone's name, the set point it is aiming for and, only
  where something measured it, **Actual**. A room with no heater is plain.
- Heaters are drawn where they hang. Floor heating is drawn as a striped
  floor. A heater is never shown as "on", because the hub cannot say so.
- A door or window with a contact on it is drawn **open** when the contact
  says open, sash by sash, with a red "Open · 4 min" label. A closed contact
  is a small dot. A contact that is unavailable, or set aside behind a Zigbee
  one, never opens anything.
- Labels that would cover each other give way. Open doors and windows always
  show. Rooms are shortened to their name, then hidden, smallest first. Any
  room is still one tap away.
- Tapping a room opens a sheet listing its heaters and sensors and where they
  are ("Under W7", "Floor heating", "W7 · left sash"). Its one button goes to
  the room's own page. **Nothing is changed from the house view itself.**

**How it is delivered.** `house.js` draws and `house_logic.js` decides what
is drawn. The second is a plain module with no imports, so every rule above
is tested in node (`tests/test_house_ui.py`). three.js r160 is vendored,
unmodified, under `app/static/ui/shared/vendor/three/`, with the hashes in
`VERSION` checked by a test. An import map in `index.html` resolves `three`
to it. Nothing is fetched from the internet. `cabin.js` loads `house.js` with
`import()` the first time the view is shown, so someone who never turns it on
never downloads either file.

It draws only when something changes, and stops while another screen is
showing. Colours come from the interface's own palette and follow dark mode.
Doors and windows swing open unless the phone asks for reduced motion.

**When it cannot draw** (no WebGL, or the files fail to load), the room list
is shown, with one line beside **Rooms** saying why. Nothing else is
affected.

## Placing devices

Settings → House View → **Place devices…**, for an administrator once a plan
is loaded. The building is drawn from above, one floor at a time, in the
colours of an architect's drawing, with every door and window carrying its
code. Below it are three lists: **Not placed yet**, **Placed** (with where,
in words) and **No longer here** (placed once, but the hub or the sensor list
no longer has them).

Choose a device, then tap where it is. What a tap means depends on the device:

| Device | Tap | Then choose |
| --- | --- | --- |
| Door or window contact | the door or window's code (or near it) | the sash, left or right as seen from inside, or the whole window. A name with "Left" or "Right" in it, or a sensor already on the other sash, suggests one |
| Thermometer | where it is in the room | how high: on a wall (1.5 m), above a door (2.1 m) or the ceiling. A Verisure device starts on the ceiling, as smoke detectors are |
| Heater | the wall it hangs on, or its room | from a wall, with a rough length, or the whole floor. An NTB-2R starts on the floor |

Nothing is saved until **Place it** in the sheet that follows. If the place is
not in the device's own room, the sheet says so, and that it will still warn
and heat for its own room. **⋯** on a placed device moves it, marks it **Not in
this building** (a woodshed: not drawn, and no longer asked for), or removes it
from the plan.

`house_plan.js` draws the plan as SVG, not WebGL, so it works on a browser
that cannot show the house. It is loaded with `import()` the first time the
screen opens. Its drawing is a pure function, `planSvg`, tested in node, and
every geometry rule it and the sheets use (the room at a point, the nearest
wall or opening, the rooms either side of a wall, a heater kept on its wall,
a place in words) is in `house_logic.js`, tested the same way. It saves through
the placement API above, one device at a time.

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

## Loading your own building

A floor plan names rooms and sensors by id, and those ids exist only on one
Pi. So a real building is written as a **template**: the same file, with
names where the ids would go.

| In `house.json` | In a template |
| --- | --- |
| `"schema": 1` | `"template": 1` |
| a room's `"zone_id": "9"` | `"zone": "Kitchen"`, the room's name in the app |
| `placements.heaters` keyed by serial | keyed by serial, or by the heater's name |
| `placements.sensors` keyed by sensor id | keyed by the sensor's name, or its id |

Everything else (levels, walls, openings, fixtures and the placements
themselves) is written exactly as above. `docs/house.template.example.json`
is the demo building as a template.

`scripts/make_house.py` signs in to a Pi, reads its rooms, heaters and
sensors, swaps every name for its id, checks the result with the same
`house_model.validate` the Pi uses, and writes `house.json`:

```bash
# On the Pi itself, from the installation directory
python3 scripts/make_house.py --template cabin.template.json
# From another machine, with HTTPS on the Pi's own certificate authority
python3 scripts/make_house.py --template cabin.template.json --pi https://cabin.local --insecure
```

It asks for the password (or reads `NOBO_PASSWORD`), and only reads. Load the
file under Settings → House View, or add `--upload`, which asks before it
replaces the plan on the Pi. Names are matched ignoring case and spacing. A
name the Pi does not have, or has twice, stops it with a list of what to
fix; `--allow-missing` instead leaves such a heater or sensor out, to be
placed by hand afterwards. A room is never left out, because a room on the
wrong zone would show the wrong heating.

**Keep a real template out of this repository.** It says where every door,
window and alarm sensor is, and the repository is public. `.gitignore` covers
`*.template.json` and `house.json` wherever they are. A secret GitHub gist works well: give the
script its raw link, which must be https.

```bash
python3 scripts/make_house.py --template https://gist.githubusercontent.com/<you>/<id>/raw/cabin.template.json
```

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
