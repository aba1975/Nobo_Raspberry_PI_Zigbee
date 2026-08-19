"""End-to-end tests for the real-hub code paths.

Every other test module runs in demo mode, which means the real-hub half of
each endpoint was never executed by anything. That is how the app shipped for
so long with features that "did not work with a real hub" — nothing proved
whether they did or not.

These tests point the actual FastAPI app at ``tests.fake_hub``, a protocol-level
Nobø hub, and drive it through the HTTP API exactly as the browser does. The
fake speaks the documented wire protocol and the real ``pynobo`` client talks to
it, so what is under test here is the app's own logic: name encoding, week
profile sharing, component field handling, pairing and error mapping.

This is *not* a substitute for a real hub. The fake encodes the same reading of
the specification that the app does, so a genuine hub that behaves differently
would fool both. See README, "Verified against a fake hub, not real hardware".
"""

import os
import time

import pytest
from fastapi.testclient import TestClient

import server
from server import app
from tests.fake_hub import NBSP, FakeHubThread, decode_name

HUB_SERIAL = "123123123123"
SESSION_ID = "pytest-fixed-session-id"


def wait_until(predicate, timeout=10.0, message="condition not met in time"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


@pytest.fixture
def hub_env(monkeypatch, tmp_path):
    """Point the app at a fake hub and connect to it for real."""
    with FakeHubThread() as fake:
        monkeypatch.setattr(server, "DEMO_MODE", False)
        monkeypatch.setattr(server, "NOBO_SERIAL", HUB_SERIAL)
        monkeypatch.setattr(server, "NOBO_IP", "127.0.0.1")
        # Zone icons are an app-local concept and must not leak between tests.
        monkeypatch.setattr(
            server.config_persistence, "ZONE_ICONS_FILE", tmp_path / "zone_icons.json"
        )

        server.connect_to_hub_sync()
        try:
            yield fake
        finally:
            server.disconnect_from_hub()


@pytest.fixture
def client(hub_env):
    with TestClient(app) as test_client:
        test_client.cookies.set("session_id", SESSION_ID)
        yield test_client


@pytest.fixture
def fake(hub_env):
    return hub_env


# ---------------------------------------------------------------------------
# Connection and reading
# ---------------------------------------------------------------------------


class TestReadingFromARealHub:
    def test_zones_are_listed(self, client):
        response = client.get("/api/zones")
        assert response.status_code == 200
        names = {z["name"] for z in response.json()["zones"]}
        assert names == {"Living Room", "Bathroom"}

    def test_zone_names_have_no_non_breaking_spaces(self, client):
        """The hub stores spaces as U+00A0 and pynobo does not decode them.

        Left alone the names look right but compare, sort and search wrong.
        """
        for zone in client.get("/api/zones").json()["zones"]:
            assert NBSP not in zone["name"], f"undecoded name: {zone['name']!r}"

    def test_device_names_come_from_the_hub(self, client):
        """This used to be a list of empty strings regardless of the hub."""
        zones = {z["name"]: z for z in client.get("/api/zones").json()["zones"]}
        assert zones["Living Room"]["components_names"] == ["Living Room Panel"]

    def test_devices_endpoint_lists_hub_components(self, client):
        devices = client.get("/api/devices").json()["devices"]
        by_serial = {d["serial"]: d for d in devices}
        assert "186100000001" in by_serial
        assert by_serial["186100000001"]["name"] == "Living Room Panel"


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilitiesAgainstARealHub:
    def test_editing_features_are_no_longer_gated(self, client):
        """These used to be demo-only, and the UI greyed them out.

        The endpoint only lists features that are restricted in some mode, so
        the assertion is that they are either absent or explicitly supported.
        """
        features = client.get("/api/capabilities").json()["features"]
        for feature in (
            "add_zone", "delete_zone", "edit_schedule", "add_device",
            "rename_device", "move_device", "remove_device", "replace_device",
        ):
            assert features.get(feature, {"supported": True})["supported"], feature

    def test_discovery_is_available_only_here(self, client):
        features = client.get("/api/capabilities").json()["features"]
        assert features["discover_devices"]["supported"] is True


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------


class TestZoneManagement:
    def test_add_zone_reaches_the_hub_and_returns_its_id(self, client, fake):
        response = client.post("/api/zones", json={"name": "Cellar", "icon": "🏠"})
        assert response.status_code == 200, response.text
        zone_id = response.json()["zone_id"]

        wait_until(lambda: fake.zone_named("Cellar") is not None)
        # The hub assigns the id; inventing one client-side would collide.
        assert fake.zone_named("Cellar")[0] == zone_id

    def test_add_zone_rejects_a_duplicate_name(self, client):
        response = client.post("/api/zones", json={"name": "Bathroom"})
        assert response.status_code == 400
        assert "already" in response.json()["detail"].lower()

    def test_add_zone_rejects_an_over_long_name(self, client):
        response = client.post("/api/zones", json={"name": "x" * 101})
        assert response.status_code == 400

    def test_rename_zone_reaches_the_hub(self, client, fake):
        response = client.put("/api/zones/1", json={"name": "Lounge"})
        assert response.status_code == 200, response.text
        wait_until(lambda: fake.zone_named("Lounge") is not None)

    def test_zone_icon_is_stored_locally(self, client):
        """The hub has no icon field, so the app keeps it in data/."""
        assert client.put("/api/zones/1", json={"icon": "🛋️"}).status_code == 200
        zones = {z["zone_id"]: z for z in client.get("/api/zones").json()["zones"]}
        assert zones["1"]["icon"] == "🛋️"

    def test_delete_zone_refuses_while_devices_remain(self, client):
        """A hub silently orphans the devices; that is worse than an error."""
        response = client.delete("/api/zones/1")
        assert response.status_code == 409
        assert "device" in response.json()["detail"].lower()

    def test_delete_zone_succeeds_once_it_is_empty(self, client, fake):
        assert client.delete("/api/devices/186100000001").status_code == 200
        wait_until(lambda: "186100000001" not in fake.components)

        assert client.delete("/api/zones/1").status_code == 200
        wait_until(lambda: "1" not in fake.zones)


# ---------------------------------------------------------------------------
# Week profiles / schedules
# ---------------------------------------------------------------------------


class TestScheduleReading:
    def test_schedule_is_returned_in_the_same_shape_as_demo_mode(self, client):
        response = client.get("/api/zones/1/schedule")
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body["schedule"]) == {
            "monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday",
        }
        monday = body["schedule"]["monday"]
        assert monday[0]["start"] == "00:00"
        assert monday[0]["mode"] == "eco"

    def test_shared_profiles_are_reported(self, client):
        """Both zones start on the factory profile; the user must be told."""
        body = client.get("/api/zones/1/schedule").json()
        assert body["week_profile_name"] == "Default"
        assert sorted(body["shared_with_zones"]) == ["Bathroom"]


