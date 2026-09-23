"""The contact-sensor rules, on a fake clock and with no hub at all.

Read as a specification. The rule the whole file is about:

    While a contact is open, the room runs whichever is colder — what the rule
    asks for, or what the room would be doing anyway. Unless the zone has
    "override colder modes" set, in which case the rule always wins.

Everything else is bookkeeping around that sentence.
"""

from datetime import datetime, timezone

import pytest

from sensor_automation import (
    ActionKind,
    ActionStatus,
    AggregateState,
    BlockReason,
    ConditionEventKind,
    HeatingZone,
    RECHECK_WHILE_OPEN_SECONDS,
    SensorAutomation,
    is_colder,
)
from sensor_persistence import ActionWhenOpen, AutomationZoneState, ZoneSensorPolicy
from sensor_provider import ContactSnapshot, ContactState


def sensor(sensor_id, state, *, available=True, zone="1"):
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ContactSnapshot(
        sensor_id, f"p:{sensor_id}", sensor_id, zone, ContactState(state),
        available, 50, stamp, stamp,
    )


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


class Commands:
    def __init__(self):
        self.calls = []
        self.fail_release = False
        self.fail_apply = False

    async def apply_override(self, zone_id, action):
        self.calls.append((action.value, zone_id))
        if self.fail_apply:
            raise RuntimeError("hub unavailable")

    async def release_override(self, zone_id):
        self.calls.append(("normal", zone_id))
        if self.fail_release:
            raise RuntimeError("hub unavailable")


def heating(
    running="comfort", *, fallback=None, held=False, equipment=True, connected=True,
    may_act=True,
):
    """One zone, described the way the server describes it.

    ``running`` is what the room is doing now; ``fallback`` is what it would do
    with its own override cancelled — the global mode or the week profile —
    and defaults to the same thing. ``held`` means a zone-level override is in
    place, whoever put it there.
    """
    return {
        "1": HeatingZone(
            zone_id="1",
            has_equipment=equipment,
            connected=connected,
            effective_mode=running,
            fallback_mode=fallback if fallback is not None else running,
            has_zone_override=held,
            sensors_may_act=may_act,
        )
    }


def policy(action=ActionWhenOpen.NOTHING, *, warn=300, delay=0, override=False):
    return {"1": ZoneSensorPolicy(warn, action, delay, override)}


def machine(commands=None, clock=None, states=None, save=None):
    return SensorAutomation(
        states=states, commands=commands, clock=clock or Clock(), save=save
    )


def owned(engine, mode=ActionWhenOpen.ECO, *, opened_at=50.0):
    """An automation that already holds *mode* on zone 1."""
    return {"1": AutomationZoneState(opened_at, False, mode)}


# ---------------------------------------------------------------------------
# The ordering
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("colder, warmer", [
    ("off", "away"), ("away", "eco"), ("eco", "comfort"), ("off", "comfort"),
])
def test_away_is_below_eco_is_below_comfort(colder, warmer):
    assert is_colder(colder, warmer) is True
    assert is_colder(warmer, colder) is False
    assert is_colder(colder, colder) is False


def test_a_mode_this_build_does_not_know_is_never_called_colder():
    assert is_colder("normal", "comfort") is None
    assert is_colder("comfort", "") is None


@pytest.mark.parametrize("action, ambient, expected", [
    # Colder than the room: the rule gets its way.
    (ActionWhenOpen.ECO, "comfort", ActionWhenOpen.ECO),
    (ActionWhenOpen.AWAY, "comfort", ActionWhenOpen.AWAY),
    (ActionWhenOpen.AWAY, "eco", ActionWhenOpen.AWAY),
    # Same or warmer: the house wins and the rule holds nothing.
    (ActionWhenOpen.ECO, "away", None),
    (ActionWhenOpen.COMFORT, "away", None),
    (ActionWhenOpen.COMFORT, "eco", None),
    (ActionWhenOpen.ECO, "eco", None),
    (ActionWhenOpen.ECO, "off", None),
    # Nothing to hold either way.
    (ActionWhenOpen.NOTHING, "comfort", None),
    (ActionWhenOpen.SCHEDULE, "comfort", None),
    # An unrankable mode is left alone rather than guessed at.
    (ActionWhenOpen.ECO, "something-new", None),
])
def test_the_colder_of_the_two_is_what_should_be_running(action, ambient, expected):
    assert SensorAutomation.mode_to_hold(action, ambient, False) is expected


