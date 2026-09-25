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


def _const(name):
    start = CABIN.index(f"const {name} = {{")
    end = CABIN.index("\n  };\n", start) + len("\n  };\n")
    return CABIN[start:end]


_STUBS = """
  const esc = (v) => String(v == null ? '' : v);
  const Nobo = {
    fmtTemp: (v, d = 1) => Number(v).toFixed(d),
    icon: (name) => `<i data-icon="${name}"></i>`,
    fmtTimeOfDay: (v) => {
      const d = new Date(v);
      return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    },
  };
"""


def _node(body, *functions, consts=()):
    lifted = "\n".join([_const(name) for name in consts] + [_function(name) for name in functions])
    script = _STUBS + lifted + body
    # UTC, so an hour bucket's clock time does not depend on the machine.
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
        env={"TZ": "UTC", "PATH": __import__("os").environ["PATH"]},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _history(history, humidity_max=None):
    return _node(
        "console.log(JSON.stringify(climateHistory(%s, %s)));"
        % (json.dumps(history), json.dumps(humidity_max)),
        "fmtHumidity", "climateHistory", "climateHistoryChart",
    )


DAY = 1_800_000_000 - (1_800_000_000 % 86400)   # a midnight, UTC


def _day(temperatures, humidities):
    """24 hourly rows from midnight: each temperature is the hour's middle."""
    rows = [
        {"start": DAY + hour * 3600,
         "t_min": t - 0.3 if t is not None else None, "t_max": t + 0.2 if t is not None else None,
         "h_min": h - 2 if h is not None else None, "h_max": h}
        for hour, (t, h) in enumerate(zip(temperatures, humidities))
    ]
    t_values = [row for row in rows if row["t_min"] is not None]
    h_values = [row["h_max"] for row in rows if row["h_max"] is not None]
    return {
        "window_start": DAY,
        "hours": rows,
        "temperature_min": min(row["t_min"] for row in t_values) if t_values else None,
        "temperature_max": max(row["t_max"] for row in t_values) if t_values else None,
        "humidity_min": min(row["h_min"] for row in rows if row["h_min"] is not None) if h_values else None,
        "humidity_max": max(h_values) if h_values else None,
    }


def _bathroom():
    temperatures = [21.0] * 24
    temperatures[4] = 18.5          # the cold of the night
    temperatures[17] = 22.9         # a warm evening
    humidities = [50] * 24
    humidities[7], humidities[8], humidities[9] = 86, 74, 60   # a shower
    humidities[21] = 79
    return _day(temperatures, humidities)


@needs_node
def test_the_last_24_hours_say_coldest_warmest_and_dampest():
    out = _history(_bathroom(), 70)
    assert "Last 24 hours" in out
    assert "Coldest <b>18.2\u00b0C</b> around 04:00" in out
    assert "How cold it got overnight." in out
    assert "Warmest <b>23.1\u00b0C</b> around 17:00" in out
    assert "Dampest <b>86\u00a0%</b> around 07:00, back under 70\u00a0% by 09:00" in out
    assert "Above the 70\u00a0% damp-air limit in 3 of the last 24 hours." in out
    assert "Still above it now" not in out


@needs_node
def test_the_chart_has_bars_a_humidity_line_the_limit_and_a_legend():
    out = _history(_bathroom(), 70)
    assert out.count("<rect") == 24
    assert "climate-chart-humidity" in out
    assert "climate-chart-limit" in out
    assert "Damp limit 70\u00a0%" in out
    assert "Temperature 17\u201324\u00b0C" in out
    # Ticks at 06, 12 and 18 o'clock; midnight is at the very edge and left out.
    assert "06:00" in out and "12:00" in out and "18:00" in out
    assert "left:25.0%" in out
    assert 'role="img"' in out


@needs_node
def test_without_a_humidity_limit_there_is_advice_and_no_limit_line():
    out = _history(_bathroom(), None)
    assert "Dampest <b>86\u00a0%</b> around 07:00</span>" not in out  # no "back under"
    assert "back under" not in out
    assert "damp that lasts for hours is worth airing out" in out
    assert "climate-chart-limit" not in out
    assert "Damp limit" not in out