class TestScheduleWriting:
    SIMPLE_DAY = [
        {"start": "00:00", "end": "06:00", "mode": "eco"},
        {"start": "06:00", "end": "22:00", "mode": "comfort"},
        {"start": "22:00", "end": "24:00", "mode": "eco"},
    ]

    def _week(self, monday=None):
        week = {
            day: list(self.SIMPLE_DAY)
            for day in ("monday", "tuesday", "wednesday", "thursday",
                        "friday", "saturday", "sunday")
        }
        if monday is not None:
            week["monday"] = monday
        return week

    def test_editing_a_shared_profile_creates_a_private_copy(self, client, fake):
        """Editing the shared profile in place would change every zone.

        The zone must end up on a profile of its own, and the other zone must
        keep the one it had.
        """
        before = set(fake.week_profiles)
        response = client.post("/api/zones/1/schedule", json={"schedule": self._week()})
        assert response.status_code == 200, response.text

        wait_until(lambda: set(fake.week_profiles) != before)
        new_id = (set(fake.week_profiles) - before).pop()
        wait_until(lambda: fake.zones["1"][2] == new_id)
        assert fake.zones["2"][2] == "1", "the other zone was moved off its profile"
        # Never silently trash the factory default.
        assert "1" in fake.week_profiles

    def test_editing_an_owned_profile_edits_it_in_place(self, client, fake):
        # Give zone 1 a profile of its own first.
        assert client.post(
            "/api/zones/1/schedule", json={"schedule": self._week()}
        ).status_code == 200
        wait_until(lambda: fake.zones["1"][2] != "1")
        owned = fake.zones["1"][2]

        profiles_before = set(fake.week_profiles)
        assert client.post(
            "/api/zones/1/schedule",
            json={"schedule": self._week(monday=[
                {"start": "00:00", "end": "24:00", "mode": "away"},
            ])},
        ).status_code == 200

        wait_until(lambda: "00002" in fake.week_profiles[owned][2])
        assert set(fake.week_profiles) == profiles_before, "a redundant profile was created"
        assert fake.zones["1"][2] == owned

    def test_off_is_a_valid_schedule_mode(self, client, fake):
        assert client.post(
            "/api/zones/1/schedule",
            json={"schedule": self._week(monday=[
                {"start": "00:00", "end": "24:00", "mode": "off"},
            ])},
        ).status_code == 200

        # 4 is "off" — API_Nobo.pdf page 6 says 3, but pynobo's own validator
        # only accepts 0/1/2/4 and the hub agrees with pynobo.
        wait_until(lambda: any(
            "00004" in profile[2] for profile in fake.week_profiles.values()
        ))

    def test_a_written_schedule_reads_back_identically(self, client):
        written = self._week()
        assert client.post(
            "/api/zones/1/schedule", json={"schedule": written}
        ).status_code == 200

        read_back = client.get("/api/zones/1/schedule").json()["schedule"]
        for day, blocks in written.items():
            assert [
                {"start": b["start"], "end": b["end"], "mode": b["mode"]}
                for b in read_back[day]
            ] == blocks, day

    def test_an_invalid_mode_is_rejected_before_it_reaches_the_hub(self, client, fake):
        commands_before = len(fake.received)
        response = client.post(
            "/api/zones/1/schedule",
            json={"schedule": self._week(monday=[
                {"start": "00:00", "end": "24:00", "mode": "sauna"},
            ])},
        )
        assert response.status_code == 400
        assert len(fake.received) == commands_before


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


