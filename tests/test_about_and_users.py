"""Settings → About, user management in Cabin, and the retirement of Classic.

About reports what the installation is without disclosing anything new. Users
are managed from Cabin now that the classic interface, which used to be the
only place to do it, is gone — so the server guards that matter there (never
lose the last administrator, never leave a removed user signed in) are pinned
here too.
"""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import server

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
CABIN = (STATIC / "ui" / "cabin" / "cabin.js").read_text(encoding="utf-8")
CORE = (STATIC / "ui" / "shared" / "core.js").read_text(encoding="utf-8")
CSS = (STATIC / "ui" / "cabin" / "cabin.css").read_text(encoding="utf-8")
SESSION = "pytest-fixed-session-id"


@pytest.fixture
def client():
    with TestClient(server.app) as c:
        c.cookies.set("session_id", SESSION)
        yield c


@pytest.fixture
def users(tmp_path, monkeypatch):
    """A private user store holding the test session's admin."""
    monkeypatch.setattr(auth, "DATA_DIR", tmp_path)
    monkeypatch.setattr(auth, "USERS_FILE", tmp_path / "users.json")
    auth.init_user_store()
    yield
    for sid in [s for s in auth.sessions if s != SESSION]:
        auth.sessions.pop(sid, None)


def _session_for(username, device="phone"):
    sid = f"session-{username}-{device}"
    auth.sessions[sid] = {"username": username, "created": time.time()}
    return sid


# -- /api/about --------------------------------------------------------------


class TestAbout:
    def test_needs_a_session(self):
        with TestClient(server.app) as anon:
            anon.cookies.clear()
            assert anon.get("/api/about", follow_redirects=False).status_code == 401

    def test_reports_the_running_system(self, client):
        body = client.get("/api/about").json()
        assert set(body) == {"build", "started_at", "timezone", "hub", "addons", "displays"}
        assert body["started_at"] == server.APP_STARTED_AT.isoformat()
        assert body["timezone"]["offset"].startswith("UTC")
        assert isinstance(body["timezone"]["dst"], bool)
        assert set(body["addons"]) == {"sensors", "alarm", "weather", "forecast", "alerts"}

    def test_demo_reports_no_real_connection_details(self, client):
        hub = client.get("/api/about").json()["hub"]
        assert hub["demo_mode"] is True
        assert hub["connected_since"] is None
        assert hub["clock_offset"] is None

    def test_a_real_connection_says_since_when_and_what_clock(self, client, monkeypatch):
        when = server.local_now()
        monkeypatch.setattr(server, "DEMO_MODE", False)
        monkeypatch.setattr(server, "hub_connected", True)
        monkeypatch.setattr(server, "hub_connected_since", when)
        monkeypatch.setattr(server, "hub_clock_offset", when.utcoffset())
        hub = client.get("/api/about").json()["hub"]
        assert hub["connected_since"] == when.isoformat()
        assert hub["clock_offset"] == server._format_offset(when.utcoffset())

    def test_a_dropped_connection_reports_neither(self, client, monkeypatch):
        monkeypatch.setattr(server, "DEMO_MODE", False)
        monkeypatch.setattr(server, "hub_connected", False)
        monkeypatch.setattr(server, "hub_connected_since", server.local_now())
        hub = client.get("/api/about").json()["hub"]
        assert hub["connected"] is False
        assert hub["connected_since"] is None

    def test_add_ons_report_only_on_or_off_and_the_provider(self, client):
        addons = client.get("/api/about").json()["addons"]
        for name in ("sensors", "alarm", "weather"):
            assert set(addons[name]) == {"enabled", "provider"}
            if not addons[name]["enabled"]:
                assert addons[name]["provider"] is None
        assert set(addons["forecast"]) == {"enabled", "place_set"}
        assert set(addons["alerts"]) == {"enabled"}

    def test_nothing_secret_is_in_it(self, client):
        text = client.get("/api/about").text
        for secret in (server.NOBO_SERIAL, "password", "token", "secret"):
            assert secret not in text

    def test_display_last_seen_is_an_instant(self, client, monkeypatch):
        seen = time.time() - 120
        monkeypatch.setattr(server.display_registry, "list",
                            lambda: [{"last_seen_at": seen}, {"last_seen_at": None}])
        displays = client.get("/api/about").json()["displays"]
        assert displays["count"] == 2
        assert abs(server.datetime.fromisoformat(displays["last_seen_at"]).timestamp() - seen) < 1


