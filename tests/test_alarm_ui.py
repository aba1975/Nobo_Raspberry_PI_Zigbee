"""The alarm in the Cabin interface.

Contract tests like the sensor ones: the API goes through the shared client,
nothing appears while the integration is off, the password field is handled
as a password, and the wording that decides whether somebody checks a window
is exercised by running the real functions in node.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "app" / "static"
CABIN = (ROOT / "ui" / "cabin" / "cabin.js").read_text(encoding="utf-8")
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


# -- static contract ------------------------------------------------------


def test_every_alarm_call_goes_through_the_shared_api_client():
    for path in ("/api/alarm/settings", "/api/alarm/verisure/login",
                 "/api/alarm/verisure/code", "/api/alarm/verisure/logout",
                 "/api/alarm/verisure/installation", "/api/alarm/simulate"):
        assert path in CORE, path
    assert "fetch('/api/alarm" not in CABIN


def test_nothing_is_shown_to_an_ordinary_user_or_while_it_is_off():
    assert "if (!isAdmin || !state.me || !state.alarmSettings) return '';" in CABIN
    assert "Nothing about an alarm is shown elsewhere while this is off." in CABIN
    # Loaded for administrators only.
    admin_block = CABIN[CABIN.index("if (state.me && state.me.role === 'admin') {"):]
    assert "Nobo.api.alarmSettings()" in admin_block[:400]


def test_the_section_sits_with_the_other_integrations():
    settings = CABIN[CABIN.index("function renderSettings("):]
    assert settings.index("renderSensorSettingsCard(isAdmin)") < settings.index(
        "renderAlarmSettingsCard(isAdmin)") < settings.index("settingsSection('schedules'")
    assert "wireAlarmSettings(root);" in settings


def test_the_password_field_is_a_password_field():
    sheet = CABIN[CABIN.index("function verisureSignInSheet("):]
    sheet = sheet[:sheet.index("\n  }\n")]
    assert 'id="vsPassword" type="password" autocomplete="current-password"' in sheet
    assert 'autocomplete="one-time-code"' in sheet
    # Cleared as soon as it has been sent.
    assert "passwordInput.value = '';" in sheet
    # Never kept in state or storage.
    assert "localStorage" not in sheet and "state.password" not in sheet


def test_sign_in_is_disabled_off_https():
    block = CABIN[CABIN.index("function alarmAccountBlock("):]
    block = block[:block.index("\n  }\n")]
    assert "${secure ? '' : 'disabled'}" in block
    assert "Signing in needs HTTPS" in block


def test_the_log_names_the_alarm():
    assert "e.source === 'alarm' ? 'Alarm'" in CABIN
    assert "['api', 'schedule', 'alarm'].includes(e.source)" in CABIN


def test_an_alarm_change_is_picked_up_without_waiting_for_the_poll():
    boot = CABIN[CABIN.index("(async function boot()"):]
    assert "if (state.status && state.status.alarm) {" in boot[:1500]


def test_classic_is_left_alone():
    """Classic is the legacy interface. It gains nothing it would have to explain."""
    assert "/api/alarm" not in CLASSIC


def test_there_is_no_control_that_could_arm_or_unlock_verisure():
    """The demo alarm's panel is the only arm/lock control, and it calls the
    demo-only endpoint."""
    assert "simulateAlarm" in CABIN
    for word in ("disarmVerisure", "unlockVerisure", "/api/alarm/arm", "/api/alarm/lock"):
        assert word not in CABIN and word not in CORE


# -- the words, run for real ---------------------------------------------


def _trip_alert(zones, status):
    script = """
      const esc = (v) => String(v == null ? '' : v);
      const SITE_IN = () => 'the cabin';
      const Nobo = { icon: (n) => `<svg data-icon="${n}"/>` };
      const state = { zones: %s, status: %s };
      const away = () => ({ enabled: false });
      %s
      console.log(tripAlertHtml());
    """ % (json.dumps(zones), json.dumps(status), _lift(
        "function sensorKindLabel", "function sensorGroupNoun",
        "function sensorCountLabel", "function compactSensorNames",
        "function awayNow", "function alarmLeaving", "function leavingSentence",
        "function openWhileAway", "function tripAlertHtml",
    ))
    return _node(script)


def _window(state="open", available=True):
    return {"sensor_id": "w", "name": "Kitchen Window", "kind": "window",
            "state": state, "available": available}


def _alarm(reason=None, known=True, lock_name=None):
    return {"known": known, "leaving": {"reason": reason, "lock_name": lock_name} if reason else None}


KITCHEN = [{"zone_id": "1", "name": "Kitchen", "sensors": [_window()]}]


@needs_node
def test_locked_from_outside_with_a_window_open_is_on_the_top_card():
    markup = _trip_alert(KITCHEN, {"global_override_mode": None,
                                   "alarm": _alarm("locked_outside", lock_name="Front door")})
    assert "Window still open" in markup
    assert "Front door was locked from outside." in markup
    assert "Kitchen Window" in markup


@needs_node
def test_armed_away_says_so():
    markup = _trip_alert(KITCHEN, {"global_override_mode": "away",
                                   "alarm": _alarm("armed_away")})
    assert "The alarm is armed" in markup
    assert "is on\n          Away" not in markup


@needs_node
def test_armed_at_home_says_so():
    markup = _trip_alert(KITCHEN, {"global_override_mode": None, "alarm": _alarm("armed_home")})
    assert "The alarm is armed at home." in markup


@needs_node
def test_an_alarm_that_cannot_be_read_says_nothing():
    """Not knowing is not a reason to warn."""
    markup = _trip_alert(KITCHEN, {"global_override_mode": None,
                                   "alarm": _alarm("armed_away", known=False)})
    assert markup == ""


@needs_node
def test_disarmed_and_home_says_nothing():
    assert _trip_alert(KITCHEN, {"global_override_mode": None, "alarm": _alarm()}) == ""
    assert _trip_alert(KITCHEN, {"global_override_mode": None, "alarm": None}) == ""


@needs_node
def test_everything_shut_says_nothing_even_when_armed():
    shut = [{"zone_id": "1", "name": "Kitchen", "sensors": [_window("closed")]}]
    assert _trip_alert(shut, {"alarm": _alarm("armed_away")}) == ""


@needs_node
def test_away_without_an_alarm_still_reads_as_before():
    markup = _trip_alert(KITCHEN, {"global_override_mode": "away", "alarm": None})
    assert "The cabin is on Away" in markup


@needs_node
@pytest.mark.parametrize("alarm, expected", [
    (None, "null"),
    ({"provider": "verisure", "connection": "signed_out"}, "Verisure · signed out"),
    ({"provider": "verisure", "connection": "not_configured"}, "Verisure · not set up"),
    ({"provider": "verisure", "connection": "rate_limited"}, "Verisure · asked to wait"),
    ({"provider": "simulated", "connection": "ok", "arm_state": "armed_away", "locks": [
        {"name": "Front door", "locked": True, "outside": True}]},
     "Demo · Armed away · Front door locked from outside"),
    ({"provider": "verisure", "connection": "ok", "arm_state": "disarmed", "locks": [
        {"name": "Front door", "locked": True, "outside": False}]},
     "Verisure · Disarmed · Front door locked from inside"),
])
def test_the_system_status_line(alarm, expected):
    script = """
      %s
      console.log(String(alarmSystemLine(%s)));
    """ % (_lift("const ARM_LABELS", "const ALARM_PROVIDER_LABELS",
                 "function alarmSystemLine", "function alarmLockWords"), json.dumps(alarm))
    assert _node(script) == expected


def _settings_card(settings, protocol="https:"):
    script = """
      const esc = (v) => String(v == null ? '' : v)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
      const Nobo = { icon: (n) => `<svg data-icon="${n}"/>` };
      const window = { location: { protocol: %s, hostname: 'nobo.example.no' } };
      const settingsOpenMap = () => ({});
      const state = { me: { role: 'admin' }, alarmSettings: %s };
      %s
      console.log(renderAlarmSettingsCard(true));
    """ % (json.dumps(protocol), json.dumps(settings), _lift(
        "const ARM_LABELS", "const ALARM_PROVIDER_LABELS",
        "function alarmLockWords", "function alarmTransportSecure",
        "function alarmAccountBlock", "function alarmStatusBlock", "function alarmOption",
        "function alarmDemoControls", "function renderAlarmSettingsCard",
        "function settingsSection", "function segControl", "function settingRow",
    ))
    return _node(script)


BASE = {
    "enabled": True, "provider": "verisure", "providers": ["verisure"],
    "away_when_armed_away": True, "away_when_armed_home": False,
    "warn_when_armed_away": True, "warn_when_armed_home": True,
    "warn_when_locked_outside": True, "autolock_counts_as_leaving": False,
    "sensors_enabled": True,
    "verisure": {"signed_in": False, "email": None, "installation": None,
                 "installations": [], "awaiting_code": False},
    "status": {"connection": "not_configured", "message": "Sign in to Verisure in Settings."},
}


@needs_node
def test_off_shows_only_the_switch():
    markup = _settings_card({**BASE, "enabled": False, "status": None})
    assert "Nothing about an alarm is shown elsewhere" in markup
    assert "Sign in to Verisure" not in markup
    assert "data-alarm-opt" not in markup


@needs_node
def test_not_signed_in_offers_sign_in_over_https_only():
    https = _settings_card(BASE)
    assert "Signing in needs HTTPS" not in https
    button = https[https.index('data-act="alarm-signin"'):]
    assert "disabled" not in button[:button.index(">")]
    http = _settings_card(BASE, protocol="http:")
    assert "Signing in needs HTTPS" in http
    button = http[http.index('data-act="alarm-signin"'):]
    assert "disabled" in button[:button.index(">")]


@needs_node
def test_signed_in_shows_the_masked_account_and_no_sign_in_button():
    markup = _settings_card({**BASE, "verisure": {
        "signed_in": True, "email": "a***@example.no", "installation": "Mostugu",
        "installations": [{"giid": "1", "alias": "Mostugu"}], "awaiting_code": False,
    }, "status": {"connection": "ok", "arm_state": "armed_away", "locks": [], "owns_away": True}})
    assert "a***@example.no" in markup and "Mostugu" in markup
    assert 'data-act="alarm-signout"' in markup
    assert 'data-act="alarm-signin"' not in markup
    assert "Armed away" in markup
    assert "The heating is on Away because the alarm is armed." in markup


@needs_node
def test_a_lapsed_sign_in_is_flagged_on_the_section():
    markup = _settings_card({**BASE, "verisure": {**BASE["verisure"], "email": "a***@example.no"},
                             "status": {"connection": "signed_out"}})
    assert "Verisure ended the sign-in" in markup
    assert "is-alert" in markup


@needs_node
def test_the_demo_alarm_has_its_own_panel():
    markup = _settings_card({**BASE, "provider": "simulated",
                             "providers": ["simulated", "verisure"],
                             "status": {"provider": "simulated", "connection": "ok",
                                        "arm_state": "disarmed", "locks": [
                                            {"name": "Front door", "locked": True,
                                             "method": "code", "outside": True}]}})
    assert 'data-seg="alarm-sim-arm"' in markup
    assert 'data-seg="alarm-sim-lock" data-value="outside"' in markup
    outside = markup[markup.index('data-seg="alarm-sim-lock" data-value="outside"'):]
    assert 'aria-pressed="true"' in outside[:120]
    assert "Sign in to Verisure" not in markup


@needs_node
def test_warnings_explain_that_they_need_sensors():
    markup = _settings_card({**BASE, "sensors_enabled": False})
    assert "The warnings need door and window sensors, which are off." in markup
