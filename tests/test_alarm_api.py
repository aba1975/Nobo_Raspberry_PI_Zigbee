"""The alarm integration end to end: HTTP, the heating, and the warning.

Everything here runs against the demo hub and the demo alarm, except the
Verisure sign-in tests, which use a stand-in for vsure's session so nothing
reaches Verisure. No email is sent: the notifier's ``notify`` is replaced.
"""

import asyncio
import copy
import json
import time

import pytest
from fastapi.testclient import TestClient

import alarm_persistence
import auth
import server
from alarm_persistence import AlarmSettings
from alarm_provider import AlarmReading, LockReading
from alarm_verisure import VerisureAlarm
from sensor_persistence import SensorSettings

SECRET_PASSWORD = "hunter2-but-longer"


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


@pytest.fixture(autouse=True)
def demo_house_at_home(monkeypatch):
    # Through monkeypatch so the mode is put back afterwards: a test that
    # leaves the demo house on Away breaks whichever file runs next.
    monkeypatch.setattr(server, "demo_global_mode", "normal")
    monkeypatch.setattr(server, "global_mode_source", "manual")
    zones = copy.deepcopy(server.DEMO_ZONES)
    yield
    server.DEMO_ZONES[:] = zones


def turn_on(client, **extra):
    response = client.put("/api/alarm/settings",
                          json={"enabled": True, "provider": "simulated", **extra})
    assert response.status_code == 200, response.text
    return response.json()