@needs_node
def test_a_room_still_damp_now_says_so():
    history = _bathroom()
    history["hours"][-1]["h_max"] = 88
    history["humidity_max"] = 88
    out = _history(history, 70)
    assert "Dampest <b>88\u00a0%</b> around 23:00" in out
    assert "back under" not in out
    assert "Still above it now." in out


@needs_node
def test_a_room_that_stayed_dry_says_so():
    out = _history(_day([21.0] * 23 + [21.5], [50] * 24), 70)
    assert "Stayed under the 70\u00a0% damp-air limit." in out


@needs_node
def test_a_steady_room_is_held_not_coldest_and_warmest():
    history = _day([21.0] * 3 + [None] * 21, [None] * 24)
    history["hours"] = history["hours"][:3]
    history["temperature_min"] = history["temperature_max"] = 21.0
    out = _history(history)
    assert "Held at <b>21.0\u00b0C</b>" in out
    assert "No change in the last 3 hours." in out
    assert "Coldest" not in out and "Dampest" not in out


@needs_node
def test_a_daytime_low_is_not_called_overnight():
    temperatures = [21.0] * 24
    temperatures[13] = 17.0
    out = _history(_day(temperatures, [None] * 24))
    assert "around 13:00" in out
    assert "The coldest it got." in out
    assert "overnight" not in out


@needs_node
def test_one_hour_of_history_has_words_but_no_chart():
    start = DAY
    out = _history({
        "window_start": start, "temperature_min": 21.0, "temperature_max": 21.0,
        "humidity_min": None, "humidity_max": None,
        "hours": [{"start": start, "t_min": 21.0, "t_max": 21.0, "h_min": None, "h_max": None}],
    })
    assert "Held at <b>21.0\u00b0C</b>" in out
    assert "No change in the last hour." in out
    assert "humidity" not in out.lower()
    assert "<svg" not in out


@needs_node
def test_no_history_draws_nothing():
    assert _history(None) == ""


# -- the weather outlook ------------------------------------------------------


def _outlook(climate):
    return _node(
        "console.log(JSON.stringify(pressureOutlook(%s)));" % json.dumps(climate),
        "pressureChangeText", "pressureOutlook", "pressureChart",
        consts=("PRESSURE_OUTLOOK",),
    )


def test_the_card_shows_the_outlook_only_where_there_is_a_barometer():
    card = _function("climateStatus")
    assert "climate.measures_pressure" in card
    assert "? pressureOutlook(climate)" in card
    assert ": climateHistory(climate.last_24h, climate.humidity_max)" in card


def test_there_is_one_message_for_each_tendency_the_server_sends():
    from pressure_outlook import Tendency

    table = _const("PRESSURE_OUTLOOK")
    keys = re.findall(r"^    (\w+): \{", table, re.M)
    assert keys == [item.value for item in Tendency]
    for icon in re.findall(r"icon: '([\w-]+)'", table):
        assert f"'{icon}':" in CORE, icon


@needs_node
@pytest.mark.parametrize("tendency, change, title, words, tone", [
    ("storm", -7.2, "Storm possible", "Secure anything loose outside.", "storm"),
    ("falling_fast", -4.4, "Rain and wind likely soon", "within 12 hours", "worse"),
    ("falling", -2.0, "Weather turning", "perhaps rain", "worse"),
    ("steady", 0.4, "No big change expected", "stay much as it is now", "steady"),
    ("rising", 2.5, "Improving", "Drier, brighter weather is likely.", "better"),
    ("rising_fast", 4.8, "Clearing, but gusty", "strong gusts", "better"),
])
def test_every_outlook_says_what_it_means(tendency, change, title, words, tone):
    out = _outlook({
        "measures_pressure": True,
        "pressure_outlook": {"tendency": tendency, "change_3h": change, "rooms": 1, "ready_at": None},
        "pressure_24h": [[1_800_000_000 - 7200, 1010.0], [1_800_000_000, 1008.0]],
    })
    assert f"<strong>{title}</strong>" in out
    assert words in out
    assert f'class="baro is-{tone}"' in out
    assert "Weather outlook" in out
    # A guide, and it says so.
    assert "not a forecast" in out
    size = f"{abs(change):.1f}\u00a0hPa in 3 hours"
    assert (("Down " if change < 0 else "Up ") + size) in out


