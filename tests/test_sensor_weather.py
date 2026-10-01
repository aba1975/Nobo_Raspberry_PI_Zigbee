"""The weather station's pieces on their own: the choices file, the readings
as thermometers, the precedence between sources, and the settings file."""

import json
import stat
from datetime import datetime, timedelta, timezone

import pytest

import sensor_verisure
import sensor_weather
import weather_persistence
from sensor_provider import ContactSnapshot, ContactState, SensorKind
from weather_provider import SimulatedWeather, WeatherModule, WeatherReading

NOW = 1_800_000_000.0


def module(module_id="m1", kind="indoor", temperature=12.0, reachable=True, age=60):
    return WeatherModule(
        module_id=module_id, kind=kind, name=module_id, temperature=temperature, humidity=50.0,
        co2=None, pressure=1010.0 if kind == "base" else None, min_temperature=None,
        max_temperature=None, battery=70, reachable=reachable, reported_at=NOW - age,
    )


def reading(*modules):
    return WeatherReading(station_name="S", modules=tuple(modules), read_at=NOW)


CHOSEN = [sensor_weather.WeatherSensor("netatmo-m1", "m1", "Tech", "6")]


def snap(*args, **kwargs):
    return sensor_weather.snapshots(CHOSEN, *args, stale_seconds=1800, now=NOW, **kwargs)[0]


def test_a_fresh_module_is_a_climate_reading():
    item = snap(reading(module()), fresh=True)
    assert item.available is True and item.temperature == 12.0
    assert item.kind is SensorKind.CLIMATE and item.source == "netatmo"
    assert item.state is ContactState.UNKNOWN and item.zone_id == "6"


@pytest.mark.parametrize("case", ["stale_reading", "old_measure", "unreachable", "missing", "none"])
def test_anything_doubtful_reads_offline(case):
    value = {
        "stale_reading": lambda: snap(reading(module()), fresh=False),
        "old_measure": lambda: snap(reading(module(age=3600)), fresh=True),
        "unreachable": lambda: snap(reading(module(reachable=False, temperature=None)), fresh=True),
        "missing": lambda: snap(reading(module("other")), fresh=True),
        "none": lambda: snap(None, fresh=False),
    }[case]()
    assert value.available is False


def test_the_choices_round_trip(tmp_path):
    path = tmp_path / "weather_sensors.json"
    sensor_weather.save({item.sensor_id: item for item in CHOSEN}, path)
    assert sensor_weather.load(path) == {item.sensor_id: item for item in CHOSEN}
    assert "temperature" not in path.read_text()


def test_a_damaged_choices_file_is_set_aside(tmp_path):
    path = tmp_path / "weather_sensors.json"
    path.write_text('{"schema_version": 9}')
    assert sensor_weather.load(path) == {}
    assert path.with_suffix(".backup").exists()


def test_the_catalogue_offers_rooms_not_outside():
    items = sensor_weather.catalogue(
        reading(module("b", "base"), module("o", "outdoor"), module("m1")),
        {item.sensor_id: item for item in CHOSEN},
    )
    assert [item["module_id"] for item in items] == ["b", "m1"]
    assert items[1]["sensor_id"] == "netatmo-m1"


# -- precedence: Zigbee, then the station, then Verisure ---------------------


def climate(sensor_id, source, *, zone_id="6", available=True, age=0, temperature=20.0):
    seen = datetime.fromtimestamp(NOW, timezone.utc) - timedelta(seconds=age)
    return ContactSnapshot(
        sensor_id=sensor_id, provider_id=sensor_id, name=sensor_id, zone_id=zone_id,
        state=ContactState.UNKNOWN, available=available, battery=None, changed_at=seen,
        last_seen_at=seen, kind=SensorKind.CLIMATE, temperature=temperature, source=source,
    )


def test_a_fresh_zigbee_thermometer_sets_the_module_aside():
    items = [climate("zig", ""), climate("netatmo-m1", "netatmo")]
    assert sensor_weather.standing_by(items, now=NOW, climate_stale_seconds=3600) == {
        "netatmo-m1": sensor_weather.STANDING_BY_THERMOMETER,
    }


