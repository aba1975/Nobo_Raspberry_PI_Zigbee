"""
The app must not depend on the current working directory (QA defect D-03).

server.py mounted StaticFiles(directory="static") and opened "static/index.html"
by relative path, and the storage modules used Path("data"). Importing the app
from anywhere except app/ therefore blew up, which is why the documented test
command in the README and CLAUDE.md could never have worked, and why running the
suite from the repository root would have scattered a stray data/ directory.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("NOBO_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import auth
import away_schedule
import config_persistence
import server
from server import app

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = Path(server.__file__).resolve().parent


def fresh_data_dir(module_name):
    """DATA_DIR as the module defines it, ignoring conftest's monkeypatch."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        f"_fresh_{module_name}", APP_DIR / f"{module_name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DATA_DIR


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.cookies.set("session_id", "pytest-fixed-session-id")
        yield c


class TestPathsAreAbsolute:
    def test_static_dir_is_absolute_and_real(self):
        assert server.STATIC_DIR.is_absolute()
        assert (server.STATIC_DIR / "index.html").is_file()
        assert (server.STATIC_DIR / "app.js").is_file()

    @pytest.mark.parametrize(
        "module_name", ["auth", "away_schedule", "config_persistence"]
    )
    def test_storage_dirs_are_absolute(self, module_name):
        """A relative data dir silently moves with the working directory.

        conftest monkeypatches the live DATA_DIR to a temp directory, so this
        loads a pristine copy of the module to see the value the app really
        starts with.
        """
        data_dir = fresh_data_dir(module_name)
        assert data_dir.is_absolute(), (
            f"{module_name}.DATA_DIR is relative, so data would be written next "
            f"to whatever directory the app happened to be started from"
        )

    @pytest.mark.parametrize(
        "module_name", ["auth", "away_schedule", "config_persistence"]
    )
    def test_storage_dirs_sit_next_to_the_code(self, module_name):
        """In the container this resolves to /app/data, where the volume mounts."""
        data_dir = fresh_data_dir(module_name)
        assert data_dir.name == "data"
        assert data_dir.parent == APP_DIR


class TestServingWorksFromAnyDirectory:
    def test_index_is_served(self, client, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        r = client.get("/")
        assert r.status_code == 200
        assert "<html" in r.text.lower()

    def test_static_assets_are_served(self, client, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        for asset in ("app.js", "auth.js", "style.css"):
            assert client.get(f"/static/{asset}").status_code == 200

    def test_no_stray_data_dir_is_created(self, client, tmp_path, monkeypatch):
        """Running from elsewhere must not litter that directory with data/."""
        monkeypatch.chdir(tmp_path)
        client.get("/api/zones")
        assert not (tmp_path / "data").exists()


class TestPytestConfiguration:
    """The documented command is `python -m pytest` from the repository root."""

    def test_app_is_on_the_import_path_via_config(self, pytestconfig):
        """Without this ini setting the plain documented command cannot work."""
        configured = [Path(p).name for p in pytestconfig.getini("pythonpath")]
        assert "app" in configured, (
            "pytest.ini must set pythonpath = app, otherwise `python -m pytest` "
            "from the repository root fails on `import server`"
        )

    def test_app_modules_are_importable_by_plain_name(self):
        """This is what pythonpath = app buys us."""
        import importlib

        for name in ("server", "auth", "away_schedule", "config_persistence"):
            assert importlib.import_module(name)
