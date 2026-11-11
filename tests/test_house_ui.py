"""The house view in the Cabin interface (docs/HOUSE.md, stage 2).

Two kinds of check. Static ones hold the promises the view makes about how it
is delivered: three.js is vendored and pinned, nothing is fetched from the
internet, and nobody who has not chosen the house view downloads any of it.
The rest run the real ``house_logic.js`` in node, because every rule about
*what* the view shows lives there: a room's colour is its mode, never "on";
"Actual" appears only where something measured it; a door is drawn open only
when a sensor that counts says so.
"""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static" / "ui"
CABIN = STATIC / "cabin"
VENDOR = STATIC / "shared" / "vendor" / "three"
HOUSE_JS = CABIN / "house.js"
LOGIC_JS = CABIN / "house_logic.js"
PLAN_JS = CABIN / "house_plan.js"
INDEX = CABIN / "index.html"
CABIN_JS = CABIN / "cabin.js"


# --- Delivery ---------------------------------------------------------------

def test_vendored_three_matches_its_recorded_hashes():
    lines = (VENDOR / "VERSION").read_text(encoding="utf-8").splitlines()
    assert "r160" in lines[0]
    recorded = {}
    for line in lines:
        m = re.match(r"^([0-9a-f]{64})\s+(\S+)", line)
        if m:
            recorded[m.group(2)] = m.group(1)
    assert set(recorded) == {"three.module.min.js", "OrbitControls.js", "CSS2DRenderer.js", "LICENSE"}
    for name, digest in recorded.items():
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name


def test_vendored_three_is_the_version_recorded():
    src = (VENDOR / "three.module.min.js").read_text(encoding="utf-8")
    assert re.search(r"""\bconst t=["']160["']""", src[:2000])


def test_the_list_stays_the_default():
    import user_prefs
    assert user_prefs.DEFAULTS["rooms_view"] == "list"


def test_three_is_vendored_under_shared_not_as_an_interface():
    # Every non-shared folder under ui/ is an interface with an index.html.
    assert not (STATIC / "vendor").exists()


def test_nothing_is_fetched_from_the_internet():
    for path in (HOUSE_JS, LOGIC_JS, PLAN_JS):
        assert "http://" not in path.read_text(encoding="utf-8"), path.name
        assert "https://" not in path.read_text(encoding="utf-8"), path.name
    html = INDEX.read_text(encoding="utf-8")
    for url in re.findall(r"""(?:src|href)=["'](https?://[^"']+)""", html):
        pytest.fail(f"index.html loads {url}")


def test_import_map_points_at_the_vendored_copy():
    html = INDEX.read_text(encoding="utf-8")
    m = re.search(r'<script type="importmap">\s*(\{.*?\})\s*</script>', html, re.S)
    assert m, "no import map"
    target = json.loads(m.group(1))["imports"]["three"]
    assert target == "/static/ui/shared/vendor/three/three.module.min.js"
    assert (ROOT / "app" / target.lstrip("/")).is_file()


@pytest.mark.parametrize("path", [HOUSE_JS, PLAN_JS], ids=lambda p: p.name)
def test_house_imports_only_vendored_or_own_modules(path):
    src = path.read_text(encoding="utf-8")
    for spec in re.findall(r"^import .* from '([^']+)';", src, re.M):
        assert spec == "three" or spec.startswith("/static/ui/"), spec
        if spec != "three":
            assert (ROOT / "app" / spec.lstrip("/")).is_file(), spec


def test_the_blueprint_needs_no_webgl():
    """Placing must work on a browser that cannot draw the house."""
    specs = re.findall(r"^import .* from '([^']+)';", PLAN_JS.read_text(encoding="utf-8"), re.M)
    assert specs == ["/static/ui/cabin/house_logic.js"]


def test_house_is_only_loaded_on_demand():
    """Somebody who never turns the house view on never downloads it."""
    html = re.sub(r"<!--.*?-->", "", INDEX.read_text(encoding="utf-8"), flags=re.S)
    assert "house.js" not in html
    assert "three.module" not in re.sub(r'<script type="importmap">.*?</script>', "", html, flags=re.S)
    src = CABIN_JS.read_text(encoding="utf-8")
    assert "import('/static/ui/cabin/house.js')" in src
    assert "house_plan.js" not in html
    assert "import('/static/ui/cabin/house_plan.js')" in src
    assert not re.search(r"^import ", src, re.M)