@pytest.mark.parametrize("zigbee", [
    climate("zig", "", available=False),
    climate("zig", "", age=7200),
    climate("zig", "", zone_id="1"),
])
def test_an_offline_stale_or_elsewhere_zigbee_does_not(zigbee):
    items = [zigbee, climate("netatmo-m1", "netatmo")]
    assert sensor_weather.standing_by(items, now=NOW, climate_stale_seconds=3600) == {}


def test_a_verisure_thermometer_never_sets_the_module_aside():
    items = [climate("verisure-t", "verisure"), climate("netatmo-m1", "netatmo")]
    assert sensor_weather.standing_by(items, now=NOW, climate_stale_seconds=3600) == {}


def test_verisure_says_which_source_is_reading_the_room():
    chosen = {"verisure-t": sensor_verisure.VerisureSensor(
        "verisure-t", "climate:T", "T", SensorKind.CLIMATE, "6", None)}

    def run(*items):
        return sensor_verisure.apply_precedence(
            items, chosen, now=NOW, climate_stale_seconds=3600).standing_by

    smoke = climate("verisure-t", "verisure")
    assert run(climate("netatmo-m1", "netatmo"), smoke) == {
        "verisure-t": sensor_verisure.STANDING_BY_WEATHER}
    assert run(climate("zig", ""), smoke) == {
        "verisure-t": sensor_verisure.STANDING_BY_THERMOMETER}
    # Both reading: Zigbee is named, whichever is listed first.
    assert run(climate("netatmo-m1", "netatmo"), climate("zig", ""), smoke) == {
        "verisure-t": sensor_verisure.STANDING_BY_THERMOMETER}
    assert run(climate("netatmo-m1", "netatmo", available=False), smoke) == {}


# -- the settings and the demo station ---------------------------------------


def test_settings_default_off_and_round_trip():
    assert weather_persistence.load_settings() == weather_persistence.WeatherSettings()
    assert weather_persistence.load_settings().enabled is False
    changed = weather_persistence.WeatherSettings(
        enabled=True, provider="netatmo", outdoor_cold_below=-22.5)
    weather_persistence.save_settings(changed)
    assert weather_persistence.load_settings() == changed


@pytest.mark.parametrize("value", [-41, 11, "cold", None, True])
def test_a_cold_limit_out_of_range_is_refused(value):
    with pytest.raises(weather_persistence.InvalidWeatherData):
        weather_persistence.parse_cold_below(value)


def test_the_cold_limit_is_rounded_to_a_half():
    assert weather_persistence.parse_cold_below(-12.3) == -12.5


def test_the_account_file_is_private():
    weather_persistence.save_account({"client_id": "abc", "client_secret": "def"})
    path = weather_persistence.NETATMO_ACCOUNT_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text())["client_secret"] == "def"


def test_the_demo_station_keeps_what_it_was_set_to():
    station = SimulatedWeather(weather_persistence.load_simulated,
                               weather_persistence.save_simulated, clock=lambda: NOW)
    station.set_module("02:00:00:00:00:04", temperature=-9.0, battery=30)
    again = SimulatedWeather(weather_persistence.load_simulated,
                             weather_persistence.save_simulated, clock=lambda: NOW)
    outdoor = next(item for item in again.modules() if item["kind"] == "outdoor")
    assert outdoor["temperature"] == -9.0 and outdoor["battery"] == 30
    assert outdoor["min_temperature"] <= -9.0


def test_the_demo_station_remembers_when_a_module_went_quiet():
    clock = {"now": NOW}
    station = SimulatedWeather(weather_persistence.load_simulated,
                               weather_persistence.save_simulated, clock=lambda: clock["now"])
    station.set_module("03:00:00:00:00:02", reachable=False)
    clock["now"] = NOW + 600
    import asyncio

    tech = asyncio.run(station.read()).module("03:00:00:00:00:02")
    assert tech.reachable is False and tech.temperature is None and tech.reported_at == NOW
