"""Two faults found in the cabin, on a real hub, that no test had caught.

Both were invisible to the existing suite for the same reason: the interface
and the API were checked separately, and each was self-consistent.  These tie
them together, and run the real rendering function rather than asserting on
source text where they can.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CABIN = (ROOT / "app" / "static" / "ui" / "cabin" / "cabin.js").read_text(encoding="utf-8")
CORE = (ROOT / "app" / "static" / "ui" / "shared" / "core.js").read_text(encoding="utf-8")
CLASSIC = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "ui" / "cabin" / "cabin.css").read_text(encoding="utf-8")
SERVER = (ROOT / "app" / "server.py").read_text(encoding="utf-8")


# -- moving a heater --------------------------------------------------------


def _move_body_field() -> str:
    """The field name the API actually requires, read from its own model."""
    block = SERVER[SERVER.index("class DeviceMove(BaseModel):"):]
    return re.search(r"\n    (\w+):", block).group(1)


def test_the_move_request_uses_the_field_the_api_requires():
    """The fault, exactly: the Cabin interface sent ``zone_id`` where the API
    requires ``new_zone_id``, so every move was rejected with a 422 before it
    reached the hub — while a toast said "Heater moved".

    Read from the Pydantic model rather than written out here, so renaming the
    field on the server fails this test instead of silently breaking the
    interface again.
    """
    field = _move_body_field()
    assert field == "new_zone_id"
    assert field in CORE, "the shared client does not send what the API wants"


def test_the_caller_cannot_choose_the_field_name():
    """It went wrong because the body was assembled by the caller. Passing the
    id and building the body in one place removes the whole class of fault."""
    # \b does not help here: "removeDevice:" ends in "moveDevice:", so an
    # unanchored search silently reads the wrong function's signature.
    call = re.search(r"(?<![a-zA-Z])moveDevice:\s*\(([^)]*)\)", CORE).group(1)
    assert "body" not in call, "the caller is still handing over a whole body"
    assert "zoneId" in call
    assert "Nobo.api.moveDevice(serial, zoneId)" in CABIN


def test_the_classic_interface_was_right_and_stays_right():
    """Classic always sent the correct field, which is why moving worked there
    and not here. Worth pinning so a tidy-up does not level them downwards."""
    assert "new_zone_id: newZoneId" in CLASSIC


def test_no_interface_sends_the_old_field_name():
    for name, text in (("cabin.js", CABIN), ("core.js", CORE), ("app.js", CLASSIC)):
        assert "{ zone_id:" not in text, f"{name} still sends zone_id"


# -- a zone with nothing in it ----------------------------------------------


def _render_zone(zone):
    """Run the real zone-card renderer in node and return its markup."""
    wanted = ["function zoneRow"]
    lifted = []
    for marker in wanted:
        start = CABIN.index(marker)
        end = CABIN.index("\n  }\n", start) + len("\n  }\n")
        lifted.append(CABIN[start:end])
    script = """
      const esc = (v) => String(v == null ? '' : v);
      const setpointKey = (z) => z.__key === undefined ? 'comfort_temperature' : z.__key;
      const sensorZoneHeadline = () => '';
      const sensorRuleLine = () => null;
      const sensorStatus = () => '';
      const zoneSensorStrips = () => '';
      const zoneNeedsSensorAttention = () => false;
      const climateOf = (z) => (z.climate && z.climate.sensor_count ? z.climate : null);
      const fmtHumidity = (v) => `${Math.round(v)} %%`;
      const zoneOverrideBadge = () => '';
      const renderSetpointDriftBadge = () => '';
      const Nobo = {
        MODES: { normal: { label: 'Schedule' }, comfort: { label: 'Comfort' },
                 eco: { label: 'Eco' }, away: { label: 'Away' } },
        effectiveMode: (z) => z.current_mode || 'normal',
        targetTemp: (z) => z.comfort_temperature == null ? null : z.comfort_temperature,
        bigTemp: (v) => `${v}`,
        fmtTemp: (v) => `${v}`,
        fmtDuration: (v) => `${v} seconds`,
        deviceImg: () => '<img/>',
        icon: (n) => `<svg data-icon="${n}"/>`,
      };
      %s
      console.log(zoneRow(%s));
    """ % ("\n".join(lifted), json.dumps(zone))
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _zone(**over):
    base = {
        "zone_id": "7", "name": "Loft", "components": [],
        "current_mode": "normal", "comfort_temperature": 21,
        "current_temperature": None, "supports_temp_adjust": False,
        "has_manual_devices": False,
    }
    base.update(over)
    return base


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_an_empty_zone_says_it_is_empty():
    """Reported from the cabin: a zone created with nothing in it claimed
    "Set on heater" and "Dial sets the temperature", which sends somebody to
    look for a dial that does not exist. The official app calls it empty.

    The server cannot distinguish the two cases for us — with no components,
    "does anything support adjustment?" is false whether the room is empty or
    full of dial-only heaters. The distinguishing fact is what it contains.
    """
    markup = _render_zone(_zone())

    assert "Empty" in markup, markup
    assert "Set on heater" not in markup
    assert "Dial sets the temperature" not in markup
    assert "No heater or sensor" in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_dial_only_zone_still_says_where_the_dial_is():
    """The other half of the same distinction: this room does have a heater,
    it simply cannot be adjusted from here."""
    markup = _render_zone(_zone(components=["186100000001"]))

    assert "Set on heater" in markup, markup
    assert "Dial sets the temperature" in markup
    assert ">Empty<" not in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_an_ordinary_zone_is_untouched():
    markup = _render_zone(_zone(
        components=["186100000001"], supports_temp_adjust=True,
        current_temperature=19.5,
    ))

    assert "Set to" in markup
    assert ">Empty<" not in markup
    assert "Set on heater" not in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_an_empty_zone_offers_no_temperature_to_step():
    """There is nothing for +/- to act on, and the card already omits them for
    any room it cannot adjust. Pinned because "Empty" would be a poor label
    next to two live buttons."""
    markup = _render_zone(_zone())
    assert "data-step" not in markup


def test_the_empty_badge_is_quieter_than_the_dial_one():
    assert ".badge-empty" in CSS
    assert "var(--ink-faint)" in CSS.split(".badge-empty")[1][:200]


# -- rooms watched by sensors ------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_room_with_only_sensors_is_monitoring_not_empty():
    """A bedroom with a window contact and no heater is a first-class
    monitoring-only room. "Empty" invited somebody to add a heater it was
    never meant to have."""
    markup = _render_zone(_zone(sensors=[{"sensor_id": "s1"}]))

    assert "Monitoring only" in markup, markup
    assert ">Empty<" not in markup
    assert "No heater or sensor" not in markup
    assert "Set on heater" not in markup
    assert "data-step" not in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_room_thermometer_fills_in_the_missing_temperature():
    """Most Nobø receivers measure nothing, so the card said "No sensor". With
    an Aqara thermometer in the room the server fills the reading in, and the
    card shows it with the humidity beside it."""
    markup = _render_zone(_zone(
        components=["186100000001"], supports_temp_adjust=True,
        current_temperature=21.3, temperature_source="sensor",
        climate={"sensor_count": 1, "humidity": 45.2, "stale_after_seconds": 10800},
    ))

    assert "Actual" in markup, markup
    assert "21.3" in markup
    assert "now 21.3" not in markup
    assert "45 %" in markup
    assert "No sensor" not in markup
    assert "room thermometer" in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_heaters_own_reading_is_also_labelled_actual():
    """The set point is the big number and the measurement sits under it, so
    the measurement says what it is in words rather than by size alone."""
    markup = _render_zone(_zone(
        components=["186100000001"], supports_temp_adjust=True,
        current_temperature=19.5, temperature_source="hub",
    ))
    assert "Set to" in markup
    assert 'class="set-actual"' in markup
    assert "Actual" in markup and "19.5" in markup
    assert "measured by the heater" in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_monitoring_room_leads_with_its_actual_temperature():
    """A room with no heater has nothing to be set to, so the reading is the
    headline there, still called Actual."""
    markup = _render_zone(_zone(
        current_temperature=16.2, temperature_source="sensor",
        climate_sensors=[{"sensor_id": "t1"}],
        climate={"sensor_count": 1, "humidity": 55.0, "stale_after_seconds": 10800},
    ))
    assert "Monitoring only" in markup, markup
    assert '<span class="set-label">Actual</span>' in markup
    assert "set-value-actual" in markup and "16.2" in markup
    assert "Set to" not in markup
    assert "1 sensor" in markup and "55 %" in markup


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_silent_thermometer_is_not_the_same_as_no_thermometer():
    """A thermometer whose readings have gone stale is a fault worth seeing,
    not the ordinary "this room cannot measure" state."""
    markup = _render_zone(_zone(
        components=["186100000001"], supports_temp_adjust=True,
        climate={"sensor_count": 1, "humidity": None, "stale_after_seconds": 10800},
    ))

    assert "No recent reading" in markup, markup
    assert ">No sensor<" not in markup
