"""Static checks on the Cabin contact-sensor interface.

These are contract tests, not screenshots: they pin down the things that have
actually gone wrong here before — a control that silently stops matching the
API, an escaping hole on a user-supplied name, sensor wording leaking into
Classic — and leave visual judgement to a person with the app open.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent / "app" / "static"
CABIN = (ROOT / "ui" / "cabin" / "cabin.js").read_text(encoding="utf-8")
CORE = (ROOT / "ui" / "shared" / "core.js").read_text(encoding="utf-8")
CLASSIC = (ROOT / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "cabin" / "cabin.css").read_text(encoding="utf-8")


def test_every_sensor_call_goes_through_the_shared_api_client():
    for path in ("/api/sensors/settings", "/api/sensors", "/simulate"):
        assert path in CORE
    # Nothing may reach past the client and build its own sensor request.
    assert "fetch('/api/sensors" not in CABIN


def test_nothing_sensor_shaped_is_rendered_for_an_ordinary_user():
    assert "if (!isAdmin || !state.me || !state.sensorSettings) return ''" in CABIN
    assert "Nothing sensor-related is shown elsewhere while this is off." in CABIN
    # The management buttons are behind the same admin check as the heaters.
    assert "const admin = state.me && state.me.role === 'admin';" in CABIN


def test_the_pairing_sheet_opens_ready_not_showing_the_last_attempt():
    """What the user hit: it opened saying "Close / Try again".

    The sheet rendered whatever outcome the server still remembered from
    somebody's previous attempt, so it looked finished before anything had
    been done — and the obvious next action, Start pairing, was not offered.
    """
    start = CABIN.index("function zigbeePairSheet")
    end = CABIN.index("\n  /* A battery contact sensor", start)
    sheet = CABIN[start:end]
    assert "let phase = 'ready';" in sheet
    # The initial render must not consult a stale outcome at all.
    assert "pairing = {};" in sheet
    # Ready offers the one thing the user came to do.
    assert "Start pairing</button>" in sheet


def test_the_sheet_says_what_to_do_before_the_radio_starts_listening():
    assert "pair-steps" in CABIN
    assert "pair-steps" in CSS
    # The order matters: the sensor has to be in place before the window opens.
    assert "where it will actually live" in CABIN
    assert "until its light blinks" in CABIN


def test_listening_tells_the_user_to_press_the_button_now():
    start = CABIN.index("function zigbeePairSheet")
    end = CABIN.index("\n  /* A battery contact sensor", start)
    sheet = CABIN[start:end]
    assert "phase === 'listening'" in sheet
    assert "<strong>now</strong>" in sheet


def test_a_sensor_that_will_not_unpair_offers_to_force_it():
    """A battery sensor is asleep, so Zigbee2MQTT often cannot evict it."""
    assert "removeSensorWithRetry" in CABIN
    assert "Remove anyway" in CABIN
    assert "may rejoin by itself later" in CABIN
    # Both the delete button and a replacement go through the same path.
    assert CABIN.count("removeSensorWithRetry(") >= 3
    assert "removeSensor: " in CORE.replace("removeSensor:  ", "removeSensor: ")
    assert "force ? '?force=true' : ''" in CORE


def test_a_real_sensors_readings_are_not_offered_as_fields():
    """Gating the simulator's controls on demo mode conflated two questions.

    The hub can be simulated while the sensors are real, and that is the
    arrangement this was tested in — so a real Aqara offered an editable
    battery percentage, a number the hardware never reported.
    """
    assert "state.sensorSettings.simulated" in CABIN
    assert "state.sensorSettings.demo_mode" not in CABIN
    assert "real Zigbee sensors" in CABIN


def test_pairing_asks_for_the_type_the_name_and_the_room():
    for hook in ("pair-sensor", "pairSensorSheet", "pairSensorKind",
                 "pairSensorName", "pairSensorZone"):
        assert hook in CABIN
    assert "Add simulated sensor" in CABIN
    assert "Start pairing" in CABIN


def test_sensors_are_managed_in_their_room_with_the_heater_icon_language():
    for hook in ("edit-sensor", "move-sensor", "replace-sensor", "remove-sensor"):
        assert hook in CABIN
    for icon in ("rename", "move", "replace", "remove", "door", "window"):
        assert icon in CORE
    # Settings stays a switch and a count; the long list lives on the rooms.
    assert "sensor-settings-summary" in CSS
    assert "Open a zone to see status" in CABIN


def test_a_sensor_that_lost_its_room_can_still_be_reached():
    assert "!knownZones.has(String(sensor.zone_id))" in CABIN
    assert "wireZoneSensors(root)" in CABIN


def test_the_zone_card_names_the_thing_that_is_open_and_counts_it():
    assert "sensorZoneHeadline" in CABIN
    assert "sensorKindLabel(open[0])} ${state}" in CABIN
    assert "zsensor-tally" in CABIN and ".zsensor-tally" in CSS
    for tone in ("closed", "open", "warning", "unavailable"):
        assert f".zsensor-{tone}" in CSS
    # Doors are doors and windows are windows, unless the room has both.
    assert "sensorGroupNoun" in CABIN


def test_zone_card_text_stays_short_however_many_sensors_are_open():
    assert "compactSensorNames" in CABIN
    assert "and ${remaining} more" in CABIN


def test_unknown_unavailable_and_battery_are_their_own_states():
    for value in ("open", "closed", "unknown", "unavailable"):
        assert f".sensor-{value}" in CSS
    assert "sensor-batt" in CABIN and ".sensor-batt.is-low" in CSS


def test_the_rule_is_summarised_on_the_room_and_edited_in_a_sheet():
    assert "sensorRuleSummary" in CABIN
    assert "data-edit-sensor-policy" in CABIN
    assert "editSensorPolicySheet" in CABIN
    for control in ("#spWarn", "#spAction", "#spDelay", "#spOverride"):
        assert control in CABIN
    # No form is left sitting open in the card any more.
    assert "data-save-sensor-policy" not in CABIN


def test_the_action_choice_offers_exactly_the_five_outcomes():
    assert "const SENSOR_ACTIONS = ['nothing', 'away', 'eco', 'comfort', 'schedule']" in CABIN
    for label in ("Do nothing", "Set to Away", "Set to Eco",
                  "Set to Comfort", "Return to schedule"):
        assert label in CABIN
    # Immediately and a couple of short spans, plus the demo-only one.
    for label in ("Immediately", "10 seconds (demo)", "1 minute",
                  "2 minutes", "5 minutes", "10 minutes"):
        assert label in CABIN


def test_the_rule_is_explained_where_it_is_chosen():
    assert "Override colder modes" in CABIN
    assert "the room runs whichever is colder" in CABIN
    # One name for the switch wherever it is referred to.
    assert "sensor override" not in CABIN
    # And the sheet only offers it where it can do anything.
    assert "if (chosen === 'nothing') override.checked = false;" in CABIN


def test_both_delays_say_what_they_are_counted_from():
    assert "once it has been open for" in CABIN
    assert "Both delays are counted from the moment it" in CABIN


def test_everything_reachable_by_thumb_meets_the_apps_44px_floor():
    # .btn and .icon-btn already do; the rule editor's entry point is a
    # .btn-small and had been left out of that rule.
    assert ".btn-small { min-height: 44px; }" in CSS


def test_sensor_colours_come_from_the_theme_rather_than_being_hard_coded():
    # Hard-coded reds went dark-on-dark under prefers-color-scheme: dark.
    for literal in ("#c84b31", "#b33b24", "#d99a00", "#8a6500"):
        assert literal not in CSS
    assert "var(--danger)" in CSS and "var(--danger-wash)" in CSS


def test_a_flat_battery_is_labelled_not_merely_recoloured():
    assert "low ? ' low' : ''" in CABIN
    assert ".sensor-batt.is-low" in CSS


def test_a_rule_that_stands_down_says_so_rather_than_looking_broken():
    assert "sensorBlockedText" in CABIN
    assert "already colder than" in CABIN
    assert "colder_mode" in CABIN and "no_equipment" in CABIN
    assert "action_status" in CABIN and "block_reason" in CABIN
    # Suppression is gone from the model, so no wording may imply it.
    assert "suppressed" not in CABIN


def test_a_sensor_the_pi_has_lost_is_shown_as_offline_with_a_last_heard_time():
    assert "'Offline'" in CABIN
    assert "last heard from" in CABIN
    assert "Nobo.fmtAgo(sensor.last_seen_at)" in CABIN
    assert ".sensor-row.is-offline" in CSS


def test_offline_stays_on_the_zone_card_while_something_else_is_open():
    """Open and out-of-touch are separate facts and a room can have both.

    They shared one headline, so a single open contact hid the fact that
    another sensor had gone quiet — which is when knowing it matters most,
    because a sensor nobody can hear from might be open too.
    """
    headline = CABIN[
        CABIN.index("function sensorZoneHeadline"):CABIN.index("function sensorRow")
    ]
    # The badge is chosen on its own facts, not inside the headline's if/else.
    assert "const alsoOffline = unavailable.length && open.length" in headline
    assert "zsensor-offline" in headline and ".zsensor-offline" in CSS
    # A band along the foot of the strip, not a lozenge beside the count.
    band = CSS.split(".zsensor-offline {")[1].split("}")[0]
    assert "999px" not in band, "the offline warning is a band, not a pill"
    assert "border-radius: 0 0" in band, "its top corners are square"
    assert "grid-column: 1 / -1" in band, "it spans the whole strip"
    # Amber on its own wash is about 2.3:1, which will not do for the line
    # that has to be read.
    assert "color: var(--ink);" in band
    assert "alert" in CORE, "the warning glyph is part of the shared icon set"
    # And the zone detail says it once at card level, above the rows.
    assert "sensor-offline-note" in CABIN and ".sensor-offline-note" in CSS
    assert "offlineNote" in CABIN


def test_reading_what_a_room_will_do_does_not_need_admin_settings():
    # The action travels on the zone payload, so an ordinary user sees "about
    # to set this room to Eco" without being able to open the settings that
    # would tell them so.
    assert "summary.action_when_open || 'nothing'" in CABIN
    assert "sensorPolicyFor(zone.zone_id)" not in CABIN.split(
        "function sensorRuleLine")[1].split("function sensorModeWord")[0]


def test_the_payload_matches_what_the_server_accepts():
    for field in ("warning_delay_seconds", "action_when_open",
                  "action_delay_seconds", "override_all_modes"):
        assert field in CABIN
    # The dead v1 aliases are gone from both ends.
    for stale in ("eco_enabled", "eco_delay_seconds", "eco_owned",
                  "eco_available", "action_owned"):
        assert stale not in CABIN


def test_a_persistent_warning_does_not_re_announce_itself_on_every_update():
    # The card re-renders on each zone update, so role="alert" would repeat the
    # same open window at a screen reader indefinitely, and a live region
    # around the whole card would read the sensor list out with it.
    assert 'role="alert"' not in CABIN.replace(
        'role="alert" would be worse still and', ''
    )
    assert '<div aria-live="polite">${warning}${offline}</div>' in CABIN


def test_sensor_names_are_escaped_before_they_reach_the_markup():
    assert "${esc(sensor.name)}" in CABIN
    assert "${esc(zone.name)}" in CABIN


def test_classic_remains_a_sensor_free_legacy_surface():
    assert "/api/sensors" not in CLASSIC
    assert "sensor-warning" not in CLASSIC


def test_no_style_is_left_pointing_at_a_class_the_markup_dropped():
    for gone in ("sensor-zone-strip", "sensor-behavior", "sensor-mode-override"):
        assert gone not in CSS
        assert gone not in CABIN


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


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_delay_reads_the_way_somebody_would_say_it():
    script = f"""
      const window = {{}}; const document = {{}};
      {CORE}
      const out = [0, 45, 60, 300, 3600].map(Nobo.fmtDuration);
      console.log(JSON.stringify(out));
    """
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert "immediately" in result.stdout
    assert "45 seconds" in result.stdout
    assert "1 minute" in result.stdout
    assert "5 minutes" in result.stdout
    assert "1 hour" in result.stdout


def _render_zone_strip(zone):
    """Run the real zone-card strip in node and return the markup.

    The pure rendering helpers are lifted out of the interface and evaluated
    with stubs for the few things they lean on, so this exercises the actual
    branching rather than asserting on the source text.
    """
    wanted = [
        "function sensorKindLabel", "function sensorGroupNoun",
        "function sensorCountLabel", "function compactSensorNames",
        "function offlineNote", "function sensorZoneHeadline",
    ]
    lifted = []
    for marker in wanted:
        start = CABIN.index(marker)
        end = CABIN.index("\n  }\n", start) + len("\n  }\n")
        lifted.append(CABIN[start:end])
    script = """
      const esc = (v) => String(v == null ? '' : v);
      const sensorIcon = () => '<i/>';
      const sensorRuleLine = () => null;
      const Nobo = { fmtAgo: () => '20 min ago', icon: (n) => `<svg data-icon="${n}"/>` };
      %s
      console.log(sensorZoneHeadline(%s));
    """ % ("\n".join(lifted), json.dumps(zone))
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _sensor(name, kind, state, available=True):
    return {
        "name": name, "kind": kind, "state": state, "available": available,
        "battery": 100, "last_seen_at": "2026-09-09T08:00:00+02:00",
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_the_card_shows_open_and_offline_at_the_same_time():
    """The case that was reported: one open, one offline, both must be visible."""
    markup = _render_zone_strip({
        "zone_id": "5",
        "name": "Kitchen",
        "sensors": [
            _sensor("Kitchen Window", "window", "open"),
            _sensor("Kitchen Window 2", "window", "closed", available=False),
            _sensor("Back Door", "door", "closed"),
        ],
        "sensor_summary": {
            "sensor_count": 3, "open_count": 1, "unavailable_count": 1,
            "warning_raised": False, "state": "open",
        },
    })
    assert "Window open" in markup, markup
    assert "Kitchen Window" in markup
    # ...and the one that used to disappear the moment anything opened.
    assert "offline" in markup, markup
    assert "zsensor-offline" in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_offline_is_the_headline_when_nothing_is_open():
    markup = _render_zone_strip({
        "zone_id": "5",
        "name": "Kitchen",
        "sensors": [
            _sensor("Kitchen Window", "window", "closed"),
            _sensor("Kitchen Window 2", "window", "closed", available=False),
        ],
        "sensor_summary": {
            "sensor_count": 2, "open_count": 0, "unavailable_count": 1,
            "warning_raised": False, "state": "unavailable",
        },
    })
    assert "1 window offline" in markup, markup
    # No second badge repeating what the headline already says.
    assert "zsensor-offline" not in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_room_with_nothing_wrong_says_so_plainly():
    markup = _render_zone_strip({
        "zone_id": "5", "name": "Kitchen",
        "sensors": [_sensor("Kitchen Window", "window", "closed")],
        "sensor_summary": {
            "sensor_count": 1, "open_count": 0, "unavailable_count": 0,
            "warning_raised": False, "state": "closed",
        },
    })
    assert "All closed" in markup
    assert "offline" not in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_left_open_room_escalates_its_wording_and_still_flags_offline():
    markup = _render_zone_strip({
        "zone_id": "5", "name": "Kitchen",
        "sensors": [
            _sensor("Terrace Door", "door", "open"),
            _sensor("Kitchen Window", "window", "closed", available=False),
        ],
        "sensor_summary": {
            "sensor_count": 2, "open_count": 1, "unavailable_count": 1,
            "warning_raised": True, "state": "open",
        },
    })
    assert "Door left open" in markup, markup
    assert "zsensor-warning" in markup
    assert "zsensor-offline" in markup


# ---------------------------------------------------------------------------
# Left the building with something open
# ---------------------------------------------------------------------------

def _render_trip_alert(zones, status, away_period=None):
    """Run the real top-card alert in node and return its markup."""
    wanted = [
        "function sensorKindLabel", "function sensorGroupNoun",
        "function sensorCountLabel", "function compactSensorNames",
        "function awayNow", "function openWhileAway", "function tripAlertHtml",
    ]
    lifted = []
    for marker in wanted:
        start = CABIN.index(marker)
        end = CABIN.index("\n  }\n", start) + len("\n  }\n")
        lifted.append(CABIN[start:end])
    script = """
      const esc = (v) => String(v == null ? '' : v);
      const SITE_IN = () => 'the cabin';
      const Nobo = { icon: (n) => `<svg data-icon="${n}"/>` };
      const state = { zones: %s, status: %s };
      const away = () => (%s);
      %s
      console.log(tripAlertHtml());
    """ % (
        json.dumps(zones), json.dumps(status),
        json.dumps(away_period or {"enabled": False}), "\n".join(lifted),
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _room(name, sensors, zone_id="1"):
    return {"zone_id": zone_id, "name": name, "sensors": sensors}


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_leaving_with_a_window_open_is_called_out_on_the_top_card():
    markup = _render_trip_alert(
        [_room("Kitchen", [_sensor("Kitchen Window", "window", "open")])],
        {"global_override_mode": "away"},
    )
    assert "still open" in markup, markup
    assert "Kitchen" in markup and "Kitchen Window" in markup
    assert "on\n          Away" in markup or "is on" in markup
    assert 'data-icon="alert"' in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_running_away_period_counts_even_if_the_hub_stopped_honouring_it():
    """You are still not there, which is the whole point of the warning."""
    markup = _render_trip_alert(
        [_room("Kitchen", [_sensor("Terrace Door", "door", "open")])],
        {"global_override_mode": None},
        away_period={"enabled": True, "currently_active": True},
    )
    assert "Door still open" in markup, markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_an_away_exception_room_does_not_stop_the_house_counting_as_away():
    """A room held on Eco while the house is Away reads as "mixed" from the
    zones alone, which is why the global override is asked for instead."""
    markup = _render_trip_alert(
        [
            _room("Kitchen", [_sensor("Kitchen Window", "window", "open")]),
            _room("Large Bathroom", [], zone_id="2"),
        ],
        {"global_override_mode": "away"},
    )
    assert "still open" in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_nothing_is_said_when_somebody_is_home():
    markup = _render_trip_alert(
        [_room("Kitchen", [_sensor("Kitchen Window", "window", "open")])],
        {"global_override_mode": None},
    )
    assert markup == "", markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_nothing_is_said_when_the_place_is_shut_up_properly():
    markup = _render_trip_alert(
        [_room("Kitchen", [_sensor("Kitchen Window", "window", "closed")])],
        {"global_override_mode": "away"},
    )
    assert markup == "", markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_several_rooms_are_each_named():
    markup = _render_trip_alert(
        [
            _room("Kitchen", [_sensor("Kitchen Window", "window", "open")]),
            _room("Living Room", [
                _sensor("Terrace Door", "door", "open"),
                _sensor("Window Left", "window", "open"),
            ], zone_id="2"),
        ],
        {"global_override_mode": "away"},
    )
    assert "3 sensors still open" in markup, markup
    for name in ("Kitchen", "Living Room", "Terrace Door", "Window Left"):
        assert name in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_sensor_that_cannot_be_reached_is_worth_saying_while_away():
    """Away is exactly when "I cannot tell you" matters as much as "it is open"."""
    markup = _render_trip_alert(
        [_room("Kitchen", [
            _sensor("Kitchen Window", "window", "closed", available=False),
        ])],
        {"global_override_mode": "away"},
    )
    assert "cannot be checked" in markup, markup
    assert "Kitchen Window" in markup
    # ...and the room list names them rather than repeating the headline.
    assert markup.count("cannot be checked") == 1, markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_an_open_window_leads_and_an_unreachable_one_follows():
    markup = _render_trip_alert(
        [_room("Kitchen", [
            _sensor("Kitchen Window", "window", "open"),
            _sensor("Back Door", "door", "closed", available=False),
        ])],
        {"global_override_mode": "away"},
    )
    assert markup.index("still open") < markup.index("cannot be checked"), markup


def test_the_top_card_has_somewhere_to_put_the_alert():
    html = (ROOT / "ui" / "cabin" / "index.html").read_text(encoding="utf-8")
    assert 'id="tripAlert"' in html
    assert 'role="alert"' in html
    assert ".trip-alert" in CSS
    # It outranks whatever else the card was saying.
    assert ".trip:has(.trip-alert:not([hidden]))" in CSS


# -- the pairing window ----------------------------------------------------
#
# Somebody is at a door holding a paperclip against a battery device, so every
# outcome has to be distinguishable — and "nothing came" has to be
# distinguishable from "still waiting".


def _render_pairing(pairing):
    """Run the real pairing-status renderer in node and return the markup."""
    wanted = ["function pairingReport", "function pairingStatusHtml"]
    lifted = []
    for marker in wanted:
        start = CABIN.index(marker)
        end = CABIN.index("\n  }\n", start) + len("\n  }\n")
        lifted.append(CABIN[start:end])
    table_start = CABIN.index("  const PAIRING_REPORT = {")
    table_end = CABIN.index("\n  };\n", table_start) + len("\n  };\n")
    script = """
      const esc = (v) => String(v == null ? '' : v);
      const Nobo = { icon: (n) => `<svg data-icon="${n}"/>` };
      %s
      %s
      console.log(pairingStatusHtml(%s));
    """ % (CABIN[table_start:table_end], "\n".join(lifted), json.dumps(pairing))
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_an_open_window_says_it_is_listening_and_counts_down():
    markup = _render_pairing(
        {"supported": True, "active": True, "seconds_remaining": 97, "outcome": None}
    )

    assert "is-busy" in markup
    assert "Listening for a sensor" in markup
    assert "97s left" in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_found_sensor_says_so_and_asks_for_a_name():
    markup = _render_pairing(
        {"supported": True, "active": False, "outcome": "joined",
         "sensor_id": "0x00158d008c8bc4f2"}
    )

    assert "is-ok" in markup
    assert "Sensor found" in markup
    assert "name" in markup.lower()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_failure_is_not_shown_as_a_success():
    markup = _render_pairing(
        {"supported": True, "active": False, "outcome": "failed",
         "detail": "The device started joining but the interview did not finish"}
    )

    assert "is-error" in markup
    assert "Pairing failed" in markup
    assert "is-ok" not in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_window_that_found_nothing_says_so_rather_than_going_quiet():
    markup = _render_pairing(
        {"supported": True, "active": False, "outcome": "expired"}
    )

    # The failure this guards against is a window that closes unannounced,
    # leaving somebody pressing a button at a sensor that stopped listening.
    assert "is-warn" in markup
    assert "Nothing joined in time" in markup
    assert "is-busy" not in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_repeater_is_not_reported_as_a_broken_sensor():
    markup = _render_pairing(
        {"supported": True, "active": False, "outcome": "ignored",
         "detail": "IKEA TRETAKT smart plug"}
    )

    assert "is-warn" in markup
    assert "not a contact sensor" in markup
    assert "TRETAKT" in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_nothing_is_claimed_before_anything_has_happened():
    assert _render_pairing(
        {"supported": True, "active": False, "outcome": None}
    ).strip() == ""


def test_every_outcome_the_server_can_send_is_rendered():
    """A new outcome must not land in the interface as silence."""
    from sensor_provider import PairingOutcome

    for outcome in PairingOutcome:
        assert f"{outcome.value}:" in CABIN, outcome.value


def test_the_pairing_status_has_a_tone_for_each_state():
    for tone in ("is-busy", "is-ok", "is-warn", "is-error", "is-idle"):
        assert f".pair-status.{tone}" in CSS
    # Colour alone is not an answer, so the wording repeats it.
    assert "not an answer" in CSS
    # Only the unfinished state moves.
    assert "prefers-reduced-motion" in CSS


def test_pairing_goes_through_the_shared_api_client():
    for call in ("sensorPairing", "startSensorPairing", "cancelSensorPairing"):
        assert call in CORE
        assert call in CABIN
    assert "/api/sensors/pairing" in CORE
    assert "/api/sensors/pairing" not in CABIN


def test_dismissing_the_sheet_closes_the_join_window():
    """Scrim and Escape go through closeSheet, not the buttons.

    Without this the radio stays in permit-join for the rest of its four
    minutes with nothing on screen saying so, and anything that joins in that
    time is accepted silently. The hub's device search already does this; the
    pairing sheet was only doing half of it.
    """
    start = CABIN.index("function zigbeePairSheet")
    end = CABIN.index("\n  function ", start + 10)
    sheet = CABIN[start:end]
    assert "onSheetClose(" in sheet
    assert "cancelSensorPairing()" in sheet
    assert "clearInterval(timer)" in CABIN


def test_a_late_poll_cannot_write_into_another_sheet():
    """clearInterval does not cancel a callback already awaiting."""
    start = CABIN.index("function zigbeePairSheet")
    end = CABIN.index("\n  function ", start + 10)
    sheet = CABIN[start:end]
    assert "closed = true;" in sheet
    assert sheet.count("if (closed) return;") >= 3


def test_classic_gains_no_sensor_pairing_surface():
    # Classic deliberately has no sensor controls at all. It does talk about
    # pairing Nobø receivers, which is a different thing entirely, so this
    # checks for the sensor pairing surface rather than the word.
    for hook in ("/api/sensors/pairing", "pair-status", "startSensorPairing",
                 "Listening for a sensor"):
        assert hook not in CLASSIC


def test_a_battery_nobody_has_reported_is_named_not_left_blank():
    """Rendering nothing read as a broken sensor.

    A battery device sends its level on its own schedule — Aqara's own
    definition warns it can take a day — so one sensor showing a percentage
    and another showing an empty space looks like a fault when it is not.
    """
    assert "Battery not reported yet" in CABIN
    # And says it cannot be hurried: on the MCCGQ11LM battery is report-only,
    # and asking for it is refused with "No converter available".
    assert "cannot be asked for it" in CABIN
    assert "sensor-batt.is-unknown" in CSS
    # Quieter than a real reading, and quieter than the low warning: this is
    # the absence of news, not bad news.
    assert "absence of news" in CSS


def test_unpairing_shows_that_something_is_happening():
    """confirmSheet closes before it runs its action.

    Unpairing waits ten seconds for a sleeping sensor to answer, so without a
    progress sheet there was nothing on screen at all for those ten seconds —
    indistinguishable from the button not working, which is how it was
    reported.
    """
    assert "function workingSheet" in CABIN
    assert "Asking the hub to unpair" in CABIN
    start = CABIN.index("async function removeSensorWithRetry")
    end = CABIN.index("\n  function workingSheet", start)
    body = CABIN[start:end]
    assert "workingSheet(" in body
    assert body.index("workingSheet(") < body.index("Nobo.api.removeSensor(")


def test_the_first_dialog_warns_that_a_second_may_follow():
    # So the "Remove anyway" step reads as a continuation, not a failure.
    assert "asleep most of the time" in CABIN


def test_a_quiet_sensor_is_mentioned_long_before_anything_calls_it_offline():
    """Zigbee2MQTT waits 25 hours before declaring a battery device offline.

    That is right for avoiding false alarms and useless for noticing a flat
    battery: for most of a day a dead sensor looks exactly like a healthy one,
    and whatever it last said is still believed — including "open", which can
    hold a room's heating action the whole time.
    """
    assert "function sensorIsStale" in CABIN
    assert "nothing heard since" in CABIN
    assert "SENSOR_QUIET_HOURS = 6" in CABIN
    # And the threshold is justified against what real sensors do, not picked.
    assert "two and a half hours between" in CABIN


def test_staleness_is_not_confused_with_offline():
    start = CABIN.index("function sensorRow")
    end = CABIN.index("\n  /* One line summarising the rule", start)
    row = CABIN[start:end]
    # Offline keeps its own wording; stale is a separate, softer statement.
    assert "last heard from" in row
    assert "nothing heard since" in row
    assert row.index("!sensor.available") < row.index("sensorIsStale(sensor)")


# -- signal strength -------------------------------------------------------


def _render_signal(link_quality):
    """Run the real signal badge in node and return its markup."""
    start = CABIN.index("function sensorSignal")
    end = CABIN.index("\n  }\n", start) + len("\n  }\n")
    script = """
      const esc = (v) => String(v == null ? '' : v);
      const Nobo = { icon: (n) => `<svg data-icon="${n}"/>` };
      %s
      console.log(sensorSignal(%s));
    """ % (CABIN[start:end], json.dumps({"link_quality": link_quality}))
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
@pytest.mark.parametrize(
    "lqi, word, band",
    [
        (255, "Good signal", "is-good"),
        (140, "Good signal", "is-good"),
        (100, "Good signal", "is-good"),   # on the boundary, the better side
        (99, "Fair signal", "is-fair"),
        (50, "Fair signal", "is-fair"),
        (49, "Weak signal", "is-weak"),
        (0, "Weak signal", "is-weak"),
    ],
)
def test_the_signal_is_reported_as_a_verdict_not_a_number(lqi, word, band):
    """The raw LQI means nothing to anybody; "does this need a repeater?" does."""
    markup = _render_signal(lqi)
    assert word in markup, markup
    assert band in markup
    # The number is still there for anyone who wants it, in the tooltip.
    assert f"Link quality {lqi} of 255" in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_weak_signal_says_what_to_do_about_it():
    markup = _render_signal(20)
    assert "repeater" in markup
    assert "mains-powered" in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_good_signal_does_not_suggest_a_repeater():
    assert "repeater" not in _render_signal(200)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_missing_reading_is_named_rather_than_left_blank():
    """Rendering nothing for an unknown reading reads as a broken sensor —
    the same mistake the battery label already had to fix."""
    markup = _render_signal(None)
    assert "Signal unknown" in markup
    assert "is-unknown" in markup


def test_the_signal_badge_is_styled_from_the_theme():
    for selector in (".sensor-signal", ".sensor-signal.is-weak",
                     ".sensor-signal.is-unknown"):
        assert selector in CSS
    assert "var(--danger)" in CSS


def test_the_signal_sits_with_the_battery_and_the_row_may_wrap():
    """Three facts do not fit one phone row, and the alternative to wrapping
    is squeezing the sensor's name."""
    start = CABIN.index("function sensorRow")
    end = CABIN.index("\n  /* One line summarising the rule", start)
    row = CABIN[start:end]
    assert "sensorSignal(sensor)" in row
    assert row.index("${battery}") < row.index("${sensorSignal(sensor)}")
    facts = CSS[CSS.index(".sensor-facts"):CSS.index(".sensor-state")]
    assert "flex-wrap: wrap" in facts


