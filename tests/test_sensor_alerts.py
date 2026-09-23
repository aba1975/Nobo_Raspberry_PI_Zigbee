"""Alerts about the sensors themselves, and about a window nobody has shut.

These are the only alerts in the system whose subject is the *monitoring
equipment* rather than the heating, because they are the only part of this
installation that can report its own health — a Nobø component's ``Status``
field is permanently 0, so a heater cannot say it is unwell.

Every one of them is time-based, which is what makes them worth testing
carefully: a condition that becomes true because nothing happened cannot be
noticed by waiting for something to happen. The deadline tests below are the
point of this file.
"""

import time

import pytest

import server
from notifications import EVENT_TYPES


@pytest.fixture
def sent(monkeypatch):
    """Capture what would have been emailed, without a mail server."""
    posted = []

    def fake_notify(event_type, subject, body, severity="warning", key=None):
        posted.append({
            "type": event_type, "subject": subject,
            "body": body, "severity": severity,
        })
        return True

    monkeypatch.setattr(server.notifier, "notify", fake_notify)
    # Conditions are level-triggered and remembered, so a test must not inherit
    # another test's raised state.
    server.notifier._conditions.clear()
    return posted


def _aggregate(zone_id="1", open_started_at=None):
    class _Agg:
        pass

    agg = _Agg()
    agg.zone_id = zone_id
    agg.open_started_at = open_started_at
    return agg


def _result(zones):
    class _Result:
        pass

    result = _Result()
    result.zones = zones
    return result


def _snapshot(sensor_id="0x1", name="Kitchen Window", last_seen=None,
              battery=100, zone_id="1", state="closed"):
    from datetime import datetime, timezone

    from sensor_provider import ContactSnapshot, ContactState, SensorKind

    stamp = datetime.fromtimestamp(
        last_seen if last_seen is not None else time.time(), timezone.utc
    )
    return ContactSnapshot(
        sensor_id=sensor_id,
        provider_id=f"test:{sensor_id}",
        name=name,
        zone_id=zone_id,
        state=ContactState(state),
        available=True,
        battery=battery,
        changed_at=stamp,
        last_seen_at=stamp,
        kind=SensorKind.WINDOW,
    )


# -- the alerts exist and are off ------------------------------------------


@pytest.mark.parametrize("key", [
    "contact_open_long", "sensor_quiet", "sensor_battery_low", "sensor_all_quiet",
])
def test_every_new_alert_is_off_by_default(key):
    """The module's own rule: an alert nobody wants to read is not harmless,
    it teaches people to ignore the ones that matter."""
    assert key in EVENT_TYPES
    assert EVENT_TYPES[key]["default"] is False


@pytest.mark.parametrize("key", [
    "contact_open_long", "sensor_quiet", "sensor_battery_low", "sensor_all_quiet",
])
def test_the_sensor_alerts_are_hidden_when_sensors_are_off(key, monkeypatch):
    """A Nobø-only installation must not be offered alerts about equipment it
    does not have."""
    from sensor_persistence import SensorSettings

    # SensorSettings is frozen, so the whole object is replaced rather than
    # poked — which is also how the application changes it.
    monkeypatch.setattr(server, "sensor_settings", SensorSettings(enabled=False))
    out = server._filter_sensor_notification_settings({
        "events": {key: False}, "event_types": {key: EVENT_TYPES[key]},
    })
    assert key not in out["event_types"]
    assert key not in out["events"]


# -- a door left open ------------------------------------------------------


def test_nothing_is_said_before_the_day_is_up(sent, monkeypatch):
    monkeypatch.setattr(server, "sensor_snapshots", [])
    opened = time.time() - 3600  # one hour

    server._evaluate_sensor_alerts(
        _result({"1": _aggregate(open_started_at=opened)}), {"1": "Kitchen"}
    )

    assert [m for m in sent if m["type"] == "contact_open_long"] == []


def test_a_day_later_it_says_so_once(sent, monkeypatch):
    monkeypatch.setattr(server, "sensor_snapshots", [])
    opened = time.time() - (25 * 3600)
    result = _result({"1": _aggregate(open_started_at=opened)})

    server._evaluate_sensor_alerts(result, {"1": "Kitchen"})
    server._evaluate_sensor_alerts(result, {"1": "Kitchen"})  # still open

    escalations = [m for m in sent if m["type"] == "contact_open_long"]
    assert len(escalations) == 1, "a continuing state must be reported once"
    assert "Kitchen" in escalations[0]["subject"]
    assert "25 hours" in escalations[0]["subject"]


def test_the_escalation_names_the_case_that_looks_identical(sent, monkeypatch):
    """A sensor knocked off its frame reads open for ever and is
    indistinguishable from a genuinely open window, so the email says so
    rather than sending somebody to the cabin for nothing."""
    monkeypatch.setattr(server, "sensor_snapshots", [])
    server._evaluate_sensor_alerts(
        _result({"1": _aggregate(open_started_at=time.time() - 25 * 3600)}),
        {"1": "Kitchen"},
    )
    body = [m for m in sent if m["type"] == "contact_open_long"][0]["body"]
    assert "moved or taken off its frame" in body


