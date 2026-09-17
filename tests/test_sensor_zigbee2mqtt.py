"""The Zigbee2MQTT provider, against a faithful fake of Zigbee2MQTT."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.fake_zigbee2mqtt import (
    FakeBroker, FakeTransport, FakeZigbee2Mqtt, contact_device, other_device,
    topic_matches, unhelpful_device,
)
from sensor_provider import (
    ContactState, PairingOutcome, SensorEventKind, SensorKind,
)
from sensor_zigbee2mqtt import (
    MAX_PERMIT_JOIN_SECONDS, ProviderUnavailable, SensorNotFound,
    SensorRemovalFailed, Zigbee2MqttContactSensorProvider,
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


# -- signal strength -------------------------------------------------------


@pytest.mark.parametrize(
    "reported, expected",
    [(156, 156), (0, 0), (255, 255), (300, 255), (-5, 0), (86.4, 86),
     ("strong", None), (True, None), (None, None)],
)
@pytest.mark.asyncio
async def test_link_quality_values(rig, events, reported, expected):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))

    await z2m.report(ADDRESS, contact=True, linkquality=reported)

    assert (await provider.list())[0].link_quality == expected


@pytest.mark.asyncio
async def test_link_quality_arrives_with_the_first_report(rig, events):
    """Unlike battery, which a sleeping sensor may withhold for most of a day,
    link quality rides along with every message the coordinator hears."""
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))

    await z2m.report(ADDRESS, contact=True, linkquality=142)

    sensor = (await provider.list())[0]
    assert sensor.link_quality == 142
    assert sensor.battery is None


@pytest.mark.asyncio
async def test_a_report_without_link_quality_keeps_the_last_one(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await z2m.report(ADDRESS, contact=True, linkquality=120)

    await z2m.report(ADDRESS, contact=False)

    assert (await provider.list())[0].link_quality == 120


@pytest.mark.asyncio
async def test_readings_survive_a_restart(rig, events):
    """Battery and signal are the last measurements taken, not live facts, and
    a sleeping sensor may not speak again for hours.  Blanking them on every
    restart left both empty for most of a day after each update.

    The contact state is deliberately *not* treated this way: whether a window
    is open now is a safety question, and it starts unknown until heard.
    """
    provider, z2m, _transport, clock, _store = rig
    await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await z2m.report(
        ADDRESS, contact=True, battery=63, linkquality=140,
        last_seen=clock().isoformat(),
    )

    await provider.stop()
    await provider.start()
    await z2m.publish_devices()

    sensor = (await provider.list())[0]
    assert (sensor.battery, sensor.link_quality) == (63, 140)


@pytest.mark.asyncio
async def test_a_reading_that_was_never_taken_stays_absent(rig, events):
    provider, z2m, _transport, _clock, store = rig
    await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))

    assert store[ADDRESS]["battery"] is None
    assert store[ADDRESS]["link_quality"] is None
    sensor = (await provider.list())[0]
    assert (sensor.battery, sensor.link_quality) == (None, None)


@pytest.mark.parametrize("stored", [300, -1, "good", True])
def test_an_implausible_stored_reading_is_refused(tmp_path, stored):
    import json

    from sensor_persistence import SCHEMA_VERSION, load_zigbee_metadata

    path = tmp_path / "zigbee_sensor_metadata.json"
    path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "sensors": {
            ADDRESS: {
                "name": "x", "kind": "window", "zone_id": None,
                "last_seen": None, "battery": None, "link_quality": stored,
            },
        },
    }), encoding="utf-8")

    # _load backs up and returns the default rather than raising, so the
    # observable result is that nothing survives a corrupt file.
    assert load_zigbee_metadata(path) == {}


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
async def test_remove_waits_for_zigbee2mqtt_to_confirm(rig, events):
    provider, z2m, _transport, _clock, store = rig
    await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await provider.update(ADDRESS, name="Kitchen window", zone_id="3")

    await provider.remove(ADDRESS)

    assert z2m.removed == [ADDRESS]
    assert await provider.list() == []
    # A removal the user asked for really is a removal. Keeping the metadata
    # here is what made a deleted sensor come straight back.
    assert ADDRESS not in store


@pytest.mark.asyncio
async def test_a_removal_that_fails_says_so_and_keeps_the_sensor(rig, events):
    """The ordinary case for a battery contact sensor, not an exotic one.

    Zigbee2MQTT asks the device to leave first, and one that reports twice a
    day is asleep. Firing the request and returning made Delete appear to work
    while the sensor stayed exactly where it was.
    """
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    z2m.refuse_removal = True

    with pytest.raises(SensorRemovalFailed) as caught:
        await provider.remove(ADDRESS)

    # Zigbee2MQTT's own words, not a timeout message invented twenty seconds
    # later because the reply was dropped for want of an id.
    assert "mgmtLeaveRsp" in str(caught.value)
    assert [item.sensor_id for item in await provider.list()] == [ADDRESS]


@pytest.mark.asyncio
async def test_a_failure_is_matched_even_though_it_carries_no_id(rig, events):
    """The reply that actually comes back has an empty "data".

    Correlating on data.id alone meant every failure was ignored and the
    request sat until it timed out, so a sleeping sensor — the one case this
    exists for — reported "Zigbee2MQTT did not answer" instead of why.
    """
    import sensor_zigbee2mqtt

    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await z2m.add_device(contact_device(SECOND))
    z2m.refuse_removal = True

    with pytest.raises(SensorRemovalFailed):
        await provider.remove(SECOND)

    # Matched by the address in the error text, with two candidates pending,
    # so it cannot have been a lucky guess at "the only one".
    assert {item.sensor_id for item in await provider.list()} == {ADDRESS, SECOND}


@pytest.mark.asyncio
async def test_a_failure_arrives_promptly_rather_than_timing_out(rig, events, monkeypatch):
    import sensor_zigbee2mqtt

    monkeypatch.setattr(sensor_zigbee2mqtt, "REMOVE_TIMEOUT_SECONDS", 30.0)
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    z2m.refuse_removal = True

    import time
    began = time.monotonic()
    with pytest.raises(SensorRemovalFailed):
        await provider.remove(ADDRESS)

    # If this ever waits for the backstop again, somebody has broken the
    # correlation and the user is back to staring at a frozen dialog.
    assert time.monotonic() - began < 2.0


@pytest.mark.asyncio
async def test_a_forced_removal_gets_rid_of_a_sleeping_sensor(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    z2m.refuse_removal = True

    await provider.remove(ADDRESS, force=True)

    assert await provider.list() == []
    assert z2m.remove_requests[-1]["force"] is True


@pytest.mark.asyncio
async def test_a_silent_zigbee2mqtt_is_a_failure_not_a_hang(rig, events, monkeypatch):
    import sensor_zigbee2mqtt

    monkeypatch.setattr(sensor_zigbee2mqtt, "REMOVE_TIMEOUT_SECONDS", 0.05)
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    z2m.ignore_requests = True

    with pytest.raises(SensorRemovalFailed):
        await provider.remove(ADDRESS)

    # And it must not be left holding a waiter for a reply that never came.
    assert provider._remove_waiters == {}


@pytest.mark.asyncio
async def test_a_device_that_leaves_keeps_its_room(rig, events):
    provider, z2m, _transport, _clock, store = rig
    await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await provider.update(ADDRESS, name="Kitchen window", zone_id="3")

    await z2m.device_left(ADDRESS)

    # Different from a deletion: a flat battery or a re-pair should not cost
    # the user the name and room they chose.
    assert store[ADDRESS]["zone_id"] == "3"


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
        ADDRESS: {
            "name": "Kitchen window", "kind": "window", "zone_id": "3",
            "last_seen": None, "battery": None, "link_quality": None,
        }
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


# -- recorded from real hardware -------------------------------------------
#
# An Aqara MCCGQ11LM (IEEE 0x00158d008c8bc4f2) joined a real ZBDongle-P and
# sent exactly these payloads.  They are here so the assumptions above are
# anchored to something a radio actually produced rather than to what the
# documentation implies.


@pytest.mark.asyncio
async def test_the_payloads_a_real_aqara_sent(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    broker = z2m._broker

    # Verbatim, including the absence of a battery field.
    for payload, expected in (
        ('{"contact":true,"linkquality":98}', ContactState.CLOSED),
        ('{"contact":false,"linkquality":105}', ContactState.OPEN),
        ('{"contact":true,"linkquality":98}', ContactState.CLOSED),
    ):
        await broker.publish(f"zigbee2mqtt/{ADDRESS}", payload)
        assert (await provider.list())[0].state is expected


@pytest.mark.asyncio
async def test_no_battery_reading_is_normal_not_a_fault(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))

    # Zigbee2MQTT's own definition says battery "can take up to 24 hours
    # before reported", so a freshly paired sensor legitimately has none.
    # Substituting a number here would invent a reading, and showing a fault
    # would cry wolf on every new sensor.
    await z2m.report(ADDRESS, contact=True, linkquality=98)

    sensor = (await provider.list())[0]
    assert sensor.battery is None
    assert sensor.available is True
    assert sensor.state is ContactState.CLOSED


@pytest.mark.asyncio
async def test_a_rejoining_sensor_leaves_then_joins(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await provider.update(ADDRESS, name="Kitchen window", zone_id="3")
    events.clear()

    # A real Aqara re-pairing emits device_leave immediately before joining
    # again.  The sensor must come back to its room, not arrive anonymous.
    await z2m.device_left(ADDRESS)
    await z2m.add_device(contact_device(ADDRESS))

    sensor = (await provider.list())[0]
    assert (sensor.name, sensor.zone_id) == ("Kitchen window", "3")
    assert [event.kind for event in events] == [
        SensorEventKind.REMOVED, SensorEventKind.CREATED,
    ]


# -- pairing status --------------------------------------------------------
#
# Somebody is standing there holding a button on a battery device.  Every one
# of these outcomes has to be distinguishable from "nothing is happening".


async def _interview(z2m, address, status, device=None):
    import json
    data = dict(device or {"ieee_address": address})
    data["ieee_address"] = address
    data["status"] = status
    await z2m._broker.publish(
        "zigbee2mqtt/bridge/event",
        json.dumps({"type": "device_interview", "data": data}),
    )


@pytest.mark.asyncio
async def test_nothing_is_happening_by_default(rig, events):
    provider, _z2m = await started(rig, events)

    status = provider.pairing_status()
    assert status.supported is True
    assert status.active is False
    assert status.outcome is None


@pytest.mark.asyncio
async def test_a_window_reports_itself_open_and_counting_down(rig, events):
    provider, _z2m, _transport, clock, _store = rig
    await started(rig, events)

    await provider.begin_pairing(120)
    assert provider.pairing_status().active is True
    assert provider.pairing_status().seconds_remaining == 120

    clock.advance(45)
    assert provider.pairing_status().seconds_remaining == 75


@pytest.mark.asyncio
async def test_a_successful_join_is_reported(rig, events):
    provider, z2m = await started(rig, events)
    await provider.begin_pairing(254)

    await _interview(z2m, ADDRESS, "successful", contact_device(ADDRESS))

    status = provider.pairing_status()
    assert status.active is False
    assert status.outcome is PairingOutcome.JOINED
    assert status.sensor_id == ADDRESS


@pytest.mark.asyncio
async def test_a_failed_interview_is_reported(rig, events):
    provider, z2m = await started(rig, events)
    await provider.begin_pairing(254)

    await _interview(z2m, ADDRESS, "failed")

    status = provider.pairing_status()
    assert status.active is False
    assert status.outcome is PairingOutcome.FAILED
    assert "interview" in (status.detail or "")


@pytest.mark.asyncio
async def test_something_that_is_not_a_contact_sensor_says_so(rig, events):
    """A remote joins, is not a sensor, and being an end device relays nothing
    either — so there is genuinely nothing to celebrate."""
    provider, z2m = await started(rig, events)
    await provider.begin_pairing(254)

    await _interview(z2m, "0x0017880100abcdef", "successful",
                     unhelpful_device("0x0017880100abcdef"))

    status = provider.pairing_status()
    assert status.outcome is PairingOutcome.IGNORED
    assert "IKEA" in (status.detail or "")
    assert await provider.list() == []


@pytest.mark.asyncio
async def test_a_plug_that_joins_is_a_success_not_a_rejection(rig, events):
    """Somebody who buys a plug to extend the mesh has done exactly what they
    set out to do. Telling them "that is not a contact sensor" reads as a
    failure and invites them to try again, or take it back to the shop."""
    provider, z2m = await started(rig, events)
    await provider.begin_pairing(254)

    await _interview(z2m, "0x0017880100abcdef", "successful",
                     other_device("0x0017880100abcdef"))

    status = provider.pairing_status()
    assert status.outcome is PairingOutcome.ROUTER
    assert "IKEA" in (status.detail or "")
    # It is still not a sensor, and must never be counted as one.
    assert await provider.list() == []


@pytest.mark.asyncio
async def test_a_window_that_closes_with_nothing_says_expired(rig, events):
    provider, _z2m, _transport, clock, _store = rig
    await started(rig, events)
    await provider.begin_pairing(60)

    clock.advance(61)

    status = provider.pairing_status()
    assert status.active is False
    assert status.outcome is PairingOutcome.EXPIRED


@pytest.mark.asyncio
async def test_cancelling_says_cancelled(rig, events):
    provider, z2m = await started(rig, events)
    await provider.begin_pairing(254)

    await provider.cancel_pairing()

    assert provider.pairing_status().outcome is PairingOutcome.CANCELLED
    assert z2m.permit_join_requests == [254, 0]


@pytest.mark.asyncio
async def test_an_interview_outside_a_window_is_not_an_outcome(rig, events):
    provider, z2m = await started(rig, events)

    # A device re-announcing itself must not look like a pairing result to
    # somebody who never opened the window.
    await _interview(z2m, ADDRESS, "successful", contact_device(ADDRESS))

    assert provider.pairing_status().outcome is None


@pytest.mark.asyncio
async def test_starting_a_new_window_clears_the_last_outcome(rig, events):
    provider, z2m = await started(rig, events)
    await provider.begin_pairing(254)
    await _interview(z2m, ADDRESS, "failed")
    assert provider.pairing_status().outcome is PairingOutcome.FAILED

    await provider.begin_pairing(254)

    status = provider.pairing_status()
    assert status.active is True
    assert status.outcome is None


@pytest.mark.asyncio
async def test_pairing_changes_are_announced(rig, events):
    provider, z2m = await started(rig, events)
    events.clear()

    await provider.begin_pairing(254)
    await _interview(z2m, ADDRESS, "successful", contact_device(ADDRESS))

    # The interface must not have to poll to find out.
    assert SensorEventKind.PAIRING in [event.kind for event in events]


def test_the_simulator_has_no_pairing_window():
    from sensor_simulated import SimulatedContactSensorProvider

    status = SimulatedContactSensorProvider().pairing_status()

    # Unsupported, not "idle": the interface should offer the simulator's
    # straight create form, not a progress display that would never move.
    assert status.supported is False
    assert status.active is False


@pytest.mark.asyncio
async def test_reading_the_status_does_not_change_it(rig, events):
    """The automation loop reads this twice and compares the two results.

    A version that settled the expiry as it was read made the first call do
    the transition, so the comparison found no change and the window closing
    was never broadcast — somebody would watch "3s left" and then nothing.
    """
    provider, _z2m, _transport, clock, _store = rig
    await started(rig, events)
    await provider.begin_pairing(60)
    clock.advance(61)

    first = provider.pairing_status()
    second = provider.pairing_status()

    assert first == second
    assert first.outcome is PairingOutcome.EXPIRED
    assert first.active is False


@pytest.mark.asyncio
async def test_the_moment_of_expiry_is_visible_as_a_change(rig, events):
    provider, _z2m, _transport, clock, _store = rig
    await started(rig, events)
    await provider.begin_pairing(60)

    while_open = provider.pairing_status()
    clock.advance(61)
    after = provider.pairing_status()

    # Something a browser can see must actually differ across the deadline,
    # or the loop has nothing to announce.
    assert while_open.active is True and after.active is False
    assert (while_open.active, while_open.outcome) != (after.active, after.outcome)


@pytest.mark.asyncio
async def test_an_expired_window_stays_expired(rig, events):
    provider, _z2m, _transport, clock, _store = rig
    await started(rig, events)
    await provider.begin_pairing(60)
    clock.advance(600)

    # Not quietly back to "nothing has happened" ten minutes later.
    assert provider.pairing_status().outcome is PairingOutcome.EXPIRED


@pytest.mark.asyncio
async def test_a_join_just_before_the_deadline_is_not_overwritten_by_expiry(
    rig, events
):
    provider, z2m, _transport, clock, _store = rig
    await started(rig, events)
    await provider.begin_pairing(60)
    await _interview(z2m, ADDRESS, "successful", contact_device(ADDRESS))

    clock.advance(600)

    status = provider.pairing_status()
    assert status.outcome is PairingOutcome.JOINED
    assert status.sensor_id == ADDRESS


# -- what the review found -------------------------------------------------


@pytest.mark.asyncio
async def test_a_missing_sensor_raises_the_shared_not_found(rig, events):
    """Both providers must raise the class ``server.py`` catches for 404.

    A provider with its own same-named class is not caught, and the handler
    answers 500 — which is how a successful pairing ended in "Internal Server
    Error" when the save arrived before bridge/devices.
    """
    import sensor_simulated
    from sensor_provider import SensorNotFound as Shared

    provider, _z2m = await started(rig, events)

    with pytest.raises(Shared):
        await provider.update("0x00158d000000dead", name="x")
    assert sensor_simulated.SensorNotFound is Shared
    assert SensorNotFound is Shared


@pytest.mark.asyncio
async def test_losing_the_broker_marks_everything_unavailable(rig, events):
    """The bridge's will covers Zigbee2MQTT stopping, not the broker dying."""
    provider, z2m, transport, _clock, _store = rig
    await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await z2m.report(ADDRESS, contact=True)
    assert (await provider.list())[0].available is True

    await provider._connection_lost()

    sensor = (await provider.list())[0]
    assert sensor.available is False
    # Not rewritten to closed: what was shut is still shut, we have merely
    # stopped being able to see it.
    assert sensor.state is ContactState.CLOSED


