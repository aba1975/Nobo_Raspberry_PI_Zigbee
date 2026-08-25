"""The interface switch: which UI answers "/", and the promise that the other
one is still there.

The whole point of shipping both interfaces in one image is that a rollback is
a configuration change rather than a deployment. That promise is only worth
anything if `/classic` genuinely keeps working after Cabin becomes the default,
so it is tested rather than assumed.
"""

import importlib
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))


def _server_with_ui(monkeypatch, value):
    """Re-import the server module with NOBO_UI set, since the choice is read
    once at startup on purpose."""
    if value is None:
        monkeypatch.delenv("NOBO_UI", raising=False)
    else:
        monkeypatch.setenv("NOBO_UI", value)
    monkeypatch.setenv("NOBO_DEMO", "true")
    import server
    return importlib.reload(server)


@pytest.fixture
def restore_server():
    """Leave the module as the rest of the suite expects to find it."""
    yield
    import server
    os.environ.pop("NOBO_UI", None)
    importlib.reload(server)


def test_cabin_is_the_default(monkeypatch, restore_server):
    server = _server_with_ui(monkeypatch, None)
    assert server.ACTIVE_UI == "cabin"


def test_classic_can_be_selected(monkeypatch, restore_server):
    server = _server_with_ui(monkeypatch, "classic")
    assert server.ACTIVE_UI == "classic"


def test_an_unknown_value_falls_back_rather_than_refusing_to_start(monkeypatch, restore_server):
    """A typo in .env must not leave a cabin with no way to control the heating."""
    server = _server_with_ui(monkeypatch, "cabbin")
    assert server.ACTIVE_UI == "cabin"


def test_the_value_is_not_case_or_space_sensitive(monkeypatch, restore_server):
    server = _server_with_ui(monkeypatch, "  Classic ")
    assert server.ACTIVE_UI == "classic"


def _client(server):
    client = TestClient(server.app)
    client.cookies.set("session_id", "pytest-fixed-session-id")
    return client


def test_root_serves_the_selected_interface(monkeypatch, restore_server):
    server = _server_with_ui(monkeypatch, "cabin")
    body = _client(server).get("/").text
    assert "/static/ui/cabin/cabin.js" in body

    server = _server_with_ui(monkeypatch, "classic")
    body = _client(server).get("/").text
    assert "/static/ui/cabin/cabin.js" not in body


@pytest.mark.parametrize("selected", ["cabin", "classic"])
def test_both_interfaces_stay_reachable_whatever_is_selected(monkeypatch, restore_server, selected):
    """This is the rollback promise: you can always get to the other one."""
    server = _server_with_ui(monkeypatch, selected)
    client = _client(server)

    cabin = client.get("/cabin")
    classic = client.get("/classic")
    assert cabin.status_code == 200
    assert classic.status_code == 200
    assert "/static/ui/cabin/cabin.js" in cabin.text
    assert "/static/ui/cabin/cabin.js" not in classic.text


@pytest.mark.parametrize("selected", ["cabin", "classic"])
def test_the_login_page_matches_the_selected_interface(monkeypatch, restore_server, selected):
    server = _server_with_ui(monkeypatch, selected)
    body = TestClient(server.app).get("/login").text
    assert body.startswith("<!DOCTYPE html>")
    # Cabin's sign-in page carries the pine brand colour; the classic one does not.
    assert ("#2F5D50" in body) is (selected == "cabin")


def test_the_login_page_is_still_public(monkeypatch, restore_server):
    """It is reached by people who are by definition not signed in."""
    server = _server_with_ui(monkeypatch, "cabin")
    assert TestClient(server.app).get("/login").status_code == 200


@pytest.mark.parametrize("path", ["/", "/cabin", "/classic"])
def test_neither_interface_is_readable_without_signing_in(monkeypatch, restore_server, path):
    server = _server_with_ui(monkeypatch, "cabin")
    r = TestClient(server.app).get(path, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