@pytest.mark.parametrize("ambient", ["away", "eco", "comfort", "off"])
def test_override_colder_modes_skips_the_comparison(ambient):
    assert SensorAutomation.mode_to_hold(
        ActionWhenOpen.COMFORT, ambient, True
    ) is ActionWhenOpen.COMFORT


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warning_and_action_run_on_their_own_delays_from_the_moment_it_opens():
    clock, commands, saves = Clock(), Commands(), []
    engine = machine(commands, clock, save=lambda state: saves.append(True))
    rules = policy(ActionWhenOpen.ECO, warn=10, delay=20)

    first = await engine.evaluate([sensor("a", "open")], rules, heating())
    assert first.next_deadline == 110
    assert first.zones["1"].action_status is ActionStatus.PENDING

    clock.value = 111
    warned = await engine.evaluate([sensor("a", "open")], rules, heating())
    assert [event.kind for event in warned.events] == [ConditionEventKind.WARNING]
    assert commands.calls == []

    clock.value = 121
    applied = await engine.evaluate([sensor("a", "open")], rules, heating())
    assert applied.actions[0].kind is ActionKind.APPLY_OVERRIDE
    assert applied.zones["1"].action_status is ActionStatus.ACTIVE

    closed = await engine.evaluate(
        [sensor("a", "closed")], rules, heating("eco", fallback="comfort", held=True)
    )
    assert commands.calls[-1] == ("normal", "1")
    assert [event.kind for event in closed.events] == [ConditionEventKind.RECOVERY]
    assert engine.states["1"].open_started_at is None
    assert saves, "state changes must be persisted"


@pytest.mark.asyncio
async def test_a_zero_delay_warns_and_acts_immediately():
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [sensor("a", "open")],
        policy(ActionWhenOpen.ECO, warn=0, delay=0),
        heating("comfort"),
    )
    assert result.zones["1"].warning_raised is True
    assert commands.calls == [("eco", "1")]


@pytest.mark.asyncio
async def test_unknown_or_unavailable_never_counts_as_closed():
    engine = machine()
    rules = policy(warn=0)
    await engine.evaluate(
        [sensor("a", "open"), sensor("b", "closed")], rules, heating()
    )

    result = await engine.evaluate(
        [sensor("a", "closed"), sensor("b", "unknown")], rules, heating()
    )
    assert result.zones["1"].state is AggregateState.UNKNOWN
    assert engine.states["1"].open_started_at is not None

    result = await engine.evaluate(
        [sensor("a", "closed"), sensor("b", "closed", available=False)],
        rules, heating(),
    )
    assert result.zones["1"].state is AggregateState.UNAVAILABLE
    assert result.zones["1"].unavailable_count == 1
    assert engine.states["1"].warning_raised

    recovered = await engine.evaluate(
        [sensor("a", "closed"), sensor("b", "closed")], rules, heating()
    )
    assert recovered.events[-1].kind is ConditionEventKind.RECOVERY


@pytest.mark.asyncio
async def test_removing_the_last_sensor_ends_the_cycle():
    engine = machine()
    rules = policy(warn=0)
    await engine.evaluate([sensor("a", "open")], rules, heating())
    assert engine.states["1"].warning_raised

    ended = await engine.evaluate([], rules, heating())
    assert ended.events[-1].kind is ConditionEventKind.RECOVERY
    assert engine.states["1"].open_started_at is None


