"""Static checks on the Cabin contact-sensor interface.

These are contract tests, not screenshots: they pin down the things that have
actually gone wrong here before — a control that silently stops matching the
API, an escaping hole on a user-supplied name, sensor wording leaking into
Classic — and leave visual judgement to a person with the app open.
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
