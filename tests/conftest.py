"""
Shared pytest fixtures for nobo-web-control test suite.

All tests run in demo mode (NOBO_DEMO=true) so no real Nobø Hub is needed.
"""

import os
import pytest

# Force demo mode before importing the application module
os.environ.setdefault("NOBO_DEMO", "true")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import config_persistence


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