def test_closing_it_does_not_send_a_second_recovery(sent, monkeypatch):
    """`contact_closed` already reports the recovery. Two emails saying the
    window is shut is the same news twice."""
    monkeypatch.setattr(server, "sensor_snapshots", [])
    opened = time.time() - 25 * 3600
    server._evaluate_sensor_alerts(
        _result({"1": _aggregate(open_started_at=opened)}), {"1": "Kitchen"}
    )
    sent.clear()

    server._evaluate_sensor_alerts(
        _result({"1": _aggregate(open_started_at=None)}), {"1": "Kitchen"}
    )

    assert sent == []


# -- the deadline, which is the whole reason this is not a simple loop ------


def test_an_open_window_schedules_its_own_escalation(monkeypatch):
    """The automation loop sleeps until something *reports*, and a window left
    open in an empty cabin reports nothing. Without a deadline the escalation
    would fire whenever the next unrelated thing happened — possibly days."""
    monkeypatch.setattr(server, "sensor_snapshots", [])
    opened = time.time() - 3600

    deadline = server._evaluate_sensor_alerts(
        _result({"1": _aggregate(open_started_at=opened)}), {"1": "Kitchen"}
    )

    assert deadline is not None
    assert deadline == pytest.approx(opened + server.SENSOR_OPEN_ESCALATE_SECONDS)


def test_a_reporting_sensor_schedules_its_own_silence(monkeypatch):
    last = time.time() - 60
    monkeypatch.setattr(server, "sensor_snapshots", [_snapshot(last_seen=last)])

    deadline = server._evaluate_sensor_alerts(_result({}), {})

    assert deadline == pytest.approx(last + server.SENSOR_QUIET_SECONDS)


def test_nothing_pending_asks_for_no_wake_up(monkeypatch):
    """An idle house with nothing open and everything reporting must not be
    made to poll."""
    monkeypatch.setattr(server, "sensor_snapshots", [])
    assert server._evaluate_sensor_alerts(_result({}), {}) is None


# -- the sensors' own health -----------------------------------------------


def test_a_quiet_sensor_is_reported(sent, monkeypatch):
    quiet = time.time() - (7 * 3600)
    monkeypatch.setattr(server, "sensor_snapshots", [_snapshot(last_seen=quiet)])

    server._evaluate_sensor_alerts(_result({}), {"1": "Kitchen"})

    alerts = [m for m in sent if m["type"] == "sensor_quiet"]
    assert len(alerts) == 1
    assert "Kitchen Window" in alerts[0]["subject"]


def test_a_quiet_sensor_says_what_is_still_being_believed(sent, monkeypatch):
    """The danger is not the silence, it is that the last thing it said is
    still driving the left-open warning and any heating rule."""
    quiet = time.time() - (7 * 3600)
    monkeypatch.setattr(
        server, "sensor_snapshots", [_snapshot(last_seen=quiet, state="open")]
    )

    server._evaluate_sensor_alerts(_result({}), {"1": "Kitchen"})

    body = [m for m in sent if m["type"] == "sensor_quiet"][0]["body"]
    assert "is being believed" in body
    assert "open" in body


def test_a_recently_heard_sensor_is_not_reported(sent, monkeypatch):
    monkeypatch.setattr(
        server, "sensor_snapshots", [_snapshot(last_seen=time.time() - 60)]
    )
    server._evaluate_sensor_alerts(_result({}), {})
    assert [m for m in sent if m["type"] == "sensor_quiet"] == []


def test_all_of_them_at_once_is_one_alert_not_many(sent, monkeypatch):
    """Nineteen silent sensors is not nineteen flat batteries, it is one
    stopped container — and nineteen emails about it would be worse than one."""
    quiet = time.time() - (7 * 3600)
    monkeypatch.setattr(server, "sensor_snapshots", [
        _snapshot(sensor_id=f"0x{n}", name=f"Window {n}", last_seen=quiet)
        for n in range(5)
    ])

    server._evaluate_sensor_alerts(_result({}), {})

    assert len([m for m in sent if m["type"] == "sensor_all_quiet"]) == 1
    assert [m for m in sent if m["type"] == "sensor_quiet"] == [], (
        "individual alerts must be held back while the system alert is raised"
    )


def test_the_system_alert_says_the_heating_is_unaffected(sent, monkeypatch):
    """It runs over a separate connection to the hub, and somebody reading
    'no sensor has reported' at midnight should not fear for the pipes."""
    quiet = time.time() - (7 * 3600)
    monkeypatch.setattr(server, "sensor_snapshots", [
        _snapshot(sensor_id=f"0x{n}", last_seen=quiet) for n in range(3)
    ])

    server._evaluate_sensor_alerts(_result({}), {})

    body = [m for m in sent if m["type"] == "sensor_all_quiet"][0]["body"]
    assert "heating is unaffected" in body


