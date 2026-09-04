"""Dates and times are written the way this part of the world writes them.

Nobø is sold mainly in Norway and the rest of the Nordics, where "30.08.2026"
is a date and "18:00" is a time. Two things were wrong before this: the trip
card wrote English day and month names whatever the installation's language,
and the classic interface's "last updated" clock followed the browser, so on a
US locale it read "7:30:15 PM" while every other time on screen was 24-hour.

The rules, and why they are rules rather than preferences:

* **The clock is always 24-hour.** The hub's own week profiles are "HHMM"
  strings and its handshake timestamp is "yyyyMMddHHmmss". 24-hour is the
  protocol's format. A 12-hour display would invent an ambiguity the system
  does not have, and "7:30" against a heating schedule is a genuinely
  dangerous thing to read wrong.
* **Temperatures are always Celsius.** ``API_Nobo.pdf`` states it outright:
  "temperatures are in celsius". There is no other unit to ask the hub for.

Everything else — the order of day and month, the language of their names —
follows a locale stored per installation, so every device in the house agrees.

These tests run the real ``core.js`` in Node, because the formatting is
JavaScript and asserting on it from Python would only test a reimplementation.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CORE_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "ui" / "shared" / "core.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is needed to run the browser code"
)


def run_core(script: str):
    """Evaluate a snippet with core.js loaded, and return its JSON result.

    core.js expects a browser, so the few globals it touches at load time are
    stubbed. Nothing else is faked — the formatting under test is the real
    implementation.
    """
    harness = f"""
    const fs = require('fs');
    global.window = {{ location: {{ href: '' }}, addEventListener() {{}} }};
    global.document = {{
      addEventListener() {{}}, querySelector: () => null,
      querySelectorAll: () => [], createElement: () => ({{ style: {{}}, classList: {{ add(){{}}, remove(){{}} }} }}),
      body: {{ appendChild() {{}} }},
    }};
    global.fetch = () => Promise.reject(new Error('no network in tests'));
    global.WebSocket = function () {{}};
    {CORE_JS.read_text(encoding='utf-8')}
    const result = (() => {{ {script} }})();
    console.log(JSON.stringify(result));
    """
    proc = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, timeout=60, encoding="utf-8"
    )
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestTheClockIsAlways24Hour:
    def test_an_evening_time_is_not_written_as_pm(self):
        """The regression: a US locale rendering 19:30 as "7:30 PM"."""
        for locale in ["en-US", "en-GB", "nb-NO", "sv-SE", ""]:
            out = run_core(f"""
                Nobo.setLocale({json.dumps(locale)});
                return Nobo.fmtWhen('2026-08-30T19:30:00');
            """)
            assert "19" in out, f"{locale or 'browser'} did not use a 24-hour clock: {out}"
            assert "PM" not in out.upper(), f"{locale or 'browser'} produced a 12-hour time: {out}"

    def test_midnight_and_noon_are_distinguishable(self):
        """00:00 and 12:00 are the pair a 12-hour clock confuses."""
        midnight = run_core("Nobo.setLocale('nb-NO'); return Nobo.fmtWhen('2026-08-30T00:00:00');")
        noon = run_core("Nobo.setLocale('nb-NO'); return Nobo.fmtWhen('2026-08-30T12:00:00');")
        assert "00" in midnight and "12" in noon
        assert midnight != noon

    def test_the_time_of_day_helper_is_24_hour_too(self):
        out = run_core("Nobo.setLocale('en-US'); return Nobo.fmtTimeOfDay('2026-08-30T23:05:00');")
        assert out.startswith("23"), out

    def test_schedule_clock_times_are_untouched_by_locale(self):
        """fmtClock renders the hub's own HHMM minutes and must never localise."""
        out = run_core("Nobo.setLocale('en-US'); return [Nobo.fmtClock(0), Nobo.fmtClock(19*60+30)];")
        assert out == ["00:00", "19:30"]