class TestBuildInfo:
    def test_missing_file_is_none(self, tmp_path):
        assert server.load_build_info(tmp_path / "none.json") is None

    def test_a_good_stamp_is_read(self, tmp_path):
        path = tmp_path / "b.json"
        path.write_text(json.dumps({"commit": "7bf91a0", "date": "2026-10-21T16:55:00+02:00",
                                    "branch": "main"}))
        assert server.load_build_info(path) == {
            "commit": "7bf91a0", "date": "2026-10-21T16:55:00+02:00", "branch": "main"}

    @pytest.mark.parametrize("content", [
        "not json", "[]", json.dumps({"commit": "<script>"}), json.dumps({"date": "x"}),
    ])
    def test_a_bad_stamp_is_ignored(self, tmp_path, content):
        path = tmp_path / "b.json"
        path.write_text(content)
        assert server.load_build_info(path) is None

    def test_odd_fields_are_dropped_not_trusted(self, tmp_path):
        path = tmp_path / "b.json"
        path.write_text(json.dumps({"commit": "abcdef1", "date": "yesterday", "branch": "a b"}))
        assert server.load_build_info(path) == {"commit": "abcdef1", "date": None, "branch": None}

    def test_the_scripts_write_it_before_building_and_git_ignores_it(self):
        for script in ("update.sh", "install.sh"):
            text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
            assert "> app/build_info.json" in text, script
            assert text.index("app/build_info.json") < text.index("docker compose build"), script
        assert "app/build_info.json" in (ROOT / ".gitignore").read_text(encoding="utf-8")


# -- users -------------------------------------------------------------------


class TestUserGuards:
    def test_the_last_administrator_cannot_be_demoted(self, client, users):
        r = client.patch("/auth/admin/users/admin", json={"role": "user"})
        assert r.status_code == 400
        assert auth.load_users()["admin"]["role"] == "admin"

    def test_one_of_two_administrators_can_be(self, client, users):
        client.post("/auth/admin/users", json={"username": "kari", "password": "x" * 8,
                                               "role": "admin"})
        assert client.patch("/auth/admin/users/kari", json={"role": "user"}).status_code == 200
        assert auth.load_users()["kari"]["role"] == "user"

    def test_a_removed_user_is_signed_out(self, client, users):
        client.post("/auth/admin/users", json={"username": "kari", "password": "x" * 8})
        sid = _session_for("kari")
        assert client.delete("/auth/admin/users/kari").status_code == 200
        assert sid not in auth.sessions

    def test_a_renamed_user_stays_signed_in_as_the_new_name(self, client, users):
        client.post("/auth/admin/users", json={"username": "kari", "password": "x" * 8})
        sid = _session_for("kari")
        client.patch("/auth/admin/users/kari", json={"new_username": "kari2"})
        assert auth.sessions[sid]["username"] == "kari2"

    def test_renaming_yourself_moves_your_other_sessions_too(self, client, users):
        client.post("/auth/admin/users", json={"username": "kari", "password": "x" * 8})
        phone, laptop = _session_for("kari"), _session_for("kari", "laptop")
        client.cookies.set("session_id", laptop)
        assert client.post("/auth/rename", json={"new_username": "kari2"}).status_code == 200
        assert auth.sessions[phone]["username"] == "kari2"
        assert auth.sessions[laptop]["username"] == "kari2"

    def test_a_one_letter_name_is_refused(self, client, users):
        r = client.post("/auth/admin/users", json={"username": "k", "password": "x" * 8})
        assert r.status_code == 400

    def test_an_ordinary_user_cannot_manage_users(self, client, users):
        client.post("/auth/admin/users", json={"username": "kari", "password": "x" * 8})
        client.cookies.set("session_id", _session_for("kari"))
        assert client.get("/auth/admin/users").status_code == 403


