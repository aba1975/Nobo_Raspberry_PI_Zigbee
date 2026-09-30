"""The alarm's rules, with no network, no files and no clock.

The heating rule is the one that matters: the alarm may only undo what the
alarm did. Every test below that ends in "leaves it alone" is a way a house
could otherwise be warmed, or kept cold, by a rule that forgot somebody had
pressed something since.
"""

from dataclasses import replace

import pytest

import alarm_automation as automation
from alarm_persistence import AlarmLedger, AlarmSettings
from alarm_provider import AlarmReading, LockReading

SETTINGS = AlarmSettings(enabled=True)


def reading(arm="disarmed", at="2026-09-30T08:00:00Z", locks=()):
    return AlarmReading(arm_state=arm, arm_changed_at=at, locks=tuple(locks), read_at=0.0)


def lock(locked=True, method="code", name="Front door"):
    return LockReading(lock_id="door", name=name, locked=locked, method=method,
                       changed_at="2026-09-30T08:01:00Z")


def decide(ledger, current, *, mode=None, source="manual", connected=True,
           may_act=True, settings=SETTINGS):
    return automation.decide_heating(
        ledger, current, settings,
        global_mode=mode, global_source=source,
        hub_connected=connected, may_act=may_act,
    )


# -- what counts as leaving -------------------------------------------------


@pytest.mark.parametrize("method, outside", [
    ("code", True), ("tag", True), ("remote", True), (None, True),
    ("something_new", True), ("thumb", False),
])
def test_only_the_thumb_turn_proves_somebody_is_inside(method, outside):
    """An unknown method counts as outside: being wrong that way costs one
    warning about a window, the other way a window left open for a week."""
    assert automation.lock_is_outside(lock(method=method), SETTINGS) is outside


def test_auto_lock_counts_only_when_asked():
    auto = lock(method="auto")
    assert automation.lock_is_outside(auto, SETTINGS) is False
    assert automation.lock_is_outside(
        auto, replace(SETTINGS, lock_sides={**SETTINGS.lock_sides, "auto": "outside"})) is True


@pytest.mark.parametrize("locked", [False, None])
def test_an_unlocked_or_unknown_lock_is_not_leaving(locked):
    assert automation.lock_is_outside(lock(locked=locked), SETTINGS) is False


def test_armed_away_outranks_a_lock():
    why = automation.leaving(reading("armed_away", locks=[lock()]), SETTINGS)
    assert why.reason == "armed_away"


def test_locked_from_outside_is_leaving_while_disarmed():
    why = automation.leaving(reading("disarmed", locks=[lock()]), SETTINGS)
    assert why.reason == "locked_outside"
    assert why.lock_name == "Front door"


def test_each_warning_can_be_turned_off():
    quiet = replace(SETTINGS, warn_when_armed_away=False, warn_when_armed_home=False,
                    warn_when_locked_outside=False)
    for arm in ("armed_away", "armed_home"):
        assert automation.leaving(reading(arm, locks=[lock()]), quiet) is None


def test_armed_away_falls_back_to_the_lock_when_its_own_warning_is_off():
    settings = replace(SETTINGS, warn_when_armed_away=False)
    why = automation.leaving(reading("armed_away", locks=[lock()]), settings)
    assert why.reason == "locked_outside"


def test_no_reading_is_not_leaving():
    assert automation.leaving(None, SETTINGS) is None


# -- the heating ------------------------------------------------------------


def test_arming_away_puts_the_house_on_away_and_owns_it():
    decision = decide(AlarmLedger(), reading("armed_away"))
    assert decision.action == "away"
    assert decision.ledger.owns_away is True
    assert decision.ledger.handled_event == "armed_away@2026-09-30T08:00:00Z"


def test_disarming_lifts_only_its_own_away():
    armed = decide(AlarmLedger(), reading("armed_away")).ledger
    decision = decide(armed, reading("disarmed", at="2026-09-30T17:00:00Z"),
                      mode="away", source="alarm")
    assert decision.action == "home"
    assert decision.ledger.owns_away is False


def test_disarming_leaves_a_mode_somebody_chose_since():
    """Home early, Comfort pressed: the source is no longer the alarm."""
    armed = decide(AlarmLedger(), reading("armed_away")).ledger
    decision = decide(armed, reading("disarmed", at="2026-09-30T17:00:00Z"),
                      mode="comfort", source="manual")
    assert decision.action is None
    assert decision.ledger.owns_away is False