class TestDatesFollowTheChosenLocale:
    def test_norwegian_gives_norwegian_month_names(self):
        out = run_core("Nobo.setLocale('nb-NO'); return Nobo.fmtWhen('2026-08-30T18:00:00');")
        assert "aug" in out.lower(), out
        assert "30" in out

    def test_day_names_stay_english_whatever_the_region(self):
        """
        The day labels follow the interface's language, not the date format's.

        That setting decides how a date is *written* -- "30. aug. 2026" against
        "30 Aug 2026" -- and a Norwegian household wants Norwegian dates. It
        does not decide what language the application is in. The two only
        collide on the weekly schedule, where a weekday appears as a label
        rather than as part of a date, and "man. tir. ons." next to Comfort and
        Save schedule was the result.
        """
        for locale in ["nb-NO", "sv-SE", "de-DE", "en-GB"]:
            out = run_core(f"Nobo.setLocale({json.dumps(locale)}); return Nobo.dayNames('short');")
            names = dict(out)
            assert names["monday"] == "Mon", f"{locale}: {names['monday']}"
            assert names["saturday"] == "Sat", f"{locale}: {names['saturday']}"

    def test_dates_still_follow_the_region(self):
        """The other half of the same decision: this must not have changed."""
        out = run_core("Nobo.setLocale('nb-NO'); return Nobo.fmtWhen('2026-08-30T18:00:00');")
        assert "aug" in out.lower(), out
        assert "søn" in out.lower(), f"the weekday inside a date is still Norwegian: {out}"

    def test_english_still_gives_english(self):
        out = run_core("Nobo.setLocale('en-GB'); return Nobo.dayNames('short');")
        names = dict(out)
        assert names["monday"] == "Mon"
        assert names["sunday"] == "Sun"

    def test_day_names_line_up_with_their_keys(self):
        """An off-by-one here would silently mislabel every row of every
        weekly schedule — the worst kind of bug, because it looks fine."""
        for locale in ["nb-NO", "en-GB", "sv-SE", "de-DE"]:
            out = run_core(f"Nobo.setLocale({json.dumps(locale)}); return Nobo.dayNames('long');")
            names = dict(out)
            # Compare against a date known to be that weekday: 31 Dec 2023 was
            # a Sunday, so 1 Jan 2024 was a Monday. English, because that is
            # now what dayNames answers whatever the region.
            monday = run_core("""
                return new Intl.DateTimeFormat('en-GB', { weekday: 'long' })
                  .format(new Date(2024, 0, 1));
            """)
            assert names["monday"] == monday, f"{locale}: {names['monday']} != {monday}"

    def test_switching_locale_changes_the_output(self):
        both = run_core("""
            Nobo.setLocale('nb-NO');
            const no = Nobo.fmtWhen('2026-08-30T18:00:00');
            Nobo.setLocale('en-GB');
            const en = Nobo.fmtWhen('2026-08-30T18:00:00');
            return [no, en];
        """)
        assert both[0] != both[1], both

    def test_an_unusable_locale_falls_back_instead_of_throwing(self):
        """A bad tag must not take the page down — dates in the browser's own
        format are a perfectly acceptable outcome."""
        out = run_core("Nobo.setLocale('zz-ZZ-nonsense'); return Nobo.fmtWhen('2026-08-30T18:00:00');")
        assert out, "formatting returned nothing for an unusable locale"


class TestInputValuesStayInTheHtmlFormat:
    """<input type="date"> and <input type="time"> take yyyy-mm-dd and HH:MM
    whatever the user sees. Localising these would break the controls."""

    def test_date_and_time_round_trip_unlocalised(self):
        out = run_core("""
            Nobo.setLocale('nb-NO');
            const iso = Nobo.toIsoInstant('2026-08-30', '18:30');
            return Nobo.fromIsoInstant(iso);
        """)
        assert out["date"] == "2026-08-30"
        assert out["time"] == "18:30"

    def test_the_round_trip_is_locale_independent(self):
        results = [
            run_core(f"""
                Nobo.setLocale({json.dumps(loc)});
                return Nobo.fromIsoInstant(Nobo.toIsoInstant('2026-08-30', '07:05'));
            """)
            for loc in ["nb-NO", "en-US", "fi-FI", ""]
        ]
        assert all(r == results[0] for r in results), results
        assert results[0]["time"] == "07:05"


class TestTemperatureStaysCelsius:
    def test_nothing_offers_another_unit(self):
        source = CORE_JS.read_text(encoding="utf-8")
        assert "fahrenheit" not in source.lower()
        assert "°F" not in source

    def test_no_interface_writes_a_fahrenheit_symbol(self):
        """The unit is only ever written next to a number by the interfaces,
        so that is where a stray °F would appear."""
        ui = CORE_JS.parent.parent
        for path in list(ui.rglob("*.js")) + list(ui.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            assert "°F" not in text, f"{path.name} writes a Fahrenheit symbol"

    def test_the_temperature_formatter_is_a_plain_number(self):
        """fmtTemp deliberately returns just the number — callers add "°C" —
        so it must not start localising the unit or the decimal separator.
        A comma there would break the numeric inputs it also feeds."""
        assert run_core("Nobo.setLocale('nb-NO'); return Nobo.fmtTemp(21.5);") == "21.5"
        assert run_core("Nobo.setLocale('en-US'); return Nobo.fmtTemp(21.5);") == "21.5"
