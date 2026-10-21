"""The Cabin Settings page is ordered by how much each topic matters to a
warm house: heating first, sensors second, weather and displays third, the
system itself last — each run under a small heading."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CABIN = (ROOT / "app/static/ui/cabin/cabin.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/static/ui/cabin/cabin.css").read_text(encoding="utf-8")

GROUPS = [
    ("Heating", [
        "settingsSection('schedules'",
        "settingsSection('frost'",
        "${renderZoneGroupsCard(isAdmin)}",
        "settingsSection('hub'",
    ]),
    ("Sensors and alerts", [
        "${renderSensorSettingsCard(isAdmin)}",
        "${renderAlarmSettingsCard(isAdmin)}",
        "settingsSection('alerts'",
    ]),
    ("Weather and displays", [
        "${renderWeatherSettingsCard(isAdmin)}",
        "${renderForecastSettingsCard(isAdmin)}",
        "${renderDisplaySettingsCard(isAdmin)}",
    ]),
    ("System", [
        "settingsSection('place'",
        "settingsSection('account'",
        "settingsSection('diagnostics'",
        "settingsSection('about'",
    ]),
]


def _template():
    start = CABIN.index("function renderSettings(me = state.me)")
    open_ = CABIN.index("$('#viewSettings').innerHTML = `", start)
    end = CABIN.index("const root = $('#viewSettings');", open_)
    return CABIN[open_:end]


def test_groups_and_sections_appear_in_priority_order():
    tpl = _template()
    expected = []
    for title, sections in GROUPS:
        expected.append(f"${{settingsGroup('{title}')}}")
        expected.extend(sections)
    positions = [tpl.index(marker) for marker in expected]
    assert positions == sorted(positions), expected
    for marker in expected:
        assert tpl.count(marker) == 1, marker


def test_schedules_and_frost_come_before_weather_and_displays():
    tpl = _template()
    assert tpl.index("settingsSection('frost'") < tpl.index(
        "renderWeatherSettingsCard(isAdmin)")
    assert tpl.index("settingsSection('schedules'") < tpl.index(
        "renderDisplaySettingsCard(isAdmin)")


def test_every_section_belongs_to_a_group():
    tpl = _template()
    first_group = tpl.index("${settingsGroup(")
    assert "settingsSection(" not in tpl[:first_group]
    assert "SettingsCard(" not in tpl[:first_group]


def test_a_heading_left_with_no_sections_is_removed_before_wiring():
    start = CABIN.index("function renderSettings(me = state.me)")
    body = CABIN[start:CABIN.index("wireSettingsSections(root);", start)]
    assert "dropEmptySettingsGroups(root);" in body
    fn = CABIN[CABIN.index("function dropEmptySettingsGroups(root)"):]
    fn = fn[:fn.index("\n  }\n")]
    assert "!next.matches('.sec')" in fn and "h.remove()" in fn


def test_group_heading_is_escaped_and_styled():
    fn = CABIN[CABIN.index("function settingsGroup(title)"):]
    assert '<h2 class="sec-group">${esc(title)}</h2>' in fn[:200]
    assert ".sec-group {" in CSS


def test_subtitle_names_the_new_order():
    assert "$('#topSub').textContent = 'Heating, sensors, weather and system';" in CABIN
