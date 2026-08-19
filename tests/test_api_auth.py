"""
Tests for the deny-by-default authentication policy (QA defect D-01).

Before this fix, everything under /api/ and the /ws WebSocket was reachable with
no session at all: an anonymous caller on the LAN could delete zones, remove
devices, put the whole house into Away mode, and read the hub's serial and IP.

These tests pin the new behaviour:
  * anonymous requests to protected routes get 401 (API) or a redirect (pages)
  * the public allow-list stays small and explicit
  * a valid session still reaches everything
  * NOBO_ALLOW_ANON_API re-opens the API when explicitly switched on
"""

import os
import sys
import time
import json

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import auth
import server
from server import app

SESSION_COOKIE = "pytest-fixed-session-id"


@pytest.fixture
def anon():
    """A client with no session cookie at all."""
    with TestClient(app, raise_server_exceptions=True) as c:
        c.cookies.clear()
        yield c


@pytest.fixture
def authed():
    with TestClient(app, raise_server_exceptions=True) as c:
        c.cookies.set("session_id", SESSION_COOKIE)
        yield c


# Every read route that used to be anonymous.
PROTECTED_GETS = [
    "/api/status",
    "/api/hub",
    "/api/hub/config",
    "/api/zones",
    "/api/devices",
    "/api/week_profiles",
    "/api/log",
    "/api/global-mode/away-schedule",
    "/api/zones/1/schedule",
]

# Every destructive route that used to be anonymous.
PROTECTED_WRITES = [
    ("post", "/api/zones", {"json": {"name": "intruder", "icon": "X"}}),
    ("put", "/api/zones/1", {"json": {"name": "intruder"}}),
    ("delete", "/api/zones/1", {}),
    ("post", "/api/zones/1/override/away", {}),
    ("post", "/api/global/override/away", {}),
    ("post", "/api/zones/1/temperature", {"json": {"comfort": 30, "eco": 30}}),
    ("post", "/api/zones/1/schedule", {"json": {"schedule": {}}}),
    ("post", "/api/devices", {"json": {"serial": "210000016299", "zone_id": "1"}}),
    ("patch", "/api/devices/210000016247/name", {"json": {"name": "pwned"}}),
    ("put", "/api/devices/210000016247", {"json": {"serial": "210000016299"}}),
    ("delete", "/api/devices/210000016247", {}),
    ("post", "/api/devices/210000016247/move", {"json": {"zone_id": "2"}}),
    ("post", "/api/log/clear", {}),
    ("post", "/api/hub/config", {"json": {"demo_mode": True}}),
]


class TestAnonymousIsRejected:
    @pytest.mark.parametrize("path", PROTECTED_GETS)
    def test_reads_require_a_session(self, anon, path):
        r = anon.get(path)
        assert r.status_code == 401, f"{path} leaked data to an anonymous caller"
        assert r.json()["detail"] == "Not authenticated"

    @pytest.mark.parametrize("method,path,kwargs", PROTECTED_WRITES)
    def test_writes_require_a_session(self, anon, method, path, kwargs):
        r = getattr(anon, method)(path, **kwargs)
        assert r.status_code == 401, f"{method.upper()} {path} accepted an anonymous write"

    def test_hub_config_does_not_leak_serial_or_ip(self, anon):
        """The hub serial and IP are exactly what an attacker needs to talk to
        the hub directly, so they must not be readable without a login."""
        r = anon.get("/api/hub/config")
        assert r.status_code == 401
        body = r.text
        assert server.NOBO_SERIAL not in body
        assert server.NOBO_IP not in body

    def test_pages_redirect_rather_than_401(self, anon):
        """Browsers should land on the login page, not see a JSON error."""
        for path in ("/", "/static/app.js"):
            r = anon.get(path, follow_redirects=False)
            assert r.status_code == 302
            assert r.headers["location"] == "/login"

    def test_unknown_api_path_is_also_denied(self, anon):
        """Deny-by-default must not depend on a route existing."""
        assert anon.get("/api/definitely-not-a-route").status_code == 401


class TestPublicAllowList:
    def test_health_is_public(self, anon):
        """Container healthchecks and monitoring must work without a login."""
        r = anon.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_health_does_not_disclose_the_serial(self, anon):
        assert server.NOBO_SERIAL not in anon.get("/api/health").text

    def test_login_page_is_public(self, anon):
        assert anon.get("/login").status_code == 200

    def test_login_endpoint_is_public(self, anon):
        """Reachable without a session, but still rejects bad credentials."""
        r = anon.post("/auth/login", data={"username": "admin", "password": "wrong"})
        assert r.status_code == 401

    def test_allow_list_is_exactly_these_four(self):
        assert server.PUBLIC_PATHS == frozenset(
            {"/login", "/auth/login", "/favicon.ico", "/api/health"}
        )

    def test_other_auth_endpoints_are_not_public(self, anon):
        for path in ("/auth/me", "/auth/admin/users"):
            assert anon.get(path).status_code == 401


class TestAuthenticatedStillWorks:
    @pytest.mark.parametrize("path", PROTECTED_GETS)
    def test_reads_succeed_with_a_session(self, authed, path):
        assert authed.get(path).status_code == 200

    def test_writes_succeed_with_a_session(self, authed):
        assert authed.post("/api/zones/1/override/eco").status_code == 200
        assert authed.post("/api/zones/1/override/normal").status_code == 200

    def test_expired_session_is_rejected(self, authed):
        """A cookie whose session has aged out must not keep working."""
        auth.sessions[SESSION_COOKIE]["created"] = time.time() - auth.SESSION_MAX_AGE - 1
        assert authed.get("/api/zones").status_code == 401


class TestWebSocketAuth:
    def test_anonymous_websocket_is_refused(self, anon):
        """BaseHTTPMiddleware never sees the handshake, so /ws checks itself."""
        with pytest.raises(Exception):
            with anon.websocket_connect("/ws"):
                pass

    def test_authenticated_websocket_connects(self, authed):
        with authed.websocket_connect("/ws") as ws:
            # The server pushes the current zones as soon as it accepts.
            assert json.loads(ws.receive_text())["type"] == "zones_update"
            ws.send_text("ping")
            assert ws.receive_text() == "pong"


class TestAnonymousOptIn:
    """NOBO_ALLOW_ANON_API restores the old behaviour for headless integrations."""

    @pytest.fixture
    def anon_api_enabled(self):
        original = server.ALLOW_ANON_API
        server.ALLOW_ANON_API = True
        yield
        server.ALLOW_ANON_API = original

    def test_defaults_to_off(self):
        """The insecure mode must never be the default."""
        assert server.ALLOW_ANON_API is False

    def test_api_opens_when_enabled(self, anon, anon_api_enabled):
        assert anon.get("/api/zones").status_code == 200

    def test_websocket_opens_when_enabled(self, anon, anon_api_enabled):
        with anon.websocket_connect("/ws") as ws:
            assert json.loads(ws.receive_text())["type"] == "zones_update"
            ws.send_text("ping")
            assert ws.receive_text() == "pong"

    def test_pages_stay_protected_even_when_enabled(self, anon, anon_api_enabled):
        """The opt-in covers integrations, not the browser UI."""
        r = anon.get("/", follow_redirects=False)
        assert r.status_code == 302
