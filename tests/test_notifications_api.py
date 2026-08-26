"""
Tests for the notification HTTP endpoints.

Kept apart from ``test_notifications.py`` because these need the app, a session
and an admin user, while those are pure logic. The concerns here are access
control, not leaking the mail password, and refusing to switch the feature on
in a state where it would silently never deliver anything.
"""

import os
import sys

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import auth
import notifications
import server
from server import app

TEST_SESSION_ID = "pytest-fixed-session-id"

GOOD_EMAIL = {
    "host": "mail.example.com",
    "port": 587,
    "security": "starttls",
    "from_addr": "pi@example.com",
    "to_addrs": ["me@example.com"],
}


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(notifications, "DATA_DIR", tmp_path)
    monkeypatch.setattr(notifications, "NOTIFICATIONS_FILE", tmp_path / "notifications.json")
    # A fresh notifier per test, so state cannot leak between them.
    monkeypatch.setattr(server, "notifier", notifications.Notifier())
    yield


@pytest.fixture
def client():
    c = TestClient(app)
    c.cookies.set("session_id", TEST_SESSION_ID)
    return c


@pytest.fixture
def as_ordinary_user(monkeypatch):
    """Downgrade the test session to a non-admin account."""
    original = auth.load_users

    def users():
        data = dict(original())
        data["admin"] = {**data.get("admin", {}), "role": "user"}
        return data

    monkeypatch.setattr(auth, "load_users", users)
    yield


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def test_settings_need_a_session():
    anon = TestClient(app)
    assert anon.get("/api/notifications").status_code in (401, 403)


def test_an_ordinary_user_cannot_read_the_settings(client, as_ordinary_user):
    """They hold a mail password and decide where alerts go."""
    assert client.get("/api/notifications").status_code == 403


def test_an_ordinary_user_cannot_silence_the_alarms(client, as_ordinary_user):
    assert client.put("/api/notifications", json={"enabled": False}).status_code == 403


# ---------------------------------------------------------------------------
# Reading and writing
# ---------------------------------------------------------------------------

def test_defaults_are_returned(client):
    body = client.get("/api/notifications").json()
    assert body["enabled"] is False
    assert "room_cold" in body["events"]


def test_the_event_catalogue_is_offered_to_the_ui(client):
    """So the UI never has to hardcode the list or its wording."""
    body = client.get("/api/notifications").json()
    assert body["event_types"]["room_cold"]["label"]
    assert body["event_types"]["room_cold"]["help"]


def test_the_password_is_not_in_the_response(client):
    client.put("/api/notifications", json={
        "email": {**GOOD_EMAIL, "username": "me", "password": "hunter2"},
    })
    body = client.get("/api/notifications").json()
    assert "password" not in body["email"]
    assert body["email"]["password_set"] is True
    assert "hunter2" not in str(body)


def test_settings_can_be_turned_on(client):
    r = client.put("/api/notifications", json={"enabled": True, "email": GOOD_EMAIL})
    assert r.status_code == 200
    assert r.json()["enabled"] is True


def test_turning_it_on_without_a_mail_server_is_refused(client):
    """Otherwise it looks enabled and silently never sends anything."""
    r = client.put("/api/notifications", json={"enabled": True})
    assert r.status_code == 400
    assert "mail server" in r.json()["detail"]


def test_turning_it_on_with_nobody_to_tell_is_refused(client):
    r = client.put("/api/notifications", json={
        "enabled": True,
        "email": {**GOOD_EMAIL, "to_addrs": []},
    })
    assert r.status_code == 400
    assert "nobody to send to" in r.json()["detail"]


def test_a_partial_update_keeps_the_password(client):
    """The browser is never given the password, so it cannot resend it."""
    client.put("/api/notifications", json={
        "email": {**GOOD_EMAIL, "username": "me", "password": "hunter2"},
    })
    client.put("/api/notifications", json={"events": {"room_cold": False}})
    assert notifications.load_settings()["email"]["password"] == "hunter2"


def test_an_empty_password_clears_it(client):
    client.put("/api/notifications", json={
        "email": {**GOOD_EMAIL, "username": "me", "password": "hunter2"},
    })
    client.put("/api/notifications", json={"email": {"password": ""}})
    assert notifications.load_settings()["email"]["password"] == ""


def test_one_toggle_can_be_changed_alone(client):
    client.put("/api/notifications", json={"enabled": True, "email": GOOD_EMAIL})
    client.put("/api/notifications", json={"events": {"schedule_event": True}})
    saved = notifications.load_settings()
    assert saved["events"]["schedule_event"] is True
    assert saved["enabled"] is True
    assert saved["email"]["host"] == "mail.example.com"


def test_saving_applies_immediately(client):
    """A setting that needs a restart to take effect would be a trap."""
    client.put("/api/notifications", json={"enabled": True, "email": GOOD_EMAIL})
    assert server.notifier.settings["enabled"] is True


def test_the_cold_threshold_can_be_set(client):
    r = client.put("/api/notifications", json={"cold_threshold_c": 8})
    assert r.json()["cold_threshold_c"] == 8


def test_an_absurd_threshold_is_ignored(client):
    client.put("/api/notifications", json={"cold_threshold_c": 900})
    assert notifications.load_settings()["cold_threshold_c"] == 5.0


# ---------------------------------------------------------------------------
# The test button
# ---------------------------------------------------------------------------

def test_a_test_send_reports_the_real_reason_it_failed(client, monkeypatch):
    """'Authentication failed' and 'no such host' need different fixes."""
    def explode(cfg, subject, body):
        raise RuntimeError("authentication failed")

    monkeypatch.setattr(notifications, "_send_email_blocking", explode)
    r = client.post("/api/notifications/test", json={"email": GOOD_EMAIL})
    assert r.status_code == 502
    assert "authentication failed" in r.json()["detail"]


def test_a_test_send_can_use_settings_that_are_not_saved_yet(client, monkeypatch):
    """So nobody has to save a broken configuration to discover it is broken."""
    seen = {}

    def capture(cfg, subject, body):
        seen["to"] = cfg["email"]["to_addrs"]

    monkeypatch.setattr(notifications, "_send_email_blocking", capture)
    r = client.post("/api/notifications/test", json={
        "email": {**GOOD_EMAIL, "to_addrs": ["fresh@example.com"]},
    })
    assert r.status_code == 200
    assert seen["to"] == ["fresh@example.com"]
    # Nothing was persisted by a test send.
    assert notifications.load_settings()["email"]["to_addrs"] == []


def test_testing_an_incomplete_configuration_explains_what_is_missing(client):
    r = client.post("/api/notifications/test", json={"email": {"host": ""}})
    assert r.status_code == 400
    assert "mail server" in r.json()["detail"]


def test_an_ordinary_user_cannot_send_test_mail(client, as_ordinary_user):
    """It reaches an external server, so it is not left open to any account."""
    r = client.post("/api/notifications/test", json={"email": GOOD_EMAIL})
    assert r.status_code == 403
