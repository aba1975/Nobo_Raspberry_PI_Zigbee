"""
Week profile names come off the hub padded, like every other name.

Found during Phase 5 commissioning. /api/week_profiles returned the hub's raw
strings, so a house with Norwegian room names produced:

    24  'Teknisk\xa0Rom'
    25  'Stue\xa0og\xa0Kjøkken'

The Nobø protocol has no way to send a space inside a name, so names travel with
non-breaking spaces and every other endpoint calls decode_hub_name on the way
out. This one did not.

Harmless when found -- core.js defines weekProfiles() but nothing renders it --
which is exactly why it is worth a test now rather than when something does.
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

# As the hub sends them.
RAW_PROFILES = {
    "23": {"week_profile_id": "23", "name": "Soverom",
           "profile": ["00001", "09000", "20001"]},
    "24": {"week_profile_id": "24", "name": "Teknisk\xa0Rom",
           "profile": ["00002"]},
    "25": {"week_profile_id": "25", "name": "Stue\xa0og\xa0Kjøkken",
           "profile": ["00001"]},
}


@pytest.fixture
def client():
    c = TestClient(app)
    c.cookies.set("session_id", TEST_SESSION_ID)
    return c


@pytest.fixture
def real_hub(monkeypatch):
    fake = MagicMock()
    fake.week_profiles = {k: dict(v) for k, v in RAW_PROFILES.items()}
    monkeypatch.setattr(server, "DEMO_MODE", False)
    monkeypatch.setattr(server, "hub", fake)
    monkeypatch.setattr(server, "hub_connected", True)
    return fake


def profiles(client):
    return {p["profile_id"]: p for p in client.get("/api/week_profiles").json()["week_profiles"]}


class TestWeekProfileNamesAreReadable:
    def test_padded_names_are_decoded(self, client, real_hub):
        got = profiles(client)
        assert got["24"]["name"] == "Teknisk Rom"
        assert got["25"]["name"] == "Stue og Kjøkken"

    def test_the_nested_copy_is_decoded_too(self, client, real_hub):
        """The inner object is the profile as the hub sent it, name included."""
        got = profiles(client)
        assert got["24"]["profile"]["name"] == "Teknisk Rom"

    def test_no_non_breaking_space_survives_anywhere(self, client, real_hub):
        assert "\xa0" not in client.get("/api/week_profiles").text

    def test_a_name_without_padding_is_left_alone(self, client, real_hub):
        assert profiles(client)["23"]["name"] == "Soverom"

    def test_the_schedule_itself_is_untouched(self, client, real_hub):
        assert profiles(client)["23"]["profile"]["profile"] == ["00001", "09000", "20001"]

    def test_decoding_does_not_mutate_the_hub_state(self, client, real_hub):
        """The response is a copy; pynobo's dict must still hold what it holds."""
        client.get("/api/week_profiles")
        assert real_hub.week_profiles["24"]["name"] == "Teknisk\xa0Rom"

    def test_a_nameless_profile_still_gets_one(self, client, real_hub):
        real_hub.week_profiles["99"] = {"week_profile_id": "99", "profile": []}
        assert profiles(client)["99"]["name"] == "Profile 99"