@pytest.mark.asyncio
async def test_a_nested_friendly_name_still_reports(rig, events):
    """Zigbee2MQTT allows '/' in a name and publishes it as a nested topic.

    Registered but never heard from is the worst outcome available: the zone
    can never settle, so a heating override taken for it is never released.
    """
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS, friendly_name="kitchen/window"))

    await z2m.report("kitchen/window", contact=False, linkquality=98)
    await z2m.availability("kitchen/window", online=True)

    sensor = (await provider.list())[0]
    assert sensor.sensor_id == ADDRESS
    assert sensor.state is ContactState.OPEN
    assert sensor.available is True


@pytest.mark.asyncio
async def test_a_topic_for_nobody_is_ignored(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await z2m.report(ADDRESS, contact=True)

    await z2m.report("some/other/thing", contact=False)
    await z2m.report("0x00158d000000beef", contact=False)

    sensors = await provider.list()
    assert [item.sensor_id for item in sensors] == [ADDRESS]
    assert sensors[0].state is ContactState.CLOSED


def test_the_unavailable_errors_are_one_family():
    """A handler answering 503 must not catch only half of them."""
    from sensor_mqtt import MqttUnavailable
    from sensor_provider import ProviderUnavailable

    assert issubclass(MqttUnavailable, ProviderUnavailable)


def test_a_missing_client_library_is_reported_before_the_task_starts():
    """Left inside the loop, a missing package killed the task on its first
    pass while connect() went on to log that it was still trying."""
    import inspect

    import sensor_mqtt

    source = inspect.getsource(sensor_mqtt.AiomqttTransport.connect)
    assert "_require_aiomqtt()" in source
    # And the import itself is at module scope, never on an event-loop thread:
    # importing from inside the client task can deadlock against an import in
    # progress elsewhere, turning a missing broker into a hang.
    assert "_import_aiomqtt" not in inspect.getsource(sensor_mqtt.AiomqttTransport._run)


@pytest.mark.asyncio
async def test_a_deleted_sensor_does_not_keep_its_name_and_room(rig, events):
    """Zigbee2MQTT republishes bridge/devices *before* it answers the removal.

    So by the time the reply arrives the sensor is already out of the
    registry, and a cleanup conditional on finding it there never ran — the
    deleted sensor's name and room stayed on disk and would have come back
    with it.
    """
    provider, z2m, _transport, _clock, store = rig
    await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await provider.update(ADDRESS, name="Master Bedroom Left Window", zone_id="7")
    assert ADDRESS in store

    await provider.remove(ADDRESS)

    assert await provider.list() == []
    assert ADDRESS not in store



# -- knowing when a sensor was last heard from ------------------------------


@pytest.mark.asyncio
async def test_last_heard_survives_a_restart(rig, events):
    """A battery sensor is quiet by design, so the only way to tell a dead one
    from a merely silent one is when it last spoke.

    Taking "now" at registration made a sensor whose battery died days ago
    claim it had just been heard from, every time the application started —
    which is precisely the moment somebody is most likely to be looking.
    """
    provider, z2m, _transport, clock, _store = rig
    await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await z2m.report(ADDRESS, contact=True, last_seen=clock().isoformat())
    spoke_at = (await provider.list())[0].last_seen_at

    clock.advance(3 * 24 * 60 * 60)
    await provider.stop()
    await provider.start()
    await z2m.publish_devices()

    sensor = (await provider.list())[0]
    assert sensor.last_seen_at == spoke_at
    # The state itself does come back, from the broker's retained copy of the
    # last report — that is real information and worth having. What must not
    # come back with it is a fresh timestamp, which would dress three-day-old
    # news up as current.
    assert sensor.state is ContactState.CLOSED


@pytest.mark.asyncio
async def test_every_report_updates_what_is_remembered(rig, events):
    provider, z2m, _transport, clock, store = rig
    await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))
    await z2m.report(ADDRESS, contact=True)
    first = store[ADDRESS]["last_seen"]

    clock.advance(600)
    await z2m.report(ADDRESS, contact=False)

    assert store[ADDRESS]["last_seen"] != first


