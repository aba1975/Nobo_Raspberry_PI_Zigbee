"""What a weather station can tell this application, and a demo one to try it on.

A weather station is read, never driven, and it never changes the heating. It
does three things here:

* the **outdoor module** gives the temperature outside, shown on the front
  page with today's lowest and highest;
* the **base station's barometer** gives the house its weather outlook, in
  preference to the room barometers, because it is one instrument in one
  place and already corrected to sea level;
* the **indoor modules** — and the base station itself — can be chosen as room
  thermometers, beside the Zigbee and Verisure ones. See ``sensor_weather``.

Two providers implement it:

* ``SimulatedWeather`` — demo mode only. Its readings are set by hand from
  Settings and kept in ``data/simulated_weather.json``.
* ``NetatmoAccount`` in ``weather_netatmo`` — the user's own Netatmo station,
  read through Netatmo's official cloud API. See docs/WEATHER.md.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Tuple

# The base station is indoors and has the barometer; "indoor" is an extra
# indoor module; "outdoor" is the one outside. Rain gauges and anemometers are
# read but only shown, never used as anything.
MODULE_KINDS = ("base", "indoor", "outdoor", "rain", "wind")
ROOM_KINDS = ("base", "indoor")

# What a module's battery reads as low. Netatmo's own app turns the icon red at
# about the same level; the modules then run for weeks, so it is a reminder,
# not an emergency.
BATTERY_LOW_PERCENT = 20


@dataclass(frozen=True)
class WeatherModule:
    """One module as the station last described it.

    ``reported_at`` is when the *module* last measured, which Netatmo does
    about every five minutes. ``reachable`` is the station's own word on
    whether it can still hear the module.
    """

    module_id: str
    kind: str
    name: str
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    co2: Optional[int] = None
    pressure: Optional[float] = None
    min_temperature: Optional[float] = None
    max_temperature: Optional[float] = None
    temperature_trend: Optional[str] = None
    battery: Optional[int] = None
    reachable: bool = True
    reported_at: Optional[float] = None


@dataclass(frozen=True)
class WeatherReading:
    """One consistent look at the station."""

    station_name: str
    modules: Tuple[WeatherModule, ...] = ()
    read_at: float = field(default_factory=time.time)

    def module(self, module_id: str) -> Optional[WeatherModule]:
        return next((item for item in self.modules if item.module_id == module_id), None)

    def first(self, kind: str) -> Optional[WeatherModule]:
        return next((item for item in self.modules if item.kind == kind), None)


class WeatherUnavailable(Exception):
    """The station could not be read, for a reason the interface can show.

    ``kind`` is one of ``not_configured``, ``signed_out``, ``unreachable`` or
    ``rate_limited``. The message is always one of ours, never text from
    Netatmo's servers.
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class WeatherProvider(Protocol):
    name: str

    async def read(self) -> WeatherReading:
        ...


def default_simulated_state() -> Dict[str, Any]:
    """A demo station shaped like the usual Netatmo house: the base station in
    the living room, an indoor module in a cold technical room and one in the
    kitchen, and the outdoor module on a north wall."""

    def module(module_id, kind, name, temperature, humidity, **extra):
        return {
            "module_id": module_id, "kind": kind, "name": name,
            "temperature": temperature, "humidity": humidity,
            "co2": extra.get("co2"), "pressure": extra.get("pressure"),
            "min_temperature": extra.get("low", temperature),
            "max_temperature": extra.get("high", temperature),
            "battery": extra.get("battery"), "reachable": True,
        }

    return {
        "station_name": "Demo weather station",
        "modules": [
            module("70:ee:50:00:00:01", "base", "Living Room", 21.4, 41.0,
                   co2=620, pressure=1013.2),
            module("03:00:00:00:00:02", "indoor", "Tech Room", 14.8, 52.0,
                   co2=450, battery=78),
            module("03:00:00:00:00:03", "indoor", "Kitchen", 21.9, 45.0,
                   co2=700, battery=64),
            module("02:00:00:00:00:04", "outdoor", "Outdoor", 3.2, 86.0,
                   battery=71, low=-1.5, high=5.0),
        ],
    }


class SimulatedWeather:
    """A demo station, set by hand from Settings."""

    name = "simulated"

    def __init__(
        self,
        load: Callable[[], Dict[str, Any]],
        save: Callable[[Mapping[str, Any]], None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._save = save
        self._state = load()
        self._clock = clock

    async def read(self) -> WeatherReading:
        now = self._clock()
        modules = []
        for item in self._state["modules"]:
            reachable = bool(item.get("reachable", True))
            modules.append(WeatherModule(
                module_id=item["module_id"], kind=item["kind"], name=item["name"],
                temperature=item.get("temperature") if reachable else None,
                humidity=item.get("humidity") if reachable else None,
                co2=item.get("co2") if reachable else None,
                pressure=item.get("pressure") if reachable else None,
                min_temperature=item.get("min_temperature"),
                max_temperature=item.get("max_temperature"),
                battery=item.get("battery"),
                reachable=reachable,
                # A real module measures every five minutes; an unreachable one
                # is left with the last time it was heard.
                reported_at=now if reachable else item.get("lost_at"),
            ))
        return WeatherReading(
            station_name=self._state["station_name"], modules=tuple(modules), read_at=now,
        )

    def modules(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self._state["modules"]]

    def set_module(
        self,
        module_id: str,
        *,
        temperature: Optional[float] = None,
        humidity: Optional[float] = None,
        pressure: Optional[float] = None,
        battery: Optional[int] = None,
        reachable: Optional[bool] = None,
    ) -> None:
        changed: List[Dict[str, Any]] = []
        found = False
        for item in self._state["modules"]:
            if item["module_id"] == module_id:
                found = True
                item = dict(item)
                if temperature is not None:
                    if not -50 <= temperature <= 60:
                        raise ValueError("Temperature must be between -50 and 60 °C")
                    value = round(float(temperature), 1)
                    item["temperature"] = value
                    low, high = item.get("min_temperature"), item.get("max_temperature")
                    item["min_temperature"] = value if low is None else min(low, value)
                    item["max_temperature"] = value if high is None else max(high, value)
                if humidity is not None:
                    if not 0 <= humidity <= 100:
                        raise ValueError("Humidity must be between 0 and 100 %")
                    item["humidity"] = round(float(humidity))
                if pressure is not None:
                    if item["kind"] != "base":
                        raise ValueError("Only the base station has a barometer")
                    if not 900 <= pressure <= 1100:
                        raise ValueError("Pressure must be between 900 and 1100 hPa")
                    item["pressure"] = round(float(pressure), 1)
                if battery is not None:
                    if item["kind"] == "base":
                        raise ValueError("The base station runs on mains power")
                    if not 0 <= battery <= 100:
                        raise ValueError("Battery must be between 0 and 100 %")
                    item["battery"] = int(battery)
                if reachable is not None and reachable != item.get("reachable", True):
                    item["reachable"] = reachable
                    item["lost_at"] = None if reachable else self._clock()
            changed.append(item)
        if not found:
            raise KeyError(module_id)
        self._state = {**self._state, "modules": changed}
        self._save(self._state)


def iso(at: Optional[float]) -> Optional[str]:
    if at is None:
        return None
    return datetime.fromtimestamp(at, timezone.utc).isoformat()
