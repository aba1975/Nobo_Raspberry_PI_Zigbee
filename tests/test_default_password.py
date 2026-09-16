"""The password this software ships with is published in its own README.

That is fine while it is a private project somebody is trying out, and not fine
once the repository is public and installations exist that nobody re-secured.
Until it is changed it protects nothing, so the interface says so where it
cannot be missed — and, until now, there was no way to change it from the
interface at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import server

ROOT = Path(__file__).resolve().parent.parent
CABIN = (ROOT / "app" / "static" / "ui" / "cabin" / "cabin.js").read_text(encoding="utf-8")
CORE = (ROOT / "app" / "static" / "ui" / "shared" / "core.js").read_text(encoding="utf-8")
INDEX = (ROOT / "app" / "static" / "ui" / "cabin" / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def client():
    with TestClient(server.app) as value:
        value.cookies.set("session_id", "pytest-fixed-session-id")
        yield value


def set_password(password: str) -> None:
    users = auth.load_users()
    users["admin"] = {"password_hash": auth.hash_password(password), "role": "admin"}
    auth.save_users(users)


# -- detection --------------------------------------------------------------


def test_the_shipped_password_is_recognised():
    set_password(auth.DEFAULT_PASSWORD)
    assert auth.is_using_default_password("admin") is True


def test_any_other_password_is_not():
    set_password("a-password-of-my-own")
    assert auth.is_using_default_password("admin") is False


def test_an_unknown_account_is_not_flagged():
    assert auth.is_using_default_password("nobody-by-that-name") is False


def test_a_corrupt_hash_is_a_different_problem():
    users = auth.load_users()
    users["admin"] = {"password_hash": "not-a-bcrypt-hash", "role": "admin"}
    auth.save_users(users)
    assert auth.is_using_default_password("admin") is False


# -- over the API -----------------------------------------------------------


def test_the_api_reports_it(client):
    set_password(auth.DEFAULT_PASSWORD)

    body = client.get("/auth/me").json()

    assert body["using_default_password"] is True


def test_it_stops_being_reported_once_changed(client):
    set_password(auth.DEFAULT_PASSWORD)
    assert client.get("/auth/me").json()["using_default_password"] is True

    changed = client.post("/auth/change-password", json={
        "current_password": auth.DEFAULT_PASSWORD,
        "new_password": "a-password-of-my-own",
        "confirm_password": "a-password-of-my-own",
    })

    assert changed.status_code == 200, changed.text
    assert client.get("/auth/me").json()["using_default_password"] is False


def test_it_is_not_told_to_anyone_who_is_not_an_admin(client):
    set_password(auth.DEFAULT_PASSWORD)
    users = auth.load_users()
    users["admin"]["role"] = "user"
    auth.save_users(users)

    # A prompt to act, for the person who can act. Not a fact to hand out.
    assert client.get("/auth/me").json()["using_default_password"] is False


def test_it_needs_a_session():
    with TestClient(server.app) as anonymous:
        assert anonymous.get("/auth/me").status_code in (302, 401, 403)


# -- the interface ----------------------------------------------------------


def test_there_is_now_a_way_to_change_it_from_the_interface():
    """The endpoint existed from the beginning and nothing ever called it, so
    the only way to stop using the shipped password was to edit a file."""
    assert "changePassword:" in CORE
    assert "/auth/change-password" in CORE
    assert "function changePasswordSheet" in CABIN
    # Reachable normally, not only by way of the warning.
    assert 'data-act="change-password"' in CABIN


def test_the_warning_is_shown_where_it_cannot_be_missed():
    assert 'id="passwordAlert"' in INDEX
    assert "renderPasswordAlert()" in CABIN
    assert "state.me.using_default_password" in CABIN
    # And says why it matters, rather than nagging.
    assert "published in its documentation" in CABIN


def test_the_warning_does_not_look_like_a_heating_fault():
    css = (ROOT / "app" / "static" / "ui" / "cabin" / "cabin.css").read_text(encoding="utf-8")
    assert ".trip-alert.is-security" in css
    assert "the house is fine, the software is not being used" in css