class TestDeviceManagement:
    def test_rename_device_reaches_the_hub(self, client, fake):
        response = client.patch(
            "/api/devices/186100000001/name", json={"name": "Sofa Panel"}
        )
        assert response.status_code == 200, response.text
        wait_until(lambda: fake.component_name("186100000001") == "Sofa Panel")

    def test_rename_keeps_every_other_component_field(self, client, fake):
        before = list(fake.components["186100000001"])
        assert client.patch(
            "/api/devices/186100000001/name", json={"name": "Sofa Panel"}
        ).status_code == 200
        wait_until(lambda: fake.component_name("186100000001") == "Sofa Panel")

        after = fake.components["186100000001"]
        assert after[0] == before[0]      # serial
        assert after[3] == before[3]      # reverse
        assert after[4] == before[4]      # zone
        assert after[6] == before[6]      # temp sensor zone

    def test_move_device_reaches_the_hub(self, client, fake):
        response = client.post(
            "/api/devices/186100000001/move", json={"new_zone_id": "2"}
        )
        assert response.status_code == 200, response.text
        wait_until(lambda: fake.components["186100000001"][4] == "2")

    def test_move_device_rejects_an_unknown_zone(self, client):
        response = client.post(
            "/api/devices/186100000001/move", json={"new_zone_id": "999"}
        )
        assert response.status_code == 404

    def test_a_temperature_sensor_is_not_reassigned_by_a_rename(self, client, fake):
        """pynobo rewrites zone_id from tempsensor_for_zone_id on read.

        Echoing its component dict back to the hub would move an unassigned
        sensor into the zone it merely reports temperature for. The app reads
        raw protocol rows instead; this proves it.
        """
        fake.components["186100000003"] = [
            "186100000003", "0", "Hall\u00a0Sensor", "0", "-1", "-1", "2",
        ]
        # Reconnect so the app sees it in the initial dump.
        server.disconnect_from_hub()
        server.connect_to_hub_sync()

        assert client.patch(
            "/api/devices/186100000003/name", json={"name": "Hall Thermostat"}
        ).status_code == 200
        wait_until(lambda: fake.component_name("186100000003") == "Hall Thermostat")

        assert fake.components["186100000003"][4] == "-1", "sensor was moved into a zone"
        assert fake.components["186100000003"][6] == "2"

    def test_remove_device_reaches_the_hub(self, client, fake):
        assert client.delete("/api/devices/186100000001").status_code == 200
        wait_until(lambda: "186100000001" not in fake.components)

    def test_names_with_spaces_round_trip(self, client, fake):
        assert client.patch(
            "/api/devices/186100000001/name", json={"name": "Sofa Panel Two"}
        ).status_code == 200
        wait_until(lambda: fake.component_name("186100000001") == "Sofa Panel Two")
        # Stored encoded on the wire, shown decoded to the user.
        assert NBSP in fake.components["186100000001"][2]
        assert client.get("/api/devices").json()["devices"]

    def test_an_over_long_device_name_is_rejected(self, client):
        response = client.patch(
            "/api/devices/186100000001/name", json={"name": "x" * 101}
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Discovery and pairing
# ---------------------------------------------------------------------------


class TestDeviceDiscovery:
    def test_search_finds_a_device_in_pairing_mode(self, client):
        assert client.post("/api/devices/search").json()["status"] == "searching"

        def found():
            return any(
                d["serial"] == "186100000009"
                for d in client.get("/api/devices/search").json()["devices"]
            )

        wait_until(found, message="the search never reported the device")

    def test_a_known_device_is_flagged_as_already_registered(self, client, fake):
        fake.discoverable = ["186100000001"]
        client.post("/api/devices/search")

        def flagged():
            devices = client.get("/api/devices/search").json()["devices"]
            return any(d["serial"] == "186100000001" and d["already_registered"]
                       for d in devices)

        wait_until(flagged)

    def test_search_can_be_stopped(self, client):
        client.post("/api/devices/search")
        assert client.delete("/api/devices/search").json()["status"] == "stopped"

    def test_pairing_adds_the_device_to_the_zone(self, client, fake):
        response = client.post(
            "/api/devices",
            json={"serial": "186100000009", "zone_id": "2", "name": "New Panel"},
        )
        assert response.status_code == 200, response.text
        wait_until(lambda: "186100000009" in fake.components)
        assert fake.component_name("186100000009") == "New Panel"
        assert fake.components["186100000009"][4] == "2"

    def test_a_refused_pairing_is_reported(self, client, fake):
        fake.pair_should_succeed = False
        response = client.post(
            "/api/devices", json={"serial": "186100000009", "zone_id": "2"}
        )
        assert response.status_code in (502, 504), response.text
        assert "186100000009" not in fake.components

    def test_pairing_an_already_known_device_is_rejected(self, client):
        response = client.post(
            "/api/devices", json={"serial": "186100000001", "zone_id": "2"}
        )
        assert response.status_code == 400


class TestReplaceDevice:
    def test_replace_pairs_the_new_device_and_removes_the_old(self, client, fake):
        response = client.put(
            "/api/devices/186100000001",
            json={"new_serial": "186100000009"},
        )
        assert response.status_code == 200, response.text
        wait_until(lambda: "186100000009" in fake.components)
        wait_until(lambda: "186100000001" not in fake.components)
        assert fake.components["186100000009"][4] == "1"

    def test_a_failed_replacement_leaves_the_old_device_in_place(self, client, fake):
        """Removing first would strand the zone if pairing then failed."""
        fake.pair_should_succeed = False
        response = client.put(
            "/api/devices/186100000001",
            json={"new_serial": "186100000009"},
        )
        assert response.status_code in (502, 504)
        assert "186100000001" in fake.components


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------


class TestZoneControl:
    def test_setting_a_zone_override_reaches_the_hub(self, client, fake):
        response = client.post("/api/zones/1/override/comfort")
        assert response.status_code == 200, response.text
        wait_until(lambda: bool(fake.commands_of_type("A03")))

    def test_setting_zone_temperatures_reaches_the_hub(self, client, fake):
        response = client.post(
            "/api/zones/1/temperature", json={"comfort": 23, "eco": 19}
        )
        assert response.status_code == 200, response.text
        wait_until(lambda: fake.zones["1"][3] == "23" and fake.zones["1"][4] == "19")

    def test_off_is_still_not_a_valid_override(self, client):
        """It is a week-profile state only; the hub has no "off" override."""
        assert client.post("/api/zones/1/override/off").status_code == 400
