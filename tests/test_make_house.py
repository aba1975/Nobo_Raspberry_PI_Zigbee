"""scripts/make_house.py: a floor-plan template, written with names, made
into the house.json one Pi accepts (docs/HOUSE.md, "Loading your own
building").

The script runs against a real Pi over HTTP. Here its requests are routed to
the application itself, in demo mode, so every test exercises the same sign
in, the same reads and the same validation it will meet on a Pi.
"""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import house_model
import server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import make_house  # noqa: E402

EXAMPLE = ROOT / "docs" / "house.template.example.json"
DEMO_HOUSE = ROOT / "app" / "demo_house.json"
PASSWORD = "planner-password-1"


class ClientPi(make_house.Pi):
    """make_house.Pi, talking to the application in this process."""

    def __init__(self, client):
        self.client = client
        self.calls = []

    def request(self, method, path, body=None, content_type=None):
        self.calls.append((method, path))
        headers = {"Content-Type": content_type} if content_type else {}
        response = self.client.request(method, path, content=body, headers=headers)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None


@pytest.fixture
def planner():
    users = auth.load_users()
    users["planner"] = {"password_hash": auth.hash_password(PASSWORD), "role": "admin"}
    auth.save_users(users)
    return "planner"


@pytest.fixture
def pi(planner):
    # No session cookie: the script has to sign in for itself.
    with TestClient(server.app) as client:
        yield ClientPi(client)


def example():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def run(pi, monkeypatch, tmp_path, *extra):
    monkeypatch.setattr(make_house, "Pi", lambda *a, **k: pi)
    monkeypatch.setenv("NOBO_PASSWORD", PASSWORD)
    out = tmp_path / "house.json"
    code = make_house.main(["--template", str(EXAMPLE), "--user", "planner", "-o", str(out), *extra])
    return code, out


def test_the_example_template_makes_the_demo_building(pi, planner):
    pi.login(planner, PASSWORD)
    zones, devices, sensors = make_house.read_pi(pi)
    house, notes = make_house.resolve(example(), zones, devices, sensors)
    assert make_house.check(house) == house_model.validate(json.loads(DEMO_HOUSE.read_text(encoding="utf-8")))


def test_it_only_reads_unless_asked_to_upload(pi, monkeypatch, tmp_path):
    code, out = run(pi, monkeypatch, tmp_path)
    assert code == 0
    assert house_model.validate(json.loads(out.read_text(encoding="utf-8")))
    writes = [(m, p) for m, p in pi.calls if m != "GET"]
    assert writes == [("POST", "/auth/login"), ("POST", "/auth/logout")]


def test_upload_loads_it_on_the_pi(pi, monkeypatch, tmp_path):
    code, out = run(pi, monkeypatch, tmp_path, "--upload", "--yes")
    assert code == 0
    assert ("PUT", "/api/house") in pi.calls
    model, source = house_model.load(server.DEMO_MODE)
    assert source == "file"
    assert model == json.loads(out.read_text(encoding="utf-8"))


def test_upload_asks_first(pi, monkeypatch, tmp_path):
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    code, out = run(pi, monkeypatch, tmp_path, "--upload")
    assert code == 0 and out.exists()
    assert ("PUT", "/api/house") not in pi.calls


