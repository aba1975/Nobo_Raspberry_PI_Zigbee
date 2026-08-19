"""
Feature capability gating (QA defect D-04, extended).

The server publishes what it can do at ``/api/capabilities`` and the frontend
greys out the rest. These tests pin both halves and, importantly, the drift
between them — a control the UI does not gate is a button that looks live and
answers 501.

The set of gated features has shrunk to almost nothing: zone creation and
deletion, schedule editing, zone icons and all five device operations are now
implemented against a real hub too. What remains is the opposite case —
searching for devices needs a radio, so it is the *demo* mode that cannot do it.
"""

import os
import re
import sys
from pathlib import Path

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import server
from server import app

STATIC_DIR = Path(server.__file__).resolve().parent / "static"


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.cookies.set("session_id", "pytest-fixed-session-id")
        yield c


@pytest.fixture
def real_hub_mode(monkeypatch):
    """Pretend a real hub is configured, without touching the network."""
    monkeypatch.setattr(server, "DEMO_MODE", False)
    yield


@pytest.fixture(scope="module")
def app_js():
    return (STATIC_DIR / "app.js").read_text(encoding="utf-8")


class TestCapabilitiesEndpoint:
    def test_requires_a_session(self):
        with TestClient(app) as anon:
            anon.cookies.clear()
            assert anon.get("/api/capabilities").status_code == 401

    def test_editing_features_work_in_both_modes(self, client, real_hub_mode):
        """The whole point of the implementation work: nothing is demo-only."""
        body = client.get("/api/capabilities").json()
        assert body["demo_mode"] is False
        for name in (
            "add_zone", "delete_zone", "zone_icon", "edit_schedule",
            "add_device", "rename_device", "replace_device",
            "remove_device", "move_device",
        ):
            assert name not in body["features"], (
                f"{name} is implemented for a real hub and should no longer be gated"
            )

    def test_demo_mode_cannot_search_for_devices(self, client):
        body = client.get("/api/capabilities").json()
        assert body["demo_mode"] is True
        search = body["features"]["discover_devices"]
        assert search["supported"] is False
        assert search["reason"]

    def test_real_hub_can_search_for_devices(self, client, real_hub_mode):
        search = client.get("/api/capabilities").json()["features"]["discover_devices"]
        assert search["supported"] is True
        assert search["reason"] is None

    def test_every_unsupported_feature_explains_itself(self, client):
        features = client.get("/api/capabilities").json()["features"]
        for name, feature in features.items():
            if not feature["supported"]:
                assert feature["reason"], f"{name} must say why it is unavailable"


class TestEndpointsAgreeWithCapabilities:
    """Every gated feature must really answer 501, and nothing else should."""

    def test_search_is_refused_in_demo_mode_with_the_published_reason(self, client):
        """
        The capability check must come first: an unsupported feature is
        unsupported whether or not a hub happens to be reachable, and a
        misleading 503 would send users hunting for a network fault.
        """
        published = client.get("/api/capabilities").json()["features"]
        for method, path in (
            ("post", "/api/devices/search"),
            ("get", "/api/devices/search"),
            ("delete", "/api/devices/search"),
        ):
            r = getattr(client, method)(path)
            assert r.status_code == 501, f"{method} {path} should be unimplemented here"
            assert r.json()["detail"] == published["discover_devices"]["reason"], (
                "the endpoint's message has drifted from the one published to the UI"
            )

    def test_previously_gated_endpoints_no_longer_return_501(self, client, real_hub_mode):
        """These used to be dead ends against a real hub. They must not be now.

        No hub is connected in this test, so 503 is the honest answer. What
        matters is that the request gets as far as the hub check at all.
        """
        calls = [
            ("post", "/api/zones", {"json": {"name": "x", "icon": "X"}}),
            ("delete", "/api/zones/1", {}),
            ("post", "/api/zones/1/schedule", {"json": {"schedule": {}}}),
            ("post", "/api/devices", {"json": {"serial": "210000016299", "zone_id": "1"}}),
            ("patch", "/api/devices/210000016247/name", {"json": {"name": "n"}}),
            ("put", "/api/devices/210000016247", {"json": {"new_serial": "210000016299"}}),
            ("delete", "/api/devices/210000016247", {}),
            ("post", "/api/devices/210000016247/move", {"json": {"new_zone_id": "2"}}),
        ]
        for method, path, kwargs in calls:
            r = getattr(client, method)(path, **kwargs)
            assert r.status_code != 501, f"{method} {path} still reports itself unimplemented"

    def test_no_stray_hardcoded_501s(self):
        """A 501 raised outside the capability map would never be gated in the UI."""
        source = Path(server.__file__).read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in source.splitlines()
            if "status_code=501" in line and "capability" not in line
        ]
        assert not offenders, (
            f"these raise 501 without going through require_capability(), so the "
            f"UI cannot know to disable them: {offenders}"
        )

    def test_always_available_features_are_never_gated(self):
        """Overrides and temperatures must not get caught in the gating."""
        gated = set(server.DEMO_ONLY_FEATURES) | set(server.HUB_ONLY_FEATURES)
        for name in ("set_override", "set_temperature", "away_schedule", "rename_zone"):
            assert name not in gated


class TestFrontendGating:
    def test_frontend_reads_the_endpoint(self, app_js):
        assert "/api/capabilities" in app_js

    def test_every_capability_has_at_least_one_control(self, app_js):
        """A capability nobody maps to is a control that stays clickable."""
        mapped = set(re.findall(r":\s*'([a-z_]+)',", app_js))
        known = set(server.DEMO_ONLY_FEATURES) | set(server.HUB_ONLY_FEATURES)
        missing = known - mapped
        assert not missing, (
            f"no UI control is mapped to these capabilities, so they would stay "
            f"enabled and fail with a 501: {sorted(missing)}"
        )

    def test_mapped_capabilities_all_exist(self, app_js):
        """A typo in the map silently gates nothing."""
        block = re.search(r"CAPABILITY_BY_ACTION\s*=\s*\{(.*?)\};", app_js, re.S)
        assert block, "CAPABILITY_BY_ACTION not found in app.js"

        used = set(re.findall(r":\s*'([a-z_]+)'", block.group(1)))
        known = set(server.DEMO_ONLY_FEATURES) | set(server.HUB_ONLY_FEATURES)
        unknown = used - known
        assert not unknown, f"app.js gates on unknown capabilities: {sorted(unknown)}"

    def test_handlers_in_the_map_exist_in_app_js(self, app_js):
        """Guards against a control being renamed and quietly losing its gate."""
        block = re.search(r"CAPABILITY_BY_ACTION\s*=\s*\{(.*?)\};", app_js, re.S)
        handlers = re.findall(r"^\s*([A-Za-z_$][\w$]*)\s*:", block.group(1), re.M)
        assert handlers

        for handler in handlers:
            assert re.search(rf"function\s+{re.escape(handler)}\s*\(", app_js), (
                f"CAPABILITY_BY_ACTION gates '{handler}', but no such function "
                f"exists in app.js any more"
            )

    def test_disabled_style_exists(self):
        """Without the CSS rule the controls look enabled and still fire."""
        css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        assert ".capability-disabled" in css
        assert "pointer-events: none" in css

    def test_notice_style_exists(self):
        css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        assert ".capability-notice" in css
