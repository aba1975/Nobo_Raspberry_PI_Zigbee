"""Air pressure: the three-hour tendency, its storage, and the room payload.

The tendency bands and the "reading holds until the next one" rule are the
substance here; the API tests prove only the wiring. Nothing in this file
has watched a real barometer through a real change of weather.
"""

import json
import time

import pytest

import server
from pressure_outlook import (
    KEEP_SECONDS, SAMPLE_SECONDS, TENDENCY_SECONDS, PressureHistory, Tendency, classify,
)
from sensor_persistence import load_pressure_history, save_pressure_history
from tests.test_climate_api import simulate
from tests.test_sensor_api import (  # noqa: F401 - fixtures are used by name
    add_sensor, client, enable, isolated_sensor_service, zone,
)

HOUR = 3600
FRESH = 3 * HOUR
NOW = 1_800_000_000


# ---------------------------------------------------------------------------
# The bands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("change, expected", [
    (0.0, Tendency.STEADY),
    (1.5, Tendency.STEADY),
    (-1.5, Tendency.STEADY),
    (1.6, Tendency.RISING),
    (3.5, Tendency.RISING),
    (3.6, Tendency.RISING_FAST),
    (9.0, Tendency.RISING_FAST),
    (-1.6, Tendency.FALLING),
    (-3.5, Tendency.FALLING),
    (-3.6, Tendency.FALLING_FAST),
    (-6.0, Tendency.FALLING_FAST),
    (-6.1, Tendency.STORM),
    (-12.0, Tendency.STORM),
    # Floating-point dust must not move a reading across a boundary.
    (-1.5999999, Tendency.FALLING),
    (1.54, Tendency.STEADY),
])
def test_the_bands_are_the_ones_weather_services_use(change, expected):
    assert classify(change) is expected


def test_there_are_exactly_six_outlooks():
    assert [item.value for item in Tendency] == [
        "storm", "falling_fast", "falling", "steady", "rising", "rising_fast",
    ]


# ---------------------------------------------------------------------------
# The history
# ---------------------------------------------------------------------------

def series(history, zone_id, points):
    """Feed ``[(seconds_before_NOW, hPa), ...]`` oldest first."""
    for before, value in points:
        at = NOW - before
        history.record({zone_id: (at, value)}, at)


def test_three_hours_are_needed_before_there_is_a_tendency():
    history = PressureHistory()
    series(history, "1", [(2 * HOUR, 1010.0), (0, 1005.0)])
    assert history.change("1", NOW, FRESH) is None
    outlook = history.outlook(NOW, FRESH)
    assert outlook["tendency"] is None
    # First sample two hours ago, so ready in one more hour.
    assert outlook["ready_at"] is not None
    ready = server.datetime.fromisoformat(outlook["ready_at"]).timestamp()
    assert ready == NOW - 2 * HOUR + TENDENCY_SECONDS


def test_the_tendency_is_now_against_three_hours_ago():
    history = PressureHistory()
    series(history, "1", [(3 * HOUR, 1012.0), (2 * HOUR, 1011.0), (0, 1007.6)])
    assert history.change("1", NOW, FRESH) == pytest.approx(-4.4)
    outlook = history.outlook(NOW, FRESH)
    assert outlook == {
        "tendency": "falling_fast", "change_3h": -4.4, "rooms": 1, "ready_at": None,
    }


def test_a_reading_holds_until_the_next_one():
    """An Aqara in a still room reports about hourly. The value from before a
    quiet spell is still the value, so the pressure three hours ago is the
    last report at or before then, not a gap."""
    history = PressureHistory()
    series(history, "1", [(4 * HOUR, 1015.0), (HOUR, 1013.0)])
    # Three hours ago the last report was the 1015 from an hour before that.
    assert history.change("1", NOW, FRESH) == pytest.approx(-2.0)


def test_a_silence_longer_than_a_reading_stays_fresh_has_no_tendency():
    history = PressureHistory()
    series(history, "1", [(7 * HOUR, 1015.0), (0, 1005.0)])
    # The only earlier reading is four hours before the three-hour mark.
    assert history.change("1", NOW, FRESH) is None
    outlook = history.outlook(NOW, FRESH)
    assert outlook["tendency"] is None
    # Its three hours are up but have a hole in them: no time is promised.
    assert outlook["ready_at"] is None


def test_a_room_whose_barometer_has_gone_quiet_is_left_out():
    history = PressureHistory()
    series(history, "1", [(8 * HOUR, 1012.0), (4 * HOUR, 1011.0)])
    assert history.change("1", NOW, FRESH) is None
    assert history.outlook(NOW, FRESH) is None


