"""
The Port field under Alerts.

It used to look as though it did nothing: the box was a bare browser number
input among restyled fields, choosing SSL/TLS left it on 587, and a port the
server could not use was quietly replaced with 587. These tests hold the form
to what it now promises.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "app" / "static"
CABIN = (ROOT / "ui" / "cabin" / "cabin.js").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "cabin" / "cabin.css").read_text(encoding="utf-8")


def _slice(start, end_marker):
    i = CABIN.index(start)
    j = CABIN.index(end_marker, i)
    return CABIN[i:j + len(end_marker)]


def test_the_port_box_is_styled_like_its_neighbours():
    port = re.search(r'<input[^>]*id="ntPort"[^>]*>', CABIN, re.S)
    assert port, "the port input is missing"
    tag = port.group(0)
    assert 'type="text"' in tag and 'inputmode="numeric"' in tag
    assert '.field input[type="text"]' in CSS


def test_the_encryption_choices_do_not_claim_a_fixed_port():
    assert "'STARTTLS (usually port 587)'" in CABIN
    assert "'SSL/TLS (usually port 465)'" in CABIN
    assert "wireNotifyPort(box);" in CABIN


def test_an_unusable_port_is_refused_not_replaced():
    body = _slice("function readNotifyPort()", "\n  }\n")
    assert "65535" in body and "throw new Error" in body
    assert "port: readNotifyPort()," in CABIN
    # Reading the form must be inside the try, or the refusal escapes as an
    # unhandled rejection instead of a toast.
    save = _slice("async function saveNotifications(", "\n  }\n")
    assert save.index("try {") < save.index("readNotifyForm()")


def _run(steps):
    consts = _slice("const NOTIFY_PORTS", "new Set(Object.values(NOTIFY_PORTS));")
    wire = _slice("function wireNotifyPort(box) {", "\n  }\n")
    script = f"""
      {consts}
      {wire}
      const sec = {{ value: 'starttls' }};
      const port = {{ value: '587' }};
      const box = {{ querySelector: (s) => s === '#ntSec' ? sec : port }};
      wireNotifyPort(box);
      const out = [];
      for (const [kind, value] of {json.dumps(steps)}) {{
        if (kind === 'sec') {{ sec.value = value; sec.onchange(); }}
        else {{ port.value = value; port.oninput(); }}
        out.push([sec.value, port.value]);
      }}
      console.log(JSON.stringify(out));
    """
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_choosing_encryption_moves_a_standard_port():
    out = _run([["sec", "ssl"], ["sec", "none"], ["sec", "starttls"]])
    assert out == [["ssl", "465"], ["none", "25"], ["starttls", "587"]]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_a_port_somebody_typed_is_left_alone():
    out = _run([["port", "2525"], ["sec", "ssl"]])
    assert out[-1] == ["ssl", "2525"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")
def test_typing_a_port_that_means_one_thing_picks_the_encryption():
    out = _run([["port", "465"], ["port", "587"], ["port", "25"]])
    # 25 must not quietly switch encryption off.
    assert out == [["ssl", "465"], ["starttls", "587"], ["starttls", "25"]]