def test_a_wrong_password_writes_nothing(pi, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(make_house, "Pi", lambda *a, **k: pi)
    monkeypatch.setenv("NOBO_PASSWORD", "not-it")
    out = tmp_path / "house.json"
    assert make_house.main(["--template", str(EXAMPLE), "--user", "planner", "-o", str(out)]) == 1
    assert not out.exists()
    assert "could not sign in" in capsys.readouterr().err


ZONES = [{"zone_id": "1", "name": "Kitchen"}, {"zone_id": "2", "name": "Living\u00a0Room"}]
DEVICES = [{"serial": "160009990001", "name": "Kitchen", "zone_id": "1"},
           {"serial": "160009990002", "name": "Living Room", "zone_id": "2"}]
SENSORS = [
    {"sensor_id": "0xaaa", "name": "Patio Door", "counts": True},
    {"sensor_id": "verisure-1", "name": "Patio Door", "counts": False},
    {"sensor_id": "0xbbb", "name": "Kitchen Window", "counts": True},
]


def small(**placements):
    return {
        "template": 1,
        "levels": [{"id": "g", "elevation": 0, "height": 2.5}],
        "walls": [{"id": "n", "level": "g", "a": [0, 4], "b": [8, 4], "openings": [
            {"kind": "door", "code": "D1", "at": 1, "width": 0.9},
            {"kind": "window", "code": "W1", "at": 4, "width": 1.2}]}],
        "rooms": [{"id": "k", "level": "g", "zone": "kitchen", "polygon": [[0, 0], [4, 0], [4, 4], [0, 4]]},
                  {"id": "l", "level": "g", "zone": "Living Room", "polygon": [[4, 0], [8, 0], [8, 4], [4, 4]]},
                  {"id": "s", "level": "g", "name": "Sauna", "polygon": [[8, 0], [9, 0], [9, 1]]}],
        "placements": placements,
    }


def test_names_become_ids_however_they_are_spaced_or_cased():
    house, notes = make_house.resolve(
        small(heaters={"Kitchen": {"floor": "k"}, "160 009 990 002": {"wall": "n", "at": 4, "length": 1}},
              sensors={"Patio Door": {"opening": "D1"}, "0xbbb": {"opening": "W1", "sash": "left"}}),
        ZONES, DEVICES, SENSORS)
    assert [r.get("zone_id") for r in house["rooms"]] == ["1", "2", None]
    assert set(house["placements"]["heaters"]) == {"160009990001", "160009990002"}
    # The Verisure contact standing by behind the Zigbee one is not the one placed.
    assert set(house["placements"]["sensors"]) == {"0xaaa", "0xbbb"}
    assert "zone" not in house["rooms"][0]
    make_house.check(house)


def test_a_name_the_pi_does_not_have_stops_it_and_says_which():
    with pytest.raises(make_house.TemplateError) as err:
        make_house.resolve(small(heaters={"Sauna stove": {"floor": "k"}},
                                 sensors={"Back Door": {"opening": "D1"}}), ZONES, DEVICES, SENSORS)
    assert '"Sauna stove"' in str(err.value) and '"Back Door"' in str(err.value)


def test_allow_missing_leaves_devices_out_but_never_a_room():
    house, notes = make_house.resolve(small(sensors={"Back Door": {"opening": "D1"}}),
                                      ZONES, DEVICES, SENSORS, allow_missing=True)
    assert house["placements"]["sensors"] == {}
    assert any("Back Door" in note for note in notes)
    wrong_room = small()
    wrong_room["rooms"][0]["zone"] = "Pantry"
    with pytest.raises(make_house.TemplateError, match="Pantry"):
        make_house.resolve(wrong_room, ZONES, DEVICES, SENSORS, allow_missing=True)


def test_two_heaters_with_one_name_must_be_given_by_serial():
    devices = DEVICES + [{"serial": "160009990003", "name": "Kitchen", "zone_id": "1"}]
    with pytest.raises(make_house.TemplateError, match="use its serial"):
        make_house.resolve(small(heaters={"Kitchen": {"floor": "k"}}), ZONES, devices, SENSORS,
                           allow_missing=True)


def test_sensors_cannot_be_placed_on_a_pi_with_sensors_off():
    with pytest.raises(make_house.TemplateError, match="sensors are off"):
        make_house.resolve(small(sensors={"Patio Door": {"opening": "D1"}}), ZONES, DEVICES, None)


def test_the_pi_would_refuse_what_the_validator_refuses():
    house, _ = make_house.resolve(small(sensors={"Patio Door": {"opening": "D1", "sash": "left"}}),
                                  ZONES, DEVICES, SENSORS)
    with pytest.raises(make_house.TemplateError, match="a door has no sashes"):
        make_house.check(house)


def test_a_template_is_never_fetched_over_plain_http():
    with pytest.raises(make_house.TemplateError, match="https"):
        make_house.load_template("http://example.com/cabin.template.json")


def test_a_real_buildings_template_cannot_be_committed_by_accident():
    """A real building's plan stays out of this public repository."""
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "*.template.json" in ignore and "!docs/house.template.example.json" in ignore
    # Unanchored: the script writes house.json wherever it is run.
    assert "\nhouse.json" in ignore


@pytest.mark.parametrize("template", [
    {"template": 1, "rooms": ["kitchen"]},
    {"template": 1, "placements": [1]},
    {"template": 1, "placements": {"heaters": [["Kitchen", {}]]}},
])
def test_a_malformed_template_is_a_message_not_a_traceback(template):
    with pytest.raises(make_house.TemplateError):
        make_house.resolve(template, ZONES, DEVICES, SENSORS)


def test_a_user_who_cannot_upload_is_told_before_anything_is_written(pi, monkeypatch, tmp_path, capsys):
    users = auth.load_users()
    users["viewer"] = {"password_hash": auth.hash_password(PASSWORD), "role": "user"}
    auth.save_users(users)
    monkeypatch.setattr(make_house, "Pi", lambda *a, **k: pi)
    monkeypatch.setenv("NOBO_PASSWORD", PASSWORD)
    out = tmp_path / "house.json"
    code = make_house.main(["--template", str(EXAMPLE), "--user", "viewer", "-o", str(out), "--upload", "--yes"])
    assert code == 1 and not out.exists()
    assert "not an administrator" in capsys.readouterr().err
