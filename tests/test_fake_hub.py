"""Prove the fake hub is faithful enough for the real pynobo client.

If these fail, every other real-hub test is worthless — the fake has drifted
from the protocol and the client can no longer talk to it.
"""

import asyncio

import pytest
import pynobo

from server import HubLoop
from tests.fake_hub import NBSP, FakeHubThread

HUB_SERIAL = "123123123123"


@pytest.fixture
def fake_hub():
    with FakeHubThread() as hub:
        yield hub


@pytest.fixture
def loop():
    hub_loop = HubLoop()
    hub_loop.start()
    yield hub_loop
    hub_loop.shutdown()


@pytest.fixture
def client(fake_hub, loop):
    hub = pynobo.nobo(HUB_SERIAL, "127.0.0.1", discover=False, synchronous=False)
    loop.run(hub.start(), timeout=30)
    yield hub
    loop.run(hub.stop(), timeout=10)


def test_handshake_and_initial_data(client):
    assert client.connected is True
    assert len(client.zones) == 2
    assert len(client.components) == 2
    assert len(client.week_profiles) == 1
    assert client.hub_info["name"] == f"Fake{NBSP}Hub"


def test_names_with_spaces_survive_the_round_trip(client):
    # Spaces travel as non-breaking spaces; if either side forgets to convert,
    # the name arrives mangled or the field count is wrong.
    zone = client.zones["1"]
    assert zone["name"] == f"Living{NBSP}Room"


def test_serial_mismatch_is_rejected(fake_hub, loop):
    with pytest.raises(Exception):
        client = pynobo.nobo("999999999999", "127.0.0.1", discover=False, synchronous=False)
        loop.run(client.start(), timeout=30)


def test_update_zone_reaches_the_hub(fake_hub, client, loop):
    loop.run(client.async_update_zone("1", name="Kitchen", temp_comfort_c=22))

    _wait_until(lambda: fake_hub.zone_named("Kitchen") is not None)
    zone = fake_hub.zone_named("Kitchen")
    assert zone is not None
    assert zone[3] == "22"


def test_add_and_remove_week_profile(fake_hub, client, loop):
    profile = ["00000", "07001", "23000"] * 7
    before = set(fake_hub.week_profiles)

    loop.run(client.async_add_week_profile("Test Profile", profile))

    _wait_until(lambda: set(fake_hub.week_profiles) != before)
    new_id = (set(fake_hub.week_profiles) - before).pop()
    # The hub assigns the id, not the client.
    assert new_id not in before
    assert fake_hub.week_profiles[new_id][1] == f"Test{NBSP}Profile"


def test_search_and_pair(fake_hub, client, loop):
    found = []
    original = client.response_handler

    def capture(response):
        if response and response[0] in ("Y00", "Y01", "Y03", "Y04"):
            found.append(response)
            return
        original(response)

    client.response_handler = capture

    loop.run(client.async_send_command(["X00"]))

    _wait_until(lambda: any(r[0] == "Y04" for r in found))
    assert ["Y00"] in found
    assert ["Y04", "186100000009"] in found

    loop.run(client.async_send_command(["X03", "186100000009"]))

    _wait_until(lambda: any(r[0] == "Y03" for r in found))
    assert ["Y03", "186100000009", "1"] in found


def test_error_response_for_unknown_command(fake_hub, client, loop):
    errors = []
    original = client.response_handler

    def capture(response):
        if response and response[0].startswith("E"):
            errors.append(response)
            return
        original(response)

    client.response_handler = capture

    loop.run(client.async_send_command(["Z99"]))

    _wait_until(lambda: bool(errors))
    assert errors[0][0] == "E00"


def _wait_until(predicate, timeout=5.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met within timeout")