def test_disarming_leaves_an_away_somebody_else_set():
    """The house was already on Away for a holiday when the alarm was armed.
    That Away belongs to the holiday; disarming must not end it."""
    decision = decide(AlarmLedger(), reading("armed_away"), mode="away", source="schedule")
    assert decision.action is None
    assert decision.ledger.owns_away is False
    decision = decide(decision.ledger, reading("disarmed", at="2026-09-30T17:00:00Z"),
                      mode="away", source="schedule")
    assert decision.action is None


def test_the_same_arming_is_not_acted_on_twice():
    """A restart reads "armed away since 08:00" again. If somebody lifted the
    Away by hand in the meantime, putting it back would override them."""
    armed = decide(AlarmLedger(), reading("armed_away")).ledger
    decision = decide(armed, reading("armed_away"), mode=None, source="manual")
    assert decision.action is None


def test_rearming_is_a_new_event():
    armed = decide(AlarmLedger(), reading("armed_away")).ledger
    back = decide(armed, reading("disarmed", at="2026-09-30T12:00:00Z"),
                  mode="away", source="alarm").ledger
    again = decide(back, reading("armed_away", at="2026-09-30T18:00:00Z"))
    assert again.action == "away"


def test_armed_at_home_is_not_away_by_default():
    decision = decide(AlarmLedger(), reading("armed_home"))
    assert decision.action is None
    decision = decide(AlarmLedger(), reading("armed_home"),
                      settings=replace(SETTINGS, away_when_armed_home=True))
    assert decision.action == "away"


@pytest.mark.parametrize("kwargs", [
    {"connected": False},
])
def test_nothing_is_decided_without_the_hub(kwargs):
    ledger = AlarmLedger()
    decision = decide(ledger, reading("armed_away"), **kwargs)
    assert decision.action is None
    assert decision.ledger == ledger, "unhandled, so it is acted on when the hub is back"


def test_nothing_is_decided_without_a_reading():
    ledger = AlarmLedger(owned_mode="away", handled_event="x")
    assert decide(ledger, None, mode="away", source="alarm").ledger == ledger
    unknown = reading(arm=None)
    assert decide(ledger, unknown, mode="away", source="alarm").action is None


def test_a_demo_alarm_never_drives_a_real_hub():
    decision = decide(AlarmLedger(), reading("armed_away"), may_act=False)
    assert decision.action is None
    assert decision.ledger.handled_event, "marked handled so it is not retried"


def test_away_turned_off_in_settings_means_no_heating_change():
    settings = replace(SETTINGS, away_when_armed_away=False)
    assert decide(AlarmLedger(), reading("armed_away"), settings=settings).action is None


# -- the lock and the heating -------------------------------------------------


OUTSIDE_ECO = replace(SETTINGS, heating_when_locked_outside="eco")
OUTSIDE_AWAY = replace(SETTINGS, heating_when_locked_outside="away")
INSIDE_ECO = replace(SETTINGS, heating_when_locked_inside="eco")


def test_the_lock_changes_nothing_unless_asked():
    assert decide(AlarmLedger(), reading(locks=[lock()])).action is None
    assert decide(AlarmLedger(), reading(locks=[lock(method="thumb")])).action is None


@pytest.mark.parametrize("settings, method, wanted", [
    (OUTSIDE_ECO, "code", "eco"),
    (OUTSIDE_AWAY, "code", "away"),
    (INSIDE_ECO, "thumb", "eco"),
    # The inside choice does nothing for a door locked from outside, and the
    # other way round.
    (INSIDE_ECO, "code", None),
    (OUTSIDE_ECO, "thumb", None),
])
def test_locking_sets_what_was_chosen_for_that_side(settings, method, wanted):
    decision = decide(AlarmLedger(), reading(locks=[lock(method=method)]), settings=settings)
    assert decision.action == wanted
    assert decision.ledger.owned_mode == wanted


def test_which_side_a_method_counts_as_is_the_users_to_say():
    """Verisure's name for the Doorman's * button is not documented, so it
    is placed by the user rather than guessed here."""
    star_inside = replace(OUTSIDE_ECO, lock_sides={**SETTINGS.lock_sides, "star": "inside"})
    assert automation.lock_side(lock(method="star"), OUTSIDE_ECO) == "outside"
    assert automation.lock_side(lock(method="star"), star_inside) == "inside"
    assert decide(AlarmLedger(), reading(locks=[lock(method="star")]),
                  settings=star_inside).action is None


