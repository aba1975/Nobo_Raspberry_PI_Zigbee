"""
Shared pytest fixtures for nobo-web-control test suite.

All tests run in demo mode (NOBO_DEMO=true) so no real Nobø Hub is needed.
"""

import os
import time

import pytest

# Force demo mode before importing the application module
os.environ.setdefault("NOBO_DEMO", "true")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import auth
import config_persistence


# ---------------------------------------------------------------------------
# Authenticated session for tests
# ---------------------------------------------------------------------------
# The API is deny-by-default (see AuthMiddleware), so tests that exercise
# /api/* need a session cookie. A fixed session id is re-injected before every
# test, which lets module-scoped TestClients keep the same cookie for the whole
# module without it going stale between tests.
TEST_SESSION_ID = "pytest-fixed-session-id"
TEST_USERNAME = "admin"


@pytest.fixture(autouse=True)
def authenticated_session():
    """Make TEST_SESSION_ID a valid admin session for the duration of a test."""
    auth.sessions[TEST_SESSION_ID] = {"username": TEST_USERNAME, "created": time.time()}
    yield
    auth.sessions.pop(TEST_SESSION_ID, None)


def authenticate(client):
    """Attach the shared test session cookie to a TestClient.

    Test modules generally inline ``client.cookies.set("session_id", ...)``
    instead of importing this, because ``tests`` is a package and importing
    from ``conftest`` is fragile under pytest's import modes. Keep the literal
    in sync with TEST_SESSION_ID above.
    """
    client.cookies.set("session_id", TEST_SESSION_ID)
    return client


@pytest.fixture(autouse=True)
def redirect_persistence(tmp_path, monkeypatch):
    """Redirect all config_persistence file paths to a per-test temp directory.

    This prevents test runs from writing to the real ``data/`` directory and
    ensures that persistence operations in one test cannot bleed into another.
    The same monkeypatching pattern is used by test_away_schedule.py for
    ``away_schedule.DATA_DIR`` / ``SCHEDULE_FILE``.
    """
    monkeypatch.setattr(config_persistence, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config_persistence, "DEMO_ZONES_FILE", tmp_path / "demo_zones.json")
    monkeypatch.setattr(config_persistence, "DEMO_SCHEDULES_FILE", tmp_path / "demo_schedules.json")
    monkeypatch.setattr(config_persistence, "SERVER_STATE_FILE", tmp_path / "server_state.json")
    yield
