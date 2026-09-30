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
        auto, replace(SETTINGS, autolock_counts_as_leaving=True)) is True


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
    ledger = AlarmLedger(owns_away=True, handled_event="x")
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
