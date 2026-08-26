"""
Tests for notifications — telling somebody when the heating needs attention.

The feature exists because a cabin stands empty: nobody notices a problem until
the damage is done. So the tests are written around the two ways this could be
worse than useless — **staying silent** when something is wrong, and **crying
wolf** until the owner filters the alerts into a folder — plus the hard limits
of the hardware, which are what the feature had to be rebuilt around.

Nothing here talks to a mail server. ``send_impl`` is replaced by a list append,
so what is asserted is "would this have been sent, and what would it have said".
"""

import os
import sys
import time

os.environ.setdefault("NOBO_DEMO", "true")

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import notifications
import notify_watch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(notifications, "DATA_DIR", tmp_path)
    monkeypatch.setattr(notifications, "NOTIFICATIONS_FILE", tmp_path / "notifications.json")
    yield


class FakeClock:
    """A clock the test drives, so "for 24 hours" does not take 24 hours."""

    def __init__(self, start=1_000_000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def sent():
    return []


@pytest.fixture
def notifier(sent):
    # Every event is off by default now, so a test fixture has to opt in - which
    # is itself worth having, because it means a test can never pass by accident
    # on a default that later changes.
    n = notifications.Notifier(settings=notifications._merge({
        "enabled": True,
        "email": {"host": "mail.example.com", "to_addrs": ["me@example.com"],
                  "from_addr": "pi@example.com"},
        "events": {k: True for k in notifications.EVENT_TYPES},
        "min_minutes_between": 0,
    }))
    n.send_impl = lambda cfg, subject, body: sent.append((subject, body))
    return n


@pytest.fixture
def watcher(notifier):
    clock = FakeClock()
    w = notify_watch.ZoneWatcher(notifier=notifier, now_fn=clock)
    w.clock = clock
    return w


def zone(zone_id="1", name="Large Bathroom", mode="comfort", temp=None,
         comfort=21.0, eco=17.0, schedule_mode=None):
    """A zone as the server reports it.

    ``temp`` defaults to None because that is what almost all real hardware
    reports: only the SW4 has a thermometer, and it is no longer sold.
    """
    return {
        "zone_id": zone_id, "name": name, "current_mode": mode,
        "current_temperature": temp, "comfort_temperature": comfort,
        "eco_temperature": eco, "away_temperature": 7.0,
        "schedule_mode": schedule_mode,
    }


def flush(sent):
    """Sends happen on a worker thread; give them a moment to land."""
    for _ in range(50):
        if sent:
            return
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# Only alerts that can actually fire
# ---------------------------------------------------------------------------

def test_no_alert_depends_on_a_room_temperature():
    """
    Only the SW4 reports a temperature and it is no longer sold, so alerts that
    needed one could never fire while sitting there looking like protection.
    """
    for gone in ("room_cold", "sensor_silent", "cannot_reach"):
        assert gone not in notifications.EVENT_TYPES


def test_the_noisy_alerts_were_removed_too():
    """
    A periodic check-in, a room-left-off warning and a per-zone schedule diary
    were all built and then removed as not worth reading. An alert nobody wants
    is not harmless: it teaches people to ignore the ones that matter.
    """
    for gone in ("heartbeat", "zone_off", "schedule_event"):
        assert gone not in notifications.EVENT_TYPES


def test_only_the_four_honest_alerts_remain():
    assert set(notifications.EVENT_TYPES) == {
        "hub_offline", "hub_online", "changed_elsewhere", "away_period",
    }


def test_everything_is_off_by_default():
    """
    The hardware reports so little that none of this is worth mailing somebody
    unasked. The feature is kept for whoever wants it, not recommended.
    """
    assert all(v["default"] is False for v in notifications.EVENT_TYPES.values())
    assert notifications.load_settings()["enabled"] is False
    assert all(v is False for v in notifications.load_settings()["events"].values())


def test_the_watcher_has_no_removed_detectors_left():
    for gone in ("_check_cold", "_clear_cold", "_check_silent",
                 "_check_cannot_reach", "_check_off", "_check_schedule"):
        assert not hasattr(notify_watch.ZoneWatcher, gone), gone


def test_the_heartbeat_machinery_is_gone():
    for gone in ("heartbeat_due", "load_state", "save_state", "STATE_FILE"):
        assert not hasattr(notifications, gone), gone


# ---------------------------------------------------------------------------
# Settings and secrecy
# ---------------------------------------------------------------------------

def test_notifications_are_off_until_asked_for():
    assert notifications.load_settings()["enabled"] is False


def test_the_password_is_never_returned():
    """It would otherwise leak through a screenshot or a support bundle."""
    notifications.save_settings({
        "enabled": False,
        "email": {"host": "mail.example.com", "password": "hunter2",
                  "to_addrs": ["me@example.com"]},
    })
    public = notifications.public_settings()
    assert "password" not in public["email"]
    assert public["email"]["password_set"] is True


def test_password_set_is_false_when_there_is_none():
    notifications.save_settings({"email": {"host": "mail.example.com"}})
    assert notifications.public_settings()["email"]["password_set"] is False


def test_a_corrupt_file_disables_notifications_rather_than_crashing(tmp_path, monkeypatch):
    path = tmp_path / "notifications.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(notifications, "NOTIFICATIONS_FILE", path)
    assert notifications.load_settings()["enabled"] is False


def test_nonsense_values_fall_back_to_defaults():
    saved = notifications.save_settings({
        "min_minutes_between": "ages",
        "email": {"port": 99999},
        "quiet_hours": {"enabled": True, "start": "25:00", "end": "07:00"},
    })
    assert saved["min_minutes_between"] == 10
    assert saved["email"]["port"] == 587
    assert saved["quiet_hours"]["start"] == "23:00"


def test_an_unknown_event_key_is_ignored():
    saved = notifications.save_settings({"events": {"nonsense": True}})
    assert "nonsense" not in saved["events"]


def test_a_settings_file_from_an_older_version_still_loads():
    """
    Anyone who ran an earlier build has removed keys and removed events saved.
    Those must be dropped without taking the surviving preferences with them.
    """
    saved = notifications.save_settings({
        "enabled": False,
        "cold_threshold_c": 5.0,
        "off_for_hours": 24,
        "heartbeat_days": 7,
        "events": {"room_cold": True, "zone_off": True, "heartbeat": True,
                   "hub_offline": True},
    })
    for gone in ("cold_threshold_c", "off_for_hours", "heartbeat_days"):
        assert gone not in saved
    for gone in ("room_cold", "zone_off", "heartbeat"):
        assert gone not in saved["events"]
    assert saved["events"]["hub_offline"] is True, "surviving preferences are kept"


def test_addresses_that_are_not_addresses_are_dropped():
    saved = notifications.save_settings({
        "email": {"to_addrs": ["me@example.com", "not an address", ""]},
    })
    assert saved["email"]["to_addrs"] == ["me@example.com"]


# ---------------------------------------------------------------------------
# Not crying wolf
# ---------------------------------------------------------------------------

def test_nothing_is_sent_while_the_feature_is_off(notifier, sent):
    notifier.settings["enabled"] = False
    assert notifier.notify("hub_offline", "x", "y") is False
    assert sent == []


def test_nothing_is_sent_for_an_event_the_user_turned_off(notifier, sent):
    notifier.settings["events"]["hub_offline"] = False
    assert notifier.notify("hub_offline", "x", "y") is False


def test_a_continuing_condition_speaks_once(notifier, sent):
    for _ in range(5):
        notifier.set_condition("hub_offline", "hub_offline", True, subject="gone", body="b")
    flush(sent)
    assert len(sent) == 1


def test_recovery_is_reported_once(notifier, sent):
    notifier.set_condition("hub_offline", "hub_offline", True, subject="gone", body="b")
    flush(sent)
    sent.clear()
    for _ in range(3):
        notifier.set_condition("hub_offline", "hub_offline", False,
                               recovery_event_type="hub_online",
                               recovery_subject="back", recovery_body="b")
    flush(sent)
    assert len(sent) == 1
    assert "back" in sent[0][0]


def test_conditions_alarm_independently(notifier, sent):
    notifier.set_condition("changed_elsewhere", "changed:1", True, subject="a", body="b")
    notifier.set_condition("changed_elsewhere", "changed:2", True, subject="b", body="b")
    flush(sent)
    assert len(sent) == 2


def test_a_repeat_inside_the_rate_limit_is_dropped(notifier, sent):
    notifier.settings["min_minutes_between"] = 10
    assert notifier.notify("hub_offline", "one", "b") is True
    assert notifier.notify("hub_offline", "two", "b") is False


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------

def test_quiet_hours_hold_back_routine_news(notifier, monkeypatch):
    notifier.settings["quiet_hours"] = {"enabled": True, "start": "23:00", "end": "07:00"}
    monkeypatch.setattr(notifier, "_quiet_now", lambda now=None: True)
    assert notifier.notify("changed_elsewhere", "x", "y", severity="info") is False


def test_quiet_hours_do_not_hold_back_something_urgent(notifier, monkeypatch):
    notifier.settings["quiet_hours"] = {"enabled": True, "start": "23:00", "end": "07:00"}
    monkeypatch.setattr(notifier, "_quiet_now", lambda now=None: True)
    assert notifier.notify("hub_offline", "gone", "y", severity="critical") is True


def test_quiet_hours_wrap_around_midnight(notifier):
    notifier.settings["quiet_hours"] = {"enabled": True, "start": "23:00", "end": "07:00"}
    assert notifier._quiet_now(time.struct_time((2026, 1, 1, 23, 30, 0, 0, 1, 0))) is True
    assert notifier._quiet_now(time.struct_time((2026, 1, 1, 3, 0, 0, 0, 1, 0))) is True
    assert notifier._quiet_now(time.struct_time((2026, 1, 1, 12, 0, 0, 0, 1, 0))) is False


# ---------------------------------------------------------------------------
# A broken mail server must never break the heating
# ---------------------------------------------------------------------------

def test_a_failing_mail_server_does_not_raise(notifier):
    def explode(cfg, subject, body):
        raise RuntimeError("connection refused")

    notifier.send_impl = explode
    assert notifier.notify("hub_offline", "x", "y") is True
    time.sleep(0.1)


# ---------------------------------------------------------------------------
# Who changed it — the elimination logic
# ---------------------------------------------------------------------------

def test_our_own_change_is_not_reported_as_somebody_else(watcher, sent):
    watcher.observe([zone(mode="comfort")])
    watcher.record_local_write("1", "mode", "eco")
    watcher.observe([zone(mode="eco")])
    flush(sent)
    assert sent == []


def test_a_change_we_did_not_make_is_reported(watcher, sent):
    watcher.observe([zone(mode="comfort")])
    watcher.observe([zone(mode="eco")])
    flush(sent)
    assert len(sent) == 1
    assert "Eco" in sent[0][0]


def test_the_alert_does_not_claim_to_know_which_app(watcher, sent):
    """The protocol carries no source. The wording must not invent one."""
    watcher.observe([zone(mode="comfort")])
    watcher.observe([zone(mode="eco")])
    flush(sent)
    body = sent[0][1]
    assert "not changed from here" in body
    assert "does not record which app" in body


def test_a_global_change_covers_every_zone(watcher, sent):
    watcher.observe([zone("1"), zone("2", name="Hallway")])
    watcher.record_local_write("*", "mode", "away")
    watcher.observe([zone("1", mode="away"), zone("2", name="Hallway", mode="away")])
    flush(sent)
    assert sent == []


def test_a_stale_local_write_no_longer_excuses_a_change(watcher, sent):
    watcher.observe([zone(mode="comfort")])
    watcher.record_local_write("1", "mode", "eco")
    watcher.clock.advance(notify_watch.LOCAL_WRITE_TTL_SECONDS + 10)
    watcher.observe([zone(mode="eco")])
    flush(sent)
    assert len(sent) == 1


def test_a_setpoint_changed_elsewhere_is_reported(watcher, sent):
    watcher.observe([zone(comfort=21.0)])
    watcher.observe([zone(comfort=24.0)])
    flush(sent)
    assert len(sent) == 1
    assert "comfort" in sent[0][0]


def test_our_own_setpoint_change_is_quiet(watcher, sent):
    watcher.observe([zone(comfort=21.0)])
    watcher.record_local_write("1", "comfort", 24.0)
    watcher.observe([zone(comfort=24.0)])
    flush(sent)
    assert sent == []


def test_the_first_look_at_the_house_says_nothing(watcher, sent):
    """Otherwise every restart mails a summary of everything."""
    watcher.observe([zone("1"), zone("2", name="Hallway"), zone("3", name="Loft")])
    flush(sent)
    assert sent == []


def test_a_newly_added_room_is_not_an_alert(watcher, sent):
    watcher.observe([zone("1")])
    watcher.observe([zone("1"), zone("2", name="New Room", mode="eco")])
    flush(sent)
    assert sent == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_a_configuration_that_cannot_deliver_is_described():
    problems = notifications.validate_email_config(
        notifications._merge({"email": {"host": "", "to_addrs": []}})
    )
    assert any("mail server" in p for p in problems)
    assert any("nobody to send to" in p for p in problems)


def test_a_username_without_a_password_is_caught():
    problems = notifications.validate_email_config(notifications._merge({
        "email": {"host": "m.example.com", "to_addrs": ["a@b.com"],
                  "username": "me", "password": ""},
    }))
    assert any("no password" in p for p in problems)


def test_a_complete_configuration_has_no_complaints():
    problems = notifications.validate_email_config(notifications._merge({
        "email": {"host": "m.example.com", "to_addrs": ["a@b.com"],
                  "from_addr": "pi@example.com"},
    }))
    assert problems == []