def test_one_sensor_of_several_still_reports_individually(sent, monkeypatch):
    quiet = time.time() - (7 * 3600)
    monkeypatch.setattr(server, "sensor_snapshots", [
        _snapshot(sensor_id="0x1", name="Quiet one", last_seen=quiet),
        _snapshot(sensor_id="0x2", name="Fine one", last_seen=time.time() - 60),
    ])

    server._evaluate_sensor_alerts(_result({}), {})

    assert [m for m in sent if m["type"] == "sensor_all_quiet"] == []
    individual = [m for m in sent if m["type"] == "sensor_quiet"]
    assert len(individual) == 1
    assert "Quiet one" in individual[0]["subject"]


def test_a_single_sensor_going_quiet_is_not_a_system_failure(sent, monkeypatch):
    """With one sensor paired, "all of them" and "that one" are the same set,
    and the specific alert is the more useful of the two."""
    monkeypatch.setattr(
        server, "sensor_snapshots", [_snapshot(last_seen=time.time() - 7 * 3600)]
    )

    server._evaluate_sensor_alerts(_result({}), {})

    assert [m for m in sent if m["type"] == "sensor_all_quiet"] == []
    assert len([m for m in sent if m["type"] == "sensor_quiet"]) == 1


# -- battery ---------------------------------------------------------------


def test_a_low_battery_is_reported_with_its_level(sent, monkeypatch):
    monkeypatch.setattr(server, "sensor_snapshots", [_snapshot(battery=15)])

    server._evaluate_sensor_alerts(_result({}), {})

    alerts = [m for m in sent if m["type"] == "sensor_battery_low"]
    assert len(alerts) == 1
    assert "15%" in alerts[0]["subject"]


def test_a_healthy_battery_says_nothing(sent, monkeypatch):
    monkeypatch.setattr(server, "sensor_snapshots", [_snapshot(battery=100)])
    server._evaluate_sensor_alerts(_result({}), {})
    assert [m for m in sent if m["type"] == "sensor_battery_low"] == []


def test_an_unreported_battery_is_not_treated_as_empty(sent, monkeypatch):
    """A sensor that has not yet sent a level reads None, and None is not
    zero — these devices can take a day to volunteer one."""
    monkeypatch.setattr(server, "sensor_snapshots", [_snapshot(battery=None)])
    server._evaluate_sensor_alerts(_result({}), {})
    assert [m for m in sent if m["type"] == "sensor_battery_low"] == []


def test_the_battery_threshold_matches_the_one_on_screen():
    """The email and the badge cannot disagree about what "low" means."""
    cabin = (
        server.Path(server.__file__).resolve().parent
        / "static" / "ui" / "cabin" / "cabin.js"
    ).read_text(encoding="utf-8")
    assert f"<= {server.SENSOR_BATTERY_LOW_PERCENT}" in cabin


def test_the_quiet_threshold_matches_the_one_on_screen():
    cabin = (
        server.Path(server.__file__).resolve().parent
        / "static" / "ui" / "cabin" / "cabin.js"
    ).read_text(encoding="utf-8")
    hours = server.SENSOR_QUIET_SECONDS // 3600
    assert f"SENSOR_QUIET_HOURS = {hours}" in cabin


# -- the From address ------------------------------------------------------


def test_the_configured_from_address_is_the_one_sent():
    """It reached the message correctly all along — the confusion came from
    Gmail rewriting it, not from this code — so pin it."""
    import notifications

    captured = {}

    class _FakeServer:
        def ehlo(self): pass
        def starttls(self, context=None): pass
        def login(self, u, p): pass
        def send_message(self, msg): captured["from"] = msg["From"]
        def quit(self): pass

    original = notifications.smtplib.SMTP
    notifications.smtplib.SMTP = lambda *a, **k: _FakeServer()
    try:
        notifications._send_email_blocking(
            {"email": {
                "host": "smtp.example.com", "port": 587,
                "to_addrs": ["someone@example.com"],
                "from_addr": "alerts@example.com",
                "username": "account@gmail.com", "password": "x",
                "security": "starttls",
            }},
            "Subject", "Body",
        )
    finally:
        notifications.smtplib.SMTP = original

    assert captured["from"] == "alerts@example.com", (
        "the address the user typed must be the address on the message"
    )


def test_the_from_field_warns_that_a_provider_may_overrule_it():
    """The field had no hint at all, so it silently promised something the
    provider can ignore — which is exactly how it was reported."""
    cabin = (
        server.Path(server.__file__).resolve().parent
        / "static" / "ui" / "cabin" / "cabin.js"
    ).read_text(encoding="utf-8")
    hint = cabin[cabin.index('id="ntFrom"'):]
    hint = hint[:hint.index("</label>")]
    assert "may overrule this" in hint
    assert "Send mail as" in hint
