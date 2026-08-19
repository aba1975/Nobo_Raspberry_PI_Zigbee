"""
Tests for the web-based hub configuration endpoints (/api/hub/config).

These cover the switch between demo mode and a real Nobo Eco Hub that is exposed
on the Devices page, including authorisation, validation, persistence and the
deliberate sign-out that follows a mode change.
"""

import os
import sys

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import auth
import config_persistence
import server
from server import app


@pytest.fixture(autouse=True)
def isolated_auth(tmp_path):
    """Redirect auth storage to a temp dir and reset in-memory session state."""
    original_data_dir = auth.DATA_DIR
    original_users_file = auth.USERS_FILE

    auth.DATA_DIR = tmp_path
    auth.USERS_FILE = tmp_path / "users.json"
    auth.sessions.clear()
    auth.login_attempts.clear()
    auth.init_user_store()

    yield

    auth.DATA_DIR = original_data_dir
    auth.USERS_FILE = original_users_file
    auth.sessions.clear()
    auth.login_attempts.clear()


@pytest.fixture(autouse=True)
def restore_hub_globals():
    """Any test that flips the mode must not leak it into the next test."""
    original = (server.DEMO_MODE, server.NOBO_SERIAL, server.NOBO_IP, server.HUB_CONFIG_SOURCE)
    yield
    server.DEMO_MODE, server.NOBO_SERIAL, server.NOBO_IP, server.HUB_CONFIG_SOURCE = original


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _login(client, username="admin", password="nobohub"):
    return client.post(
        "/auth/login",
        data={"username": username, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


class TestReadHubConfig:
    def test_get_requires_a_session(self, client):
        """The serial and IP are enough to talk to the hub directly, so the
        config must not be readable without logging in (QA defect D-01)."""
        assert client.get("/api/hub/config").status_code == 401

    def test_get_reports_demo_mode(self, client):
        _login(client)
        r = client.get("/api/hub/config")
        assert r.status_code == 200
        body = r.json()
        assert body["demo_mode"] is True
        assert body["source"] in ("environment", "web interface")
        assert "serial_display" in body


class TestAuthorisation:
    def test_post_requires_a_session(self, client):
        r = client.post("/api/hub/config", json={"demo_mode": True})
        assert r.status_code == 401

    def test_post_rejects_non_admin(self, client):
        _login(client)
        created = client.post(
            "/auth/admin/users",
            json={"username": "bob", "password": "hunter2hunter2", "role": "user"},
        )
        assert created.status_code == 200
        client.post("/auth/logout")

        assert _login(client, "bob", "hunter2hunter2").status_code == 200
        r = client.post("/api/hub/config", json={"demo_mode": True})
        assert r.status_code == 403


class TestValidation:
    def test_rejects_bad_serial(self, client):
        _login(client)
        r = client.post(
            "/api/hub/config",
            json={"demo_mode": False, "serial": "12", "ip": "192.168.1.10"},
        )
        assert r.status_code == 400

    def test_rejects_bad_ip(self, client):
        _login(client)
        r = client.post(
            "/api/hub/config",
            json={"demo_mode": False, "serial": "123456789012", "ip": "not-an-ip"},
        )
        assert r.status_code == 400

    def test_accepts_serial_with_spaces(self, client):
        _login(client)
        r = client.post(
            "/api/hub/config",
            json={"demo_mode": False, "serial": "210 000 016 247", "ip": "192.0.2.10"},
        )
        assert r.status_code == 200
        assert r.json()["serial"] == "210000016247"


class TestModeSwitch:
    def test_switch_signs_the_user_out(self, client):
        _login(client)
        assert client.get("/auth/me").status_code == 200

        r = client.post("/api/hub/config", json={"demo_mode": True})
        assert r.status_code == 200
        assert r.json()["signed_out"] is True

        # The session is gone, so an authenticated endpoint must now reject us.
        assert client.get("/auth/me").status_code == 401

    def test_unreachable_hub_still_saves_and_warns(self, client):
        _login(client)
        r = client.post(
            "/api/hub/config",
            json={"demo_mode": False, "serial": "210000016247", "ip": "192.0.2.10"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["demo_mode"] is False
        assert body["connected"] is False
        assert "warning" in body

    def test_settings_are_persisted(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(config_persistence, "HUB_CONFIG_FILE", tmp_path / "hub_config.json")

        _login(client)
        r = client.post(
            "/api/hub/config",
            json={"demo_mode": False, "serial": "210000016247", "ip": "192.0.2.10"},
        )
        assert r.status_code == 200

        saved = config_persistence.load_hub_config()
        assert saved["demo_mode"] is False
        assert saved["serial"] == "210000016247"
        assert saved["ip"] == "192.0.2.10"

    def test_source_becomes_web_interface(self, client):
        _login(client)
        client.post("/api/hub/config", json={"demo_mode": True})
        # Changing the mode signs the user out on purpose, so log back in
        # before reading the config.
        _login(client)
        assert client.get("/api/hub/config").json()["source"] == "web interface"
