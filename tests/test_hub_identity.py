"""
Tests for what the hub says about itself.

Found during commissioning: the interface footer read "Nobø Hub · Version
Unknown" against a healthy hub on firmware 116. The endpoint was reading
``hub.hub_version`` and ``hub.hub_name`` through ``getattr`` with fallbacks, and
pynobo's client has neither attribute — so the fallbacks were the only answer it
could ever give, and the mistake was invisible because it looked like a hub that
had not told us anything.

The real values live in ``hub.hub_info``, filled in from the handshake. From the
hardware:

    {'serial': '102000147017', 'name': 'My\xa0Eco\xa0Hub',
     'software_version': '116', 'hardware_version': '11123610_rev._1',
     'production_date': '20211013', ...}

Firmware is worth showing. Version 115 has a fault that stops the hub reaching
the update service, and the only outward sign is a blinking LED.
"""

import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import server
from server import app

TEST_SESSION_ID = "pytest-fixed-session-id"

# Exactly what the hub in the cabin returns.
REAL_HUB_INFO = {
    "serial": "102000147017",
    "name": "My\xa0Eco\xa0Hub",
    "default_away_override_length": "46080",
    "override_id": "88",
    "software_version": "116",
    "hardware_version": "11123610_rev._1",
    "production_date": "20211013",
}


@pytest.fixture
def client():
    c = TestClient(app)
    c.cookies.set("session_id", TEST_SESSION_ID)
    return c


@pytest.fixture
def real_hub(monkeypatch):
    """Stand in for a connected hub, so this runs with no hardware."""
    fake = MagicMock()
    fake.hub_info = dict(REAL_HUB_INFO)
    # The bug was reading these. A MagicMock would happily invent them, which is
    # precisely the trap the original getattr fell into, so they are removed.
    del fake.hub_version
    del fake.hub_name
    monkeypatch.setattr(server, "DEMO_MODE", False)
    monkeypatch.setattr(server, "hub", fake)
    monkeypatch.setattr(server, "hub_connected", True)
    monkeypatch.setattr(server, "NOBO_IP", "10.42.0.227")
    monkeypatch.setattr(server, "NOBO_SERIAL", "102000147017")
    return fake


class TestTheHubIdentifiesItself:
    def test_the_firmware_version_is_the_real_one(self, client, real_hub):
        body = client.get("/api/hub").json()
        assert body["software_version"] == "116", "this read 'Unknown' before"

    def test_the_name_is_the_real_one_and_is_decoded(self, client, real_hub):
        """The hub pads names with non-breaking spaces on the wire."""
        body = client.get("/api/hub").json()
        assert body["name"] == "My Eco Hub"
        assert "\xa0" not in body["name"]

    def test_the_hardware_details_are_reported(self, client, real_hub):
        body = client.get("/api/hub").json()
        assert body["hardware_version"] == "11123610_rev._1"
        assert body["production_date"] == "2021-10-13"

    def test_the_serial_is_grouped_for_reading(self, client, real_hub):
        body = client.get("/api/hub").json()
        assert body["serial"] == "102000147017"
        assert body["serial_display"] == "102 000 147 017"

    def test_the_protocol_version_is_stated(self, client, real_hub):
        """The official app shows 1.1; it should not be a literal here."""
        import pynobo
        body = client.get("/api/hub").json()
        assert body["api_version"] == pynobo.nobo.API.VERSION == "1.1"

    def test_the_hub_address_is_reported(self, client, real_hub):
        assert client.get("/api/hub").json()["ip"] == "10.42.0.227"

    def test_a_hub_that_says_nothing_does_not_crash(self, client, real_hub):
        """Some fields can be absent; an empty hub_info must still answer."""
        real_hub.hub_info = {}
        body = client.get("/api/hub").json()
        assert body["software_version"] == "Unknown"
        assert body["hardware_version"] is None
        assert body["production_date"] is None
        assert body["name"] == "Nobø Hub"

    def test_an_unparseable_production_date_is_shown_anyway(self, client, real_hub):
        real_hub.hub_info = dict(REAL_HUB_INFO, production_date="not a date")
        assert client.get("/api/hub").json()["production_date"] == "not a date"

    def test_an_impossible_production_date_is_shown_anyway(self, client, real_hub):
        """Eight digits, but not a real day. Better shown than swallowed."""
        real_hub.hub_info = dict(REAL_HUB_INFO, production_date="20211345")
        assert client.get("/api/hub").json()["production_date"] == "20211345"


class TestDemoModeAnswersTheSameShape:
    def test_every_field_the_interface_reads_is_present(self, client):
        body = client.get("/api/hub").json()
        for key in ("name", "serial", "serial_display", "software_version",
                    "hardware_version", "production_date", "api_version",
                    "ip", "connected", "demo_mode"):
            assert key in body, f"demo mode is missing {key}"
        assert body["demo_mode"] is True