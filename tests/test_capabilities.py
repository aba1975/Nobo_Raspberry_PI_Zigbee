"""
Feature capability gating (QA defect D-04).

Adding a zone, deleting a zone, editing a week schedule and everything in the
device manager are implemented for demo mode only. Against a real hub they
answer 501, but the web UI offered the buttons anyway, so pressing them looked
like the app was broken.

The server now publishes what it can do at /api/capabilities and the frontend
greys out the rest. These tests pin both halves, including the drift between
them, which is the failure mode that would quietly come back.
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

    def test_everything_is_supported_in_demo_mode(self, client):
        body = client.get("/api/capabilities").json()
        assert body["demo_mode"] is True
        assert all(f["supported"] for f in body["features"].values())
        assert all(f["reason"] is None for f in body["features"].values())

    def test_demo_only_features_are_unsupported_on_a_real_hub(self, client, real_hub_mode):
        body = client.get("/api/capabilities").json()
        assert body["demo_mode"] is False
        assert body["features"]
        for name, feature in body["features"].items():
            assert feature["supported"] is False, f"{name} should be gated"
            assert feature["reason"], f"{name} must explain why it is unavailable"

    def test_reasons_point_at_the_official_app(self, client, real_hub_mode):
        """Users need to be told where they *can* do it, not just that they can't."""
        features = client.get("/api/capabilities").json()["features"]
        mentions = [f for f in features.values() if "Nobø" in f["reason"]]
        assert len(mentions) >= 5


class TestEndpointsAgreeWithCapabilities:
    """Every gated feature must really answer 501, and nothing else should."""

    CALLS = {
        "add_zone": ("post", "/api/zones", {"json": {"name": "x", "icon": "X"}}),
        "delete_zone": ("delete", "/api/zones/1", {}),
        "edit_schedule": ("post", "/api/zones/1/schedule", {"json": {"schedule": {}}}),
        "add_device": (
            "post",
            "/api/devices",
            {"json": {"serial": "210000016299", "zone_id": "1"}},
        ),
        "rename_device": (
            "patch",
            "/api/devices/210000016247/name",
            {"json": {"name": "n"}},
        ),
        "replace_device": (
            "put",
            "/api/devices/210000016247",
            {"json": {"new_serial": "210000016299"}},
        ),
        "remove_device": ("delete", "/api/devices/210000016247", {}),
        "move_device": (
            "post",
            "/api/devices/210000016247/move",
            {"json": {"new_zone_id": "2"}},
        ),
    }

    @pytest.mark.parametrize("feature", sorted(CALLS))
    def test_gated_feature_returns_501_with_the_published_reason(
        self, client, real_hub_mode, feature
    ):
        """
        The capability check must come first: an unsupported feature is
        unsupported whether or not the hub happens to be reachable, and a
        misleading 503 would send users hunting for a network fault.
        """
        method, path, kwargs = self.CALLS[feature]
        r = getattr(client, method)(path, **kwargs)

        assert r.status_code == 501, f"{feature} did not report itself unimplemented"
        assert r.json()["detail"] == server.DEMO_ONLY_FEATURES[feature], (
            "the endpoint's message has drifted from the one published to the UI"
        )

    def test_no_stray_hardcoded_501s(self):
        """A 501 raised outside the map would never be gated in the UI."""
        source = Path(server.__file__).read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in source.splitlines()
            if "status_code=501" in line and "DEMO_ONLY_FEATURES" not in line
        ]
        assert not offenders, (
            f"these raise 501 without going through require_capability(), so the "
            f"UI cannot know to disable them: {offenders}"
        )

    def test_always_available_features_still_work_on_a_real_hub(self):
        """Overrides and temperatures must not get caught in the gating."""
        for name in ("set_override", "set_temperature", "away_schedule", "rename_zone"):
            assert name not in server.DEMO_ONLY_FEATURES


class TestFrontendGating:
    def test_frontend_reads_the_endpoint(self, app_js):
        assert "/api/capabilities" in app_js

    def test_every_capability_has_at_least_one_control(self, app_js):
        """A capability nobody maps to is a control that stays clickable."""
        mapped = set(re.findall(r":\s*'([a-z_]+)',", app_js))
        missing = set(server.DEMO_ONLY_FEATURES) - mapped
        assert not missing, (
            f"no UI control is mapped to these capabilities, so they would stay "
            f"enabled and fail with a 501: {sorted(missing)}"
        )

    def test_mapped_capabilities_all_exist(self, app_js):
        """A typo in the map silently gates nothing."""
        block = re.search(r"CAPABILITY_BY_ACTION\s*=\s*\{(.*?)\};", app_js, re.S)
        assert block, "CAPABILITY_BY_ACTION not found in app.js"

        used = set(re.findall(r":\s*'([a-z_]+)'", block.group(1)))
        unknown = used - set(server.DEMO_ONLY_FEATURES)
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
