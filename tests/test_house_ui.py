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
    for path in (HOUSE_JS, LOGIC_JS):
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


def test_house_imports_only_vendored_or_own_modules():
    src = HOUSE_JS.read_text(encoding="utf-8")
    for spec in re.findall(r"^import .* from '([^']+)';", src, re.M):
        assert spec == "three" or spec.startswith("/static/ui/"), spec
        if spec != "three":
            assert (ROOT / "app" / spec.lstrip("/")).is_file(), spec


def test_house_is_only_loaded_on_demand():
    """Somebody who never turns the house view on never downloads it."""
    html = re.sub(r"<!--.*?-->", "", INDEX.read_text(encoding="utf-8"), flags=re.S)
    assert "house.js" not in html
    assert "three.module" not in re.sub(r'<script type="importmap">.*?</script>', "", html, flags=re.S)
    src = CABIN_JS.read_text(encoding="utf-8")
    assert "import('/static/ui/cabin/house.js')" in src
    assert not re.search(r"^import ", src, re.M)


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