# ---------------------------------------------------------------------------
# The house changes its mind while the window is still open
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_warmer_global_mode_does_not_take_the_room_off_the_rule():
    """Press Comfort with a window open and an Eco rule: the room stays Eco."""
    commands = Commands()
    engine = machine(commands)
    rules = policy(ActionWhenOpen.ECO)

    await engine.evaluate([sensor("a", "open")], rules, heating("comfort"))
    assert engine.states["1"].owned_action is ActionWhenOpen.ECO

    # Choosing a global mode releases the zone overrides that follow it, so the
    # room comes back showing Comfort and holding nothing.
    engine.manual_takeover("1")
    result = await engine.evaluate(
        [sensor("a", "open")], rules, heating("comfort", held=False)
    )
    assert commands.calls == [("eco", "1"), ("eco", "1")]
    assert engine.states["1"].owned_action is ActionWhenOpen.ECO
    assert result.zones["1"].action_status is ActionStatus.ACTIVE


@pytest.mark.asyncio
async def test_a_colder_global_mode_wins_and_the_rule_hands_the_room_back():
    """Press Away with a window open and an Eco rule: Away is colder, so it wins."""
    commands = Commands()
    engine = machine(commands)
    rules = policy(ActionWhenOpen.ECO)
    await engine.evaluate([sensor("a", "open")], rules, heating("comfort"))
    commands.calls.clear()

    result = await engine.evaluate(
        [sensor("a", "open")], rules, heating("eco", fallback="away", held=True)
    )
    assert commands.calls == [("normal", "1")]
    assert engine.states["1"].owned_action is None
    assert result.zones["1"].block_reason is BlockReason.COLDER_MODE


@pytest.mark.asyncio
async def test_going_back_to_a_warmer_house_puts_the_rule_back_in_charge():
    """The decision is taken afresh, so nothing is permanently stood down."""
    commands = Commands()
    engine = machine(commands)
    rules = policy(ActionWhenOpen.ECO)

    await engine.evaluate([sensor("a", "open")], rules, heating("comfort"))
    # Away, so the rule lets go.
    await engine.evaluate(
        [sensor("a", "open")], rules, heating("eco", fallback="away", held=True)
    )
    # ...and Home again, with a Comfort week profile.
    await engine.evaluate([sensor("a", "open")], rules, heating("comfort"))
    assert commands.calls == [("eco", "1"), ("normal", "1"), ("eco", "1")]
    assert engine.states["1"].owned_action is ActionWhenOpen.ECO


@pytest.mark.asyncio
async def test_setting_the_room_warmer_by_hand_is_the_same_story():
    clock, commands = Clock(), Commands()
    engine = machine(commands, clock)
    rules = policy(ActionWhenOpen.ECO)
    await engine.evaluate([sensor("a", "open")], rules, heating("comfort"))
    commands.calls.clear()

    # Somebody sets this room to Comfort in the official app. Our override is
    # gone, the room is warmer than the rule, so the rule takes it back.
    clock.value += 30
    result = await engine.evaluate(
        [sensor("a", "open")], rules, heating("comfort", held=True)
    )
    assert commands.calls == [("eco", "1")]
    assert result.zones["1"].action_status is ActionStatus.ACTIVE


@pytest.mark.asyncio
async def test_a_command_still_in_flight_is_not_mistaken_for_being_overruled():
    """A hub applies an override asynchronously and echoes it back afterwards.

    In the gap the zone still reports its old mode and no override at all.
    Reading that as "somebody has taken the room off us" would send the same
    command again, once per evaluation — and any contact anywhere in the house
    causes one.
    """
    clock, commands = Clock(), Commands()
    engine = machine(commands, clock)
    rules = policy(ActionWhenOpen.ECO)
    await engine.evaluate([sensor("a", "open")], rules, heating("comfort"))
    assert commands.calls == [("eco", "1")]

    # Three more passes before the hub has caught up.
    for _ in range(3):
        clock.value += 1
        await engine.evaluate([sensor("a", "open")], rules, heating("comfort"))
    assert commands.calls == [("eco", "1")]
    assert engine.states["1"].owned_action is ActionWhenOpen.ECO

    # The echo arrives and nothing further is sent.
    clock.value += 1
    await engine.evaluate(
        [sensor("a", "open")], rules, heating("eco", fallback="comfort", held=True)
    )
    assert commands.calls == [("eco", "1")]