def test_the_tooltip_admits_the_reading_is_only_the_last_hop():
    """A sensor reporting through a repeater is scored on the short leg to
    that repeater, so presenting it as distance from the Pi would mislead."""
    assert "last hop" in CABIN
    assert "measured on the last hop" in CABIN


def test_link_quality_can_be_simulated_but_only_where_readings_are_simulated():
    start = CABIN.index("function editSensorSheet")
    end = CABIN.index("\n  function ", start + 10)
    sheet = CABIN[start:end]
    assert "editSensorLqi" in sheet
    assert "clear_link_quality" in sheet
    # Inside the same demo gate as the battery field, not beside it.
    assert sheet.index("const demo =") < sheet.index("editSensorLqi")
    assert "${demo ? `" in sheet


# -- repeaters -------------------------------------------------------------


def test_a_plug_that_joins_reads_as_a_success():
    """It joined and it will relay. "That is not a contact sensor" reads as a
    rejection and invites somebody to take it back to the shop."""
    assert "router:" in CABIN
    report = CABIN[CABIN.index("const PAIRING_REPORT"):CABIN.index("function pairingReport")]
    assert "Repeater added" in report
    assert "'ok'" in report.split("router:")[1].split("\n")[0]


def test_the_repeater_message_says_to_pair_sensors_after_it():
    """A sensor picks its route when it joins and Aqara devices are poor at
    changing their minds, so the order matters and the moment it matters is
    the moment the plug has just joined."""
    assert "it will relay for sensors near it" in CABIN
    assert "Pair those after it" in CABIN


