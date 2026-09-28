"""A contact that heats more than its own room, on a fake clock.

Production's example: the Patio Door belongs to the Living Room, but opening it
lets the cold into the Kitchen and the Hallway too. It follows the Living
Room's rule, warns only on the Living Room, and each room gives back only the
hold this automation put there, once nothing open is asking for it.
"""

import json
from datetime import datetime, timezone

import pytest

import sensor_persistence
from sensor_automation import (
    ActionStatus,
    BlockReason,
    ConditionEventKind,
    HeatingZone,
    SensorAutomation,
)
from sensor_persistence import (
    ActionWhenOpen,
    AutomationZoneState,
    ZoneSensorPolicy,
    load_automation_state,
    load_heating_links,
    save_automation_state,
    save_heating_links,
)
from sensor_provider import ContactSnapshot, ContactState, SensorKind

LIVING, KITCHEN, HALLWAY, STORE = "2", "9", "5", "12"
ECO, AWAY = ActionWhenOpen.ECO, ActionWhenOpen.AWAY


def contact(sensor_id, zone, state="closed", *, available=True):
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ContactSnapshot(
        sensor_id, f"p:{sensor_id}", sensor_id, zone, ContactState(state),
        available, 90, stamp, stamp,
    )


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class House:
    """Zones that do what they are told, the way the hub does."""

    def __init__(self, zones=(LIVING, KITCHEN, HALLWAY), running="comfort", without=()):
        self.schedule = {zone: running for zone in (*zones, *without)}
        self.held = {}
        self.equipment = {zone: zone not in without for zone in self.schedule}
        self.calls = []

    async def apply_override(self, zone_id, action):
        self.calls.append((action.value, zone_id))
        self.held[zone_id] = action.value

    async def release_override(self, zone_id):
        self.calls.append(("normal", zone_id))
        self.held.pop(zone_id, None)

    def heating(self):
        return {
            zone: HeatingZone(
                zone_id=zone,
                has_equipment=self.equipment[zone],
                connected=True,
                effective_mode=self.held.get(zone, mode),
                fallback_mode=mode,
                has_zone_override=zone in self.held,
            )
            for zone, mode in self.schedule.items()
        }


def rules(**overrides):
    base = {zone: ZoneSensorPolicy(300, ECO, 60) for zone in (LIVING, KITCHEN, HALLWAY)}
    base.update(overrides)
    return base


PATIO_LINKS = {"patio": [KITCHEN, HALLWAY]}


class Rig:
    def __init__(self, house=None, policies=None, links=PATIO_LINKS, states=None):
        self.house = house or House()
        self.clock = Clock()
        self.saved = []
        self.policies = policies or rules()
        self.links = links
        self.engine = SensorAutomation(
            states=states, commands=self.house, clock=self.clock,
            save=lambda states: self.saved.append(
                {k: AutomationZoneState(**vars(v)) for k, v in states.items()}
            ),
        )

    async def run(self, *sensors):
        return await self.engine.evaluate(
            list(sensors), self.policies, self.house.heating(), self.links
        )


def patio(state="closed", **kwargs):
    return contact("patio", LIVING, state, **kwargs)


def couch(state="closed"):
    return contact("couch", LIVING, state)


def kitchen_window(state="closed"):
    return contact("kitchen-window", KITCHEN, state)