@pytest.mark.asyncio
async def test_a_zone_that_never_confirms_is_eventually_believed():
    """The grace period is a few seconds, not indefinite."""
    clock, commands = Clock(), Commands()
    engine = machine(commands, clock)
    rules = policy(ActionWhenOpen.ECO)
    await engine.evaluate([sensor("a", "open")], rules, heating("comfort"))

    clock.value += 60
    await engine.evaluate([sensor("a", "open")], rules, heating("comfort"))
    assert commands.calls == [("eco", "1"), ("eco", "1")]


@pytest.mark.asyncio
async def test_setting_the_room_colder_by_hand_is_left_alone():
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [sensor("a", "open")],
        policy(ActionWhenOpen.ECO),
        heating("away", held=True),
    )
    assert commands.calls == []
    assert engine.states["1"].owned_action is None
    assert result.zones["1"].block_reason is BlockReason.COLDER_MODE


@pytest.mark.asyncio
async def test_with_override_on_the_rule_holds_whatever_the_house_says():
    commands = Commands()
    engine = machine(commands)
    rules = policy(ActionWhenOpen.ECO, override=True)

    # Colder than the rule, and still the rule wins.
    await engine.evaluate([sensor("a", "open")], rules, heating("away"))
    assert commands.calls == [("eco", "1")]

    # A global Away arrives and releases our override; we take it straight back.
    engine.manual_takeover("1")
    await engine.evaluate([sensor("a", "open")], rules, heating("away"))
    assert commands.calls == [("eco", "1"), ("eco", "1")]
    assert engine.states["1"].owned_action is ActionWhenOpen.ECO


@pytest.mark.asyncio
async def test_turning_override_off_hands_back_a_hold_it_was_allowing():
    commands = Commands()
    engine = machine(commands)
    await engine.evaluate(
        [sensor("a", "open")],
        policy(ActionWhenOpen.COMFORT, override=True),
        heating("away"),
    )
    result = await engine.evaluate(
        [sensor("a", "open")],
        policy(ActionWhenOpen.COMFORT, override=False),
        heating("comfort", fallback="away", held=True),
    )
    assert commands.calls == [("comfort", "1"), ("normal", "1")]
    assert engine.states["1"].owned_action is None
    assert result.zones["1"].block_reason is BlockReason.COLDER_MODE


@pytest.mark.asyncio
async def test_changing_the_action_swaps_the_hold_in_one_command():
    commands = Commands()
    engine = machine(commands)
    await engine.evaluate(
        [sensor("a", "open")], policy(ActionWhenOpen.ECO), heating("comfort")
    )
    await engine.evaluate(
        [sensor("a", "open")],
        policy(ActionWhenOpen.AWAY),
        heating("eco", fallback="comfort", held=True),
    )
    assert commands.calls == [("eco", "1"), ("away", "1")]
    assert engine.states["1"].owned_action is ActionWhenOpen.AWAY


@pytest.mark.asyncio
async def test_a_settled_hold_is_not_re_sent_on_every_pass():
    commands = Commands()
    engine = machine(commands)
    rules = policy(ActionWhenOpen.ECO)
    await engine.evaluate([sensor("a", "open")], rules, heating("comfort"))
    for _ in range(3):
        await engine.evaluate(
            [sensor("a", "open")], rules, heating("eco", fallback="comfort", held=True)
        )
    assert commands.calls == [("eco", "1")]


