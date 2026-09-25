"""The zone page on a phone: one line per heater or sensor, actions behind ⋯.

At 375 px a row's four action icons and three status facts used to take the
width first and squeeze the name to a letter or two per line. On a phone each
row is now picture, name and status, with ⋯ opening the same four actions in a
sheet. Desktop keeps its icons: both are rendered and the stylesheet picks.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "app" / "static" / "ui" / "cabin"
CABIN = (ROOT / "cabin.js").read_text(encoding="utf-8")
CSS = (ROOT / "cabin.css").read_text(encoding="utf-8")

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")


def _function(name):
    start = CABIN.index(f"function {name}(")
    end = CABIN.index("\n  }\n", start) + len("\n  }\n")
    return CABIN[start:end]


def _phone_css():
    start = CSS.index("/* ------------------------------------------------ zone page on a phone")
    return CSS[start:]


def _phone_media():
    css = _phone_css()
    return css[css.index("@media (max-width: 560px)"):]


def _rule(css, selector):
    match = re.search(r"(?m)^\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, selector
    return match.group(1)


def _node(script):
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# -- both layouts are rendered, and the stylesheet picks one ---------------


def test_heater_rows_keep_their_icons_and_add_a_menu_button():
    row = _function("devRow")
    for attr in ("data-rename-device", "data-move-device", "data-replace-device",
                 "data-remove-device"):
        assert attr in row
    assert 'moreButton(`data-device-menu="${esc(d.serial)}"`, name)' in row
    assert 'class="dev-brief phone-only"' in row


def test_only_an_admin_gets_a_sensor_menu():
    row = _function("sensorRow")
    admin_block = row[row.index("${admin ? `<span class=\"dev-actions sensor-actions\">"):]
    assert "data-sensor-menu" in admin_block
    assert row.count("data-sensor-menu") == 1


def test_outside_a_phone_the_menu_and_header_add_are_hidden():
    base = _phone_css()[:_phone_css().index("@media (max-width: 560px)")]
    hidden = _rule(base, ".phone-only, .more-btn, .card-add, .fact-short")
    assert "display: none" in hidden
    # Battery and signal still sit in the facts as before on a wide screen.
    assert "display: contents" in _rule(base, ".sensor-health")


def test_on_a_phone_the_icons_give_way_to_the_menu():
    media = _phone_media()
    assert "display: inline-flex" in _rule(media, ".more-btn, .card-add")
    assert "display: none" in _rule(media, ".dev .dev-meta, .dev .dev-tags, .dev .dev-actions")
    assert "display: none" in _rule(media, ".sensor-row .sensor-actions")
    assert "display: none" in _rule(media, ".card-add-row")
    # The name can no longer be squeezed: it ellipsises instead of wrapping.
    assert "text-overflow: ellipsis" in _rule(media, ".dev-name")
    assert "text-overflow: ellipsis" in _rule(media, ".sensor-row .sensor-copy strong")


def test_a_quiet_or_offline_sensor_keeps_its_line_on_a_phone():
    row = _function("sensorRow")
    assert "!sensor.available || quiet ? 'is-alert' : ''" in row
    media = _phone_media()
    assert "display: none" in _rule(media, ".sensor-row .sensor-copy small:not(.is-alert)")


def test_the_phone_readings_are_one_strip_with_a_short_label():
    status = _function("climateStatus")
    assert "fact('Actual temperature'" in status
    assert "'Actual', 'is-actual')" in status
    assert '<span class="fact-unit">\\u00A0hPa</span>' in status
    media = _phone_media()
    strip = _rule(media, ".climate-facts")
    assert "grid-auto-flow: column" in strip
    assert "display: none" in _rule(media, ".fact-long")
    # The desktop label rule must not reach the unit span inside the value.
    assert ".climate-fact > span { font-size: .76rem;" in CSS
    assert ".climate-fact span {" not in CSS


# -- the menu runs exactly what the icons run ------------------------------


def test_the_heater_menu_offers_the_four_heater_actions():
    menu = _function("deviceMenuSheet")
    for call in ("renameDevice(serial)", "moveDevice(serial)", "replaceDevice(serial)",
                 "removeDevice(serial)"):
        assert call in menu


def test_the_sensor_menu_offers_the_four_sensor_actions():
    menu = _function("sensorMenuSheet")
    for call in ("editSensorSheet(sensorId)", "moveSensorSheet(sensorId)",
                 "pairSensorSheet('', configuredSensor(sensorId))", "removeSensor(sensorId)"):
        assert call in menu
    # The sheet shows what the phone row left out, without offering a second menu.
    assert "sensorRow(sensor, false)" in menu


def test_menus_and_header_add_buttons_are_wired():
    detail = _function("renderZoneDetail")
    assert "root.querySelectorAll('[data-device-menu]')" in detail
    assert "'[data-act=\"add-device\"], [data-add-device]'" in detail
    wire = _function("wireZoneSensors")
    assert "root.querySelectorAll('[data-sensor-menu]')" in wire
    assert "querySelectorAll('[data-add-sensor]')" in wire


@needs_node
def test_choosing_an_action_closes_the_menu_first_then_runs_it():
    script = """
      const calls = [];
      const esc = v => String(v == null ? '' : v);
      const Nobo = { icon: n => `<svg data-icon="${n}"/>` };
      let sheet = null;
      function openSheet(title, html, wire) { sheet = { title, html }; wire(body); }
      function closeSheet() { calls.push('close'); }
      let made = null;
      const body = { querySelectorAll: () => made || (made = [...sheet.html.matchAll(/data-menu-act="(\\d+)"/g)]
        .map(m => ({ dataset: { menuAct: m[1] } }))) };
      %s
      const actions = ['a', 'b', 'c', 'd'].map(k => ({ icon: 'rename', cls: '', label: k,
        run: () => calls.push(k) }));
      actionMenuSheet('Heater', '<div></div>', actions);
      const buttons = body.querySelectorAll();
      buttons[2].onclick();
      console.log(JSON.stringify({ title: sheet.title, count: buttons.length, calls }));
    """ % _function("actionMenuSheet")
    out = _node(script)
    assert out == {"title": "Heater", "count": 4, "calls": ["close", "c"]}


@needs_node
def test_a_heater_row_renders_a_brief_line_and_a_labelled_menu_button():
    script = """
      const esc = v => String(v == null ? '' : v).replace(/"/g, '&quot;');
      const Nobo = { icon: n => `<svg data-icon="${n}"/>`, MODES: {},
        isManualDevice: d => d.supports_temp_adjust === false,
        deviceImg: () => '<img>' };
      const MORE_ICON = '<svg/>';
      %s
      %s
      console.log(JSON.stringify(devRow({ serial: '160004028115', display_name: 'Living Room Heater',
        device_type: 'R80 RDC 700', supports_temp_adjust: false })));
    """ % (_function("moreButton"), _function("devRow"))
    html = _node(script)
    assert 'data-device-menu="160004028115"' in html
    assert 'aria-label="Manage Living Room Heater"' in html
    assert "R80 RDC 700 &middot; dial on heater" in html