@needs_node
def test_the_outlook_is_learning_until_three_hours_are_in():
    out = _outlook({
        "measures_pressure": True,
        "pressure_outlook": {"tendency": None, "change_3h": None, "rooms": 0,
                             "ready_at": "2027-01-15T15:00:00+00:00"},
        "pressure_24h": [[1_800_000_000, 1010.0]],
    })
    assert "Learning the weather" in out
    assert "three hours of air pressure readings" in out
    assert "Ready around 15:00." in out
    # One point is not a line.
    assert "baro-chart" not in out


@needs_node
def test_a_barometer_gone_quiet_has_no_outlook_and_says_why():
    out = _outlook({"measures_pressure": True, "pressure_outlook": None, "pressure_24h": []})
    assert "No outlook just now" in out
    assert "No recent air pressure reading" in out


@needs_node
def test_no_change_is_written_as_no_change():
    out = _node(
        "console.log(JSON.stringify([pressureChangeText(0), pressureChangeText(-0.1),"
        " pressureChangeText(null)]));",
        "pressureChangeText",
    )
    assert out == ["No change in 3 hours", "Down 0.1\u00a0hPa in 3 hours", ""]


def _front_page(zones):
    return _node("""
      const el = { hidden: true, innerHTML: '', className: '', attrs: {},
                   setAttribute(k, v) { this.attrs[k] = v; } };
      const $ = () => el;
      const opened = [];
      const showZone = (id) => opened.push(id);
      const state = { zones: %s };
      function climateOf(zone) {
        const climate = zone && zone.climate;
        return climate && climate.sensor_count ? climate : null;
      }
      renderWeather();
      if (el.onclick) el.onclick();
      console.log(JSON.stringify({ hidden: el.hidden, html: el.innerHTML,
                                   cls: el.className, opened }));
    """ % json.dumps(zones), "pressureChangeText", "houseOutlook", "renderWeather",
        consts=("PRESSURE_OUTLOOK",))


def _barometer_zone(tendency, change=-4.4, zone_id="4", summary=True):
    return {
        "zone_id": zone_id, "name": "Living Room",
        "sensor_summary": {} if summary else None,
        "climate": {"sensor_count": 1, "measures_pressure": True,
                    "pressure_outlook": {"tendency": tendency, "change_3h": change,
                                         "rooms": 1, "ready_at": None}},
    }


@needs_node
def test_the_front_page_mentions_the_weather_only_when_it_is_changing():
    out = _front_page([{"zone_id": "1", "name": "Hall", "sensor_summary": {},
                        "climate": {"sensor_count": 1, "measures_pressure": False}},
                       _barometer_zone("falling_fast")])
    assert out["hidden"] is False
    assert "Rain and wind likely soon" in out["html"]
    assert "Down 4.4\u00a0hPa in 3 hours" in out["html"]
    assert out["cls"] == "weather-chip is-worse"
    # It opens the room the barometer is in.
    assert out["opened"] == ["4"]

    steady = _front_page([_barometer_zone("steady", 0.3)])
    assert steady["hidden"] is True and steady["html"] == ""


@needs_node
def test_the_front_page_says_nothing_while_learning_or_with_sensors_off():
    assert _front_page([_barometer_zone(None)])["hidden"] is True
    assert _front_page([_barometer_zone("storm", -8, summary=False)])["hidden"] is True
    assert _front_page([])["hidden"] is True


def test_the_front_page_has_a_hidden_place_for_the_outlook():
    html = (ROOT / "ui" / "cabin" / "index.html").read_text(encoding="utf-8")
    assert '<button class="weather-chip" id="weatherChip" type="button" hidden></button>' in html
    assert "renderWeather();" in _function("renderHome")


def test_a_demo_thermometer_can_be_made_to_have_no_barometer():
    sheet = _function("editSensorSheet")
    assert "clear_pressure: root.querySelector('#editSensorPressure').value === ''" in sheet
