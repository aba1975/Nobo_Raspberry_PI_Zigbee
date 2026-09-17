"""What ``scripts/zigbee-map.sh`` makes of a network map.

The script answers a question the interface deliberately cannot: link quality
in the UI grades the *last hop*, so a sensor reporting through a repeater looks
healthy however far it is from the Pi.  That is the right number for "does this
spot need a repeater?" and the wrong one for "is there a repeater at all?".

Its summary is the part a person acts on — it is what says "buy a plug" — so
the parsing is exercised here against payloads shaped like Zigbee2MQTT's,
rather than trusted because it ran once.  The link direction is the detail most
easily got backwards: Zigbee2MQTT builds each link from a device's neighbour
table, so ``source`` is the neighbour that was heard and ``target`` is the
device that heard it.  Reading those the other way round would name every
sensor as its own parent and look entirely plausible.
"""

import json
import os
import subprocess
import tempfile
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "zigbee-map.sh"
MARKER = "python3 - \"$RAW_FILE\" <<'PY'"

COORDINATOR = {"ieeeAddr": "0xC", "friendlyName": "Coordinator", "type": "Coordinator"}
ROUTER = {"ieeeAddr": "0xR", "friendlyName": "Hall plug", "type": "Router"}


def _summary_source() -> str:
    """The summary is a heredoc inside the shell script; lift it out and run it.

    Testing the Python where it lives keeps one copy of it, so the test cannot
    pass against a version of the logic the script does not use.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index(MARKER) + len(MARKER)
    return text[start:text.index("\nPY\n", start)]


def _run(nodes, links, status="ok", error=None):
    """The summary takes the path of a file holding one Zigbee2MQTT reply."""
    message = {"status": status, "data": {"value": {"nodes": nodes, "links": links}}}
    if error is not None:
        message["error"] = error
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(message, handle)
        path = handle.name
    try:
        return subprocess.run(
            [sys.executable, "-c", _summary_source(), path],
            capture_output=True, text=True, timeout=30,
        )
    finally:
        os.unlink(path)


def _sensor(addr, name):
    return {"ieeeAddr": addr, "friendlyName": name, "type": "EndDevice"}


def _link(source, target, quality=None):
    link = {"source": {"ieeeAddr": source}, "target": {"ieeeAddr": target}}
    if quality is not None:
        link["linkquality"] = quality
    return link


def summarise(nodes, links):
    result = _run(nodes, links)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_a_network_of_only_sensors_is_named_as_having_no_mesh():
    """The finding this exists to produce.  A coordinator and nothing but
    battery sensors is a hub and spokes, and every weak sensor is weak for the
    same reason — which no amount of per-sensor signal reading reveals."""
    out = summarise(
        [COORDINATOR, _sensor("0xA", "Bathroom")], [_link("0xA", "0xC", 130)]
    )
    assert "Routers     : 0" in out
    assert "No routers." in out
    assert "mains-powered" in out


def test_a_sensor_reporting_straight_to_the_dongle_is_marked_as_such():
    out = summarise(
        [COORDINATOR, _sensor("0xA", "Bathroom")], [_link("0xA", "0xC", 130)]
    )
    assert "Bathroom" in out
    assert "direct to coordinator" in out
    assert "LQI 130 good" in out


def test_a_sensor_is_credited_to_the_router_it_chose():
    out = summarise(
        [COORDINATOR, ROUTER, _sensor("0xA", "Bathroom")], [_link("0xA", "0xR", 88)]
    )
    assert "via Hall plug" in out
    assert "LQI 88 fair" in out
    assert "direct to coordinator" not in out


def test_the_strongest_reported_parent_wins():
    """A sensor can appear in several neighbour tables at once."""
    out = summarise(
        [COORDINATOR, ROUTER, _sensor("0xA", "Bathroom")],
        [_link("0xA", "0xC", 30), _link("0xA", "0xR", 140)],
    )
    assert "via Hall plug" in out
    assert "LQI 140 good" in out


@pytest.mark.parametrize(
    "quality, verdict", [(0, "weak"), (49, "weak"), (50, "fair"), (99, "fair"),
                         (100, "good"), (255, "good")],
)
def test_the_bands_match_the_ones_the_interface_uses(quality, verdict):
    """Two places grading the same number differently would be worse than
    either grading alone."""
    out = summarise(
        [COORDINATOR, _sensor("0xA", "Bathroom")], [_link("0xA", "0xC", quality)]
    )
    assert f"LQI {quality} {verdict}" in out


def test_an_unscored_link_is_not_quietly_called_fair():
    """A link can be reported with no quality at all.  The absent value was
    being carried as -1 and fell through the comparisons into "fair", which
    invents a measurement — the one thing this must not do."""
    out = summarise(
        [COORDINATOR, ROUTER, _sensor("0xA", "Bathroom")], [_link("0xA", "0xR")]
    )
    assert "quality not reported" in out
    assert "fair" not in out
    assert "LQI -1" not in out


def test_a_sleeping_sensor_missing_from_every_table_is_not_reported_as_a_fault():
    """A scan reads neighbour tables, and a sleeping contact sensor is often in
    none of them.  Presented as a problem it would send somebody up a ladder."""
    out = summarise(
        [COORDINATOR, _sensor("0xA", "Bathroom"), _sensor("0xB", "Bedroom")],
        [_link("0xA", "0xC", 120)],
    )
    assert "Bedroom" in out
    assert "not heard during the scan" in out
    assert "it is not a fault" in out


def test_an_empty_network_does_not_advise_buying_a_repeater_for_nothing():
    """Every sensor had just been unpaired.  "One plug will help most" is not
    the advice for a network with nothing in it."""
    out = summarise([COORDINATOR], [])
    assert "End devices : 0" in out
    assert "nothing to route yet" in out
    assert "will help most" not in out
    assert "Pair a mains-powered plug or" in out


def test_a_refused_scan_says_so_and_fails():
    result = _run([], [], status="error", error="Failed to execute LQI scan")
    assert result.returncode != 0
    assert "refused the scan" in result.stdout
    assert "Failed to execute LQI scan" in result.stdout


def test_a_device_with_no_friendly_name_still_appears():
    """Zigbee2MQTT falls back to the address, and so must this — a sensor
    missing from the list reads as a sensor that is not there."""
    out = summarise(
        [COORDINATOR, {"ieeeAddr": "0x00158d0001a2b3c4", "type": "EndDevice"}],
        [_link("0x00158d0001a2b3c4", "0xC", 90)],
    )
    assert "0x00158d0001a2b3c4" in out


def test_the_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