def test_unlocking_puts_back_only_what_the_lock_set():
    locked = decide(AlarmLedger(), reading(locks=[lock()]), settings=OUTSIDE_ECO).ledger
    open_door = reading(locks=[lock(locked=False)])
    decision = decide(locked, open_door, mode="eco", source="alarm", settings=OUTSIDE_ECO)
    assert decision.action == "home"
    assert decision.reason == "released"
    assert decision.ledger.owned_mode is None


def test_unlocking_leaves_a_mode_somebody_chose_since():
    locked = decide(AlarmLedger(), reading(locks=[lock()]), settings=OUTSIDE_ECO).ledger
    decision = decide(locked, reading(locks=[lock(locked=False)]),
                      mode="comfort", source="manual", settings=OUTSIDE_ECO)
    assert decision.action is None


def test_the_alarm_outranks_the_lock():
    decision = decide(AlarmLedger(), reading("armed_away", locks=[lock()]), settings=OUTSIDE_ECO)
    assert decision.action == "away"
    assert decision.reason == "armed_away"


def test_disarming_with_the_door_still_locked_goes_to_the_locks_choice():
    armed = decide(AlarmLedger(), reading("armed_away", locks=[lock()]),
                   settings=OUTSIDE_ECO).ledger
    decision = decide(armed, reading("disarmed", at="2026-09-30T17:00:00Z", locks=[lock()]),
                      mode="away", source="alarm", settings=OUTSIDE_ECO)
    assert decision.action == "eco"
    assert decision.ledger.owned_mode == "eco"


def test_away_is_never_replaced_by_eco():
    """Somebody's own Away is colder than anything the lock would choose."""
    decision = decide(AlarmLedger(), reading(locks=[lock()]),
                      mode="away", source="manual", settings=OUTSIDE_ECO)
    assert decision.action is None
    assert decision.ledger.owned_mode is None


def test_an_away_chosen_before_arming_is_neither_taken_nor_lifted():
    """Away pressed in the app before leaving, with or without a return date,
    stays the person's: arming does not claim it and disarming does not end
    it."""
    for source in ("manual", "away_schedule"):
        decision = decide(AlarmLedger(), reading("armed_away"), mode="away", source=source)
        assert decision.action is None
        assert decision.ledger.owned_mode is None
        decision = decide(decision.ledger, reading("disarmed", at="2026-09-30T17:00:00Z"),
                          mode="away", source=source)
        assert decision.action is None


def test_the_same_lock_event_is_not_acted_on_twice():
    locked = decide(AlarmLedger(), reading(locks=[lock()]), settings=OUTSIDE_ECO).ledger
    # Somebody pressed Comfort; the lock is read again, unchanged.
    again = decide(locked, reading(locks=[lock()]), mode="comfort", source="manual",
                   settings=OUTSIDE_ECO)
    assert again.action is None


def test_locked_from_inside_can_be_a_warning_too():
    quiet = automation.leaving(reading(locks=[lock(method="thumb")]), SETTINGS)
    assert quiet is None
    asked = replace(SETTINGS, warn_when_locked_inside=True)
    why = automation.leaving(reading(locks=[lock(method="thumb")]), asked)
    assert why.reason == "locked_inside"


# -- settings written by an earlier version -----------------------------------


def test_the_old_auto_lock_switch_becomes_a_side():
    from alarm_persistence import parse_settings

    moved = parse_settings({"schema_version": 1, "enabled": True, "autolock_counts_as_leaving": True})
    assert moved.lock_side("auto") == "outside"
    kept = parse_settings({"schema_version": 1, "enabled": True, "autolock_counts_as_leaving": False})
    assert kept.lock_side("auto") == "inside"
    assert kept.lock_side("thumb") == "inside"
    assert kept.lock_side("something_new") == "outside"


def test_an_old_ledger_that_owned_away_still_does():
    from alarm_persistence import _parse_ledger as parse_ledger

    ledger = parse_ledger({"schema_version": 1, "owns_away": True, "handled_event": "armed_away@x"})
    assert ledger.owned_mode == "away"
    assert ledger.owns_away is True


@pytest.mark.parametrize("field, value", [
    ("heating_when_locked_inside", "away"),
    ("heating_when_locked_outside", "comfort"),
])
def test_a_choice_the_lock_cannot_make_is_refused(field, value):
    from alarm_persistence import InvalidAlarmData, parse_settings

    with pytest.raises(InvalidAlarmData):
        parse_settings({"schema_version": 1, "enabled": True, field: value})
