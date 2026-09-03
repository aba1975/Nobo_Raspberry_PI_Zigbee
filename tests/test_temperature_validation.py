"""
Temperature set point validation (QA defects D-08, D-09, D-10).

Three separate ways the temperature endpoint used to mislead people:

  D-08  eco could be set at or above comfort, so the zone never saved energy
        when it dropped to eco, with nothing in the UI to explain why.
  D-09  set points were truncated, so asking for 20.6°C silently gave 20°C
        and the room ran colder than requested.
  D-10  a body with neither value returned "success" while changing nothing.
"""

import os
import sys

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import server
from server import app, resolve_temperature_update, round_to_whole_degree, TemperatureUpdate
from fastapi import HTTPException


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.cookies.set("session_id", "pytest-fixed-session-id")
        yield c


@pytest.fixture
def zone(client):
    """A demo zone whose devices accept a remote temperature change."""
    for z in server.DEMO_ZONES:
        for comp in z['components']:
            _, comfort, eco = server.detect_device_type(comp)
            if comfort or eco:
                return z
    pytest.skip("no demo zone supports remote temperature changes")


def temps(comfort=None, eco=None):
    return TemperatureUpdate(comfort=comfort, eco=eco)


class TestRounding:
    """D-09"""

    @pytest.mark.parametrize(
        "value,expected",
        [
            (20.0, 20),
            (20.4, 20),
            (20.5, 21),
            (20.6, 21),
            (20.9, 21),
            (7.5, 8),
            (29.5, 30),
        ],
    )
    def test_rounds_to_nearest_degree(self, value, expected):
        assert round_to_whole_degree(value) == expected

    def test_a_request_just_below_the_half_degree_is_not_rounded_up(self):
        assert round_to_whole_degree(20.49) == 20

    def test_endpoint_stores_the_rounded_value(self, client, zone):
        r = client.post(f"/api/zones/{zone['zone_id']}/temperature", json={"comfort": 22.6})
        assert r.status_code == 200
        assert zone['comfort_temp'] == 23.0, "22.6°C was truncated to 22°C again"


class TestTheHubGivesUsStrings:
    """
    Found during commissioning against a real hub, 3 September 2026.

    The Nobø protocol is text on the wire, so pynobo keeps every zone value
    exactly as it arrived — ``temp_comfort_c`` is the string ``'17'``, not the
    number 17. Setting one set point on its own fills the other in from the
    hub, so the old ``value + 0.5`` evaluated ``'15' + 0.5`` and raised
    TypeError. Every temperature change from the web interface answered 500,
    and the raw Python message was shown to the user as a toast.

    It survived because demo mode stores floats and every test above passes
    ints, so nothing in the suite ever modelled what the hub actually sends.
    These tests do.
    """

    @pytest.mark.parametrize("value,expected", [("17", 17), ("7", 7), ("30", 30)])
    def test_a_whole_degree_string_from_the_hub_is_accepted(self, value, expected):
        assert round_to_whole_degree(value) == expected

    def test_a_decimal_string_is_accepted_too(self):
        assert round_to_whole_degree("20.6") == 21

    def test_ints_and_floats_still_work(self):
        assert round_to_whole_degree(17) == 17
        assert round_to_whole_degree(20.6) == 21

    def test_setting_only_comfort_when_the_hub_holds_strings(self):
        """The exact call that returned 500 on the production hub."""
        assert resolve_temperature_update(temps(comfort=20), "17", "15") == (20, 15)

    def test_setting_only_eco_when_the_hub_holds_strings(self):
        assert resolve_temperature_update(temps(eco=16), "22", "16") == (22, 16)

    def test_supplying_both_still_works(self):
        """This path always worked, which is what masked the fault."""
        assert resolve_temperature_update(temps(comfort=20, eco=15), "17", "15") == (20, 15)

    def test_the_comfort_eco_ordering_is_still_enforced_with_strings(self):
        """The string path must not skip validation on its way through."""
        with pytest.raises(HTTPException) as exc:
            resolve_temperature_update(temps(eco=25), "20", "15")
        assert exc.value.status_code == 400

    def test_a_nonsense_value_from_the_hub_is_not_a_500(self):
        """
        If the hub ever sends something unreadable that is the hub's problem,
        and the user should be told that rather than shown a Python traceback.
        """
        with pytest.raises(HTTPException) as exc:
            round_to_whole_degree("N/A")
        assert exc.value.status_code == 502
        assert "N/A" in exc.value.detail

    def test_none_is_not_a_500_either(self):
        with pytest.raises(HTTPException) as exc:
            round_to_whole_degree(None)
        assert exc.value.status_code == 502