# ---------------------------------------------------------------------------
# The example
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_patio_door_turns_down_all_three_rooms_after_the_living_room_delay():
    rig = Rig()
    result = await rig.run(patio("open"))
    assert rig.house.calls == []
    kitchen = result.zones[KITCHEN]
    assert kitchen.action_status is ActionStatus.PENDING
    assert kitchen.action_deadline == 1060.0
    assert [hold.source_zone_id for hold in kitchen.linked] == [LIVING]
    assert kitchen.linked[0].open_sensor_ids == ("patio",)
    assert kitchen.linked[0].due is False

    rig.clock.value = 1060.0
    result = await rig.run(patio("open"))
    assert set(rig.house.calls) == {("eco", HALLWAY), ("eco", KITCHEN), ("eco", LIVING)}
    assert result.zones[KITCHEN].action_status is ActionStatus.ACTIVE
    assert result.zones[KITCHEN].owned_action is ECO
    assert result.zones[KITCHEN].linked[0].due is True

    rig.house.calls.clear()
    rig.clock.value = 1200.0
    result = await rig.run(patio("closed"))
    assert set(rig.house.calls) == {
        ("normal", HALLWAY), ("normal", KITCHEN), ("normal", LIVING),
    }
    assert result.zones[KITCHEN].linked == ()
    assert result.zones[KITCHEN].action_status is ActionStatus.IDLE


@pytest.mark.asyncio
async def test_a_window_with_no_links_stays_in_its_own_room():
    rig = Rig()
    rig.clock.value = 1000.0
    await rig.run(couch("open"))
    rig.clock.value = 1100.0
    result = await rig.run(couch("open"))
    assert rig.house.calls == [("eco", LIVING)]
    assert result.zones[KITCHEN].linked == ()
    assert result.zones[KITCHEN].action_status is ActionStatus.IDLE


@pytest.mark.asyncio
async def test_the_warning_stays_on_the_door_s_own_room():
    rig = Rig(policies=rules(**{LIVING: ZoneSensorPolicy(0, ECO, 60)}))
    result = await rig.run(patio("open"))
    warned = [event.zone_id for event in result.events if event.kind is ConditionEventKind.WARNING]
    assert warned == [LIVING]
    assert result.zones[LIVING].warning_raised is True
    assert result.zones[KITCHEN].warning_raised is False
    assert result.zones[KITCHEN].open_started_at is None


@pytest.mark.asyncio
async def test_the_delay_counts_from_the_linked_door_not_the_living_room_s_first_window():
    rig = Rig()
    await rig.run(couch("open"))
    rig.clock.value = 1050.0
    await rig.run(couch("open"), patio("open"))
    rig.clock.value = 1070.0
    await rig.run(couch("open"), patio("open"))
    # The living room's own cycle began at 1000 and is due; the kitchen's
    # began when the patio door opened, at 1050.
    assert rig.house.calls == [("eco", LIVING)]
    rig.clock.value = 1110.0
    await rig.run(couch("open"), patio("open"))
    assert set(rig.house.calls[1:]) == {("eco", HALLWAY), ("eco", KITCHEN)}


# ---------------------------------------------------------------------------
# Several things open at once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_kitchen_stays_down_while_its_own_window_is_still_open():
    rig = Rig()
    await rig.run(patio("open"), kitchen_window("open"))
    rig.clock.value = 1100.0
    await rig.run(patio("open"), kitchen_window("open"))
    rig.house.calls.clear()

    rig.clock.value = 1200.0
    await rig.run(patio("closed"), kitchen_window("open"))
    assert set(rig.house.calls) == {("normal", HALLWAY), ("normal", LIVING)}

    rig.house.calls.clear()
    rig.clock.value = 1300.0
    await rig.run(patio("closed"), kitchen_window("closed"))
    assert rig.house.calls == [("normal", KITCHEN)]


@pytest.mark.asyncio
async def test_the_kitchen_window_closing_first_ends_its_warning_but_not_the_door_s_hold():
    rig = Rig(policies=rules(**{KITCHEN: ZoneSensorPolicy(0, ECO, 60)}))
    await rig.run(patio("open"), kitchen_window("open"))
    rig.clock.value = 1100.0
    await rig.run(patio("open"), kitchen_window("open"))
    rig.house.calls.clear()

    rig.clock.value = 1200.0
    result = await rig.run(patio("open"), kitchen_window("closed"))
    assert rig.house.calls == []
    assert ConditionEventKind.RECOVERY in {
        event.kind for event in result.events if event.zone_id == KITCHEN
    }
    kitchen = result.zones[KITCHEN]
    assert kitchen.warning_raised is False
    assert kitchen.owned_action is ECO
    assert kitchen.action_status is ActionStatus.ACTIVE


