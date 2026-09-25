"""The Cabin interface for room thermometers.

Contract tests on the source, plus the pure rendering helpers run in node so
the wording a user reads is checked against the real branching.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent / "app" / "static"
CABIN = (ROOT / "ui" / "cabin" / "cabin.js").read_text(encoding="utf-8")
CLASSIC = (ROOT / "app.js").read_text(encoding="utf-8")
CORE = (ROOT / "ui" / "shared" / "core.js").read_text(encoding="utf-8")


def _function(name):
    start = CABIN.index(f"function {name}(")
    end = CABIN.index("\n  }\n", start) + len("\n  }\n")
    return CABIN[start:end]


def test_every_rule_writer_sends_every_field():
    """A contact-rule save that forgot the temperature fields would reset
    them; the server keeps omitted fields, but the client must not rely on
    that, so both writers go through one list."""
    fields = re.search(
        r"const SENSOR_POLICY_FIELDS = \[(.*?)\];", CABIN, re.S
    ).group(1)
    for field in (
        "warning_delay_seconds", "action_when_open", "action_delay_seconds",
        "override_all_modes", "temperature_max", "action_when_too_warm",
        "temperature_min", "action_when_too_cold",
    ):
        assert f"'{field}'" in fields
    assert "sensorPolicyBody(" in _function("sensorPolicyPayload")
    assert "sensorPolicyBody(" in CABIN[CABIN.index("function saveSensorSettings"):][:2000]


def test_the_choices_are_exactly_what_the_server_accepts():
    assert "const CLIMATE_WARM_ACTIONS = ['nothing', 'eco', 'away'];" in CABIN
    assert "const CLIMATE_COLD_ACTIONS = ['nothing', 'eco', 'comfort'];" in CABIN


def test_the_rules_sheet_refuses_a_gap_the_server_would_refuse():
    sheet = _function("editClimatePolicySheet")
    assert "high - low < 1" in sheet
    # A room with no heater can warn, and is told it cannot do more.
    assert "!policy.has_equipment && value !== 'nothing' ? 'disabled' : ''" in sheet


def test_nothing_thermometer_shaped_is_shown_while_the_feature_is_off():
    # The detail card and the card strip both key off sensor_summary, which the
    # server only sends while sensors are enabled.
    assert "if (!zone.sensor_summary) return '';" in _function("climateStatus")
    assert "zone.sensor_summary ? climateZoneHeadline(zone) : ''" in CABIN


def test_a_thermometer_cannot_be_retyped_into_a_contact():
    field = _function("sensorKindField")
    assert "anyKind" in field
    assert 'type="hidden"' in field


def test_the_warning_does_not_re_announce_itself():
    assert 'role="alert"' not in CABIN.replace(
        'role="alert" would be worse still and', ''
    )
    assert '<div aria-live="polite">${warning}</div>' in _function("climateStatus")


def test_classic_stays_thermometer_free():
    for word in ("climate", "temperature_max", "humidity"):
        assert word not in CLASSIC


def test_the_thermometer_icon_exists():
    assert re.search(r"^\s+thermo:\s", CORE, re.M)
    assert re.search(r"^\s+frost:\s", CORE, re.M)


def test_a_button_is_held_before_the_handler_awaits():
    """``currentTarget`` is null once an event has been dispatched, so reading
    it in a ``catch`` after an ``await`` threw — and the error toast saying
    why a save failed was never shown."""
    assert "event.currentTarget.disabled = false" not in CABIN


def _run(zone):
    lifted = "\n".join(_function(name) for name in (
        "climateOf", "fmtLimit", "climateConditionTitle",
        "climateConditionDetail", "climateRuleLine", "climateZoneHeadline",
    ))
    script = """
      const esc = (v) => String(v == null ? '' : v);
      const sensorModeWord = (m) => ({eco: 'Eco', away: 'Away', comfort: 'Comfort'})[m] || m;
      const Nobo = {
        fmtTemp: (v, d = 1) => Number(v).toFixed(d),
        icon: (n) => `<svg data-icon="${n}"/>`,
      };
      %s
      const zone = %s;
      console.log(JSON.stringify({
        rule: climateRuleLine(zone), strip: climateZoneHeadline(zone),
      }));
    """ % (lifted, json.dumps(zone))
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _zone(**climate):
    base = {
        "sensor_count": 1, "temperature": 26.0, "condition": None,
        "temperature_max": 24, "action_when_too_warm": "eco",
        "temperature_min": None, "action_when_too_cold": "nothing",
        "hysteresis": 0.5, "action_status": "idle", "block_reason": None,
        "owned_action": None,
    }
    base.update(climate)
    return {"zone_id": "1", "name": "Bathroom", "climate": base}


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")


@needs_node
def test_a_room_inside_its_limits_puts_nothing_on_the_card():
    out = _run(_zone(temperature=21.0))
    assert out == {"rule": None, "strip": ""}


@needs_node
def test_a_held_room_says_what_it_is_holding_and_until_when():
    out = _run(_zone(condition="too_warm", owned_action="eco", action_status="active"))
    assert out["rule"]["text"] == "Holding Eco until it is back below 23.5\u00b0."
    assert "Too warm" in out["strip"]
    assert "26.0\u00b0 now \u00b7 maximum 24\u00b0" in out["strip"]
    assert 'data-icon="thermo"' in out["strip"]


@needs_node
def test_a_cold_room_uses_the_frost_icon_and_the_minimum():
    out = _run(_zone(
        condition="too_cold", temperature=9.0, temperature_min=12,
        action_when_too_cold="nothing",
    ))
    assert "minimum 12\u00b0" in out["strip"]
    assert 'data-icon="frost"' in out["strip"]
    assert out["rule"]["text"].startswith("Warning only")


@needs_node
@pytest.mark.parametrize("reason, words", [
    ("contact_open", "door or window is open"),
    ("colder_mode", "already running colder than Eco"),
    ("no_equipment", "no heater in this room"),
    ("demo_sensors", "Demo sensors never change a real heater"),
])
def test_a_rule_that_stands_down_says_why(reason, words):
    out = _run(_zone(condition="too_warm", action_status="blocked", block_reason=reason))
    assert words in out["rule"]["text"]


@needs_node
def test_a_room_without_a_thermometer_has_no_climate_at_all():
    zone = _zone(condition="too_warm")
    zone["climate"]["sensor_count"] = 0
    assert _run(zone) == {"rule": None, "strip": ""}