class TestEcoBelowComfort:
    """D-08"""

    def test_eco_above_comfort_is_rejected(self):
        with pytest.raises(HTTPException) as exc:
            resolve_temperature_update(temps(comfort=20, eco=22), 21, 17)
        assert exc.value.status_code == 400
        assert "lower than the comfort" in exc.value.detail

    def test_eco_equal_to_comfort_is_rejected(self):
        with pytest.raises(HTTPException) as exc:
            resolve_temperature_update(temps(comfort=20, eco=20), 21, 17)
        assert exc.value.status_code == 400

    def test_eco_below_comfort_is_accepted(self):
        assert resolve_temperature_update(temps(comfort=22, eco=18), 21, 17) == (22, 18)

    def test_checked_against_the_existing_setting_when_only_one_is_sent(self):
        """Sending eco alone must still be compared with the stored comfort."""
        with pytest.raises(HTTPException) as exc:
            resolve_temperature_update(temps(eco=24), current_comfort=21, current_eco=17)
        assert exc.value.status_code == 400

        with pytest.raises(HTTPException) as exc:
            resolve_temperature_update(temps(comfort=16), current_comfort=21, current_eco=17)
        assert exc.value.status_code == 400

    def test_ordering_is_checked_after_rounding(self):
        """20.6 and 20.4 both become 21 and 20, which is still valid."""
        assert resolve_temperature_update(temps(comfort=20.6, eco=20.4), 21, 17) == (21, 20)

        # ...but 20.4 and 19.6 both round to 20, which is not.
        with pytest.raises(HTTPException):
            resolve_temperature_update(temps(comfort=20.4, eco=19.6), 21, 17)

    def test_endpoint_rejects_eco_above_comfort(self, client, zone):
        r = client.post(
            f"/api/zones/{zone['zone_id']}/temperature",
            json={"comfort": 20, "eco": 22},
        )
        assert r.status_code == 400
        assert "lower than the comfort" in r.json()["detail"]

    def test_rejected_request_changes_nothing(self, client, zone):
        before = (zone['comfort_temp'], zone['eco_temp'])
        client.post(
            f"/api/zones/{zone['zone_id']}/temperature",
            json={"comfort": 20, "eco": 22},
        )
        assert (zone['comfort_temp'], zone['eco_temp']) == before


class TestEmptyRequest:
    """D-10"""

    def test_empty_body_is_rejected(self):
        with pytest.raises(HTTPException) as exc:
            resolve_temperature_update(temps(), 21, 17)
        assert exc.value.status_code == 400
        assert "comfort and/or eco" in exc.value.detail

    def test_endpoint_rejects_an_empty_body(self, client, zone):
        r = client.post(f"/api/zones/{zone['zone_id']}/temperature", json={})
        assert r.status_code == 400, "an empty request reported success again"

    def test_endpoint_rejects_explicit_nulls(self, client, zone):
        r = client.post(
            f"/api/zones/{zone['zone_id']}/temperature",
            json={"comfort": None, "eco": None},
        )
        assert r.status_code == 400


class TestRangeStillEnforced:
    @pytest.mark.parametrize("payload", [{"comfort": 6.9}, {"comfort": 30.1}, {"eco": 6.9}, {"eco": 31}])
    def test_out_of_range_is_rejected(self, client, zone, payload):
        r = client.post(f"/api/zones/{zone['zone_id']}/temperature", json=payload)
        assert r.status_code == 400, f"{payload} on zone {zone['zone_id']} -> {r.status_code} {r.text}"
        assert "between 7 and 30" in r.json()["detail"], f"{payload} -> {r.text}"

    def test_range_is_checked_before_ordering(self):
        """The range message is the more useful one, so it must win."""
        with pytest.raises(HTTPException) as exc:
            resolve_temperature_update(temps(comfort=5, eco=40), 21, 17)
        assert "between 7 and 30" in exc.value.detail
