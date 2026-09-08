"""Static/browser checks for the maintained Cabin sensor interface."""

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent / "app" / "static"
CABIN = (ROOT / "ui" / "cabin" / "cabin.js").read_text(encoding="utf-8")
CORE = (ROOT / "ui" / "shared" / "core.js").read_text(encoding="utf-8")
CLASSIC = (ROOT / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "cabin" / "cabin.css").read_text(encoding="utf-8")


def test_sensor_api_is_centralized_in_the_shared_client():
    for path in (
        "/api/sensors/settings",
        "/api/sensors",
        "/simulate",
    ):
        assert path in CORE


def test_disabled_settings_are_not_rendered_for_an_unknown_or_ordinary_user():
    assert "if (!isAdmin || !state.me || !state.sensorSettings) return ''" in CABIN
    assert "Nothing sensor-related is shown elsewhere while this is off." in CABIN


def test_warning_is_persistent_prominent_and_accessible():
    assert 'class="sensor-warning" role="alert"' in CABIN
    assert "warning_raised" in CABIN
    assert ".zone-sensor-warning" in CSS
    assert ".sensor-warning" in CSS
    assert "sensorZoneHeadline" in CABIN
    assert "sensor-zone-strip" in CSS
    assert "sensor_count" in CABIN


def test_unknown_unavailable_and_battery_are_distinct_from_closed():
    for value in ("open", "closed", "unknown", "unavailable"):
        assert value in CABIN
        assert f".sensor-{value}" in CSS
    assert "battery" in CABIN.lower()


def test_pairing_is_a_focused_typed_sensor_sheet():
    for hook in (
        "pair-sensor",
        "pairSensorSheet",
        "pairSensorKind",
        "pairSensorName",
        "pairSensorZone",
    ):
        assert hook in CABIN
    assert "Create sensor" not in CABIN
    assert "Add simulated sensor" in CABIN
    assert "Start pairing" in CABIN
    assert "Window" in CABIN and "Door" in CABIN


def test_sensor_management_lives_on_the_zone_with_compact_actions():
    for hook in (
        "edit-sensor",
        "move-sensor",
        "replace-sensor",
        "remove-sensor",
    ):
        assert hook in CABIN
    for icon in ("rename", "move", "replace", "remove", "door", "window"):
        assert icon in CORE
    assert "'sensors need'" in CABIN
    assert "!knownZones.has(String(sensor.zone_id))" in CABIN
    assert "wireZoneSensors(root)" in CABIN


def test_zone_card_sensor_names_are_bounded():
    assert "compactSensorNames" in CABIN
    assert "+${remaining} more" in CABIN


def test_live_zone_snapshots_take_precedence_over_cached_sensor_records():
    zone_lookup = CABIN.index("return state.zones.flatMap")
    cached_lookup = CABIN.index("|| (state.sensorDevices || []).find", zone_lookup)
    assert zone_lookup < cached_lookup


def test_zone_behavior_uses_warning_action_and_separate_delay():
    for hook in (
        "data-warning-delay",
        "data-open-action",
        "data-action-delay",
        "data-save-sensor-policy",
    ):
        assert hook in CABIN
    for action in ("nothing", "away", "eco", "comfort", "schedule"):
        assert action in CABIN
    assert "10 seconds (demo test)" in CABIN
    assert "Set zone to Eco while open" not in CABIN


def test_sensor_values_are_escaped_before_entering_markup():
    assert "${esc(sensor.name)}" in CABIN
    assert "${esc(zone.name)}" in CABIN


def test_classic_remains_a_sensor_free_legacy_surface():
    assert "/api/sensors" not in CLASSIC
    assert "sensor-warning" not in CLASSIC


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
@pytest.mark.parametrize("relative", ["ui/shared/core.js", "ui/cabin/cabin.js"])
def test_browser_javascript_parses(relative):
    result = subprocess.run(
        ["node", "--check", str(ROOT / relative)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
