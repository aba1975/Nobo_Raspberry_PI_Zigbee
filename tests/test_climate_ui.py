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
        "humidity_max", "humidity_delay_seconds", "frost_warning",
    ):
        assert f"'{field}'" in fields
    assert "sensorPolicyBody(" in _function("sensorPolicyPayload")
    assert "sensorPolicyBody(" in CABIN[CABIN.index("function saveSensorSettings"):][:2000]


def test_every_field_sent_is_one_the_policy_reader_keeps():
    """sensorPolicyFor normalises the server's policy into a fresh object, so
    a field missing from it is silently undefined everywhere — the rules
    sheet showed a saved humidity limit as "No maximum" for exactly that
    reason."""
    fields = re.findall(r"'(\w+)'", re.search(
        r"const SENSOR_POLICY_FIELDS = \[(.*?)\];", CABIN, re.S
    ).group(1))
    reader = _function("sensorPolicyFor")
    for field in fields:
        assert f"{field}:" in reader, field


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
    assert "zone.sensor_summary ? climateStrips(zone) : []" in CABIN


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
        "climateOf", "fmtLimit", "fmtHumidity", "climateConditionTitle",
        "climateConditionDetail", "climateRuleLine", "climateAlerts",
        "climateZoneHeadline", "climateStrips",
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
    assert "Actual 26.0\u00b0 \u00b7 maximum 24\u00b0" in out["strip"]
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


# -- humidity, frost, the last 24 hours --------------------------------------


@needs_node
def test_a_room_near_freezing_says_so_on_the_card():
    out = _run(_zone(temperature=3.2, temperature_max=None, frost=True,
                     frost_temperature=5.0))
    assert "Near freezing" in out["strip"]
    assert "Actual 3.2\u00b0 \u00b7 warns below 5\u00b0" in out["strip"]
    assert 'data-icon="frost"' in out["strip"]


@needs_node
def test_frost_stands_in_for_too_cold_rather_than_repeating_it():
    out = _run(_zone(temperature=3.0, condition="too_cold", temperature_min=10,
                     frost=True, frost_temperature=5.0))
    assert "Near freezing" in out["strip"]
    assert "Too cold" not in out["strip"]


@needs_node
def test_damp_air_is_its_own_strip_beside_a_temperature_one():
    out = _run(_zone(condition="too_warm", humidity=82.4, humidity_max=70,
                     humidity_raised=True))
    assert "Too warm" in out["strip"]
    assert "Damp air" in out["strip"]
    assert "82\u00a0%" in out["strip"] and "maximum 70\u00a0%" in out["strip"]
    assert 'data-icon="drop"' in out["strip"]


def test_the_drop_icon_exists():
    assert re.search(r"^\s+drop:\s", CORE, re.M)


def test_the_zone_page_labels_the_measurement_actual():
    detail = _function("renderZoneDetail")
    assert "measuring ' + Nobo.fmtTemp" not in detail
    assert "'Running now'" not in detail
    assert "<span class=\"set-label\">Actual</span>" in detail
    assert "headLabel = monitoringOnly ? 'Actual' : remote ? 'Set to' : 'Running'" in detail
    assert "fact('Actual temperature'" in _function("climateStatus")


def test_frost_and_damp_air_mark_the_card_for_attention():
    check = _function("zoneNeedsSensorAttention")
    assert "climate.humidity_raised" in check
    assert "climate.frost" in check
    assert "if (!zone.sensor_summary) return false;" in check


def test_the_rules_sheet_edits_humidity_and_frost():
    sheet = _function("editClimatePolicySheet")
    for control in ("#cpHumid", "#cpHumidDelay", "#cpFrost"):
        assert control in sheet
    assert "humidity_max: humid.value === '' ? null : Number(humid.value)" in sheet
    assert "frost_warning: frost.checked" in sheet
    # The server refuses under 40 %; the list starts well above it.
    assert "[50, 55, 60, 65, 70, 75, 80, 85, 90]" in sheet


def test_the_card_carries_placement_advice():
    assert "${CLIMATE_PLACEMENT_TIP}" in _function("climateStatus")
    assert "1.5 m above the floor" in CABIN


def _history(history):
    lifted = "\n".join(_function(name) for name in (
        "fmtHumidity", "climateHistory", "climateHistoryChart",
    ))
    script = """
      const esc = (v) => String(v == null ? '' : v);
      const Nobo = { fmtTemp: (v, d = 1) => Number(v).toFixed(d) };
      %s
      console.log(JSON.stringify(climateHistory(%s)));
    """ % (lifted, json.dumps(history))
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
def test_the_last_24_hours_show_lowest_and_highest_with_a_bar_per_hour():
    start = 1_800_000_000 - (1_800_000_000 % 3600)
    out = _history({
        "window_start": start,
        "temperature_min": 18.0, "temperature_max": 22.5,
        "humidity_min": 40.0, "humidity_max": 61.0,
        "hours": [
            {"start": start, "t_min": 18.0, "t_max": 19.0, "h_min": 40.0, "h_max": 45.0},
            {"start": start + 3600 * 5, "t_min": 20.0, "t_max": 22.5, "h_min": 50.0, "h_max": 61.0},
        ],
    })
    assert "Last 24 hours" in out
    assert "actual 18.0\u00b0C \u2013 22.5\u00b0C" in out
    assert "humidity 40\u00a0% \u2013 61\u00a0%" in out
    assert out.count("<rect") == 2
    assert 'role="img"' in out


@needs_node
def test_one_hour_of_history_has_words_but_no_chart():
    start = 1_800_000_000 - (1_800_000_000 % 3600)
    out = _history({
        "window_start": start, "temperature_min": 21.0, "temperature_max": 21.0,
        "humidity_min": None, "humidity_max": None,
        "hours": [{"start": start, "t_min": 21.0, "t_max": 21.0, "h_min": None, "h_max": None}],
    })
    assert "actual 21.0\u00b0C" in out
    assert "humidity" not in out
    assert "<svg" not in out


@needs_node
def test_no_history_draws_nothing():
    assert _history(None) == ""