def test_the_house_averages_each_rooms_own_change():
    """Two Aqaras can sit a hectopascal or more apart in absolute terms. Each
    room's change is taken from its own series, so the offset cancels."""
    history = PressureHistory()
    series(history, "1", [(3 * HOUR, 1012.0), (0, 1010.0)])
    series(history, "2", [(3 * HOUR, 1013.5), (0, 1011.1)])
    outlook = history.outlook(NOW, FRESH)
    assert outlook["change_3h"] == pytest.approx(-2.2)
    assert outlook["tendency"] == "falling"
    assert outlook["rooms"] == 2


def test_a_new_barometer_shares_the_house_outlook_at_once():
    history = PressureHistory()
    series(history, "1", [(3 * HOUR, 1012.0), (0, 1016.0)])
    series(history, "2", [(10 * 60, 1015.0)])
    outlook = history.outlook(NOW, FRESH)
    assert outlook["tendency"] == "rising_fast"
    assert outlook["rooms"] == 1


def test_one_sample_per_slot_and_saves_only_when_one_is_added():
    saved = []
    history = PressureHistory(save=saved.append)
    base = (NOW // SAMPLE_SECONDS) * SAMPLE_SECONDS
    history.record({"1": (base + 10, 1010.0)}, base + 10)
    history.record({"1": (base + 70, 1009.8)}, base + 70)
    history.record({"1": (base + 70, 1009.8)}, base + 80)
    assert len(saved) == 1
    # The later reading in the slot replaced the first, in memory.
    assert history.hourly("1", base + 80)[-1] == [base + 70, 1009.8]
    history.record({"1": (base + SAMPLE_SECONDS + 5, 1009.5)}, base + SAMPLE_SECONDS + 5)
    assert len(saved) == 2
    assert saved[-1]["1"] == [[base + 70, 1009.8], [base + SAMPLE_SECONDS + 5, 1009.5]]


def test_a_day_is_kept_and_no_more():
    history = PressureHistory()
    history.record({"1": (NOW - KEEP_SECONDS - 60, 1000.0)}, NOW)
    assert history.hourly("1", NOW) == []
    series(history, "1", [(KEEP_SECONDS - 60, 1001.0), (0, 1002.0)])
    later = NOW + 120
    history.record({}, later)
    assert [value for _, value in history.hourly("1", later)] == [1002.0]


def test_a_reading_from_the_future_or_an_older_one_is_not_recorded():
    history = PressureHistory()
    history.record({"1": (NOW + 60, 1000.0)}, NOW)
    assert history.hourly("1", NOW) == []
    history.record({"1": (NOW, 1000.0)}, NOW)
    history.record({"1": (NOW - HOUR, 990.0)}, NOW)
    assert history.hourly("1", NOW) == [[NOW, 1000.0]]


def test_hourly_keeps_the_last_reading_of_each_hour():
    history = PressureHistory()
    start = (NOW // HOUR) * HOUR - 2 * HOUR
    for minutes, value in ((5, 1010.0), (45, 1009.0), (65, 1008.0)):
        at = start + minutes * 60
        history.record({"1": (at, value)}, at)
    assert history.hourly("1", start + 70 * 60) == [
        [start + 45 * 60, 1009.0], [start + 65 * 60, 1008.0],
    ]


def test_a_failed_save_does_not_stop_recording(caplog):
    def broken(_payload):
        raise OSError("disk full")

    history = PressureHistory(save=broken)
    history.record({"1": (NOW, 1000.0)}, NOW)
    assert history.hourly("1", NOW) == [[NOW, 1000.0]]
    assert "Could not save pressure history" in caplog.text


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------

def test_the_file_round_trips(tmp_path):
    path = tmp_path / "pressure.json"
    zones = {"1": [[NOW - HOUR, 1012.0], [NOW, 1010.5]]}
    save_pressure_history(zones, path)
    assert load_pressure_history(path) == zones
    history = PressureHistory(zones=load_pressure_history(path))
    assert history.hourly("1", NOW) == zones["1"]


@pytest.mark.parametrize("rows", [
    [[NOW, 200.0]],                       # below anything a barometer reports
    [[NOW, None]],
    [[NOW, 1000.0], [NOW, 1001.0]],       # not strictly later
    [[NOW, 1000.0], [NOW - 1, 1001.0]],
    [[float(NOW), 1000.0]],
    [[-1, 1000.0]],
    [[NOW, 1000.0, 1]],
    [{"at": NOW, "pressure": 1000.0}],
    [[NOW, True]],
])
def test_a_bad_row_sets_the_file_aside(tmp_path, rows):
    path = tmp_path / "pressure.json"
    path.write_text(json.dumps({"schema_version": 1, "zones": {"1": rows}}))
    assert load_pressure_history(path) == {}
    assert (tmp_path / "pressure.backup").exists()


def test_an_unknown_version_is_set_aside(tmp_path):
    path = tmp_path / "pressure.json"
    path.write_text(json.dumps({"schema_version": 2, "zones": {}}))
    assert load_pressure_history(path) == {}


def test_a_missing_file_is_simply_empty(tmp_path):
    assert load_pressure_history(tmp_path / "nothing.json") == {}


# ---------------------------------------------------------------------------
# Over HTTP, in demo mode
# ---------------------------------------------------------------------------

def test_a_room_with_a_barometer_is_learning_at_first(client):
    enable(client)
    add_sensor(client, kind="climate")
    climate = zone(client)["climate"]
    assert climate["measures_pressure"] is True
    assert climate["pressure_outlook"]["tendency"] is None
    assert climate["pressure_outlook"]["ready_at"] is not None
    assert len(climate["pressure_24h"]) == 1
    # The temperature history is still kept, only not the one shown.
    assert climate["last_24h"] is not None


def test_a_thermometer_without_a_barometer_keeps_the_plain_history(client):
    enable(client)
    sensor = add_sensor(client, name="Tuya", kind="climate")
    response = simulate(client, sensor["sensor_id"], clear_pressure=True)
    assert response.status_code == 200, response.text
    assert response.json()["pressure"] is None
    climate = zone(client)["climate"]
    assert climate["pressure"] is None
    # At once, although this room recorded a pressure a moment ago.
    assert climate["measures_pressure"] is False
    assert climate["pressure_outlook"] is None
    assert climate["pressure_24h"] is None
    assert climate["last_24h"]["temperature_min"] == 21.0


def test_clearing_and_setting_pressure_together_is_refused(client):
    enable(client)
    sensor = add_sensor(client, kind="climate")
    assert simulate(
        client, sensor["sensor_id"], pressure=1000, clear_pressure=True,
    ).status_code == 400


def test_a_contact_has_no_pressure_to_clear(client):
    enable(client)
    window = add_sensor(client, name="Window")
    assert simulate(client, window["sensor_id"], clear_pressure=True).status_code == 400


def test_every_barometer_room_shows_the_same_house_outlook(client, monkeypatch):
    enable(client, zone_id="1")
    enable(client, zone_id="2")
    bath = add_sensor(client, name="Bath", kind="climate", zone_id="1")
    hall = add_sensor(client, name="Hall", kind="climate", zone_id="2")
    tuya = add_sensor(client, name="Tuya", kind="climate", zone_id="3")
    assert simulate(client, tuya["sensor_id"], clear_pressure=True).status_code == 200
    now = time.time()
    history = PressureHistory(zones={
        "1": [[int(now - 3 * HOUR), 1012.0]],
        "2": [[int(now - 3 * HOUR), 1013.0]],
    })
    monkeypatch.setattr(server, "pressure_history", history)
    # Two Aqaras a hectopascal apart, both down about 7.5 in three hours.
    assert simulate(client, bath["sensor_id"], pressure=1004).status_code == 200
    assert simulate(client, hall["sensor_id"], pressure=1006).status_code == 200
    rooms = {item["zone_id"]: item for item in client.get("/api/zones").json()["zones"]}
    expected = {"tendency": "storm", "change_3h": -7.5, "rooms": 2, "ready_at": None}
    assert rooms["1"]["climate"]["pressure_outlook"] == expected
    assert rooms["2"]["climate"]["pressure_outlook"] == expected
    assert rooms["3"]["climate"]["pressure_outlook"] is None
    assert rooms["1"]["climate"]["pressure_24h"][-1][1] == 1004.0

    body = client.get("/api/display").json()
    assert body["weather_outlook"] == {"tendency": "storm", "change_3h": -7.5}


def test_the_display_has_no_outlook_until_it_is_known(client):
    enable(client)
    add_sensor(client, kind="climate")
    assert client.get("/api/display").json()["weather_outlook"] is None


def test_the_display_has_no_outlook_while_sensors_are_off(client, monkeypatch):
    now = time.time()
    monkeypatch.setattr(server, "pressure_history", PressureHistory(zones={
        "1": [[int(now - 3 * HOUR), 1012.0], [int(now - 60), 1004.0]],
    }))
    assert client.get("/api/display").json()["weather_outlook"] is None


def test_pressure_is_recorded_to_its_own_file(client):
    enable(client)
    add_sensor(client, kind="climate")
    saved = load_pressure_history(server.sensor_persistence.PRESSURE_HISTORY_FILE)
    assert [value for _, value in saved["1"]] == [1013.0]