@pytest.mark.asyncio
async def test_an_open_room_is_looked_at_again_even_with_no_timer_pending():
    """The week profile can change under an open window with nothing to fire."""
    engine = machine(Commands())
    result = await engine.evaluate(
        [sensor("a", "open")],
        policy(ActionWhenOpen.ECO, warn=0),
        heating("away"),
    )
    assert result.next_deadline == 100.0 + RECHECK_WHILE_OPEN_SECONDS


# ---------------------------------------------------------------------------
# Return to schedule
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_return_to_schedule_lets_go_of_a_hold_the_schedule_undercuts():
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [sensor("a", "open")],
        policy(ActionWhenOpen.SCHEDULE),
        heating("comfort", fallback="eco", held=True),
    )
    assert commands.calls == [("normal", "1")]
    # Nothing is owned: there is no override left to give back on closure.
    assert engine.states["1"].owned_action is None
    assert result.actions[0].kind is ActionKind.RELEASE_OVERRIDE


@pytest.mark.asyncio
async def test_return_to_schedule_will_not_warm_a_room_by_letting_go():
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [sensor("a", "open")],
        policy(ActionWhenOpen.SCHEDULE),
        heating("away", fallback="comfort", held=True),
    )
    assert commands.calls == []
    assert result.zones["1"].block_reason is BlockReason.COLDER_MODE

    allowed = await engine.evaluate(
        [sensor("a", "open")],
        policy(ActionWhenOpen.SCHEDULE, override=True),
        heating("away", fallback="comfort", held=True),
    )
    assert commands.calls == [("normal", "1")]
    assert allowed.actions[0].succeeded


@pytest.mark.asyncio
async def test_return_to_schedule_is_quiet_when_there_is_no_hold_and_never_loops():
    commands = Commands()
    engine = machine(commands)
    rules = policy(ActionWhenOpen.SCHEDULE)
    await engine.evaluate(
        [sensor("a", "open")], rules, heating("comfort", fallback="eco", held=True)
    )
    result = await engine.evaluate(
        [sensor("a", "open")], rules, heating("eco", held=False)
    )
    assert commands.calls == [("normal", "1")]
    assert result.zones["1"].action_status is ActionStatus.IDLE


@pytest.mark.asyncio
async def test_do_nothing_warns_and_leaves_the_heating_alone():
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [sensor("a", "open")], policy(ActionWhenOpen.NOTHING, warn=0), heating()
    )
    assert commands.calls == []
    assert result.zones["1"].warning_raised is True
    assert result.zones["1"].action_status is ActionStatus.IDLE


# ---------------------------------------------------------------------------
# Closing up
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_closing_cancels_only_our_override_and_never_sends_comfort():
    commands = Commands()
    engine = machine(commands, states=owned(None))
    await engine.evaluate(
        [sensor("a", "closed")],
        policy(ActionWhenOpen.ECO),
        heating("eco", fallback="away", held=True),
    )
    assert commands.calls == [("normal", "1")]
    assert engine.states["1"].owned_action is None


@pytest.mark.asyncio
async def test_a_hold_somebody_replaced_is_not_cancelled_on_closure():
    commands = Commands()
    engine = machine(commands, states=owned(None))
    await engine.evaluate(
        [sensor("a", "closed")],
        policy(ActionWhenOpen.ECO),
        heating("away", held=True),
    )
    assert commands.calls == []
    assert engine.states["1"].owned_action is None


# ---------------------------------------------------------------------------
# Rooms we cannot act on
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("zones, reason", [
    (heating(equipment=False), BlockReason.NO_EQUIPMENT),
    (heating(connected=False), BlockReason.DISCONNECTED),
    ({}, BlockReason.DISCONNECTED),
])
async def test_a_room_we_cannot_act_on_is_reported_rather_than_guessed(zones, reason):
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [sensor("a", "open")], policy(ActionWhenOpen.ECO, warn=0), zones
    )
    assert commands.calls == []
    assert result.zones["1"].action_status is ActionStatus.BLOCKED
    assert result.zones["1"].block_reason is reason
    # A monitoring-only room still warns; it simply cannot heat.
    assert result.zones["1"].warning_raised is True


