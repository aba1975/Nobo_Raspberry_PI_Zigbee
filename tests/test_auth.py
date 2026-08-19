"""
Tests for the authentication layer (auth.py + server.py auth endpoints).

Uses a temporary data directory so tests never touch production data/users.json.
"""

import os
import sys
import tempfile

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import auth
import server
from server import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_auth(tmp_path):
    """
    Redirect auth storage to a temp directory and reset in-memory state between
    tests so they are fully isolated.
    """
    original_data_dir = auth.DATA_DIR
    original_users_file = auth.USERS_FILE

    auth.DATA_DIR = tmp_path
    auth.USERS_FILE = tmp_path / "users.json"

    # Re-initialise with a fresh admin account in the temp dir
    auth.sessions.clear()
    auth.login_attempts.clear()
    auth.init_user_store()

    yield

    # Restore
    auth.DATA_DIR = original_data_dir
    auth.USERS_FILE = original_users_file
    auth.sessions.clear()
    auth.login_attempts.clear()


@pytest.fixture(scope="function")
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _login(client, username="admin", password="nobohub"):
    """POST /auth/login with form-encoded credentials and return the response."""
    return client.post(
        "/auth/login",
        data={"username": username, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


# ---------------------------------------------------------------------------
# API endpoints remain open (no auth required)
# ---------------------------------------------------------------------------

class TestApiUnprotected:
    def test_api_status_requires_auth(self, client):
        """/api/status used to be open to anyone on the LAN (QA defect D-01)."""
        assert client.get("/api/status").status_code == 401

    def test_api_status_with_auth(self, client):
        _login(client)
        assert client.get("/api/status").status_code == 200

    def test_api_health_no_auth(self, client):
        """Health stays public so container healthchecks keep working."""
        r = client.get("/api/health")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Unauthenticated access to UI is redirected
# ---------------------------------------------------------------------------

class TestUnauthenticatedRedirects:
    def test_root_redirects_to_login(self, client):
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"

    def test_static_asset_redirects_to_login(self, client):
        r = client.get("/static/app.js", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"

    def test_login_page_accessible_unauthenticated(self, client):
        r = client.get("/login")
        assert r.status_code == 200
        assert b"form" in r.content.lower()


# ---------------------------------------------------------------------------
# Login endpoint
# ---------------------------------------------------------------------------

class TestLogin:
    def test_login_correct_credentials(self, client):
        r = _login(client)
        assert r.status_code == 200
        body = r.json()
        assert body["username"] == "admin"
        assert body["role"] == "admin"
        assert "session_id" in r.cookies

    def test_login_wrong_password(self, client):
        r = _login(client, password="wrongpassword")
        assert r.status_code == 401

    def test_login_wrong_username(self, client):
        r = _login(client, username="ghost", password="anything")
        assert r.status_code == 401

    def test_login_missing_fields(self, client):
        r = client.post(
            "/auth/login",
            data={"username": "admin"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Authenticated access
# ---------------------------------------------------------------------------

class TestAuthenticatedAccess:
    def test_root_accessible_after_login(self, client):
        _login(client)
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 200

    def test_static_asset_accessible_after_login(self, client):
        _login(client)
        r = client.get("/static/app.js", follow_redirects=False)
        assert r.status_code == 200

    def test_auth_me_returns_user(self, client):
        _login(client)
        r = client.get("/auth/me")
        assert r.status_code == 200
        body = r.json()
        assert body["username"] == "admin"
        assert body["role"] == "admin"

    def test_auth_me_without_session(self, client):
        r = client.get("/auth/me")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

class TestLogout:
    def test_logout_clears_session(self, client):
        _login(client)
        r = client.post("/auth/logout")
        assert r.status_code == 200
        # After logout, root should redirect again
        r2 = client.get("/", follow_redirects=False)
        assert r2.status_code == 302


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimit:
    def test_five_failed_attempts_trigger_lockout(self, client):
        for _ in range(auth.MAX_ATTEMPTS):
            r = _login(client, password="bad")
            assert r.status_code == 401

        # 6th attempt should be rate-limited (429)
        r = _login(client, password="bad")
        assert r.status_code == 429

    def test_correct_login_clears_attempts(self, client):
        for _ in range(auth.MAX_ATTEMPTS - 1):
            _login(client, password="bad")
        # Correct login should succeed and reset counter
        r = _login(client)
        assert r.status_code == 200
        # Now wrong password should start fresh (not immediately locked)
        r2 = _login(client, password="bad")
        assert r2.status_code == 401


# ---------------------------------------------------------------------------
# WebSocket remains open without auth
# ---------------------------------------------------------------------------

class TestWebSocket:
    def test_ws_rejects_unauthenticated(self, client):
        """The live zone feed used to be readable by anyone (QA defect D-01)."""
        with pytest.raises(Exception):
            with client.websocket_connect("/ws"):
                pass

    def test_ws_accessible_with_auth(self, client):
        _login(client)
        with client.websocket_connect("/ws") as ws:
            # Server sends initial zones_update on connect; consume it first
            initial = ws.receive_json()
            assert initial["type"] == "zones_update"
            ws.send_text("ping")
            data = ws.receive_text()
            assert data == "pong"


# ---------------------------------------------------------------------------
# Change password
# ---------------------------------------------------------------------------

class TestChangePassword:
    def test_change_password_success(self, client):
        _login(client)
        r = client.post(
            "/auth/change-password",
            json={
                "current_password": "nobohub",
                "new_password": "newpass123",
                "confirm_password": "newpass123",
            },
        )
        assert r.status_code == 200

    def test_change_password_wrong_current(self, client):
        _login(client)
        r = client.post(
            "/auth/change-password",
            json={
                "current_password": "wrongcurrent",
                "new_password": "newpass123",
                "confirm_password": "newpass123",
            },
        )
        assert r.status_code == 401

    def test_change_password_mismatch(self, client):
        _login(client)
        r = client.post(
            "/auth/change-password",
            json={
                "current_password": "nobohub",
                "new_password": "newpass123",
                "confirm_password": "different123",
            },
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Admin user management
# ---------------------------------------------------------------------------

class TestAdminUsers:
    def test_list_users_as_admin(self, client):
        _login(client)
        r = client.get("/auth/admin/users")
        assert r.status_code == 200
        users = r.json()
        assert any(u["username"] == "admin" for u in users)

    def test_add_and_delete_user(self, client):
        _login(client)
        r = client.post(
            "/auth/admin/users",
            json={"username": "testuser", "password": "pass1234", "role": "user"},
        )
        assert r.status_code == 200

        # Should now appear in list
        users = client.get("/auth/admin/users").json()
        assert any(u["username"] == "testuser" for u in users)

        # Delete
        r2 = client.delete("/auth/admin/users/testuser")
        assert r2.status_code == 200

        users2 = client.get("/auth/admin/users").json()
        assert not any(u["username"] == "testuser" for u in users2)

    def test_cannot_delete_own_account(self, client):
        _login(client)
        r = client.delete("/auth/admin/users/admin")
        assert r.status_code == 400

    def test_non_admin_cannot_list_users(self, client):
        # Add a regular user and log in as them
        _login(client)
        client.post(
            "/auth/admin/users",
            json={"username": "regular", "password": "pass1234", "role": "user"},
        )
        client.post("/auth/logout")
        _login(client, username="regular", password="pass1234")
        r = client.get("/auth/admin/users")
        assert r.status_code == 403