# -- Classic is gone ---------------------------------------------------------


class TestOneInterface:
    def test_root_and_cabin_serve_cabin(self, client):
        for path in ("/", "/cabin"):
            r = client.get(path)
            assert r.status_code == 200
            assert "/static/ui/cabin/cabin.js" in r.text

    def test_there_is_no_classic_address_any_more(self, client):
        r = client.get("/classic", follow_redirects=False)
        assert r.status_code == 404

    def test_the_classic_files_are_gone(self, client):
        for name in ("index.html", "app.js", "auth.js", "style.css"):
            assert not (STATIC / name).exists(), name
            assert client.get(f"/static/{name}").status_code == 404

    def test_the_server_no_longer_switches(self):
        source = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
        for gone in ("UI_CHOICES", "ACTIVE_UI", "_LOGIN_CLASSIC_HTML", "_LOGIN_PAGES",
                     "NOBO_UI", "/classic"):
            assert gone not in source, gone

    def test_no_page_points_at_classic(self):
        for page in STATIC.rglob("*.html"):
            assert "/classic" not in page.read_text(encoding="utf-8"), page
        for page in STATIC.rglob("*.js"):
            assert "/classic" not in page.read_text(encoding="utf-8"), page

    def test_cabin_never_points_at_classic(self):
        assert "/classic" not in CABIN
        assert "NOBO_UI" not in CABIN
        assert "azIcon" not in CABIN


# -- the Cabin side ----------------------------------------------------------


def _function(name):
    start = CABIN.index(f"function {name}(")
    end = CABIN.index("\n  }\n", start)
    return CABIN[start:end]


class TestCabinWiring:
    def test_users_are_managed_in_a_cabin_sheet_by_admins_only(self):
        assert "${isAdmin ? settingRow('Users'" in CABIN
        assert "if (usersBtn) usersBtn.onclick = usersSheet;" in CABIN
        sheet = _function("usersSheet")
        for call in ("Nobo.api.users()", "Nobo.api.addUser(", "Nobo.api.updateUser(",
                     "Nobo.api.deleteUser("):
            assert call in sheet, call
        assert "${esc(u.username)}" in sheet
        assert "confirmSheet(" in sheet

    def test_anyone_can_change_their_own_username(self):
        assert "data-act=\"rename-me\"" in CABIN
        assert "Nobo.api.renameMe(name)" in _function("renameMeSheet")
        assert "value=\"${esc(current)}\"" in _function("renameMeSheet")

    def test_the_last_administrator_is_locked_in_the_sheet_too(self):
        sheet = _function("usersSheet")
        assert "u.role === 'admin' && admins === 1" in sheet

    def test_the_api_client_names_each_endpoint(self):
        for path in ("'/api/about'", "'/auth/admin/users'",
                     "`/auth/admin/users/${encodeURIComponent(name)}`"):
            assert path in CORE, path

    def test_about_is_filled_from_the_endpoint_and_escaped(self):
        assert "settingsSection('about', 'About', '<span id=\"aboutState\"></span>'" in CABIN
        assert "'Cabin', `" not in CABIN
        body = _function("aboutBody")
        for block in ("This system", "Nob\\u00F8 hub", "In the house", "Add-ons", "Help"):
            assert block in body, block
        assert "esc(build.commit)" in body
        assert "esc(sensorLine)" in body
        assert "Nobo.api.about()" in _function("wireAbout")

    def test_about_is_not_refetched_on_every_redraw(self):
        assert "Date.now() - state.aboutAt < 60000" in _function("wireAbout")

    def test_about_is_styled(self):
        for rule in (".ab {", ".ab dt", ".ab dd", ".ab-h {", ".ab-links", ".user-row {"):
            assert rule in CSS, rule
