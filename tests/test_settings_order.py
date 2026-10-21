"""The Cabin Settings page is ordered by how much each topic matters to a
warm house: My Home on its own at the top, then heating, sensors, weather and
displays, and the system itself last — each run under a small heading. The
order within each group is the owner's own."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CABIN = (ROOT / "app/static/ui/cabin/cabin.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/static/ui/cabin/cabin.css").read_text(encoding="utf-8")

UNGROUPED = "settingsSection('place'"

GROUPS = [
    ("Heating", [
        "settingsSection('hub'",
        "${renderZoneGroupsCard(isAdmin)}",
        "settingsSection('schedules'",
        "settingsSection('frost'",
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
    expected = [UNGROUPED]
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


def test_only_my_home_stands_before_the_first_group():
    tpl = _template()
    head = tpl[:tpl.index("${settingsGroup(")]
    assert head.count("settingsSection(") == 1
    assert UNGROUPED in head
    assert "SettingsCard(" not in head


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
