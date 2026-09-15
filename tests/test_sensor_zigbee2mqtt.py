"""The Zigbee2MQTT provider, against a faithful fake of Zigbee2MQTT."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.fake_zigbee2mqtt import (
    FakeBroker, FakeTransport, FakeZigbee2Mqtt, contact_device, other_device,
    topic_matches,
)
from sensor_provider import ContactState, SensorEventKind, SensorKind
from sensor_zigbee2mqtt import (
    MAX_PERMIT_JOIN_SECONDS, ProviderUnavailable, SensorNotFound,
    Zigbee2MqttContactSensorProvider,
)

ADDRESS = "0x00158d0001a2b3c4"
SECOND = "0x00158d0009f8e7d6"


class Clock:
    def __init__(self):
        self.value = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def rig():
    broker = FakeBroker()
    z2m = FakeZigbee2Mqtt(broker)
    transport = FakeTransport(broker)
    clock = Clock()
    store: dict = {}

    provider = Zigbee2MqttContactSensorProvider(
        transport=transport,
        now=clock,
        load_metadata=lambda: dict(store),
        save_metadata=lambda data: (store.clear(), store.update(data)) and None,
    )
    return provider, z2m, transport, clock, store


@pytest.fixture
def events():
    return []


async def started(rig, events):
    provider, z2m, _transport, _clock, _store = rig
    provider.subscribe(events.append)
    await provider.start()
    await z2m.go_online()
    return provider, z2m


# -- the fake itself -------------------------------------------------------


@pytest.mark.parametrize(
    "filter_, topic, expected",
    [
        ("zigbee2mqtt/+", "zigbee2mqtt/kitchen", True),
        ("zigbee2mqtt/+", "zigbee2mqtt/bridge/state", False),
        ("zigbee2mqtt/+/availability", "zigbee2mqtt/kitchen/availability", True),
        ("zigbee2mqtt/bridge/request/#", "zigbee2mqtt/bridge/request/a/b", True),
        ("zigbee2mqtt/bridge/state", "zigbee2mqtt/bridge/state", True),
        ("zigbee2mqtt/bridge/state", "zigbee2mqtt/bridge/devices", False),
    ],
)
def test_topic_matching(filter_, topic, expected):
    assert topic_matches(filter_, topic) is expected


# -- discovery -------------------------------------------------------------


@pytest.mark.asyncio
async def test_contact_sensors_are_discovered(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))

    sensors = await provider.list()
    assert [item.sensor_id for item in sensors] == [ADDRESS]
    assert sensors[0].provider_id == f"zigbee2mqtt:{ADDRESS}"
    assert [event.kind for event in events] == [SensorEventKind.CREATED]


@pytest.mark.asyncio
async def test_devices_without_a_contact_expose_are_ignored(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(other_device("0x0017880100abcdef"))

    assert await provider.list() == []


@pytest.mark.asyncio
async def test_a_new_sensor_starts_unknown_not_closed(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))

    sensor = (await provider.list())[0]
    # Nothing has been heard from it yet.  Claiming "closed" would mean the
    # heating rule and the left-open warning both trust a fact nobody reported.
    assert sensor.state is ContactState.UNKNOWN
    assert sensor.available is False
    assert sensor.battery is None


# -- the inversion that would be quietly wrong -----------------------------


@pytest.mark.asyncio
async def test_contact_true_means_closed(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))

    await z2m.report(ADDRESS, contact=True, battery=87)
    assert (await provider.list())[0].state is ContactState.CLOSED

    await z2m.report(ADDRESS, contact=False, battery=87)
    assert (await provider.list())[0].state is ContactState.OPEN


@pytest.mark.asyncio
async def test_a_report_without_contact_keeps_the_last_state(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await z2m.report(ADDRESS, contact=False)

    await z2m.report(ADDRESS, battery=42)

    sensor = (await provider.list())[0]
    assert sensor.state is ContactState.OPEN
    assert sensor.battery == 42


# -- times -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_changed_at_moves_only_when_the_state_changes(rig, events):
    provider, z2m, _transport, clock, _store = rig
    await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await z2m.report(ADDRESS, contact=False)
    opened_at = (await provider.list())[0].changed_at

    clock.advance(120)
    await z2m.report(ADDRESS, contact=False, battery=80)
    sensor = (await provider.list())[0]

    assert sensor.changed_at == opened_at
    assert sensor.last_seen_at == opened_at + timedelta(seconds=120)


# -- availability ----------------------------------------------------------


@pytest.mark.asyncio
async def test_availability_topic_is_honoured(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await z2m.report(ADDRESS, contact=True)
    assert (await provider.list())[0].available is True

    await z2m.availability(ADDRESS, online=False)
    assert (await provider.list())[0].available is False


@pytest.mark.asyncio
async def test_a_report_proves_reachability(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await z2m.availability(ADDRESS, online=False)

    await z2m.report(ADDRESS, contact=False)

    # Hearing from the device outranks a stale availability verdict.
    assert (await provider.list())[0].available is True


@pytest.mark.asyncio
async def test_the_bridge_going_away_makes_every_sensor_unavailable(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await z2m.add_device(contact_device(SECOND))
    await z2m.report(ADDRESS, contact=True)
    await z2m.report(SECOND, contact=False)

    await z2m.go_offline()

    sensors = await provider.list()
    assert [item.available for item in sensors] == [False, False]
    # State is not rewritten to closed.  What was open is still open; we have
    # merely stopped being able to see it.
    assert [item.state for item in sensors] == [
        ContactState.CLOSED,
        ContactState.OPEN,
    ]


# -- battery ---------------------------------------------------------------


@pytest.mark.parametrize(
    "reported, expected",
    [(87, 87), (0, 0), (100, 100), (150, 100), (-5, 0), (87.6, 88), ("low", None),
     (True, None), (None, None)],
)
@pytest.mark.asyncio
async def test_battery_values(rig, events, reported, expected):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))

    await z2m.report(ADDRESS, contact=True, battery=reported)

    assert (await provider.list())[0].battery == expected


# -- identity and metadata -------------------------------------------------


@pytest.mark.asyncio
async def test_a_renamed_device_is_still_the_same_sensor(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS, friendly_name="kitchen window"))
    await provider.update(ADDRESS, name="Kitchen window", zone_id="3")

    await z2m.report("kitchen window", contact=False)

    sensor = (await provider.list())[0]
    assert sensor.sensor_id == ADDRESS
    assert sensor.state is ContactState.OPEN
    assert sensor.zone_id == "3"


@pytest.mark.asyncio
async def test_a_re_paired_sensor_returns_to_its_room(rig, events):
    provider, z2m, _transport, _clock, store = rig
    await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await provider.update(ADDRESS, name="Bunk room window", zone_id="7",
                          kind=SensorKind.WINDOW)

    await z2m.device_left(ADDRESS)
    assert await provider.list() == []

    await z2m.add_device(contact_device(ADDRESS))

    sensor = (await provider.list())[0]
    assert sensor.name == "Bunk room window"
    assert sensor.zone_id == "7"
    assert store[ADDRESS]["zone_id"] == "7"


@pytest.mark.asyncio
async def test_metadata_survives_a_restart(rig, events):
    provider, z2m, _transport, _clock, store = rig
    await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await provider.update(ADDRESS, name="Front door", kind=SensorKind.DOOR,
                          zone_id="2")
    await provider.stop()

    await provider.start()
    await z2m.publish_devices()

    sensor = (await provider.list())[0]
    assert (sensor.name, sensor.kind, sensor.zone_id) == (
        "Front door", SensorKind.DOOR, "2",
    )


@pytest.mark.asyncio
async def test_a_device_leaving_removes_it(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    events.clear()

    await z2m.device_left(ADDRESS)

    assert await provider.list() == []
    assert [event.kind for event in events] == [SensorEventKind.REMOVED]
    assert events[-1].snapshot is None


# -- pairing ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_begin_pairing_opens_the_window(rig, events):
    provider, z2m = await started(rig, events)

    assert await provider.begin_pairing(120) == 120
    assert z2m.permit_join_requests == [120]


@pytest.mark.asyncio
async def test_pairing_is_not_synchronous(rig, events):
    provider, _z2m = await started(rig, events)

    # A real join takes anywhere from seconds to never, so a call that promises
    # a sensor back cannot be honoured.
    with pytest.raises(ProviderUnavailable):
        await provider.pair("Kitchen window")
    with pytest.raises(ProviderUnavailable):
        await provider.create("Kitchen window")


@pytest.mark.parametrize("seconds", [0, -1, 255, 3600, 1.5, "60", True])
@pytest.mark.asyncio
async def test_a_join_window_longer_than_zigbee_allows_is_refused(
    rig, events, seconds
):
    provider, _z2m = await started(rig, events)

    with pytest.raises(ValueError):
        await provider.begin_pairing(seconds)


@pytest.mark.asyncio
async def test_pairing_needs_the_bridge(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.go_offline()

    with pytest.raises(ProviderUnavailable):
        await provider.begin_pairing(MAX_PERMIT_JOIN_SECONDS)


@pytest.mark.asyncio
async def test_cancel_pairing_closes_the_window(rig, events):
    provider, z2m = await started(rig, events)
    await provider.begin_pairing(254)

    await provider.cancel_pairing()

    assert z2m.permit_join_requests == [254, 0]


# -- removal ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_asks_zigbee2mqtt_and_waits_for_it(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))

    await provider.remove(ADDRESS)

    assert z2m.removed == [ADDRESS]
    # Still listed: the request is not the outcome, and dropping it here would
    # hide a sensor that is still on the mesh.
    assert [item.sensor_id for item in await provider.list()] == [ADDRESS]

    await z2m.device_left(ADDRESS)
    assert await provider.list() == []


@pytest.mark.asyncio
async def test_removing_an_unknown_sensor_is_an_error(rig, events):
    provider, _z2m = await started(rig, events)

    with pytest.raises(SensorNotFound):
        await provider.remove("0x00158d000000dead")


# -- guards ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_provider_must_be_started(rig):
    provider, _z2m, _transport, _clock, _store = rig

    with pytest.raises(RuntimeError):
        await provider.list()


@pytest.mark.asyncio
async def test_malformed_payloads_are_ignored(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await z2m.report(ADDRESS, contact=True)

    broker = z2m._broker
    for payload in ("not json", "", "{", '{"contact":'):
        await broker.publish(f"zigbee2mqtt/{ADDRESS}", payload)
        await broker.publish("zigbee2mqtt/bridge/devices", payload)
        await broker.publish("zigbee2mqtt/bridge/event", payload)
        await broker.publish("zigbee2mqtt/bridge/state", payload)

    sensors = await provider.list()
    assert [item.sensor_id for item in sensors] == [ADDRESS]
    assert sensors[0].state is ContactState.CLOSED


@pytest.mark.asyncio
async def test_an_empty_device_list_really_means_no_devices(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    events.clear()

    # Not a malformed payload: Zigbee2MQTT saying the network is empty, which
    # is what a coordinator reports after its database is reset.
    await z2m._broker.publish("zigbee2mqtt/bridge/devices", "[]", retain=True)

    assert await provider.list() == []
    assert [event.kind for event in events] == [SensorEventKind.REMOVED]


@pytest.mark.asyncio
async def test_update_rejects_contradictory_zone_arguments(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))

    with pytest.raises(ValueError):
        await provider.update(ADDRESS, zone_id="3", clear_zone=True)


@pytest.mark.parametrize("name", ["", "   ", "x" * 81, 5])
@pytest.mark.asyncio
async def test_update_rejects_bad_names(rig, events, name):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))

    with pytest.raises(ValueError):
        await provider.update(ADDRESS, name=name)


@pytest.mark.asyncio
async def test_omitting_a_field_leaves_it_alone(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await provider.update(ADDRESS, name="Porch door", kind=SensorKind.DOOR,
                          zone_id="5")

    unchanged = await provider.update(ADDRESS)

    assert (unchanged.name, unchanged.kind, unchanged.zone_id) == (
        "Porch door", SensorKind.DOOR, "5",
    )


@pytest.mark.asyncio
async def test_clear_zone_makes_a_sensor_unassigned(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await provider.update(ADDRESS, zone_id="4")

    updated = await provider.update(ADDRESS, clear_zone=True)

    assert updated.zone_id is None


# -- the factory -----------------------------------------------------------


def test_the_factory_builds_a_zigbee_provider_outside_demo_mode():
    from sensor_provider import create_provider

    broker = FakeBroker()
    provider = create_provider(
        "zigbee2mqtt",
        demo_mode=False,
        transport=FakeTransport(broker),
        load_metadata=lambda: {},
        save_metadata=lambda _data: None,
    )

    # The demo-mode gate stops a simulator posing as hardware.  It must not
    # stop real hardware running beside a simulated hub, which is how Zigbee
    # equipment is tested without touching a building's heating.
    assert isinstance(provider, Zigbee2MqttContactSensorProvider)


def test_the_factory_still_refuses_a_simulator_outside_demo_mode():
    from sensor_provider import create_provider

    with pytest.raises(RuntimeError):
        create_provider("simulated", demo_mode=False)


def test_the_factory_refuses_an_unknown_provider():
    from sensor_provider import create_provider

    with pytest.raises(ValueError):
        create_provider("hue", demo_mode=True)


def test_settings_accept_the_zigbee_provider(tmp_path):
    from sensor_persistence import (
        SCHEMA_VERSION, load_sensor_settings, save_sensor_settings,
    )
    from sensor_persistence import SensorSettings

    path = tmp_path / "sensor_settings.json"
    save_sensor_settings(SensorSettings(enabled=True, provider="zigbee2mqtt"), path)

    assert load_sensor_settings(path).provider == "zigbee2mqtt"
    assert SCHEMA_VERSION >= 4


def test_zigbee_metadata_round_trips(tmp_path):
    from sensor_persistence import load_zigbee_metadata, save_zigbee_metadata

    path = tmp_path / "zigbee_sensor_metadata.json"
    save_zigbee_metadata(
        {ADDRESS: {"name": "Kitchen window", "kind": "window", "zone_id": "3"}}, path
    )

    assert load_zigbee_metadata(path) == {
        ADDRESS: {"name": "Kitchen window", "kind": "window", "zone_id": "3"}
    }


@pytest.mark.parametrize(
    "row",
    [
        {"name": "", "kind": "window", "zone_id": None},
        {"name": "x", "kind": "hatch", "zone_id": None},
        {"name": "x", "kind": "window", "zone_id": ""},
        {"name": "x", "kind": "window"},
    ],
)
def test_bad_zigbee_metadata_is_refused(tmp_path, row):
    from sensor_persistence import InvalidSensorData, save_zigbee_metadata

    with pytest.raises(InvalidSensorData):
        save_zigbee_metadata({ADDRESS: row}, tmp_path / "meta.json")
