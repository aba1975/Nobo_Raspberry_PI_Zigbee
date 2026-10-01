"""The weather station end to end: settings, Netatmo's setup, the front page,
room thermometers and the station's own alerts.

Everything runs against the demo hub and the demo station except the Netatmo
setup tests, which give the account a fake transport. No email is sent: the
notifier's ``notify`` is replaced wherever an alert could fire.
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

import auth
import sensor_verisure
import sensor_weather
import server
import weather_persistence
from pressure_outlook import PressureHistory
from weather_netatmo import NetatmoAccount
from tests.test_sensor_api import (  # noqa: F401 - fixtures are used by name
    add_sensor, enable, isolated_sensor_service, zone,
)
from tests.test_weather_netatmo import CLIENT_ID, CLIENT_SECRET, FakeNetatmo

HOUR = 3600
TECH = "03:00:00:00:00:02"
KITCHEN = "03:00:00:00:00:03"
BASE = "70:ee:50:00:00:01"
OUTDOOR = "02:00:00:00:00:04"


@pytest.fixture
def client():
    with TestClient(server.app, base_url="https://testserver") as value:
        value.cookies.set("session_id", "pytest-fixed-session-id")
        yield value


@pytest.fixture
def plain_http_client():
    with TestClient(server.app) as value:
        value.cookies.set("session_id", "pytest-fixed-session-id")
        yield value


@pytest.fixture
def sent(monkeypatch):
    posted = []

    def fake_notify(event_type, subject, body, severity="warning", key=None,
                    highlight=(), facts=()):
        posted.append({"type": event_type, "subject": subject, "body": body,
                       "severity": severity})
        return True

    monkeypatch.setattr(server.notifier, "notify", fake_notify)
    monkeypatch.setattr(server.notifier, "_wants", lambda event_type: True)
    server.notifier._conditions.clear()
    return posted


@pytest.fixture
def fake_netatmo(monkeypatch):
    fake = FakeNetatmo()
    monkeypatch.setattr(server, "netatmo_account", NetatmoAccount(transport=fake))
    return fake


def turn_on(client, **extra):
    response = client.put("/api/weather/settings",
                          json={"enabled": True, "provider": "simulated", **extra})
    assert response.status_code == 200, response.text
    return response.json()


def simulate(client, **body):
    response = client.post("/api/weather/simulate", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def weather(client):
    return client.get("/api/status").json()["weather"]


def as_user(monkeypatch):
    original = auth.load_users

    def users():
        data = dict(original())
        data["admin"] = {**data["admin"], "role": "user"}
        return data

    monkeypatch.setattr(auth, "load_users", users)


# -- off means absent ------------------------------------------------------


def test_off_by_default_and_absent_everywhere(client):
    assert weather(client) is None
    capability = client.get("/api/capabilities").json()["weather"]
    assert capability["enabled"] is False
    assert client.get("/api/weather/settings").json()["enabled"] is False
    display = client.get("/api/display").json()
    assert display["outdoor"] is None


def test_off_marks_its_alerts_unavailable(client):
    types = client.get("/api/notifications").json()["event_types"]
    for key in ("outdoor_cold", "weather_battery_low", "weather_connection_lost"):
        assert "weather station" in types[key]["unavailable"]
    turn_on(client)
    types = client.get("/api/notifications").json()["event_types"]
    assert "unavailable" not in types["outdoor_cold"]


def test_only_admins_change_it(client, monkeypatch):
    as_user(monkeypatch)
    assert client.get("/api/weather/settings").status_code == 403
    assert client.put("/api/weather/settings", json={"enabled": True}).status_code == 403
    assert client.post("/api/weather/simulate", json={"module_id": OUTDOOR}).status_code == 403
    assert client.post("/api/weather/netatmo/app",
                       json={"client_id": "a", "client_secret": "b"}).status_code == 403
    assert client.post("/api/weather/netatmo/connect").status_code == 403
    assert client.post("/api/weather/netatmo/token", json={"refresh_token": "x"}).status_code == 403
    assert client.post("/api/weather/netatmo/disconnect").status_code == 403


def test_a_user_sees_the_readings_but_not_the_account(client, monkeypatch):
    turn_on(client)
    as_user(monkeypatch)
    body = weather(client)
    assert body["outdoor"]["temperature"] is not None
    assert "client_id" not in json.dumps(body) and "netatmo" not in body


def test_signed_out_requests_are_refused(client):
    client.cookies.clear()
    assert client.get("/api/weather/settings", follow_redirects=False).status_code in (401, 302, 303)
    assert client.get("/api/weather/netatmo/callback?state=x&code=y",
                      follow_redirects=False).status_code in (401, 302, 303)


def test_the_demo_station_is_demo_only(client, monkeypatch):
    monkeypatch.setattr(server, "DEMO_MODE", False)
    response = client.put("/api/weather/settings", json={"enabled": True, "provider": "simulated"})
    assert response.status_code == 400
    assert client.get("/api/weather/settings").json()["providers"] == ["netatmo"]


def test_unknown_provider_and_bad_limit_are_refused(client):
    assert client.put("/api/weather/settings", json={"provider": "met.no"}).status_code == 400
    assert client.put("/api/weather/settings",
                      json={"outdoor_cold_below": -80}).status_code == 400


# -- the demo station ------------------------------------------------------


def test_on_it_reads_the_station_at_once(client):
    body = turn_on(client)
    status = body["status"]
    assert status["connection"] == "ok"
    assert status["provider"] == "simulated"
    assert status["outdoor"]["temperature"] == 3.2
    assert status["outdoor"]["fresh"] is True
    kinds = sorted(item["kind"] for item in status["modules"])
    assert kinds == ["base", "indoor", "indoor", "outdoor"]
    assert body["simulated"]
    display = client.get("/api/display").json()
    assert display["outdoor"]["temperature"] == 3.2


def test_the_settings_are_kept(client):
    turn_on(client, outdoor_cold_below=-20)
    saved = weather_persistence.load_settings()
    assert saved.enabled is True and saved.outdoor_cold_below == -20.0


def test_simulate_changes_a_module(client):
    turn_on(client)
    status = simulate(client, module_id=OUTDOOR, temperature=-11, humidity=70)["status"]
    assert status["outdoor"]["temperature"] == -11.0
    assert status["outdoor"]["min_temperature"] <= -11.0
    assert status["outdoor"]["frost"] is True


def test_simulate_refuses_nonsense(client):
    turn_on(client)
    assert client.post("/api/weather/simulate",
                       json={"module_id": OUTDOOR, "pressure": 1000}).status_code == 400
    assert client.post("/api/weather/simulate",
                       json={"module_id": BASE, "battery": 50}).status_code == 400
    assert client.post("/api/weather/simulate",
                       json={"module_id": "nope", "temperature": 1}).status_code == 404


def test_simulate_needs_the_demo_station(client):
    assert client.post("/api/weather/simulate",
                       json={"module_id": OUTDOOR, "temperature": 1}).status_code == 409


def test_an_unreachable_outdoor_module_is_not_shown_as_fresh(client):
    turn_on(client)
    status = simulate(client, module_id=OUTDOOR, reachable=False)["status"]
    assert status["outdoor"]["fresh"] is False
    assert status["outdoor"]["temperature"] is None
    assert client.get("/api/display").json()["outdoor"] is None


def test_turning_off_forgets_the_history(client):
    turn_on(client)
    assert weather_persistence.OUTDOOR_HISTORY_FILE.exists()
    client.put("/api/weather/settings", json={"enabled": False})
    assert not weather_persistence.OUTDOOR_HISTORY_FILE.exists()
    assert weather(client) is None


def test_it_survives_a_restart(client):
    turn_on(client)
    simulate(client, module_id=OUTDOOR, temperature=4.5)
    # A restart: the service stops and starts again from what is on disk.
    asyncio.run(server.stop_weather_service())
    server.weather_settings = weather_persistence.load_settings()
    asyncio.run(server.start_weather_service())
    asyncio.run(server.weather_poll_once())
    assert weather(client)["outdoor"]["temperature"] == 4.5


# -- the outlook -----------------------------------------------------------


def test_the_station_outlook_is_preferred_once_it_has_three_hours(client, monkeypatch):
    turn_on(client)
    now = time.time()
    monkeypatch.setattr(server, "station_pressure_history", PressureHistory(
        zones={"station": [[int(now - 3 * HOUR), 1013.2 + 5.0]]},
    ))
    simulate(client, module_id=BASE, pressure=1013.2)
    outlook = weather(client)["outlook"]
    assert outlook["source"] == "station"
    assert outlook["tendency"] in ("falling_fast", "storm")
    assert client.get("/api/display").json()["weather_outlook"]["tendency"] == outlook["tendency"]


def test_a_new_station_says_it_is_learning(client):
    turn_on(client)
    outlook = weather(client)["outlook"]
    assert outlook["source"] == "station"
    assert outlook["tendency"] is None and outlook["ready_at"] is not None


def test_the_rooms_outlook_stands_until_the_station_has_one(client, monkeypatch):
    enable(client, zone_id="1")
    room = add_sensor(client, name="Bath", kind="climate", zone_id="1")
    now = time.time()
    monkeypatch.setattr(server, "pressure_history", PressureHistory(
        zones={"1": [[int(now - 3 * HOUR), 1020.0]]},
    ))
    client.post(f"/api/sensors/{room['sensor_id']}/simulate", json={"pressure": 1010})
    turn_on(client)
    outlook = weather(client)["outlook"]
    assert outlook["source"] == "rooms" and outlook["tendency"] is not None


# -- room thermometers -----------------------------------------------------


def add_module(client, module_id=TECH, name="Tech Room module", zone_id="6"):
    response = client.post("/api/sensors/weather",
                           json={"module_id": module_id, "name": name, "zone_id": zone_id})
    assert response.status_code == 200, response.text
    return response.json()


def listed(client, sensor_id):
    return next(item for item in client.get("/api/sensors").json()["sensors"]
                if item["sensor_id"] == sensor_id)


def test_modules_are_offered_only_when_both_features_are_on(client):
    assert client.get("/api/sensors/weather").status_code == 409  # sensors off
    enable(client, zone_id="6")
    body = client.get("/api/sensors/weather").json()
    assert body["available"] is False and "weather station" in body["reason"]
    assert client.post("/api/sensors/weather",
                       json={"module_id": TECH, "name": "x"}).status_code == 409
    turn_on(client)
    body = client.get("/api/sensors/weather").json()
    assert body["available"] is True
    # The outdoor module is not a room.
    assert sorted(item["kind"] for item in body["modules"]) == ["base", "indoor", "indoor"]


def test_the_tech_room_module_gives_the_room_a_temperature(client):
    enable(client, zone_id="6")
    turn_on(client)
    sensor = add_module(client)
    assert sensor["source"] == "netatmo" and sensor["kind"] == "climate"
    room = zone(client, "6")
    assert room["current_temperature"] == 14.8
    assert room["temperature_source"] == "sensor"
    saved = json.loads(sensor_weather.WEATHER_SENSORS_FILE.read_text())
    assert saved["sensors"][0]["module_id"] == TECH
    # And the module list says it is taken.
    module = next(item for item in weather(client)["modules"] if item["module_id"] == TECH)
    assert module["sensor_id"] == sensor["sensor_id"] and module["zone_id"] == "6"


def test_a_module_can_be_chosen_once(client):
    enable(client, zone_id="6")
    turn_on(client)
    add_module(client)
    assert client.post("/api/sensors/weather",
                       json={"module_id": TECH, "name": "again"}).status_code == 409
    assert client.post("/api/sensors/weather",
                       json={"module_id": OUTDOOR, "name": "outside"}).status_code == 404


def test_a_module_is_renamed_moved_and_removed(client):
    enable(client, zone_id="6")
    turn_on(client)
    sensor = add_module(client)
    sid = sensor["sensor_id"]
    moved = client.put(f"/api/sensors/{sid}", json={"name": "Teknisk", "zone_id": "1"})
    assert moved.status_code == 200 and moved.json()["zone_id"] == "1"
    assert client.put(f"/api/sensors/{sid}", json={"kind": "door"}).status_code == 400
    assert client.delete(f"/api/sensors/{sid}").status_code == 200
    assert sid not in {item["sensor_id"] for item in client.get("/api/sensors").json()["sensors"]}


def test_a_demo_module_can_be_simulated_from_its_sensor(client):
    enable(client, zone_id="6")
    turn_on(client)
    sid = add_module(client)["sensor_id"]
    response = client.post(f"/api/sensors/{sid}/simulate", json={"temperature": 3.0})
    assert response.status_code == 200, response.text
    assert zone(client, "6")["current_temperature"] == 3.0
    assert client.post(f"/api/sensors/{sid}/simulate", json={"state": "open"}).status_code == 400
    client.post(f"/api/sensors/{sid}/simulate", json={"available": False})
    assert listed(client, sid)["available"] is False
    assert zone(client, "6")["current_temperature"] is None


def test_the_modules_go_away_with_the_station_and_come_back(client):
    enable(client, zone_id="6")
    turn_on(client)
    sid = add_module(client)["sensor_id"]
    client.put("/api/weather/settings", json={"enabled": False})
    assert sid not in {item["sensor_id"] for item in client.get("/api/sensors").json()["sensors"]}
    assert zone(client, "6")["current_temperature"] is None
    turn_on(client)
    assert listed(client, sid)["available"] is True


def test_zigbee_outranks_the_module_in_the_kitchen(client):
    enable(client, zone_id="5")
    turn_on(client)
    module = add_module(client, KITCHEN, "Kitchen module", zone_id="5")
    zigbee = add_sensor(client, name="Kitchen thermometer", kind="climate", zone_id="5")
    client.post(f"/api/sensors/{zigbee['sensor_id']}/simulate", json={"temperature": 22.0})
    row = listed(client, module["sensor_id"])
    assert row["counts"] is False and row["standing_by"] == sensor_weather.STANDING_BY_THERMOMETER
    assert zone(client, "5")["current_temperature"] == 22.0
    client.post(f"/api/sensors/{zigbee['sensor_id']}/simulate", json={"available": False})
    assert listed(client, module["sensor_id"])["counts"] is True
    assert zone(client, "5")["current_temperature"] == 21.9


def test_the_module_outranks_a_verisure_smoke_detector(client):
    enable(client, zone_id="6")
    turn_on(client)
    assert client.put("/api/alarm/settings",
                      json={"enabled": True, "provider": "simulated"}).status_code == 200
    add_module(client)
    smoke = client.post("/api/sensors/verisure", json={
        "device_id": "climate:DEMO 0101", "name": "Tech smoke", "zone_id": "6",
    })
    assert smoke.status_code == 200, smoke.text
    row = listed(client, smoke.json()["sensor_id"])
    assert row["counts"] is False
    assert row["standing_by"] == sensor_verisure.STANDING_BY_WEATHER
    assert zone(client, "6")["current_temperature"] == 14.8


def test_deleting_a_room_unassigns_its_module(client):
    enable(client, zone_id="6")
    turn_on(client)
    sid = add_module(client)["sensor_id"]
    server._forget_zone_in_heating_links("6")
    assert server.weather_sensors[sid].zone_id is None


def test_module_battery_and_silence_are_not_sensor_alerts(client, sent):
    enable(client, zone_id="6")
    turn_on(client)
    sid = add_module(client)["sensor_id"]
    client.post(f"/api/sensors/{sid}/simulate", json={"battery": 5})
    assert not [item for item in sent if item["type"] == "sensor_battery_low"]
    assert [item for item in sent if item["type"] == "weather_battery_low"]


# -- the station's alerts --------------------------------------------------


def test_the_outdoor_cold_warns_once_and_clears_a_degree_above(client, sent):
    turn_on(client, outdoor_cold_below=-15)
    simulate(client, module_id=OUTDOOR, temperature=-16)
    simulate(client, module_id=OUTDOOR, temperature=-17)
    cold = [item for item in sent if item["type"] == "outdoor_cold"]
    assert len(cold) == 1 and "-16.0°C" in cold[0]["subject"]
    assert server.weather_ledger.cold_raised is True
    assert weather(client)["outdoor"]["cold"] is True
    simulate(client, module_id=OUTDOOR, temperature=-14.5)
    assert len([item for item in sent if item["type"] == "outdoor_cold"]) == 1
    simulate(client, module_id=OUTDOOR, temperature=-13.5)
    assert len([item for item in sent if item["type"] == "outdoor_cold"]) == 2
    assert server.weather_ledger.cold_raised is False


def test_a_new_limit_is_applied_at_once(client, sent):
    turn_on(client)
    simulate(client, module_id=OUTDOOR, temperature=-5)
    assert not [item for item in sent if item["type"] == "outdoor_cold"]
    client.put("/api/weather/settings", json={"outdoor_cold_below": -2})
    assert [item for item in sent if item["type"] == "outdoor_cold"]


def test_a_low_battery_warns_once_and_clears_on_a_new_one(client, sent):
    turn_on(client)
    simulate(client, module_id=OUTDOOR, battery=18)
    simulate(client, module_id=OUTDOOR, battery=17)
    low = [item for item in sent if item["type"] == "weather_battery_low"]
    assert len(low) == 1
    assert server.weather_ledger.battery_low == (OUTDOOR,)
    assert weather(client)["outdoor"]["battery_low"] is True
    simulate(client, module_id=OUTDOOR, battery=25)
    assert len([item for item in sent if item["type"] == "weather_battery_low"]) == 1
    simulate(client, module_id=OUTDOOR, battery=100)
    assert len([item for item in sent if item["type"] == "weather_battery_low"]) == 2
    assert server.weather_ledger.battery_low == ()


def test_a_raised_alert_is_not_sent_again_after_a_restart(client, sent):
    turn_on(client)
    simulate(client, module_id=OUTDOOR, temperature=-20)
    assert len([item for item in sent if item["type"] == "outdoor_cold"]) == 1
    server.notifier._conditions.clear()
    server.weather_provider = None
    server.weather_ledger = weather_persistence.load_ledger()
    asyncio.run(server.start_weather_service())
    asyncio.run(server.weather_poll_once())
    assert len([item for item in sent if item["type"] == "outdoor_cold"]) == 1


def test_turning_off_clears_raised_alerts_silently(client, sent):
    turn_on(client)
    simulate(client, module_id=OUTDOOR, temperature=-20)
    client.put("/api/weather/settings", json={"enabled": False})
    assert server.weather_ledger.cold_raised is False
    assert weather_persistence.load_ledger().cold_raised is False


# -- Netatmo setup ---------------------------------------------------------


def netatmo_on(client):
    response = client.put("/api/weather/settings", json={"enabled": True, "provider": "netatmo"})
    assert response.status_code == 200, response.text
    return response.json()


def test_netatmo_before_setup_says_what_to_do(client, fake_netatmo):
    body = netatmo_on(client)
    assert body["netatmo"]["app_configured"] is False
    assert body["netatmo"]["redirect_uri"] == "https://testserver/api/weather/netatmo/callback"
    assert body["status"]["connection"] == "not_configured"
    assert fake_netatmo.calls == []


def test_the_secret_is_only_taken_over_https(plain_http_client, fake_netatmo):
    netatmo_on(plain_http_client)
    response = plain_http_client.post("/api/weather/netatmo/app",
                                      json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    assert response.status_code == 403
    assert "HTTPS" in response.json()["detail"]
    assert not weather_persistence.NETATMO_ACCOUNT_FILE.exists()
    assert plain_http_client.post("/api/weather/netatmo/token",
                                  json={"refresh_token": "x"}).status_code == 403


def test_the_app_is_saved_masked(client, fake_netatmo):
    netatmo_on(client)
    body = client.post("/api/weather/netatmo/app",
                       json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}).json()
    assert body["netatmo"]["app_configured"] is True
    text = json.dumps(body)
    assert CLIENT_SECRET not in text and CLIENT_ID not in text


def test_the_netatmo_endpoints_need_netatmo_chosen(client, fake_netatmo):
    turn_on(client)
    assert client.post("/api/weather/netatmo/app",
                       json={"client_id": "a", "client_secret": "b"}).status_code == 409
    assert client.post("/api/weather/netatmo/connect").status_code == 409


def connect_through_netatmo(client):
    netatmo_on(client)
    client.post("/api/weather/netatmo/app",
                json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    started = client.post("/api/weather/netatmo/connect")
    assert started.status_code == 200, started.text
    url = started.json()["authorize_url"]
    state = url.split("state=")[1].split("&")[0]
    return state


def test_connect_and_callback(client, fake_netatmo):
    state = connect_through_netatmo(client)
    response = client.get(f"/api/weather/netatmo/callback?state={state}&code=abc",
                          follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/?weather=connected"
    exchange = fake_netatmo.calls[0][1]
    assert exchange["code"] == "abc"
    assert exchange["redirect_uri"] == "https://testserver/api/weather/netatmo/callback"
    status = weather(client)
    assert status["connection"] == "ok" and status["station_name"] == "Mostugu"
    assert status["outdoor"]["temperature"] == -3.4


def test_a_state_is_used_once(client, fake_netatmo):
    state = connect_through_netatmo(client)
    client.get(f"/api/weather/netatmo/callback?state={state}&code=abc", follow_redirects=False)
    again = client.get(f"/api/weather/netatmo/callback?state={state}&code=abc",
                       follow_redirects=False)
    assert again.headers["location"] == "/?weather=failed"


def test_a_forged_or_expired_state_is_refused(client, fake_netatmo):
    state = connect_through_netatmo(client)
    forged = client.get("/api/weather/netatmo/callback?state=forged&code=abc",
                        follow_redirects=False)
    assert forged.headers["location"] == "/?weather=failed"
    server.netatmo_pending[state]["expires_at"] = time.time() - 1
    expired = client.get(f"/api/weather/netatmo/callback?state={state}&code=abc",
                         follow_redirects=False)
    assert expired.headers["location"] == "/?weather=failed"
    assert not [call for call in fake_netatmo.calls if call[0].endswith("token")]


def test_a_state_belongs_to_the_user_who_asked(client, fake_netatmo):
    state = connect_through_netatmo(client)
    server.netatmo_pending[state]["username"] = "someone-else"
    response = client.get(f"/api/weather/netatmo/callback?state={state}&code=abc",
                          follow_redirects=False)
    assert response.headers["location"] == "/?weather=failed"


def test_declining_at_netatmo_says_so(client, fake_netatmo):
    state = connect_through_netatmo(client)
    response = client.get(f"/api/weather/netatmo/callback?state={state}&error=access_denied",
                          follow_redirects=False)
    assert response.headers["location"] == "/?weather=denied"


def test_a_pasted_token_connects(client, fake_netatmo):
    netatmo_on(client)
    client.post("/api/weather/netatmo/app",
                json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    body = client.post("/api/weather/netatmo/token", json={"refresh_token": "pasted"}).json()
    assert body["netatmo"]["connected"] is True
    assert body["status"]["connection"] == "ok"
    assert "pasted" not in json.dumps(body)


def test_disconnect_keeps_the_app(client, fake_netatmo):
    netatmo_on(client)
    client.post("/api/weather/netatmo/app",
                json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    client.post("/api/weather/netatmo/token", json={"refresh_token": "pasted"})
    body = client.post("/api/weather/netatmo/disconnect").json()
    assert body["netatmo"]["connected"] is False and body["netatmo"]["app_configured"] is True
    assert body["status"]["connection"] == "signed_out"


def test_turning_netatmo_off_deletes_the_secret(client, fake_netatmo):
    netatmo_on(client)
    client.post("/api/weather/netatmo/app",
                json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    assert weather_persistence.NETATMO_ACCOUNT_FILE.exists()
    client.put("/api/weather/settings", json={"enabled": False})
    assert not weather_persistence.NETATMO_ACCOUNT_FILE.exists()


def test_signed_out_raises_the_connection_alert_at_once(client, fake_netatmo, sent):
    netatmo_on(client)
    client.post("/api/weather/netatmo/app",
                json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    client.post("/api/weather/netatmo/token", json={"refresh_token": "pasted"})
    client.post("/api/weather/netatmo/disconnect")
    lost = [item for item in sent if item["type"] == "weather_connection_lost"]
    assert len(lost) == 1
    assert "pasted" not in lost[0]["body"] and CLIENT_SECRET not in lost[0]["body"]


def test_a_short_outage_is_not_an_alert(client, fake_netatmo, sent):
    netatmo_on(client)
    client.post("/api/weather/netatmo/app",
                json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    client.post("/api/weather/netatmo/token", json={"refresh_token": "pasted"})
    fake_netatmo.station_answers.append((503, None))
    asyncio.run(server.weather_poll_once())
    assert weather(client)["connection"] == "unreachable"
    assert not [item for item in sent if item["type"] == "weather_connection_lost"]
    server.weather_last_good = time.time() - 2 * HOUR
    fake_netatmo.station_answers.append((503, None))
    asyncio.run(server.weather_poll_once())
    assert [item for item in sent if item["type"] == "weather_connection_lost"]


def test_being_throttled_backs_off(client, fake_netatmo):
    netatmo_on(client)
    client.post("/api/weather/netatmo/app",
                json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    client.post("/api/weather/netatmo/token", json={"refresh_token": "pasted"})
    fake_netatmo.station_answers.append((429, None))
    delay = asyncio.run(server.weather_poll_once())
    assert delay == server.WEATHER_RATE_LIMIT_BACKOFF[0]
    fake_netatmo.station_answers.append((429, None))
    assert asyncio.run(server.weather_poll_once()) == server.WEATHER_RATE_LIMIT_BACKOFF[1]