def test_placing_is_an_admin_screen_reached_from_settings():
    html = INDEX.read_text(encoding="utf-8")
    assert '<section class="view" id="viewPlace" hidden></section>' in html
    src = CABIN_JS.read_text(encoding="utf-8")
    card = src.split("function renderHouseSettingsCard", 1)[1].split("function houseStatusHtml", 1)[0]
    # The button is inside the part of the card only an administrator gets.
    assert card.index("${isAdmin ? `") < card.index('data-act="place-devices"') < card.index("Only an administrator")


# --- What is shown (node) ---------------------------------------------------

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the browser code")


def run_logic(tmp_path, script: str):
    """Run ``script`` with house_logic.js imported as ``L``; return its JSON."""
    module = tmp_path / "house_logic.mjs"
    module.write_text(LOGIC_JS.read_text(encoding="utf-8"), encoding="utf-8")
    runner = tmp_path / "run.mjs"
    runner.write_text(
        "import * as L from './house_logic.mjs';\n"
        f"const result = (() => {{ {script} }})();\n"
        "console.log(JSON.stringify(result));\n",
        encoding="utf-8",
    )
    out = subprocess.run(["node", str(runner)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


ZONES = """
const zones = L.zonesById([
  { zone_id: 1, name: 'Living Room', components: ['a'], current_mode: 'normal', schedule_mode: 'comfort',
    comfort_temperature: '21', eco_temperature: '16', current_temperature: 20.4 },
  { zone_id: 2, name: 'Master Bedroom', components: ['b'], current_mode: 'eco', schedule_mode: 'comfort',
    comfort_temperature: 21, eco_temperature: 18, current_temperature: null },
  { zone_id: 3, name: 'Tech Room', components: [], current_mode: 'normal', schedule_mode: 'comfort',
    comfort_temperature: 20, eco_temperature: 15 },
  { zone_id: 4, name: 'Shed', components: ['c'], current_mode: 'normal', schedule_mode: 'off',
    comfort_temperature: 20, eco_temperature: 15 },
]);
"""


@needs_node
def test_a_room_is_coloured_by_its_mode(tmp_path):
    result = run_logic(tmp_path, ZONES + """
      return [1, 2, 3, 4, 99, null].map(id => L.roomTint({ zone_id: id }, zones));
    """)
    assert result == ["comfort", "eco", "none", "off", "unknown", "none"]


@needs_node
def test_actual_only_where_something_measured_it(tmp_path):
    result = run_logic(tmp_path, ZONES + """
      return [1, 2, 3].map(id => L.roomLabel({ zone_id: id }, zones));
    """)
    living, master, tech = result
    assert living["actual"] == 20.4
    assert master["actual"] is None, "no thermometer: no Actual, not the set point"
    assert master["setpoint"] == 18
    assert tech["setpoint"] is None and tech["actual"] is None


@needs_node
def test_hub_string_set_points_are_shown(tmp_path):
    # The hub sends set points as strings; the label must still read 21°.
    result = run_logic(tmp_path, ZONES + """
      const label = L.roomLabel({ zone_id: 1 }, zones);
      return [label.name, L.fmtSet(label.setpoint), L.fmtActual(label.actual)];
    """)
    assert result == ["Living Room", "21°", "20.4°"]


@needs_node
def test_name_comes_from_the_hub_not_the_plan(tmp_path):
    result = run_logic(tmp_path, ZONES + """
      return [L.roomName({ zone_id: 1, name: 'Stue' }, zones),
              L.roomName({ zone_id: null, name: 'Sauna' }, zones),
              L.roomName({ zone_id: 99 }, zones)];
    """)
    assert result == ["Living Room", "Sauna", ""]


@needs_node
def test_a_window_opens_only_when_a_counting_sensor_says_so(tmp_path):
    result = run_logic(tmp_path, """
      const model = { placements: { sensors: {
        left: { opening: 'W7', sash: 'left' },
        right: { opening: 'W7', sash: 'right' },
        door: { opening: 'D1' },
        gone: { opening: 'D2' },
        ignored: { opening: 'D3' },
        thermo: { wall: 'w1', at: 2, height: 2.1 },
      } } };
      const sensors = new Map(Object.entries({
        left: { state: 'open' },
        right: { state: 'closed' },
        door: { state: 'open' },
        gone: { state: 'open', available: false },
        ignored: { state: 'open', counts: false },
      }));
      const states = L.openingStates(model, sensors);
      return Object.fromEntries([...states].map(([code, s]) => [code, [...s.open].sort()]));
    """)
    assert result == {"W7": ["left"], "D1": ["whole"], "D2": [], "D3": []}


@needs_node
def test_floor_heating_stripes_follow_an_l_shaped_room(tmp_path):
    result = run_logic(tmp_path, """
      // An L: 4 m wide at the bottom, 2 m wide above y = 2.
      const L_shape = [[0, 0], [4, 0], [4, 2], [2, 2], [2, 4], [0, 4]];
      return L.floorStripes(L_shape, 0.5, 0.1);
    """)
    assert result, "an L-shaped room gets stripes"
    for x0, x1, y in result:
        assert 0 < x0 < x1
        if y > 2:
            assert x1 <= 2, "nothing striped in the corner the L wraps round"
        else:
            assert x1 > 3


@needs_node
def test_placement_level_and_outside(tmp_path):
    result = run_logic(tmp_path, """
      const model = {
        walls: [{ id: 'w1', level: 'ground', a: [0, 0], b: [5, 0],
                  openings: [{ code: 'W14', at: 1, width: 1 }] },
                { id: 'w2', level: 'loft', a: [0, 0], b: [5, 0], openings: [] }],
        rooms: [{ id: 'r1', level: 'loft', polygon: [[0, 0], [1, 0], [1, 1]] }],
      };
      return [
        L.placementLevel(model, { outside: true }),
        L.placementLevel(model, { wall: 'w2', at: 1 }),
        L.placementLevel(model, { floor: 'r1' }),
        L.placementLevel(model, { opening: 'W14' }),
        L.placementLevel(model, { opening: 'W99' }),
      ];
    """)
    assert result == [None, "loft", "loft", "ground", None]


@needs_node
def test_open_for_reads_as_people_say_it(tmp_path):
    result = run_logic(tmp_path, """
      const now = Date.parse('2026-10-11T12:00:00Z');
      const at = (mins) => ({ changed_at: new Date(now - mins * 60000).toISOString() });
      return [L.openFor({}, now), L.openFor(at(0), now), L.openFor(at(4), now),
              L.openFor(at(150), now), L.openFor(at(60 * 72), now)];
    """)
    assert result == ["Open", "Open · now", "Open · 4 min", "Open · 3 h", "Open · 3 d"]


@needs_node
def test_levels_in_use_skips_empty_levels(tmp_path):
    result = run_logic(tmp_path, """
      const model = {
        levels: [{ id: 'loft', elevation: 2.6 }, { id: 'ground', elevation: 0 }, { id: 'attic', elevation: 5 }],
        walls: [{ level: 'loft', a: [0, 0], b: [1, 0] }],
        rooms: [{ level: 'ground', polygon: [[0, 0], [1, 0], [1, 1]] }],
      };
      return L.levelsInUse(model).map(l => l.id);
    """)
    assert result == ["ground", "loft"]


# --- Placing (stage 3, node) -------------------------------------------------

DEMO_HOUSE = ROOT / "app" / "demo_house.json"


def run_plan(tmp_path, script: str):
    """Run ``script`` with house_logic.js as ``L``, house_plan.js as ``P`` and
    the committed demo building as ``model``; return its JSON."""
    (tmp_path / "house_logic.mjs").write_text(LOGIC_JS.read_text(encoding="utf-8"), encoding="utf-8")
    plan = PLAN_JS.read_text(encoding="utf-8").replace("'/static/ui/cabin/house_logic.js'", "'./house_logic.mjs'")
    (tmp_path / "house_plan.mjs").write_text(plan, encoding="utf-8")
    runner = tmp_path / "run_plan.mjs"
    runner.write_text(
        "import * as L from './house_logic.mjs';\n"
        "import * as P from './house_plan.mjs';\n"
        f"const model = {DEMO_HOUSE.read_text(encoding='utf-8')};\n"
        f"const result = (() => {{ {script} }})();\n"
        "console.log(JSON.stringify(result));\n",
        encoding="utf-8",
    )
    out = subprocess.run(["node", str(runner)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


DEMO_ZONES = """
const zones = L.zonesById([
  { zone_id: '5', name: 'Kitchen' }, { zone_id: '12', name: 'Living Room' },
  { zone_id: '3', name: 'Hallway' }, { zone_id: '7', name: 'Master Bedroom' },
]);
"""


@needs_node
def test_a_tap_finds_the_room_it_is_in(tmp_path):
    result = run_plan(tmp_path, """
      return [L.roomAt(model, 'ground', [7.5, 6]), L.roomAt(model, 'ground', [2, 6]),
              L.roomAt(model, 'ground', [20, 20]), L.roomAt(model, 'loft', [7.5, 6])]
        .map(r => r && r.id);
    """)
    assert result == ["kitchen", "living", None, "bunk-bath"]


@needs_node
def test_a_tap_finds_the_nearest_wall_and_where_along_it(tmp_path):
    result = run_plan(tmp_path, """
      const near = L.nearestWall(model, 'ground', [7.4, 7.7]);
      return [near.wall.id, Math.round(near.t * 100) / 100, Math.round(near.dist * 100) / 100];
    """)
    # The north wall runs east to west from x = 12, so x = 7.4 is 4.6 m along it.
    assert result == ["n", 4.6, 0.3]


@needs_node
def test_a_tap_finds_a_door_or_window_but_never_an_open_doorway(tmp_path):
    result = run_plan(tmp_path, """
      const near = (pt, max) => { const f = L.nearestOpening(model, 'ground', pt, max); return f && f.opening.code; };
      return [near([7.4, 7.9]), near([7.4, 6.5], 1.2), near([6.0, 6.0], 5)];
    """)
    # [6, 6] sits in the open doorway between kitchen and living room: the
    # nearest thing a sensor can go on is a real opening further away.
    assert result[0] == "W6"
    assert result[1] is None
    assert result[2] is not None and result[2] != ""


@needs_node
def test_a_wall_knows_the_rooms_either_side_and_says_so(tmp_path):
    result = run_plan(tmp_path, DEMO_ZONES + """
      const wall = (id) => model.walls.find(w => w.id === id);
      const sides = L.roomsBeside(model, wall('n'), 4.6).map(r => r && r.id);
      const living = model.rooms.find(r => r.id === 'living');
      return [sides,
              L.wallWords(model, wall('n'), 4.6, zones),
              L.wallWords(model, wall('i1'), 4.3, zones, living),
              L.compass([1, 0.2]), L.compass([0, -1])];
    """)
    sides, outside, inside, east, south = result
    assert sides == ["kitchen", None]
    assert outside == "north wall"
    assert inside == "wall to Hallway"
    assert (east, south) == ("east", "south")


@needs_node
def test_a_heater_stays_on_its_wall(tmp_path):
    result = run_plan(tmp_path, """
      const wall = { a: [0, 0], b: [3, 0] };
      return [L.heaterSpan(wall, 1.5, 1.0), L.heaterSpan(wall, 0.1, 1.0),
              L.heaterSpan(wall, 2.95, 1.0), L.heaterSpan(wall, 1.5, 9)];
    """)
    assert result == [
        {"at": 1.0, "length": 1.0},
        {"at": 0.0, "length": 1.0},
        {"at": 2.0, "length": 1.0},
        {"at": 0.0, "length": 3.0},
    ]


@needs_node
def test_what_each_device_is_placed_on(tmp_path):
    result = run_plan(tmp_path, """
      return [L.targetFor('heaters', { device_type: 'R80 RDC 700' }),
              L.targetFor('sensors', { kind: 'window' }), L.targetFor('sensors', { kind: 'door' }),
              L.targetFor('sensors', { kind: 'climate' }),
              L.isFloorHeater({ device_type: 'NTB-2R' }), L.isFloorHeater({ device_type: 'R80 RDC 700' }),
              L.pointHeights(2.5).map(h => [h.id, h.z])];
    """)
    assert result[:4] == ["heater", "opening", "opening", "point"]
    assert result[4:6] == [True, False]
    assert result[6] == [["wall", 1.5], ["door", 2.1], ["ceiling", 2.45]]


@needs_node
def test_a_place_reads_as_people_say_it_without_repeating_the_room(tmp_path):
    result = run_plan(tmp_path, DEMO_ZONES + """
      const w = (p, own) => L.placementWords(model, p, zones, own);
      return [
        w(null), w({ outside: true }),
        w({ floor: 'hallway' }, 'Hallway'), w({ floor: 'hallway' }, 'Kitchen'),
        w({ opening: 'W7', sash: 'left' }), w({ opening: 'D2', sash: 'whole' }), w({ opening: 'W6', sash: 'whole' }),
        w({ wall: 'n', at: 4.1, length: 1.0 }, 'Kitchen'),
        w({ wall: 'i1', at: 1.0, length: 1.0 }, 'Living Room'),
        w({ point: [7.5, 6, 1.5], level: 'ground' }, 'Kitchen'),
        w({ point: [7.5, 6, 2.1], level: 'ground' }, 'Living Room'),
      ];
    """)
    assert result == [
        "Not placed yet", "Not in this building",
        "Floor heating", "Floor heating, Hallway",
        "Window W7, left sash", "Door D2", "Window W6",
        "Under W6",
        "South wall",
        "1.5 m up", "Kitchen, 2.1 m up",
    ]


@needs_node
def test_a_long_room_name_is_fitted_into_its_room(tmp_path):
    result = run_plan(tmp_path, """
      return [P.fitLabel('Kitchen', 3, 3, 0.4), P.fitLabel('Bunk Room by Entrance', 2.5, 3.8, 0.4),
              P.fitLabel('Small Bathroom', 1.0, 3.8, 0.4), P.fitLabel('', 3, 3, 0.4)];
    """)
    kitchen, bunk, bath, empty = result
    assert kitchen == {"lines": ["Kitchen"], "size": 0.4, "rotate": False}
    assert len(bunk["lines"]) > 1, "wrapped rather than shrunk to nothing"
    assert bath["rotate"] is True, "a narrow tall room reads on its side"
    assert empty is None
    for fit in (bunk, bath):
        assert 0.2 <= fit["size"] <= 0.4


@needs_node
def test_the_plan_offers_every_door_and_window_on_its_floor_and_nothing_else(tmp_path):
    result = run_plan(tmp_path, DEMO_ZONES + """
      const svg = P.planSvg(model, 'ground', { zones });
      const codes = [...svg.matchAll(/data-opening="([^"]+)"/g)].map(m => m[1]).sort();
      const expected = model.walls.filter(w => w.level === 'ground')
        .flatMap(w => w.openings || []).filter(o => o.code && o.kind !== 'gap').map(o => o.code).sort();
      return { codes, expected, used: [...svg.matchAll(/class="pl-chip [^"]*is-used/g)].length,
               loft: P.planSvg(model, 'loft', { zones }).includes('data-opening="W1"') };
    """)
    assert result["codes"] == result["expected"]
    assert result["loft"] is False


@needs_node
def test_the_plan_draws_what_is_placed(tmp_path):
    result = run_plan(tmp_path, DEMO_ZONES + """
      const svg = P.planSvg(model, 'ground', { zones, selected: { kind: 'heaters', id: '160004028114' },
                                               target: 'heater' });
      return {
        floors: (svg.match(/class="pl-floorheat/g) || []).length,
        bars: (svg.match(/class="pl-heater/g) || []).length,
        selected: svg.includes('class="pl-heater is-selected" data-heater="160004028114"'),
        points: (svg.match(/class="pl-sensor/g) || []).length,
        target: svg.includes('is-placing is-target-heater'),
      };
    """)
    # The demo has four floor-heated rooms and six wall heaters on the ground floor.
    assert result["floors"] == 4
    assert result["bars"] == 6
    assert result["selected"] is True
    assert result["target"] is True


@needs_node
def test_names_on_the_plan_are_escaped(tmp_path):
    result = run_plan(tmp_path, """
      const zones = L.zonesById([{ zone_id: '5', name: '<img src=x onerror=alert(1)>' }]);
      const evil = JSON.parse(JSON.stringify(model));
      evil.walls[0].openings[0].code = 'W1';
      return P.planSvg(evil, 'ground', { zones });
    """)
    assert "<img" not in result
    assert "&lt;img" in result
