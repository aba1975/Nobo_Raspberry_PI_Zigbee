"""The weather station in the Cabin interface.

Contract tests in the style of the alarm ones: every call goes through the
shared client, nothing appears while the integration is off, the Netatmo
secret is handled as a password, and the wording that ends up on the front
page is exercised by running the real functions in node.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "app" / "static"
CABIN = (ROOT / "ui" / "cabin" / "cabin.js").read_text(encoding="utf-8")
CABIN_HTML = (ROOT / "ui" / "cabin" / "index.html").read_text(encoding="utf-8")
CABIN_CSS = (ROOT / "ui" / "cabin" / "cabin.css").read_text(encoding="utf-8")
CORE = (ROOT / "ui" / "shared" / "core.js").read_text(encoding="utf-8")
CLASSIC = (ROOT / "app.js").read_text(encoding="utf-8")

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")


def _lift(*markers):
    """The named top-level functions and constants of cabin.js, verbatim."""
    out = []
    for marker in markers:
        start = CABIN.index(marker)
        first_line = CABIN[start:CABIN.index("\n", start) + 1]
        if marker.startswith("const ") and first_line.rstrip().endswith(";"):
            out.append(first_line)
            continue
        closing = "\n  };\n" if marker.startswith("const ") else "\n  }\n"
        end = CABIN.index(closing, start) + len(closing)
        out.append(CABIN[start:end])
    return "\n".join(out)


def _node(script):
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _function(name):
    body = CABIN[CABIN.index(f"function {name}("):]
    return body[:body.index("\n  }\n")]


# -- static contract ------------------------------------------------------


def test_every_weather_call_goes_through_the_shared_api_client():
    for path in ("/api/weather/settings", "/api/weather/netatmo/app",
                 "/api/weather/netatmo/connect", "/api/weather/netatmo/token",
                 "/api/weather/netatmo/disconnect", "/api/weather/simulate",
                 "/api/sensors/weather"):
        assert path in CORE, path
    assert "fetch('/api/weather" not in CABIN
    assert "fetch('/api/sensors/weather" not in CABIN


def test_the_settings_are_for_administrators_only():
    assert "if (!isAdmin || !state.me || !state.weatherSettings) return '';" in CABIN
    admin_block = CABIN[CABIN.index("if (state.me && state.me.role === 'admin') {"):]
    assert "Nobo.api.weatherSettings()" in admin_block[:600]


def test_the_section_sits_with_the_other_integrations():
    settings = CABIN[CABIN.index("function renderSettings("):]
    assert settings.index("renderAlarmSettingsCard(isAdmin)") < settings.index(
        "renderWeatherSettingsCard(isAdmin)") < settings.index("settingsSection('schedules'")
    assert "wireWeatherSettings(root);" in settings


def test_the_secret_and_token_are_password_fields_and_cleared_after_sending():
    account = _function("weatherAccountBlock")
    assert 'id="wxClientSecret" type="password" autocomplete="off"' in account
    assert 'id="wxRefreshToken" type="password" autocomplete="off"' in account
    wire = _function("wireWeatherSettings")
    assert "secret.value = '';" in wire
    assert "input.value = '';" in wire
    assert "localStorage" not in wire and "state.secret" not in wire


def test_setting_up_netatmo_is_disabled_off_https():
    account = _function("weatherAccountBlock")
    assert "Setting up Netatmo needs HTTPS" in account
    assert account.count("${secure ? '' : 'disabled'}") == 3


def test_connecting_leaves_for_netatmo_and_the_return_is_announced_once():
    wire = _function("wireWeatherSettings")
    assert "window.location.assign(started.authorize_url)" in wire
    announce = _function("announceWeatherReturn")
    assert "params.get('weather')" in announce
    assert "history.replaceState" in announce
    boot = CABIN[CABIN.index("(async function boot()"):]
    assert "announceWeatherReturn();" in boot[:400]


def test_a_station_change_is_picked_up_without_waiting_for_the_poll():
    boot = CABIN[CABIN.index("(async function boot()"):]
    assert "if (state.status && (state.status.alarm || state.status.weather)) {" in boot[:1500]
    assert "renderTrip(); renderWeather(); renderSystem();" in CABIN


def test_the_top_card_has_a_place_for_the_weather_and_it_starts_hidden():
    assert '<div class="trip-head">' in CABIN_HTML
    assert re.search(r'<button class="trip-weather" id="tripWeather" type="button" hidden>',
                     CABIN_HTML)
    assert ".trip-weather[hidden] { display: none; }" in CABIN_CSS


def test_the_front_page_shows_nothing_while_it_is_off():
    trip = _function("renderTripWeather")
    assert "el.hidden = !weather;" in trip
    station = _function("stationWeather")
    assert "weather && weather.enabled ? weather : null" in station


def test_the_station_never_offers_a_heating_control():
    for name in ("weatherSheet", "renderWeatherSettingsCard", "weatherModuleRow"):
        body = _function(name)
        for word in ("setZoneMode", "setGlobalMode", "override", "setTemperature"):
            assert word not in body, (name, word)


def test_a_borrowed_module_cannot_be_replaced_or_paired():
    assert "function isWeatherSensor(" in CABIN
    assert "function isBorrowedSensor(" in CABIN


def test_classic_is_left_alone():
    """Classic is the legacy interface. It gains nothing it would have to explain."""
    assert "/api/weather" not in CLASSIC
    assert "netatmo" not in CLASSIC.lower()


def test_every_field_in_the_section_is_styled_alike():
    rule = CABIN_CSS[CABIN_CSS.index('.field input[type="text"]'):]
    rule = rule[:rule.index("{")]
    for kind in ("text", "password", "number"):
        assert f'.field input[type="{kind}"]' in rule, kind


# -- the settings card, rendered -----------------------------------------


def _settings_card(settings, protocol="https:"):
    script = """
      const esc = (v) => String(v == null ? '' : v)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
      const Nobo = {
        icon: (n) => `<svg data-icon="${n}"/>`,
        fmtTemp: (v) => Number(v).toFixed(1),
        fmtTimeOfDay: () => '10:00',
        fmtDayMonth: () => '1 Oct',
      };
      const window = { location: { protocol: %s, hostname: 'nobo.example.no' } };
      const settingsOpenMap = () => ({});
      const fmtLimit = (v) => String(v).replace('-', '\\u2212');
      const state = { me: { role: 'admin' }, weatherSettings: %s };
      %s
      console.log(renderWeatherSettingsCard(true));
    """ % (json.dumps(protocol), json.dumps(settings), _lift(
        "function alarmTransportSecure", "function outdoorReading",
        "const WEATHER_PROVIDER_LABELS", "function weatherAccountBlock",
        "function weatherStatusBlock", "function weatherDemoControls",
        "function renderWeatherSettingsCard",
        "function settingsSection", "function segControl", "function settingRow",
    ))
    return _node(script)


BASE = {
    "enabled": True, "provider": "netatmo", "outdoor_cold_below": -15,
    "providers": ["netatmo"], "cold_limits": [-40, 10], "sensors_enabled": True,
    "netatmo": {"app_configured": False, "client_id": None, "connected": False,
                "connected_at": None,
                "redirect_uri": "https://nobo.example.no/api/weather/netatmo/callback"},
    "simulated": None,
    "status": {"enabled": True, "provider": "netatmo", "connection": "not_configured",
               "message": "Connect to Netatmo in Settings.", "modules": []},
}

OK_STATUS = {
    "enabled": True, "provider": "netatmo", "connection": "ok", "station_name": "Mostugu",
    "read_at": "2026-10-01T08:00:00+00:00",
    "outdoor": {"temperature": 3.2, "fresh": True, "cold": False},
    "modules": [{"module_id": "a"}, {"module_id": "b"}],
}


@needs_node
def test_off_shows_only_the_switch():
    markup = _settings_card({**BASE, "enabled": False, "status": None})
    assert "Nothing about a weather station is shown elsewhere while this is off." in markup
    assert "wxClientId" not in markup
    assert "wxColdBelow" not in markup
    assert "<b>Off</b>" in markup


@needs_node
def test_without_an_app_it_explains_the_redirect_uri_and_asks_for_both():
    markup = _settings_card(BASE)
    assert "https://nobo.example.no/api/weather/netatmo/callback" in markup
    assert 'id="wxClientId"' in markup and 'id="wxClientSecret"' in markup
    assert 'data-act="wx-connect"' not in markup
    assert "Setting up Netatmo needs HTTPS" not in markup


@needs_node
def test_over_http_the_app_cannot_be_saved():
    markup = _settings_card(BASE, protocol="http:")
    assert "Setting up Netatmo needs HTTPS" in markup
    button = markup[markup.index('data-act="wx-app"'):]
    assert "disabled" in button[:button.index(">")]


@needs_node
def test_a_saved_app_offers_to_connect_and_is_flagged_until_it_is():
    markup = _settings_card({**BASE, "netatmo": {
        **BASE["netatmo"], "app_configured": True, "client_id": "abc\u2026xyz"}})
    assert 'data-act="wx-connect"' in markup
    assert 'id="wxRefreshToken"' in markup
    assert "abc\u2026xyz" in markup
    assert "not connected" in markup
    assert "is-alert" in markup
    # The secret is never sent back, so it can never be shown.
    assert "client_secret" not in markup


@needs_node
def test_connected_shows_the_station_and_a_way_out():
    markup = _settings_card({**BASE, "netatmo": {
        **BASE["netatmo"], "app_configured": True, "client_id": "abc\u2026xyz",
        "connected": True, "connected_at": 1790000000}, "status": OK_STATUS})
    assert "Connected to Netatmo" in markup
    assert 'data-act="wx-disconnect"' in markup
    assert 'data-act="wx-connect"' not in markup
    assert "Mostugu" in markup and "3.2\u00B0 outside" in markup
    assert "2 modules" in markup
    assert "is-alert" not in markup


@needs_node
def test_a_station_that_cannot_be_read_says_why():
    markup = _settings_card({**BASE, "netatmo": {
        **BASE["netatmo"], "app_configured": True, "connected": True},
        "status": {**OK_STATUS, "connection": "signed_out",
                   "message": "Netatmo ended the connection. Connect again in Settings."}})
    assert "Netatmo ended the connection." in markup
    assert "note-warn" in markup


@needs_node
def test_the_demo_station_has_its_own_panel():
    markup = _settings_card({**BASE, "provider": "simulated",
                             "providers": ["simulated", "netatmo"],
                             "simulated": [
                                 {"module_id": "base", "kind": "base", "name": "Living Room",
                                  "temperature": 21.4, "pressure": 1013.2, "reachable": True},
                                 {"module_id": "out", "kind": "outdoor", "name": "Outdoor",
                                  "temperature": 3.2, "battery": 71, "reachable": False}],
                             "status": {**OK_STATUS, "provider": "simulated"}})
    assert 'data-act="wx-simulate"' in markup
    assert 'data-wx-module="base"' in markup and 'data-wx-module="out"' in markup
    base = markup[markup.index('data-wx-module="base"'):markup.index('data-wx-module="out"')]
    assert 'data-wx-field="pressure"' in base and 'data-wx-field="battery"' not in base
    outdoor = markup[markup.index('data-wx-module="out"'):]
    assert 'data-wx-field="battery"' in outdoor
    reachable = outdoor[outdoor.index('data-wx-field="reachable"'):]
    assert "checked" not in reachable[:reachable.index(">")]
    assert "wxClientId" not in markup
    assert 'data-seg="wx-source"' in markup


@needs_node
def test_the_cold_limit_keeps_an_unusual_saved_value():
    markup = _settings_card({**BASE, "outdoor_cold_below": -12})
    selected = re.findall(r'<option value="(-?\d+)"\s+selected', markup)
    assert selected == ["-12"]
    values = [int(v) for v in re.findall(r'<option value="(-?\d+)"', markup)]
    assert values == sorted(values) and values[0] == -40 and values[-1] == 10


@needs_node
def test_room_thermometers_explain_that_they_need_sensors():
    markup = _settings_card({**BASE, "sensors_enabled": False})
    assert "turn on\n      door, window and temperature sensors" in markup


# -- the front page corner, rendered --------------------------------------


def _trip_weather(weather):
    script = """
      const esc = (v) => String(v == null ? '' : v)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
      const Nobo = { icon: (n) => `<svg data-icon="${n}"/>`, fmtTemp: (v) => Number(v).toFixed(1) };
      const el = { hidden: false, className: '', innerHTML: '', onclick: null,
                   setAttribute(k, v) { this[k] = v; } };
      const $ = () => el;
      const weatherSheet = () => {};
      const state = { status: { weather: %s } };
      %s
      renderTripWeather();
      console.log(JSON.stringify(el));
    """ % (json.dumps(weather), _lift(
        "const PRESSURE_OUTLOOK", "function stationWeather", "function outdoorReading",
        "function renderTripWeather",
    ))
    return json.loads(_node(script))


@needs_node
def test_the_corner_is_hidden_without_a_station():
    assert _trip_weather(None)["hidden"] is True


@needs_node
def test_the_corner_shows_the_temperature_outside_and_the_outlook():
    el = _trip_weather({"enabled": True, "connection": "ok",
                        "outdoor": {"temperature": -3.4, "fresh": True, "cold": False},
                        "outlook": {"tendency": "falling_fast"}})
    assert el["hidden"] is False
    assert "-3.4\u00B0" in el["innerHTML"]
    assert "Rain and wind likely soon" in el["innerHTML"]
    assert "is-worse" in el["className"]
    # Said once, not "outside" twice.
    assert el["innerHTML"].count("outside") == 1


@needs_node
def test_the_corner_marks_very_cold():
    el = _trip_weather({"enabled": True, "connection": "ok",
                        "outdoor": {"temperature": -22, "fresh": True, "cold": True},
                        "outlook": {"tendency": None, "ready_at": "2026-10-01T12:00:00Z"}})
    assert "is-cold" in el["className"]
    assert "Outlook soon" in el["innerHTML"]


@needs_node
def test_a_stale_reading_is_not_shown_as_the_temperature_now():
    el = _trip_weather({"enabled": True, "connection": "unreachable",
                        "outdoor": {"temperature": 5.0, "fresh": False},
                        "outlook": {"tendency": None}})
    assert "5.0" not in el["innerHTML"]
    assert "Station not read" in el["innerHTML"]
    assert "is-stale" in el["className"]


def _outside_block(weather):
    script = """
      const esc = (v) => String(v == null ? '' : v)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
      const Nobo = { fmtTemp: (v) => Number(v).toFixed(1), fmtAgo: () => '2 hours ago' };
      %s
      console.log(JSON.stringify(weatherOutsideBlock(%s)));
    """ % (_lift("function outdoorReading", "function weatherOutdoorBattery",
                 "function weatherOutsideBlock"), json.dumps(weather))
    return json.loads(_node(script))


@needs_node
def test_the_sheet_shows_the_outdoor_modules_battery():
    html = _outside_block({"outdoor": {"temperature": 10.3, "humidity": 88, "fresh": True,
                                       "battery": 63, "battery_low": False}})
    assert "Battery 63%" in html
    assert "is-alert" not in html


@needs_node
def test_a_low_outdoor_battery_is_marked_even_when_the_reading_is_stale():
    html = _outside_block({"outdoor": {"temperature": 4.0, "fresh": False,
                                       "battery": 8, "battery_low": True}})
    assert "No recent reading" in html
    assert "Battery 8%" in html and "low, replace soon" in html
    assert 'class="wx-battery is-alert"' in html


@needs_node
def test_no_battery_line_when_the_station_reports_none():
    html = _outside_block({"outdoor": {"temperature": 4.0, "fresh": True, "battery": None}})
    assert "Battery" not in html


def test_a_low_outdoor_battery_is_styled_as_an_alert():
    assert ".wx-outside-copy small.is-alert" in CABIN_CSS
