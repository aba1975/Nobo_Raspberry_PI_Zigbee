"""The demo house used for contact-sensor development matches the real rooms."""

import copy

import config_persistence
import server


EXPECTED = {
    "Master Bedroom": ["160004028117"],
    "Bunk Room by Entrance": ["160004028118"],
    "Bunk Room by Large Bathroom": ["160004028119"],
    "Left Bedroom": ["160004028113"],
    "Right Bedroom": ["160004028112"],
    "Kitchen": ["160004028114"],
    "Living Room": ["160004028115", "234000012006"],
}


def test_split_room_defaults_have_the_expected_components():
    by_name = {zone["name"]: zone for zone in server._DEFAULT_DEMO_ZONES}
    for name, components in EXPECTED.items():
        assert by_name[name]["components"] == components

    all_components = [
        component
        for zone in server._DEFAULT_DEMO_ZONES
        for component in zone["components"]
    ]
    assert len(all_components) == len(set(all_components))
    assert len(server._DEFAULT_DEMO_ZONES) == 12


def test_only_the_living_room_keeps_the_sw4_temperature():
    by_name = {zone["name"]: zone for zone in server._DEFAULT_DEMO_ZONES}
    assert by_name["Living Room"]["current_temp"] == 20.4
    assert by_name["Kitchen"]["current_temp"] is None


def test_grouped_fixture_migration_splits_rooms_and_schedules(monkeypatch):
    original_zones = copy.deepcopy(server.DEMO_ZONES)
    original_schedules = copy.deepcopy(server.demo_schedules)
    defaults = {zone["name"]: copy.deepcopy(zone) for zone in server._DEFAULT_DEMO_ZONES}

    grouped = [
        copy.deepcopy(zone)
        for zone in server._DEFAULT_DEMO_ZONES
        if int(zone["zone_id"]) <= 8
    ]
    by_id = {zone["zone_id"]: zone for zone in grouped}
    by_id["4"].update({
        "name": "Upstairs Bedrooms",
        "rooms": ["North", "South"],
        "components": ["160004028112", "160004028113"],
        "component_names": ["North Room Heater", "South Room Heater"],
    })
    by_id["5"].update({
        "name": "Living Area",
        "rooms": ["Kitchen", "Living Room"],
        "components": ["160004028114", "160004028115", "234000012006"],
        "component_names": ["Kitchen Heater", "Living Room Heater", "Living Room Panel"],
        "current_temp": 20.4,
    })
    by_id["7"].update({
        "name": "Downstairs Bedrooms",
        "rooms": ["Master", "North", "South"],
        "components": ["160004028117", "160004028118", "160004028119"],
        "component_names": ["Master Heater", "North Heater", "South Heater"],
    })

    saved_zones = []
    saved_schedules = []
    monkeypatch.setattr(config_persistence, "save_demo_zones",
                        lambda zones: saved_zones.append(copy.deepcopy(zones)))
    monkeypatch.setattr(config_persistence, "save_demo_schedules",
                        lambda schedules: saved_schedules.append(copy.deepcopy(schedules)))

    try:
        server.DEMO_ZONES[:] = grouped
        server.demo_schedules.clear()
        server.demo_schedules.update({"4": {"monday": []}, "7": {"tuesday": []}})

        assert server._migrate_grouped_demo_rooms() is True
        by_name = {zone["name"]: zone for zone in server.DEMO_ZONES}
        for name, components in EXPECTED.items():
            assert by_name[name]["components"] == components
        assert server.demo_schedules["11"] == server.demo_schedules["4"]
        assert server.demo_schedules["9"] == server.demo_schedules["7"]
        assert server.demo_schedules["10"] == server.demo_schedules["7"]
        assert saved_zones and saved_schedules

        assert server._migrate_grouped_demo_rooms() is False
        assert len(saved_zones) == 1
    finally:
        server.DEMO_ZONES[:] = original_zones
        server.demo_schedules.clear()
        server.demo_schedules.update(original_schedules)


def test_split_fixture_repairs_side_data_after_interrupted_migration(monkeypatch):
    original_zones = copy.deepcopy(server.DEMO_ZONES)
    original_schedules = copy.deepcopy(server.demo_schedules)
    saved_schedules = []
    monkeypatch.setattr(
        config_persistence,
        "save_demo_schedules",
        lambda schedules: saved_schedules.append(copy.deepcopy(schedules)),
    )
    monkeypatch.setattr(config_persistence, "load_away_exceptions", lambda: [])
    monkeypatch.setattr(config_persistence, "load_away_exceptions_applied", lambda: [])

    try:
        server.DEMO_ZONES[:] = copy.deepcopy(server._DEFAULT_DEMO_ZONES)
        server.demo_schedules.clear()
        server.demo_schedules.update({"4": {"monday": []}, "7": {"tuesday": []}})

        assert server._migrate_grouped_demo_rooms() is True
        assert server.demo_schedules["11"] == server.demo_schedules["4"]
        assert server.demo_schedules["9"] == server.demo_schedules["7"]
        assert server.demo_schedules["10"] == server.demo_schedules["7"]
        assert saved_schedules
    finally:
        server.DEMO_ZONES[:] = original_zones
        server.demo_schedules.clear()
        server.demo_schedules.update(original_schedules)
