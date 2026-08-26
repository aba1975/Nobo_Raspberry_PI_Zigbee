"""
Tests for notifications — telling somebody when the heating needs attention.

The feature exists because a cabin stands empty: a thermostat switched off in
November is not noticed until a pipe bursts in March. So the tests are written
around the failures that actually cost money, and around the two ways this
feature could be worse than useless:

  - **staying silent** when something is wrong, and
  - **crying wolf** until the owner filters the alerts into a folder.

Nothing here talks to a mail server. ``send_impl`` is replaced by a list
append, so what is asserted is "would this have been sent, and what would it
have said".
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
    """A clock the test drives, so "for 30 minutes" does not take 30 minutes."""

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
    n = notifications.Notifier(settings=notifications._merge({
        "enabled": True,
        "email": {"host": "mail.example.com", "to_addrs": ["me@example.com"],
                  "from_addr": "pi@example.com"},
        # Off by default, so a test that wants it must say so.
        "events": {"schedule_event": False},
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


def zone(zone_id="1", name="Large Bathroom", mode="comfort", temp=21.0,
         comfort=21.0, eco=17.0, schedule_mode=None):
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
        "cold_threshold_c": "freezing",
        "email": {"port": 99999},
        "quiet_hours": {"enabled": True, "start": "25:00", "end": "07:00"},
    })
    assert saved["cold_threshold_c"] == 5.0
    assert saved["email"]["port"] == 587
    assert saved["quiet_hours"]["start"] == "23:00"


def test_an_unknown_event_key_is_ignored():
    saved = notifications.save_settings({"events": {"nonsense": True}})
    assert "nonsense" not in saved["events"]


def test_a_new_event_type_is_adopted_with_its_default():
    """Old settings files must not permanently hide a newly added alert."""
    notifications.save_settings({"enabled": True, "events": {"hub_offline": False}})
    loaded = notifications.load_settings()
    assert loaded["events"]["hub_offline"] is False
    assert loaded["events"]["room_cold"] is True


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
    """A cold room is cold for hours. It must not mail hourly."""
    for _ in range(5):
        notifier.set_condition("room_cold", "room_cold:1", True, subject="cold", body="b")
    flush(sent)
    assert len(sent) == 1


def test_recovery_is_reported_once(notifier, sent):
    notifier.set_condition("room_cold", "room_cold:1", True, subject="cold", body="b")
    flush(sent)
    sent.clear()
    for _ in range(3):
        notifier.set_condition("room_cold", "room_cold:1", False,
                               recovery_subject="warm", recovery_body="b")
    flush(sent)
    assert len(sent) == 1
    assert "warm" in sent[0][0]


def test_the_same_condition_can_alarm_again_after_recovering(notifier, sent):
    notifier.set_condition("room_cold", "room_cold:1", True, subject="cold", body="b")
    notifier.set_condition("room_cold", "room_cold:1", False,
                           recovery_subject="warm", recovery_body="b")
    sent.clear()
    notifier.set_condition("room_cold", "room_cold:1", True, subject="cold again", body="b")
    flush(sent)
    assert len(sent) == 1


def test_rooms_alarm_independently(notifier, sent):
    notifier.set_condition("room_cold", "room_cold:1", True, subject="a", body="b")
    notifier.set_condition("room_cold", "room_cold:2", True, subject="b", body="b")
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


def test_quiet_hours_do_not_hold_back_a_frost_warning(notifier, monkeypatch):
    """A frost alert that waits until morning is not an alert."""
    notifier.settings["quiet_hours"] = {"enabled": True, "start": "23:00", "end": "07:00"}
    monkeypatch.setattr(notifier, "_quiet_now", lambda now=None: True)
    assert notifier.notify("room_cold", "cold", "y", severity="critical") is True


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
    # The assertion is simply that this returns rather than propagating.
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
# The frost alarm
# ---------------------------------------------------------------------------

def test_a_cold_room_is_reported(watcher, sent):
    watcher.observe([zone(temp=20.0)])
    watcher.observe([zone(temp=3.0)])
    watcher.clock.advance(31 * 60)
    watcher.observe([zone(temp=3.0)])
    flush(sent)
    assert len(sent) == 1
    assert "cold" in sent[0][0].lower()
    assert "3.0" in sent[0][0]


def test_a_brief_cold_reading_is_not_reported(watcher, sent):
    """A door held open while the car is unloaded is not a frost emergency."""
    watcher.observe([zone(temp=20.0)])
    watcher.observe([zone(temp=3.0)])
    watcher.clock.advance(5 * 60)
    watcher.observe([zone(temp=20.0)])
    flush(sent)
    assert sent == []


def test_the_frost_alarm_names_the_likely_causes(watcher, sent):
    watcher.observe([zone(temp=20.0)])
    watcher.observe([zone(temp=2.0)])
    watcher.clock.advance(31 * 60)
    watcher.observe([zone(temp=2.0)])
    flush(sent)
    body = sent[0][1]
    for cause in ("switched off", "breaker", "window", "failed"):
        assert cause in body


def test_a_cold_room_that_recovers_says_so(watcher, sent):
    watcher.observe([zone(temp=20.0)])
    watcher.observe([zone(temp=2.0)])
    watcher.clock.advance(31 * 60)
    watcher.observe([zone(temp=2.0)])
    flush(sent)
    sent.clear()
    watcher.observe([zone(temp=12.0)])
    flush(sent)
    assert len(sent) == 1
    assert "warming up" in sent[0][0]


def test_a_room_hovering_on_the_threshold_does_not_flap(watcher, sent):
    """Without hysteresis this would alternate alarm and all-clear all night."""
    watcher.observe([zone(temp=20.0)])
    watcher.observe([zone(temp=4.9)])
    watcher.clock.advance(31 * 60)
    watcher.observe([zone(temp=4.9)])
    flush(sent)
    sent.clear()
    for temp in (5.1, 4.9, 5.2, 4.8):
        watcher.observe([zone(temp=temp)])
    time.sleep(0.1)
    assert sent == []


def test_a_room_with_no_sensor_is_never_called_cold(watcher, sent):
    """A dial-only R80 room reports nothing. Silence is not 0 degrees."""
    watcher.observe([zone(temp=None)])
    watcher.clock.advance(60 * 60)
    watcher.observe([zone(temp=None)])
    flush(sent)
    assert not any("cold" in s[0].lower() for s in sent)


# ---------------------------------------------------------------------------
# The switched-off NTB-2R
# ---------------------------------------------------------------------------

def test_a_thermostat_that_goes_quiet_is_reported(watcher, sent):
    """
    The Y02 'N/A' case: the hub's stored temperature went stale, which is what
    happens when a thermostat is switched off at the wall.
    """
    watcher.observe([zone(temp=19.0)])
    watcher.clock.advance(200 * 60)
    watcher.observe([zone(temp=None)])
    flush(sent)
    assert len(sent) == 1
    assert "stopped reporting" in sent[0][0]


def test_a_thermostat_is_given_time_before_being_called_silent(watcher, sent):
    """A single missed reading is a radio glitch, not a fault."""
    watcher.observe([zone(temp=19.0)])
    watcher.clock.advance(10 * 60)
    watcher.observe([zone(temp=None)])
    flush(sent)
    assert sent == []


def test_a_room_that_never_reported_is_not_called_silent(watcher, sent):
    """Dial-only rooms report nothing, forever. That is not a fault."""
    watcher.observe([zone(temp=None)])
    watcher.clock.advance(500 * 60)
    watcher.observe([zone(temp=None)])
    flush(sent)
    assert sent == []


def test_a_thermostat_coming_back_says_so(watcher, sent):
    watcher.observe([zone(temp=19.0)])
    watcher.clock.advance(200 * 60)
    watcher.observe([zone(temp=None)])
    flush(sent)
    sent.clear()
    watcher.observe([zone(temp=18.0)])
    flush(sent)
    assert len(sent) == 1
    assert "reporting its temperature again" in sent[0][0]


def test_the_silent_alert_explains_why_it_matters(watcher, sent):
    watcher.observe([zone(temp=19.0)])
    watcher.clock.advance(200 * 60)
    watcher.observe([zone(temp=None)])
    flush(sent)
    assert "frost" in sent[0][1]


# ---------------------------------------------------------------------------
# Schedule events
# ---------------------------------------------------------------------------

def test_schedule_events_are_silent_by_default(watcher, sent):
    """Dozens a day across eight rooms would train the user to ignore all of it."""
    watcher.observe([zone(mode="normal", schedule_mode="eco")])
    watcher.observe([zone(mode="normal", schedule_mode="comfort")])
    flush(sent)
    assert sent == []


def test_schedule_events_are_reported_when_asked_for(watcher, notifier, sent):
    notifier.settings["events"]["schedule_event"] = True
    watcher.observe([zone(mode="normal", schedule_mode="eco")])
    watcher.observe([zone(mode="normal", schedule_mode="comfort")])
    flush(sent)
    assert len(sent) == 1
    assert "Comfort" in sent[0][0]


def test_an_overridden_room_reports_no_schedule_events(watcher, notifier, sent):
    """While a room is overridden the schedule is not what it is doing."""
    notifier.settings["events"]["schedule_event"] = True
    watcher.observe([zone(mode="comfort", schedule_mode="eco")])
    watcher.observe([zone(mode="comfort", schedule_mode="comfort")])
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


# ---------------------------------------------------------------------------
# Which alerts need a thermometer
# ---------------------------------------------------------------------------
# Of the 25 Nobø models pynobo knows, exactly one measures room temperature.
# A house of NTB-2Rs and R80 RDC 700s reports nothing at all, so the alerts
# that depend on a reading have to be marked as such and the ones that do not
# have to keep working.

def test_the_temperature_alerts_declare_that_they_need_a_sensor():
    for key in ("room_cold", "sensor_silent", "cannot_reach"):
        assert notifications.EVENT_TYPES[key]["needs_sensor"] is True, key


def test_the_alerts_that_work_without_one_say_so():
    for key in ("hub_offline", "hub_online", "zone_off",
                "changed_elsewhere", "away_period", "schedule_event"):
        assert notifications.EVENT_TYPES[key].get("needs_sensor") is False, key


def test_at_least_one_frost_alert_works_without_a_sensor():
    """Otherwise a typical installation has no frost protection whatsoever."""
    usable = [k for k, v in notifications.EVENT_TYPES.items()
              if not v.get("needs_sensor") and v["default"]]
    assert "zone_off" in usable


def test_the_catalogue_exposes_the_flag_to_the_ui():
    assert notifications.public_settings()["event_types"]["room_cold"]["needs_sensor"] is True


# ---------------------------------------------------------------------------
# A room left switched off — the frost alert that needs no thermometer
# ---------------------------------------------------------------------------

def off_zone(**kw):
    return zone(mode="normal", schedule_mode="off", temp=None, **kw)


def test_a_room_left_switched_off_is_reported(watcher, sent):
    watcher.observe([off_zone()])
    watcher.clock.advance(25 * 3600)
    watcher.observe([off_zone()])
    flush(sent)
    assert len(sent) == 1
    assert "switched off" in sent[0][0]


def test_it_is_reported_even_with_no_temperature_at_all(watcher, sent):
    """This is the whole point: it works on hardware that measures nothing."""
    watcher.observe([off_zone()])
    watcher.clock.advance(25 * 3600)
    watcher.observe([off_zone()])
    flush(sent)
    assert sent, "a house with no thermometers must still get this alert"


def test_briefly_switching_a_room_off_is_not_reported(watcher, sent):
    watcher.observe([off_zone()])
    watcher.clock.advance(2 * 3600)
    watcher.observe([zone(mode="normal", schedule_mode="comfort", temp=None)])
    flush(sent)
    assert sent == []


def test_the_off_alert_explains_that_off_is_below_away(watcher, sent):
    watcher.observe([off_zone()])
    watcher.clock.advance(25 * 3600)
    watcher.observe([off_zone()])
    flush(sent)
    assert "not even" in sent[0][1] and "7" in sent[0][1]


def test_turning_a_room_back_on_says_so(watcher, sent):
    watcher.observe([off_zone()])
    watcher.clock.advance(25 * 3600)
    watcher.observe([off_zone()])
    flush(sent)
    sent.clear()
    watcher.observe([zone(mode="normal", schedule_mode="comfort", temp=None)])
    flush(sent)
    assert len(sent) == 1
    assert "heating again" in sent[0][0]


def test_an_overridden_room_is_not_called_switched_off(watcher, sent):
    """An override beats the schedule, so the Off in the profile is not in force."""
    watcher.observe([zone(mode="comfort", schedule_mode="off", temp=None)])
    watcher.clock.advance(50 * 3600)
    watcher.observe([zone(mode="comfort", schedule_mode="off", temp=None)])
    flush(sent)
    assert sent == []


# ---------------------------------------------------------------------------
# "The heater has been running flat out for two days"
# ---------------------------------------------------------------------------

def test_a_room_that_cannot_get_warm_is_reported(watcher, sent):
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=12.0)])
    watcher.clock.advance(49 * 3600)
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=12.1)])
    flush(sent)
    assert len(sent) == 1
    assert "cannot get warm" in sent[0][0]


def test_a_slow_warm_up_from_away_is_never_reported(watcher, sent):
    """
    The case that would otherwise make this alert useless. Going from 7 to 22
    in hard frost can genuinely take days; that room is working, and the proof
    is that it keeps gaining ground.
    """
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=7.0)])
    temp = 7.0
    for _ in range(6):                      # six days of slow, real progress
        watcher.clock.advance(24 * 3600)
        temp += 2.0
        watcher.observe([zone(mode="comfort", comfort=22.0, temp=temp)])
    flush(sent)
    assert sent == [], "a room that is still climbing is not stuck"


def test_a_room_that_stalls_after_warming_is_reported(watcher, sent):
    """
    It climbed, then stopped — a window opened partway through.

    Note it takes one further window to be sure: at the first check the room
    genuinely had gained ground, so the watcher slides forward and judges it
    again on its most recent progress. That is deliberately conservative.
    """
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=7.0)])
    watcher.clock.advance(24 * 3600)
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=13.0)])
    watcher.clock.advance(49 * 3600)
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=13.2)])   # slides
    watcher.clock.advance(49 * 3600)
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=13.3)])   # stuck
    flush(sent)
    assert len(sent) == 1
    assert "cannot get warm" in sent[0][0]


def test_being_short_of_a_brand_new_target_is_not_a_fault(watcher, notifier, sent):
    """One second after asking for Comfort, every room is short. That is physics."""
    notifier.settings["events"]["changed_elsewhere"] = False   # not what this tests
    watcher.observe([zone(mode="eco", eco=15.0, temp=15.0)])
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=15.0)])
    watcher.clock.advance(10 * 3600)
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=15.0)])
    flush(sent)
    assert sent == []


def test_changing_the_target_restarts_the_clock(watcher, notifier, sent):
    notifier.settings["events"]["changed_elsewhere"] = False   # not what this tests
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=12.0)])
    watcher.clock.advance(40 * 3600)
    # Target raised again before the window elapsed.
    watcher.observe([zone(mode="comfort", comfort=24.0, temp=12.0)])
    watcher.clock.advance(20 * 3600)
    watcher.observe([zone(mode="comfort", comfort=24.0, temp=12.0)])
    flush(sent)
    assert sent == []


def test_reaching_the_target_clears_it(watcher, sent):
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=12.0)])
    watcher.clock.advance(49 * 3600)
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=12.0)])
    flush(sent)
    sent.clear()
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=21.5)])
    flush(sent)
    assert len(sent) == 1
    assert "reached its temperature" in sent[0][0]


def test_a_room_close_to_target_is_never_reported(watcher, sent):
    """A degree short in normal operation is not a fault."""
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=21.0)])
    watcher.clock.advance(100 * 3600)
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=21.0)])
    flush(sent)
    assert sent == []


def test_a_room_with_no_thermometer_cannot_be_judged_on_reaching_target(watcher, sent):
    """The NTB-2R and R80 case: no reading, so no opinion."""
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=None)])
    watcher.clock.advance(100 * 3600)
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=None)])
    flush(sent)
    assert not any("cannot get warm" in s[0] for s in sent)


def test_the_alert_says_the_warm_up_case_is_excluded(watcher, sent):
    """So the user trusts it rather than assuming it fires on every cold snap."""
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=12.0)])
    watcher.clock.advance(49 * 3600)
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=12.0)])
    flush(sent)
    assert "keeps rising" in sent[0][1]


def test_the_threshold_is_configurable(watcher, notifier, sent):
    notifier.settings["cannot_reach_hours"] = 6
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=12.0)])
    watcher.clock.advance(7 * 3600)
    watcher.observe([zone(mode="comfort", comfort=22.0, temp=12.0)])
    flush(sent)
    assert len(sent) == 1