@pytest.mark.asyncio
async def test_demo_sensors_beside_a_real_hub_warn_but_never_heat():
    """Invented contacts are for clicking. A click must not leave a real room
    in Eco, and the room must not claim it is about to do so either."""
    clock, commands = Clock(), Commands()
    engine = machine(commands, clock)
    rules = policy(ActionWhenOpen.ECO, warn=10, delay=20)

    first = await engine.evaluate([sensor("a", "open")], rules, heating(may_act=False))
    assert first.zones["1"].action_status is ActionStatus.BLOCKED
    assert first.zones["1"].block_reason is BlockReason.DEMO_SENSORS
    assert first.zones["1"].action_deadline is None

    clock.value = 1000
    later = await engine.evaluate([sensor("a", "open")], rules, heating(may_act=False))
    assert commands.calls == []
    assert later.zones["1"].warning_raised is True
    assert later.zones["1"].owned_action is None


@pytest.mark.asyncio
async def test_a_hold_from_real_sensors_goes_back_when_demo_ones_take_over():
    """Switching source while a real window held a room in Eco must not leave
    the hold stranded behind a contact nobody will ever close."""
    commands = Commands()
    engine = machine(commands, states=owned(None))
    result = await engine.evaluate(
        [sensor("a", "open")],
        policy(ActionWhenOpen.ECO, delay=0),
        heating("eco", fallback="comfort", held=True, may_act=False),
    )
    assert commands.calls == [("normal", "1")]
    assert engine.states["1"].owned_action is None
    assert result.zones["1"].block_reason is BlockReason.DEMO_SENSORS


# ---------------------------------------------------------------------------
# Restarts and failures
# ---------------------------------------------------------------------------

def test_a_restart_never_re_applies_an_override_the_hub_disagrees_with():
    engine = machine(states=owned(None))
    engine.reconcile_owned(heating("comfort"))
    assert engine.states["1"].owned_action is None


def test_a_disconnected_hub_does_not_cost_us_persisted_ownership():
    engine = machine(states=owned(None))
    engine.reconcile_owned(heating("eco", held=True, connected=False))
    assert engine.states["1"].owned_action is ActionWhenOpen.ECO


@pytest.mark.asyncio
async def test_a_failed_release_keeps_the_cycle_open_so_it_is_retried():
    commands = Commands()
    commands.fail_release = True
    engine = machine(commands, states=owned(None))
    result = await engine.evaluate(
        [sensor("a", "closed")], policy(ActionWhenOpen.ECO), heating("eco", held=True)
    )
    assert not result.actions[0].succeeded
    assert engine.states["1"].owned_action is ActionWhenOpen.ECO
    assert engine.states["1"].open_started_at is not None

    assert not await engine.disable(heating("eco", held=True))
    assert engine.states["1"].owned_action is ActionWhenOpen.ECO

    commands.fail_release = False
    assert await engine.disable(heating("eco", held=True))
    assert engine.states["1"].owned_action is None


@pytest.mark.asyncio
async def test_a_failed_apply_is_reported_and_leaves_nothing_owned():
    commands = Commands()
    commands.fail_apply = True
    engine = machine(commands)
    result = await engine.evaluate(
        [sensor("a", "open")], policy(ActionWhenOpen.ECO), heating("comfort")
    )
    assert not result.actions[0].succeeded
    assert engine.states["1"].owned_action is None
    assert result.zones["1"].action_status is ActionStatus.PENDING


@pytest.mark.asyncio
async def test_a_deleted_zone_gives_its_override_back_before_being_forgotten():
    commands = Commands()
    engine = machine(commands, states=owned(None))
    await engine.evaluate([], {}, heating("eco", held=True))
    assert commands.calls == [("normal", "1")]
    assert "1" not in engine.states
