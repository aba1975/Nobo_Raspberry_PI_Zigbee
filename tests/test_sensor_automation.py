"""The contact-sensor state machine, on a fake clock and with no hub at all.

Read as a specification: each test names a rule the heating side of the feature
is meant to obey, and most of them exist because getting it wrong would leave a
real room at the wrong temperature.
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
    running="comfort",
    *,
    fallback=None,
    held=False,
    equipment=True,
    connected=True,
):
    """One zone, described the way the server describes it.

    ``running`` is what the room is doing; ``fallback`` is what it would do
    without its own override, defaulting to the same thing. ``held`` means
    somebody — us or a person — has a zone-level override on it.
    """
    return {
        "1": HeatingZone(
            zone_id="1",
            has_equipment=equipment,
            connected=connected,
            effective_mode=running,
            fallback_mode=fallback if fallback is not None else running,
            has_zone_override=held,
        )
    }


def policy(action=ActionWhenOpen.NOTHING, *, warn=300, delay=0, override=False):
    return {"1": ZoneSensorPolicy(warn, action, delay, override)}


def machine(commands=None, clock=None, states=None, save=None):
    return SensorAutomation(
        states=states, commands=commands, clock=clock or Clock(), save=save
    )


# ---------------------------------------------------------------------------
# The warmth ordering itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("colder, warmer", [
    ("off", "away"), ("away", "eco"), ("eco", "comfort"), ("off", "comfort"),
])
def test_the_warmth_ordering_runs_off_away_eco_comfort(colder, warmer):
    assert is_colder(colder, warmer) is True
    assert is_colder(warmer, colder) is False
    assert is_colder(colder, colder) is False


def test_an_unrankable_mode_is_never_claimed_to_be_colder():
    assert is_colder("normal", "comfort") is None
    assert is_colder("comfort", "") is None


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warning_and_action_run_on_their_own_delays():
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
    assert engine.states["1"].owned_action is ActionWhenOpen.ECO

    closed = await engine.evaluate(
        [sensor("a", "closed")], rules, heating("eco", fallback="comfort", held=True)
    )
    assert closed.actions[0].kind is ActionKind.RELEASE_OVERRIDE
    assert commands.calls[-1] == ("normal", "1")
    assert [event.kind for event in closed.events] == [ConditionEventKind.RECOVERY]
    assert engine.states["1"].open_started_at is None
    assert saves, "state changes must be persisted"


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
# A rule may cool a room down, but not warm it up
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("running, action", [
    ("comfort", ActionWhenOpen.ECO),
    ("comfort", ActionWhenOpen.AWAY),
    ("eco", ActionWhenOpen.AWAY),
])
async def test_a_rule_may_take_a_room_further_down_the_order(running, action):
    commands = Commands()
    engine = machine(commands)
    await engine.evaluate([sensor("a", "open")], policy(action), heating(running))
    assert commands.calls == [(action.value, "1")]
    assert engine.states["1"].owned_action is action


@pytest.mark.asyncio
@pytest.mark.parametrize("running, action", [
    ("away", ActionWhenOpen.ECO),
    ("away", ActionWhenOpen.COMFORT),
    ("eco", ActionWhenOpen.COMFORT),
    ("off", ActionWhenOpen.AWAY),
    ("eco", ActionWhenOpen.ECO),
])
async def test_a_rule_never_warms_a_room_that_is_already_colder(running, action):
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [sensor("a", "open")], policy(action), heating(running)
    )
    assert commands.calls == []
    assert engine.states["1"].owned_action is None
    assert result.zones["1"].action_status is ActionStatus.BLOCKED
    assert result.zones["1"].block_reason is BlockReason.COLDER_MODE


@pytest.mark.asyncio
async def test_sensor_override_is_the_only_way_past_the_ordering():
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [sensor("a", "open")],
        policy(ActionWhenOpen.COMFORT, override=True),
        heating("away"),
    )
    assert commands.calls == [("comfort", "1")]
    assert result.zones["1"].action_status is ActionStatus.ACTIVE
    assert engine.states["1"].owned_with_override is True


@pytest.mark.asyncio
async def test_turning_sensor_override_off_hands_back_a_hold_it_allowed():
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
    assert result.actions[-1].kind is ActionKind.RELEASE_OVERRIDE
    assert engine.states["1"].owned_action is None
    assert engine.states["1"].suppressed


@pytest.mark.asyncio
async def test_a_hold_the_ordering_still_allows_survives_a_policy_save():
    commands = Commands()
    engine = machine(commands)
    rules = policy(ActionWhenOpen.ECO)
    await engine.evaluate([sensor("a", "open")], rules, heating("comfort"))
    await engine.evaluate(
        [sensor("a", "open")], rules, heating("eco", fallback="comfort", held=True)
    )
    assert commands.calls == [("eco", "1")]
    assert engine.states["1"].owned_action is ActionWhenOpen.ECO


# ---------------------------------------------------------------------------
# Follow schedule: letting go rather than taking hold
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_follow_schedule_releases_a_hold_the_schedule_would_undercut():
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
async def test_follow_schedule_will_not_warm_a_room_by_letting_go():
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
async def test_follow_schedule_is_quiet_when_there_is_no_hold_to_release():
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [sensor("a", "open")],
        policy(ActionWhenOpen.SCHEDULE),
        heating("comfort", held=False),
    )
    assert commands.calls == []
    # Not a fault: the room is already on its schedule, which is the point.
    assert result.zones["1"].action_status is ActionStatus.IDLE


@pytest.mark.asyncio
async def test_follow_schedule_does_not_release_twice():
    commands = Commands()
    engine = machine(commands)
    rules = policy(ActionWhenOpen.SCHEDULE)
    await engine.evaluate(
        [sensor("a", "open")], rules, heating("comfort", fallback="eco", held=True)
    )
    await engine.evaluate(
        [sensor("a", "open")], rules, heating("eco", held=False)
    )
    assert commands.calls == [("normal", "1")]


@pytest.mark.asyncio
async def test_do_nothing_does_exactly_that():
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [sensor("a", "open")], policy(ActionWhenOpen.NOTHING, warn=0), heating()
    )
    assert commands.calls == []
    assert result.zones["1"].warning_raised is True
    assert result.zones["1"].action_status is ActionStatus.IDLE


# ---------------------------------------------------------------------------
# People outrank windows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_existing_zone_override_is_left_alone():
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [sensor("a", "open")],
        policy(ActionWhenOpen.ECO),
        heating("comfort", held=True),
    )
    assert commands.calls == []
    assert result.zones["1"].block_reason is BlockReason.MANUAL_OVERRIDE


@pytest.mark.asyncio
async def test_a_manual_takeover_stands_for_the_rest_of_the_open_cycle():
    commands = Commands()
    engine = machine(commands)
    rules = policy(ActionWhenOpen.ECO)

    await engine.evaluate([sensor("a", "open")], rules, heating("comfort", held=True))
    assert commands.calls == []

    engine.manual_takeover("1")
    result = await engine.evaluate([sensor("a", "open")], rules, heating("comfort"))
    assert commands.calls == []
    assert result.zones["1"].action_status is ActionStatus.SUPPRESSED

    # Closing everything ends the cycle, and a later one may automate again.
    await engine.evaluate([sensor("a", "closed")], rules, heating("comfort"))
    await engine.evaluate([sensor("a", "open")], rules, heating("comfort"))
    assert commands.calls == [("eco", "1")]


@pytest.mark.asyncio
async def test_somebody_replacing_our_override_takes_the_room_from_us():
    commands = Commands()
    engine = machine(commands)
    rules = policy(ActionWhenOpen.ECO)
    await engine.evaluate([sensor("a", "open")], rules, heating("comfort"))
    assert engine.states["1"].owned_action is ActionWhenOpen.ECO

    result = await engine.evaluate(
        [sensor("a", "open")], rules, heating("comfort", held=True)
    )
    assert engine.states["1"].owned_action is None
    assert engine.states["1"].suppressed
    assert result.zones["1"].action_status is ActionStatus.SUPPRESSED
    # Their override is theirs: we must not send NORMAL over the top of it.
    assert commands.calls == [("eco", "1")]


@pytest.mark.asyncio
async def test_a_replaced_override_is_not_cancelled_when_the_contact_closes():
    commands = Commands()
    engine = machine(
        commands,
        states={"1": AutomationZoneState(50, False, ActionWhenOpen.ECO, False)},
    )
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
async def test_a_room_we_cannot_reach_is_reported_rather_than_guessed(zones, reason):
    commands = Commands()
    engine = machine(commands)
    result = await engine.evaluate(
        [sensor("a", "open")], policy(ActionWhenOpen.ECO), zones
    )
    assert commands.calls == []
    assert result.zones["1"].action_status is ActionStatus.BLOCKED
    assert result.zones["1"].block_reason is reason
    # A monitoring-only room still warns; it just cannot heat.
    assert result.zones["1"].sensor_count == 1


# ---------------------------------------------------------------------------
# Restarts and failures
# ---------------------------------------------------------------------------

def test_a_restart_never_re_applies_an_override_the_hub_disagrees_with():
    engine = machine(
        states={"1": AutomationZoneState(50, True, ActionWhenOpen.ECO, False)}
    )
    engine.reconcile_owned(heating("comfort"))
    assert engine.states["1"].owned_action is None
    assert engine.states["1"].suppressed


def test_a_disconnected_hub_does_not_cost_us_persisted_ownership():
    engine = machine(
        states={"1": AutomationZoneState(50, True, ActionWhenOpen.ECO, False)}
    )
    engine.reconcile_owned(heating("eco", held=True, connected=False))
    assert engine.states["1"].owned_action is ActionWhenOpen.ECO


@pytest.mark.asyncio
async def test_a_failed_release_keeps_the_cycle_open_so_it_is_retried():
    commands = Commands()
    commands.fail_release = True
    engine = machine(
        commands,
        states={"1": AutomationZoneState(50, True, ActionWhenOpen.ECO, False)},
    )
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
    engine = machine(
        commands,
        states={"1": AutomationZoneState(50, True, ActionWhenOpen.ECO, False)},
    )
    await engine.evaluate([], {}, heating("eco", held=True))
    assert commands.calls == [("normal", "1")]
    assert "1" not in engine.states