def test_metadata_written_before_last_seen_existed_still_loads(tmp_path):
    """Refusing the old three-field shape would throw away every name and room
    on the next start."""
    import json

    from sensor_persistence import SCHEMA_VERSION, load_zigbee_metadata

    path = tmp_path / "zigbee_sensor_metadata.json"
    path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "sensors": {
            ADDRESS: {"name": "Kitchen window", "kind": "window", "zone_id": "3"},
        },
    }), encoding="utf-8")

    loaded = load_zigbee_metadata(path)

    assert loaded[ADDRESS]["name"] == "Kitchen window"
    assert loaded[ADDRESS]["zone_id"] == "3"
    assert loaded[ADDRESS]["last_seen"] is None
    assert loaded[ADDRESS]["battery"] is None
    assert loaded[ADDRESS]["link_quality"] is None


def test_a_nonsense_last_seen_is_refused(tmp_path):
    import json

    from sensor_persistence import InvalidSensorData, SCHEMA_VERSION, load_zigbee_metadata

    path = tmp_path / "zigbee_sensor_metadata.json"
    for bad in ("not-a-date", "2026-09-16T12:00:00"):  # the second has no zone
        path.write_text(json.dumps({
            "schema_version": SCHEMA_VERSION,
            "sensors": {
                ADDRESS: {
                    "name": "x", "kind": "window", "zone_id": None, "last_seen": bad,
                },
            },
        }), encoding="utf-8")
        # _load backs up and returns the default rather than raising, so the
        # observable result is that nothing survives a corrupt file.
        assert load_zigbee_metadata(path) == {}