def test_having_no_repeater_is_stated_rather_than_left_to_be_inferred():
    """The diagnosis nothing else surfaces. Per-sensor signal grades the last
    hop, so every sensor can look healthy while the network cannot reach past
    the one radio."""
    assert "function sensorMeshNote" in CABIN
    assert "No repeaters" in CABIN
    assert "talks straight to the USB stick" in CABIN


def test_the_mesh_note_stays_quiet_when_there_is_nothing_to_say():
    """On a simulated provider there is no radio, and on an empty installation
    it would be advice to go shopping for nothing."""
    start = CABIN.index("function sensorMeshNote")
    end = CABIN.index("\n  }\n", start)
    body = CABIN[start:end]
    assert "state.sensorSettings.simulated" in body
    assert "if (!sensors.length && !routers.length) return ''" in body


def test_repeaters_are_listed_not_merely_counted():
    # "Which one" is the question you have when deciding where the next goes.
    assert "sensor-router-list" in CABIN
    assert ".sensor-router-list" in CSS
    assert "router.description" in CABIN


def test_a_repeater_is_not_presented_as_something_this_app_controls():
    """It is a range extender here and nothing else. Implying otherwise would
    promise a switch that does not exist."""
    assert "are not controlled from here" in CABIN


def test_sensors_and_repeaters_arrive_in_one_request():
    """They are one answer — how much of the network is there — and a second
    request on every refresh would be paid on every WebSocket update."""
    assert "sensorNetwork" in CORE
    assert CORE.count("req('/api/sensors')") <= 2
    assert "sensorNetwork()" in CABIN


