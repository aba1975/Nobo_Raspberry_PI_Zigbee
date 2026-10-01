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
import sensor_persistence
import alarm_persistence
import sensor_verisure
import sensor_weather
import weather_netatmo
import weather_persistence


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
def clean_zone_override_state(tmp_path, monkeypatch):
    """
    Start every test with no zone-level overrides and nothing held from Away.

    Both of these are module globals in ``server``, so without a reset one
    test's Away leaks into the next test's Comfort. The applied-exception set is
    also persisted, so its file is redirected into tmp_path to keep test runs
    out of the real data directory.
    """
    import server

    monkeypatch.setattr(
        config_persistence,
        "AWAY_EXCEPTIONS_APPLIED_FILE",
        tmp_path / "away_exceptions_applied.json",
        raising=False,
    )
    server.DEMO_ZONE_OVERRIDES.clear()
    server._away_exception_zones_applied.clear()
    server.demo_global_mode = "normal"
    yield
    server.DEMO_ZONE_OVERRIDES.clear()
    server._away_exception_zones_applied.clear()
    server.demo_global_mode = "normal"


@pytest.fixture(autouse=True)
def demo_hub_is_connected():
    """
    Keep every test starting from "demo hub connected".

    test_hub_config.py deliberately points the app at 192.0.2.10 (TEST-NET-1,
    guaranteed unroutable). That connection attempt runs on a background thread
    and sits in a TCP timeout long after the test that started it has finished.
    It used to clear ``hub_connected`` when it finally gave up, and whichever
    unrelated test happened to be running at that moment got a surprise 503 —
    a single, randomly-placed failure roughly one run in three.

    That is fixed at the source: a failed attempt now refuses to clear state
    belonging to a connection it did not create (see
    ``tests/test_connection_leak.py::TestALateFailureCannotDisconnectALiveHub``,
    and the handler in ``connect_to_hub_sync``). This fixture is kept because
    resetting one flag is cheap and several tests legitimately leave the module
    disconnected, but it is no longer load-bearing against that race — if the
    randomly-placed failures ever come back, the bug is in the handler, not
    here.
    """
    import server

    server.hub_connected = True
    yield


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
    # The file paths are resolved from DATA_DIR at import time, so patching
    # DATA_DIR alone leaves them pointing at the real data directory. Each one
    # a test can write has to be redirected by name.
    monkeypatch.setattr(config_persistence, "SITE_FILE", tmp_path / "site.json")
    monkeypatch.setattr(config_persistence, "ZONE_CATEGORIES_FILE", tmp_path / "zone_categories.json")
    monkeypatch.setattr(config_persistence, "ZONE_GROUP_ORDER_FILE", tmp_path / "zone_group_order.json")
    monkeypatch.setattr(
        config_persistence, "ZONES_WITHOUT_SCHEDULE_FILE", tmp_path / "zones_without_schedule.json"
    )
    monkeypatch.setattr(sensor_persistence, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sensor_persistence, "SENSOR_SETTINGS_FILE", tmp_path / "sensor_settings.json")
    monkeypatch.setattr(
        sensor_persistence,
        "SIMULATED_SENSORS_FILE",
        tmp_path / "simulated_contact_sensors.json",
    )
    monkeypatch.setattr(
        sensor_persistence,
        "SENSOR_AUTOMATION_STATE_FILE",
        tmp_path / "sensor_automation_state.json",
    )
    monkeypatch.setattr(
        sensor_persistence, "CLIMATE_HISTORY_FILE", tmp_path / "climate_history.json",
    )
    monkeypatch.setattr(
        sensor_persistence, "PRESSURE_HISTORY_FILE", tmp_path / "pressure_history.json",
    )
    monkeypatch.setattr(
        sensor_persistence, "SENSOR_HEATING_LINKS_FILE", tmp_path / "sensor_heating_links.json",
    )
    # The alarm integration, including the Verisure session folder: a test
    # must never find — or leave — a real sign-in.
    monkeypatch.setattr(alarm_persistence, "DATA_DIR", tmp_path)
    monkeypatch.setattr(alarm_persistence, "ALARM_SETTINGS_FILE", tmp_path / "alarm_settings.json")
    monkeypatch.setattr(alarm_persistence, "ALARM_STATE_FILE", tmp_path / "alarm_state.json")
    monkeypatch.setattr(alarm_persistence, "SIMULATED_ALARM_FILE", tmp_path / "simulated_alarm.json")
    monkeypatch.setattr(alarm_persistence, "VERISURE_DIR", tmp_path / "verisure")
    monkeypatch.setattr(
        alarm_persistence, "VERISURE_SESSION_FILE", tmp_path / "verisure" / "session.json",
    )
    monkeypatch.setattr(sensor_verisure, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        sensor_verisure, "VERISURE_SENSORS_FILE", tmp_path / "verisure_sensors.json",
    )
    # The weather station, including the Netatmo account folder.
    monkeypatch.setattr(weather_persistence, "DATA_DIR", tmp_path)
    for name, filename in (
        ("WEATHER_SETTINGS_FILE", "weather_settings.json"),
        ("WEATHER_STATE_FILE", "weather_state.json"),
        ("SIMULATED_WEATHER_FILE", "simulated_weather.json"),
        ("PRESSURE_HISTORY_FILE", "weather_pressure_history.json"),
        ("OUTDOOR_HISTORY_FILE", "weather_outdoor_history.json"),
    ):
        monkeypatch.setattr(weather_persistence, name, tmp_path / filename)
    monkeypatch.setattr(weather_persistence, "NETATMO_DIR", tmp_path / "netatmo")
    monkeypatch.setattr(
        weather_persistence, "NETATMO_ACCOUNT_FILE", tmp_path / "netatmo" / "account.json",
    )
    monkeypatch.setattr(sensor_weather, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sensor_weather, "WEATHER_SENSORS_FILE", tmp_path / "weather_sensors.json")
    # A module-level history would carry one test's readings into the next.
    server_module = sys.modules.get("server")
    if server_module is not None and hasattr(server_module, "climate_history"):
        from climate_history import ClimateHistory

        monkeypatch.setattr(server_module, "climate_history", ClimateHistory(
            save=sensor_persistence.save_climate_history,
        ))
    if server_module is not None and hasattr(server_module, "alarm_settings"):
        # Off, as on a fresh install, whatever the developer's own data holds.
        from alarm_persistence import AlarmLedger, AlarmSettings
        from alarm_verisure import VerisureAlarm

        for name, value in (
            ("alarm_settings", AlarmSettings()), ("alarm_ledger", AlarmLedger()),
            ("alarm_provider", None), ("verisure_account", VerisureAlarm()),
            ("alarm_reading", None), ("alarm_failure", None), ("alarm_failures", 0),
            ("alarm_last_good", None), ("_alarm_view", None),
        ):
            monkeypatch.setattr(server_module, name, value)
    if server_module is not None and hasattr(server_module, "weather_settings"):
        from climate_history import ClimateHistory
        from pressure_outlook import PressureHistory
        from weather_netatmo import NetatmoAccount
        from weather_persistence import WeatherLedger, WeatherSettings

        for name, value in (
            ("weather_settings", WeatherSettings()), ("weather_ledger", WeatherLedger()),
            ("weather_provider", None), ("netatmo_account", NetatmoAccount()),
            ("weather_reading", None), ("weather_failure", None), ("weather_failures", 0),
            ("weather_last_good", None), ("_weather_view", None), ("weather_sensors", {}),
            ("weather_next_read", None), ("netatmo_pending", {}),
            ("station_pressure_history", PressureHistory(
                save=weather_persistence.save_pressure_history)),
            ("outdoor_history", ClimateHistory(save=weather_persistence.save_outdoor_history)),
        ):
            monkeypatch.setattr(server_module, name, value)
    if server_module is not None and hasattr(server_module, "sensor_heating_links"):
        monkeypatch.setattr(server_module, "sensor_heating_links", {})
    if server_module is not None and hasattr(server_module, "verisure_sensors"):
        monkeypatch.setattr(server_module, "verisure_sensors", {})
        monkeypatch.setattr(server_module, "sensor_precedence", sensor_verisure.Precedence(
            counted=[], standing_by={}, stood_in_for={},
        ))
    if server_module is not None and hasattr(server_module, "pressure_history"):
        from pressure_outlook import PressureHistory

        monkeypatch.setattr(server_module, "pressure_history", PressureHistory(
            save=sensor_persistence.save_pressure_history,
        ))
    # The account store too. It was left out, and a test that changed a
    # password or a role therefore rewrote the real users.json and broke every
    # test after it — the whole suite failing on an admin check because one
    # test earlier had demoted the account the shared session belongs to.
    monkeypatch.setattr(auth, "DATA_DIR", tmp_path)
    monkeypatch.setattr(auth, "USERS_FILE", tmp_path / "users.json")
    auth.init_user_store()
    yield


@pytest.fixture(autouse=True)
def netatmo_never_reached(monkeypatch):
    """No test may talk to Netatmo. Tests hand the account a fake transport."""
    def refuse(url, form, headers):
        raise AssertionError(f"A test tried to reach Netatmo: {url}")

    monkeypatch.setattr(weather_netatmo, "http_post", refuse)