def simulate(client, **body):
    response = client.post("/api/alarm/simulate", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def global_mode(client):
    return client.get("/api/status").json()["global_override_mode"]


# -- off means absent ------------------------------------------------------


def test_off_by_default_and_absent_everywhere(client):
    status = client.get("/api/status").json()
    assert status["alarm"] is None
    assert client.get("/api/capabilities").json()["alarm"]["enabled"] is False
    settings = client.get("/api/alarm/settings").json()
    assert settings["enabled"] is False


def test_only_admins(client, monkeypatch):
    original = auth.load_users

    def users():
        data = dict(original())
        data["admin"] = {**data["admin"], "role": "user"}
        return data

    monkeypatch.setattr(auth, "load_users", users)
    assert client.get("/api/alarm/settings").status_code == 403
    assert client.put("/api/alarm/settings", json={"enabled": True}).status_code == 403
    assert client.post("/api/alarm/simulate", json={"arm_state": "armed_away"}).status_code == 403
    assert client.post("/api/alarm/verisure/login",
                       json={"email": "a@b.no", "password": "x"}).status_code == 403
    assert client.post("/api/alarm/verisure/logout").status_code == 403


def test_signed_out_requests_are_refused(client):
    client.cookies.clear()
    assert client.get("/api/alarm/settings", follow_redirects=False).status_code in (401, 302, 303)


def test_the_demo_alarm_is_refused_on_a_real_hub(client, monkeypatch):
    monkeypatch.setattr(server, "DEMO_MODE", False)
    response = client.put("/api/alarm/settings", json={"enabled": True, "provider": "simulated"})
    assert response.status_code == 400
    assert server.alarm_settings.enabled is False


def test_on_a_real_hub_turning_it_on_is_one_press(client, monkeypatch):
    """The stored default source is the demo alarm, which a real hub refuses.
    Found on the production Pi: the On button sent only ``enabled`` and got
    a 400, because nothing on that page can choose the only other source."""
    monkeypatch.setattr(server, "DEMO_MODE", False)
    settings = client.get("/api/alarm/settings").json()
    assert settings["provider"] == "verisure"
    assert settings["providers"] == ["verisure"]

    response = client.put("/api/alarm/settings", json={"enabled": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is True and body["provider"] == "verisure"
    assert body["status"]["connection"] == "not_configured"
    assert server.alarm_settings.provider == "verisure"
    assert client.get("/api/capabilities").json()["alarm"]["provider"] == "verisure"


def test_an_unknown_provider_is_refused(client):
    assert client.put("/api/alarm/settings", json={"provider": "ajax"}).status_code == 400


def test_simulating_needs_the_demo_alarm(client):
    assert client.post("/api/alarm/simulate", json={"arm_state": "armed_away"}).status_code == 409


# -- the heating -----------------------------------------------------------


def test_armed_away_is_away_and_disarmed_is_home(client):
    turn_on(client)
    status = simulate(client, arm_state="armed_away")
    assert status["owns_away"] is True
    assert global_mode(client) == "away"
    assert server.global_mode_source == "alarm"
    log = client.get("/api/log").json()["entries"]
    assert any(e["source"] == "alarm" and "Away" in e["description"] for e in log)

    status = simulate(client, arm_state="disarmed")
    assert status["owns_away"] is False
    assert global_mode(client) is None
    assert server.global_mode_source == "manual"


def test_the_source_survives_a_restart(client):
    """The demo path of the global mode does not save the server state, so
    the alarm saves its own claim to the Away."""
    turn_on(client)
    simulate(client, arm_state="armed_away")
    saved = server.config_persistence.load_server_state()
    assert saved.get("global_mode_source") == "alarm"


def test_a_mode_chosen_by_hand_is_not_undone_by_disarming(client):
    turn_on(client)
    simulate(client, arm_state="armed_away")
    assert client.post("/api/global/override/eco").status_code == 200
    simulate(client, arm_state="disarmed")
    assert global_mode(client) == "eco"


def test_an_away_that_was_already_there_is_left_on_disarming(client):
    """A holiday Away set before leaving is not the alarm's to lift."""
    turn_on(client)
    assert client.post("/api/global/override/away").status_code == 200
    simulate(client, arm_state="armed_away")
    assert client.get("/api/status").json()["alarm"]["owns_away"] is False
    simulate(client, arm_state="disarmed")
    assert global_mode(client) == "away"


def test_armed_home_changes_nothing_by_default(client):
    turn_on(client)
    simulate(client, arm_state="armed_home")
    assert global_mode(client) is None


def test_locked_from_outside_warns_but_never_changes_the_heating(client):
    turn_on(client)
    status = simulate(client, lock="outside")
    assert status["leaving"]["reason"] == "locked_outside"
    assert status["leaving"]["lock_name"] == "Front door"
    assert global_mode(client) is None


def test_locked_from_inside_is_not_leaving(client):
    turn_on(client)
    assert simulate(client, lock="inside")["leaving"] is None


def test_a_restart_does_not_redo_what_somebody_undid(client):
    """Armed away, Away lifted by hand, then the Pi restarts while the alarm
    is still armed. Putting Away back would override the person."""
    turn_on(client)
    simulate(client, arm_state="armed_away")
    assert client.post("/api/global/override/normal").status_code == 200

    # What a restart leaves: the files, and nothing in memory.
    server.alarm_ledger = alarm_persistence.load_ledger()
    server.alarm_provider = None
    server.alarm_reading = None
    asyncio.run(server.start_alarm_service())
    asyncio.run(server.alarm_poll_once())

    assert global_mode(client) is None


def test_the_alarm_does_nothing_without_the_hub(client):
    turn_on(client)
    server.hub_connected = False
    try:
        simulate(client, arm_state="armed_away")
    finally:
        server.hub_connected = True
    assert not (server.alarm_ledger.handled_event or "").startswith("armed_away"), (
        "not handled, so it is acted on later"
    )
    assert global_mode(client) is None
    asyncio.run(server.alarm_poll_once())
    assert global_mode(client) == "away"


def test_turning_it_off_leaves_the_away_and_hands_it_over(client):
    """Switching a setting must not warm an empty house."""
    turn_on(client)
    simulate(client, arm_state="armed_away")
    response = client.put("/api/alarm/settings", json={"enabled": False})
    assert response.status_code == 200
    assert global_mode(client) == "away"
    assert server.global_mode_source == "manual"
    assert server.alarm_ledger.owns_away is False
    assert client.get("/api/status").json()["alarm"] is None


def test_unticking_away_hands_over_an_away_it_owns(client):
    turn_on(client)
    simulate(client, arm_state="armed_away")
    client.put("/api/alarm/settings", json={"away_when_armed_away": False})
    simulate(client, arm_state="disarmed")
    assert global_mode(client) == "away"


def test_settings_persist(client):
    turn_on(client, warn_when_armed_home=False)
    saved = alarm_persistence.load_settings()
    assert saved.enabled is True and saved.warn_when_armed_home is False


# -- the lock and the heating ------------------------------------------------

def test_locked_from_outside_can_turn_the_house_down_and_back(client):
    turn_on(client, heating_when_locked_outside="eco")
    status = simulate(client, lock="outside")
    assert global_mode(client) == "eco"
    assert status["owned_mode"] == "eco"
    assert server.global_mode_source == "alarm"
    simulate(client, lock="unlocked")
    assert global_mode(client) is None
    assert server.alarm_ledger.owned_mode is None


def test_locked_from_inside_can_be_eco_too(client):
    turn_on(client, heating_when_locked_inside="eco")
    simulate(client, lock="inside")
    assert global_mode(client) == "eco"
    simulate(client, lock="unlocked")
    assert global_mode(client) is None


def test_away_is_refused_for_locked_from_inside(client):
    response = client.put("/api/alarm/settings", json={
        "enabled": True, "provider": "simulated", "heating_when_locked_inside": "away"})
    assert response.status_code == 400


def test_which_side_a_method_counts_as_is_the_users_to_say(client):
    turn_on(client, heating_when_locked_outside="away",
            lock_sides={"star": "outside", "code": "inside"})
    simulate(client, lock_method="code")
    assert global_mode(client) is None, "code now means inside"
    simulate(client, lock="unlocked")
    status = simulate(client, lock_method="star")
    assert status["leaving"]["reason"] == "locked_outside"
    assert global_mode(client) == "away"


def test_the_alarm_outranks_the_lock_and_away_is_never_made_eco(client):
    turn_on(client, heating_when_locked_outside="eco")
    simulate(client, arm_state="armed_away")
    simulate(client, lock="outside")
    assert global_mode(client) == "away"
    simulate(client, lock="unlocked")
    assert global_mode(client) == "away", "still armed"
    simulate(client, arm_state="disarmed")
    assert global_mode(client) is None


def test_a_lock_eco_does_not_touch_an_away_set_by_hand(client):
    turn_on(client, heating_when_locked_outside="eco")
    assert client.post("/api/global/override/away").status_code == 200
    simulate(client, lock="outside")
    simulate(client, lock="unlocked")
    assert global_mode(client) == "away"


def test_arming_over_a_manual_away_adds_no_return_time_and_lifts_nothing(client):
    """The alarm's own Away holds until cancelled; one chosen beforehand is
    the person's, including its end time, and disarming leaves it."""
    turn_on(client)
    simulate(client, arm_state="armed_away")
    assert server.away_schedule.load_schedule().get("enabled") is not True
    simulate(client, arm_state="disarmed")
    assert client.post("/api/global/override/away").status_code == 200
    simulate(client, arm_state="armed_away")
    assert server.alarm_ledger.owned_mode is None
    simulate(client, arm_state="disarmed")
    assert global_mode(client) == "away"


def test_the_demo_lock_refuses_an_unknown_method(client):
    turn_on(client)
    assert client.post("/api/alarm/simulate", json={"lock_method": "magic"}).status_code == 400


# -- Verisure over HTTP ----------------------------------------------------


class _FakeVerisureSession:
    def __init__(self, username, password, cookie_file_name=None):
        self._username = username
        self._password = password
        self._cookies = None
        self._trust_token = None
        self._mfa_login_pending = False
        self.calls = []

    def login(self):
        import verisure

        self._mfa_login_pending = True
        raise verisure.LoginError("mfa")

    def request_mfa(self):
        pass

    def validate_mfa(self, code):
        self._cookies = {"vid": "cookie-value-secret", "vs-refresh": "refresh-secret"}
        self._trust_token = {"trustTokenValue": "trust-secret"}
        return {"data": {"account": {"installations": [{"giid": "999", "alias": "Hytta"}]}}}

    def set_giid(self, giid):
        pass

    def update_cookie(self):
        pass

    def arm_state(self):
        return {"operationName": "ArmState", "query": "query ArmState {}"}

    def smart_lock(self):
        return {"operationName": "SmartLock", "query": "query SmartLock {}"}

    def door_window(self):
        return {"operationName": "DoorWindow", "query": "query DoorWindow {}"}

    def climate(self):
        return {"operationName": "Climate", "query": "query Climate {}"}

    def request(self, *operations):
        return [
            {"data": {"installation": {"armState": {
                "statusType": "DISARMED", "date": "2026-09-30T08:00:00Z"}}}},
            {"data": {"installation": {"smartLocks": []}}},
        ]

    def logout(self):
        pass


@pytest.fixture
def fake_verisure(monkeypatch):
    account = VerisureAlarm(session_factory=_FakeVerisureSession)
    monkeypatch.setattr(server, "verisure_account", account)
    return account


def test_the_password_is_only_taken_over_https(plain_http_client, fake_verisure):
    plain_http_client.put("/api/alarm/settings", json={"enabled": True, "provider": "verisure"})
    response = plain_http_client.post(
        "/api/alarm/verisure/login", json={"email": "a@b.no", "password": SECRET_PASSWORD})
    assert response.status_code == 403
    assert "HTTPS" in response.json()["detail"]


def test_signing_in_needs_verisure_chosen(client, fake_verisure):
    response = client.post(
        "/api/alarm/verisure/login", json={"email": "a@b.no", "password": SECRET_PASSWORD})
    assert response.status_code == 409


def test_sign_in_end_to_end_keeps_every_secret_to_itself(client, fake_verisure):
    turn_on(client, provider="verisure")
    first = client.post(
        "/api/alarm/verisure/login",
        json={"email": "someone@example.no", "password": SECRET_PASSWORD},
    )
    assert first.status_code == 200 and first.json()["status"] == "code_sent"
    second = client.post("/api/alarm/verisure/code", json={"code": "123456"})
    assert second.status_code == 200, second.text

    everything = json.dumps([
        first.json(), second.json(),
        client.get("/api/alarm/settings").json(),
        client.get("/api/status").json(),
        client.get("/api/capabilities").json(),
        client.get("/api/log").json(),
    ])
    for secret in (SECRET_PASSWORD, "cookie-value-secret", "refresh-secret",
                   "trust-secret", "someone@example.no"):
        assert secret not in everything, secret
    assert "s***@example.no" in everything

    stored = alarm_persistence.VERISURE_SESSION_FILE.read_text()
    assert SECRET_PASSWORD not in stored
    assert client.get("/api/status").json()["alarm"]["arm_state"] == "disarmed"


def test_turning_it_off_signs_out_and_deletes_the_session(client, fake_verisure):
    turn_on(client, provider="verisure")
    client.post("/api/alarm/verisure/login",
                json={"email": "someone@example.no", "password": SECRET_PASSWORD})
    client.post("/api/alarm/verisure/code", json={"code": "123456"})
    assert alarm_persistence.VERISURE_SESSION_FILE.exists()

    client.put("/api/alarm/settings", json={"enabled": False})
    assert not alarm_persistence.VERISURE_SESSION_FILE.exists()


def test_switching_to_the_demo_alarm_signs_out_too(client, fake_verisure):
    turn_on(client, provider="verisure")
    client.post("/api/alarm/verisure/login",
                json={"email": "someone@example.no", "password": SECRET_PASSWORD})
    client.post("/api/alarm/verisure/code", json={"code": "123456"})
    client.put("/api/alarm/settings", json={"provider": "simulated"})
    assert not alarm_persistence.VERISURE_SESSION_FILE.exists()


def test_never_signed_in_is_not_an_alarm_fault(client, fake_verisure, monkeypatch):
    raised = []
    monkeypatch.setattr(server.notifier, "notify",
                        lambda event_type, *a, **k: raised.append(event_type) or True)
    server.notifier._conditions.clear()
    turn_on(client, provider="verisure")
    status = client.get("/api/status").json()["alarm"]
    assert status["connection"] == "not_configured"
    assert "alarm_connection_lost" not in raised


# -- the warning -----------------------------------------------------------


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
    monkeypatch.setattr(server, "sensor_snapshots", [])
    return posted


class _Agg:
    def __init__(self, open_started_at):
        self.open_started_at = open_started_at


class _Result:
    def __init__(self, zones):
        self.zones = zones


def _alarm(monkeypatch, arm="disarmed", lock_method=None, read_at=None):
    locks = ()
    if lock_method:
        locks = (LockReading("door", "Front door", True, lock_method, "2026-09-30T08:00:00Z"),)
    monkeypatch.setattr(server, "alarm_settings", AlarmSettings(enabled=True))
    monkeypatch.setattr(server, "alarm_provider", object())
    monkeypatch.setattr(server, "alarm_reading", AlarmReading(
        arm, "2026-09-30T08:00:00Z", locks, read_at if read_at is not None else time.time()))


def _evaluate(open_for=600, zone="Kitchen"):
    return server._evaluate_sensor_alerts(
        _Result({"1": _Agg(time.time() - open_for)}), {"1": zone})


def test_locked_from_outside_with_a_window_open_warns(sent, monkeypatch):
    _alarm(monkeypatch, lock_method="code")
    _evaluate()
    alerts = [m for m in sent if m["type"] == "alarm_left_open"]
    assert len(alerts) == 1
    assert "Kitchen" in alerts[0]["subject"]
    assert "Front door was locked from outside" in alerts[0]["body"]
    assert alerts[0]["severity"] == "critical"
    assert server.alarm_ledger.left_open_raised is True


def test_armed_at_home_is_a_warning_not_urgent(sent, monkeypatch):
    _alarm(monkeypatch, arm="armed_home")
    _evaluate()
    (alert,) = [m for m in sent if m["type"] == "alarm_left_open"]
    assert alert["severity"] == "warning"


def test_the_same_grace_as_away(sent, monkeypatch):
    """Shutting the window on the way out is not an alarm."""
    _alarm(monkeypatch, arm="armed_away")
    deadline = _evaluate(open_for=30)
    assert [m for m in sent if m["type"] == "alarm_left_open"] == []
    assert deadline is not None and deadline > time.time()


def test_it_replaces_the_away_alert_rather_than_doubling_it(sent, monkeypatch):
    _alarm(monkeypatch, arm="armed_away")
    monkeypatch.setattr(server, "_global_override_mode", lambda: "away")
    _evaluate()
    types = [m["type"] for m in sent]
    assert "alarm_left_open" in types
    assert "contact_open_while_away" not in types


def test_disarming_clears_it_without_an_email(sent, monkeypatch):
    _alarm(monkeypatch, arm="armed_away")
    _evaluate()
    sent.clear()
    _alarm(monkeypatch, arm="disarmed")
    _evaluate()
    assert sent == []
    assert server.notifier.is_raised("alarm-left-open") is False


def test_shutting_everything_sends_the_all_clear(sent, monkeypatch):
    _alarm(monkeypatch, arm="armed_away")
    _evaluate()
    sent.clear()
    server._evaluate_sensor_alerts(_Result({}), {})
    assert [m["subject"] for m in sent] == ["Everything is shut again"]


def test_a_stale_reading_neither_raises_nor_clears(sent, monkeypatch):
    _alarm(monkeypatch, arm="armed_away")
    _evaluate()
    sent.clear()
    _alarm(monkeypatch, arm="disarmed", read_at=time.time() - server.ALARM_STALE_SECONDS - 1)
    _evaluate()
    assert sent == []
    assert server.notifier.is_raised("alarm-left-open") is True


def test_an_offline_sensor_is_mentioned(sent, monkeypatch):
    from datetime import datetime, timezone

    from sensor_provider import ContactSnapshot, ContactState, SensorKind

    now = datetime.now(timezone.utc)
    _alarm(monkeypatch, arm="armed_away")
    monkeypatch.setattr(server, "sensor_snapshots", [ContactSnapshot(
        sensor_id="0x9", provider_id="test:0x9", name="Cellar Door", zone_id="2",
        state=ContactState("closed"), available=False, battery=90,
        changed_at=now, last_seen_at=now, kind=SensorKind.DOOR,
    )])
    _evaluate()
    (alert,) = [m for m in sent if m["type"] == "alarm_left_open"]
    assert "Cellar Door" in alert["body"]


def test_the_alarm_alerts_are_greyed_out_when_it_is_off(monkeypatch):
    from notifications import EVENT_TYPES

    monkeypatch.setattr(server, "sensor_settings", SensorSettings(enabled=True))
    out = server._filter_sensor_notification_settings({
        "events": {"alarm_left_open": True, "alarm_connection_lost": True},
        "event_types": {k: dict(EVENT_TYPES[k]) for k in ("alarm_left_open", "alarm_connection_lost")},
    })
    for key in ("alarm_left_open", "alarm_connection_lost"):
        assert out["event_types"][key]["unavailable"]
        assert out["events"][key] is False
    assert "unavailable" not in EVENT_TYPES["alarm_left_open"], "the shared table is not mutated"