# ---------------------------------------------------------------------------
# The whole-house sensor count in System status
# ---------------------------------------------------------------------------

FRESH_SEEN = "2099-01-01T00:00:00+00:00"
STALE_SEEN = "2000-01-01T00:00:00+00:00"


def _counted(name, available=True, last_seen=FRESH_SEEN):
    return {
        "name": name, "kind": "window", "state": "closed",
        "available": available, "battery": 100, "last_seen_at": last_seen,
    }


def _zone(sensors, zone_id="1"):
    """A zone as the payload carries it once the feature is switched on."""
    return {
        "zone_id": zone_id, "name": f"Zone {zone_id}", "sensors": sensors,
        "sensor_summary": {"sensor_count": len(sensors), "open_count": 0,
                           "unavailable_count": 0, "warning_raised": False,
                           "state": "closed"},
    }


def _sensor_system_line(zones):
    """Run the real counter in node and return what System status would show.

    Written to a file rather than passed with ``node -e``: the lifted source
    is long enough to exceed the Windows command-line limit, and a test that
    cannot run on the machine somebody is using is a test that stops being
    read.
    """
    lifted = [re.search(r"const SENSOR_QUIET_HOURS = \d+;", CABIN).group(0)]
    for marker in ("function sensorIsStale", "function sensorSystemLine"):
        start = CABIN.index(marker)
        end = CABIN.index("\n  }\n", start) + len("\n  }\n")
        lifted.append(CABIN[start:end])
    script = "%s\nconsole.log(JSON.stringify(sensorSystemLine(%s)));" % (
        "\n".join(lifted), json.dumps(zones),
    )
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(script)
        path = handle.name
    try:
        result = subprocess.run(
            ["node", path], capture_output=True, text=True,
            encoding="utf-8", timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.strip())
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_no_sensor_row_at_all_when_the_feature_is_off():
    """A Nobo-only installation must gain nothing to explain. The zones
    payload carries no summary when sensors are off, and that absence - not
    state.sensorSettings, which is fetched for admins only - is what decides
    it. Reading the admin-only settings here would blank the row for every
    ordinary user instead."""
    assert _sensor_system_line([{"zone_id": "1", "name": "Kitchen"}]) is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_healthy_house_says_so_rather_than_staying_silent():
    line = _sensor_system_line([_zone([_counted("a"), _counted("b"), _counted("c")])])
    assert line == "3 (all reporting)"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_quiet_and_offline_are_counted_separately():
    """They are different facts and the earlier one is the useful one.

    Zigbee2MQTT will not call a battery device offline until it has been
    silent for twenty-five hours, so a battery pulled at breakfast still reads
    healthy at bedtime. Six hours of silence is the signal worth acting on,
    and collapsing the two would hide it behind the late one.
    """
    line = _sensor_system_line([_zone([
        _counted("fine"),
        _counted("hush", last_seen=STALE_SEEN),
        _counted("gone", available=False),
    ])])
    assert line == "3 (1 quiet, 1 offline)"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_everything_unavailable_is_one_failure_not_many():
    """Zigbee2MQTT stopping, or the broker going, marks every sensor
    unreachable at once. Reporting "15 (15 offline)" would send somebody
    hunting fifteen windows for a fault that is in a container."""
    line = _sensor_system_line([_zone([
        _counted("a", available=False), _counted("b", available=False),
    ])])
    assert line == "2 \u00b7 sensor system offline"
    assert "2 offline" not in line


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_sensor_that_is_offline_and_stale_is_counted_once():
    """Offline already implies silence, so counting it as quiet as well would
    make the two numbers add up to more than the house has."""
    line = _sensor_system_line([_zone([
        _counted("fine"), _counted("gone", available=False, last_seen=STALE_SEEN),
    ])])
    assert line == "2 (1 offline)"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_the_count_spans_the_whole_house_not_one_room():
    """The zone cards already answer this one room at a time; the point of
    the System status line is the building."""
    line = _sensor_system_line([
        _zone([_counted("a"), _counted("b", last_seen=STALE_SEEN)], zone_id="1"),
        _zone([_counted("c", available=False)], zone_id="2"),
    ])
    assert line == "3 (1 quiet, 1 offline)"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_switched_on_with_nothing_paired_says_so():
    assert _sensor_system_line([_zone([])]) == "On, none added yet"


def test_the_row_is_built_from_the_zones_everyone_receives():
    """state.sensorSettings is admin-only. Using it to decide this row would
    hide the count from exactly the people who are not going to go and read
    the Zigbee2MQTT log themselves."""
    start = CABIN.index("function sensorSystemLine")
    end = CABIN.index("\n  }\n", start)
    body = CABIN[start:end]
    # Comments are allowed to name the wrong source in order to warn about it;
    # only the code is being checked here.
    code = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    code = re.sub(r"//.*", "", code)
    assert "state.sensorSettings" not in code
    assert "sensor_summary" in code