@pytest.mark.asyncio
async def test_the_coldest_request_wins_in_a_room_with_two():
    rig = Rig(policies=rules(**{KITCHEN: ZoneSensorPolicy(300, AWAY, 0)}))
    await rig.run(kitchen_window("open"))
    assert rig.house.calls == [("away", KITCHEN)]
    rig.clock.value = 1100.0
    await rig.run(kitchen_window("open"), patio("open"))
    rig.clock.value = 1200.0
    await rig.run(kitchen_window("open"), patio("open"))
    # The door wants Eco in the kitchen, and the kitchen's own window already
    # has it in Away, which is colder: nothing more is sent there.
    assert ("eco", KITCHEN) not in rig.house.calls


# ---------------------------------------------------------------------------
# What a linked room refuses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_door_whose_own_room_changes_nothing_changes_nothing_elsewhere():
    rig = Rig(policies=rules(**{LIVING: ZoneSensorPolicy(300, ActionWhenOpen.NOTHING, 60)}))
    await rig.run(patio("open"))
    rig.clock.value = 2000.0
    result = await rig.run(patio("open"))
    assert rig.house.calls == []
    assert result.zones[KITCHEN].linked == ()
    assert result.zones[KITCHEN].action_status is ActionStatus.IDLE


@pytest.mark.asyncio
async def test_a_linked_room_without_a_heater_is_blocked_not_commanded():
    house = House(without=(STORE,))
    rig = Rig(house=house, links={"patio": [STORE]},
              policies=rules(**{STORE: ZoneSensorPolicy()}))
    await rig.run(patio("open"))
    rig.clock.value = 1100.0
    result = await rig.run(patio("open"))
    assert (("eco", STORE) not in house.calls) and ("eco", LIVING) in house.calls
    assert result.zones[STORE].action_status is ActionStatus.BLOCKED
    assert result.zones[STORE].block_reason is BlockReason.NO_EQUIPMENT


@pytest.mark.asyncio
async def test_a_kitchen_already_colder_is_left_alone():
    house = House()
    house.schedule[KITCHEN] = "away"
    rig = Rig(house=house)
    await rig.run(patio("open"))
    rig.clock.value = 1100.0
    result = await rig.run(patio("open"))
    assert ("eco", KITCHEN) not in house.calls
    assert result.zones[KITCHEN].block_reason is BlockReason.COLDER_MODE


@pytest.mark.asyncio
async def test_a_hand_set_kitchen_is_not_released_when_the_door_closes():
    rig = Rig()
    await rig.run(patio("open"))
    rig.clock.value = 1100.0
    await rig.run(patio("open"))
    # Somebody puts the kitchen on Away by hand while the door is open.
    rig.house.held[KITCHEN] = "away"
    rig.clock.value = 1200.0
    await rig.run(patio("open"))
    rig.house.calls.clear()
    rig.clock.value = 1300.0
    await rig.run(patio("closed"))
    assert ("normal", KITCHEN) not in rig.house.calls
    assert rig.house.held[KITCHEN] == "away"


@pytest.mark.asyncio
async def test_an_offline_door_is_not_a_closed_door():
    rig = Rig()
    await rig.run(patio("open"))
    rig.clock.value = 1100.0
    await rig.run(patio("open"))
    rig.house.calls.clear()
    rig.clock.value = 1200.0
    result = await rig.run(patio("open", available=False))
    assert rig.house.calls == []
    assert result.zones[KITCHEN].owned_action is ECO


@pytest.mark.asyncio
async def test_unlinking_an_open_door_gives_the_kitchen_back():
    rig = Rig()
    await rig.run(patio("open"))
    rig.clock.value = 1100.0
    await rig.run(patio("open"))
    rig.house.calls.clear()
    rig.links = {"patio": [HALLWAY]}
    rig.clock.value = 1200.0
    result = await rig.run(patio("open"))
    assert rig.house.calls == [("normal", KITCHEN)]
    assert result.zones[KITCHEN].linked == ()