# -- routers ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_mains_device_is_reported_as_a_router_not_as_a_sensor(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(other_device("0x0017880100abcdef"))

    routers = await provider.routers()

    assert [r.router_id for r in routers] == ["0x0017880100abcdef"]
    assert routers[0].vendor == "IKEA"
    assert "TRETAKT" in (routers[0].description or "")
    # The thing that must never happen: a plug counted as a window.
    assert await provider.list() == []


@pytest.mark.asyncio
async def test_a_network_of_only_battery_sensors_reports_no_routers(rig, events):
    """The finding worth surfacing. A coordinator and nothing but sleeping
    sensors is a hub and spokes, and every distant sensor is weak for the same
    reason — which no per-sensor reading reveals."""
    provider, z2m = await started(rig, events)
    await z2m.add_device(contact_device(ADDRESS))

    assert await provider.routers() == []
    assert len(await provider.list()) == 1


@pytest.mark.asyncio
async def test_an_end_device_that_is_not_a_sensor_is_not_called_a_router(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(unhelpful_device("0x0017880100abcdef"))

    assert await provider.routers() == []
    assert await provider.list() == []


@pytest.mark.asyncio
async def test_a_router_that_goes_away_stops_being_listed(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(other_device("0x0017880100abcdef"))
    assert len(await provider.routers()) == 1

    z2m.devices = [
        d for d in z2m.devices if d["ieee_address"] != "0x0017880100abcdef"
    ]
    await z2m.publish_devices()

    assert await provider.routers() == []


@pytest.mark.asyncio
async def test_routers_and_sensors_coexist(rig, events):
    provider, z2m = await started(rig, events)
    await z2m.add_device(other_device("0x0017880100abcdef"))
    await z2m.add_device(contact_device(ADDRESS))

    assert len(await provider.routers()) == 1
    assert len(await provider.list()) == 1
