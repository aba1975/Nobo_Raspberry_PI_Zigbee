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


def test_unknown_unavailable_and_battery_are_distinct_from_closed():
    for value in ("open", "closed", "unknown", "unavailable"):
        assert value in CABIN
        assert f".sensor-{value}" in CSS
    assert "battery" in CABIN.lower()


def test_admin_workflow_has_every_requested_demo_operation():
    for hook in (
        "pair-sensor",
        "save-sensor",
        "remove-sensor",
        "sensor-state-input",
        "sensor-available-input",
        "sensor-battery-input",
        "save-sensor-policies",
    ):
        assert hook in CABIN
    assert "10 seconds (demo test)" in CABIN


def test_sensor_values_are_escaped_before_entering_markup():
    assert "${esc(sensor.name)}" in CABIN
    assert "${esc(policy.name)}" in CABIN


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