@pytest.mark.parametrize("links, sensors", [
    ({"patio": [LIVING]}, [patio("open")]),
    ({"patio": ["404"]}, [patio("open")]),
    ({"patio": [KITCHEN]}, [contact("patio", None, "open")]),
])
@pytest.mark.asyncio
async def test_links_that_mean_nothing_do_nothing(links, sensors):
    rig = Rig(links=links)
    await rig.run(*sensors)
    rig.clock.value = 1100.0
    result = await rig.run(*sensors)
    assert all(zone != KITCHEN for _, zone in rig.house.calls)
    assert result.zones[KITCHEN].linked == ()


@pytest.mark.asyncio
async def test_a_thermometer_never_heats_another_room():
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    thermo = ContactSnapshot(
        "t", "p:t", "t", LIVING, ContactState.UNKNOWN, True, 90, stamp, stamp,
        kind=SensorKind.CLIMATE, temperature=21.0,
    )
    rig = Rig(links={"t": [KITCHEN]})
    result = await rig.run(thermo)
    assert result.zones[KITCHEN].linked == ()


# ---------------------------------------------------------------------------
# Restarts and files
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_restart_keeps_the_linked_delay_where_it_was():
    rig = Rig()
    await rig.run(patio("open"))
    saved = rig.saved[-1]
    assert saved[KITCHEN].linked_open_since == {LIVING: 1000.0}

    again = Rig(house=rig.house, states=saved)
    again.clock.value = 1060.0
    await again.run(patio("open"))
    assert ("eco", KITCHEN) in rig.house.calls


def test_automation_state_round_trips_linked_cycles(tmp_path):
    path = tmp_path / "state.json"
    save_automation_state({KITCHEN: AutomationZoneState(
        owned_action=ECO, linked_open_since={LIVING: 12.5},
    )}, path)
    document = json.loads(path.read_text())
    assert document["schema_version"] == sensor_persistence.AUTOMATION_SCHEMA_VERSION
    assert load_automation_state(path)[KITCHEN].linked_open_since == {LIVING: 12.5}


def test_a_v6_automation_file_still_loads_with_no_linked_cycles(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema_version": 6, "zones": {"1": {
        "open_started_at": None, "warning_raised": False, "owned_action": None,
        "owned_reason": "open", "climate_condition": None, "climate_since": None,
        "humidity_since": None, "humidity_raised": False, "frost_raised": False,
    }}}))
    assert load_automation_state(path)["1"].linked_open_since == {}


def test_a_linked_cycle_keyed_by_its_own_zone_is_refused(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema_version": 7, "zones": {"1": {
        "open_started_at": None, "warning_raised": False, "owned_action": None,
        "owned_reason": "open", "climate_condition": None, "climate_since": None,
        "humidity_since": None, "humidity_raised": False, "frost_raised": False,
        "linked_open_since": {"1": 5},
    }}}))
    assert load_automation_state(path) == {}
    assert path.with_suffix(".backup").exists()


def test_heating_links_round_trip_and_drop_empty_lists(tmp_path):
    path = tmp_path / "links.json"
    save_heating_links({"patio": [KITCHEN, HALLWAY], "couch": []}, path)
    assert load_heating_links(path) == {"patio": [KITCHEN, HALLWAY]}


@pytest.mark.parametrize("links", [
    {"patio": [KITCHEN, KITCHEN]},
    {"patio": [""]},
    {"patio": "9"},
    {"patio": [9]},
])
def test_invalid_heating_links_are_set_aside(tmp_path, links):
    path = tmp_path / "links.json"
    path.write_text(json.dumps({"schema_version": 1, "links": links}))
    assert load_heating_links(path) == {}
    assert path.with_suffix(".backup").exists()
