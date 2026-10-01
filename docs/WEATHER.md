# Weather station (optional)

The temperature outside on the front page, a weather outlook from the station's
own barometer, and indoor modules as room thermometers — from a **Netatmo
weather station**, or from an invented one in demo mode.

It is **off** unless an administrator turns it on under **Settings → Weather
Station**, and while it is off nothing about it appears anywhere else. It **only
ever reads**: nothing here changes the heating, and the heating does not depend
on it.

**Status: not yet run against a real Netatmo account.** Everything below is
built to Netatmo's published API and tested against a fake of it
(`tests/test_weather_netatmo.py`). Token lifetimes, how often Netatmo replaces
the refresh token, and its rate limits are taken from Netatmo's documentation
and have not been observed here. Treat the first real connection as a test.

## What it shows

| Where | What |
| --- | --- |
| **Front page, top card** | The temperature outside, in the card's top-right corner, with the outlook under it ("Weather turning", "Improving"…). Tap it for the details. A reading the station has not refreshed for 30 minutes is not shown as the temperature now; the corner says the station is not being read instead. |
| **The weather sheet** | Headed with the home's name in Netatmo (not the base station's, which Netatmo appends in brackets). Outside, under the outdoor module's own name: temperature, humidity, today's lowest and highest, the last 24 hours, and the outdoor module's battery (amber when low, and shown even when the reading is stale, since a flat battery is the usual reason). The outlook with its 24-hour pressure curve. Each indoor module with its temperature, humidity, CO₂, battery and which room uses it. |
| **Rooms** | An indoor module (or the base station) added to a room is that room's thermometer: its **Actual** temperature, humidity, limits, near-freezing warning and 24 hours of history, exactly as a Zigbee thermometer. |

### The outlook

The station's base measures air pressure, so with a station the house-wide
outlook is read from **the station's barometer** rather than from Zigbee
thermometers in the rooms. It is the same rough guide as before — three hours
of pressure change, not a forecast — and it needs three hours of readings after
the station is turned on before it says anything. With no station the outlook
is worked out from room sensors as it always was.

## Which thermometer is believed

A room can have more than one source of temperature. One is believed at a time:

1. **Zigbee** — the room's own thermometer, if it has reported recently.
2. **The weather station** — a module stands by in any room with a fresh Zigbee
   thermometer, and takes over when that thermometer goes quiet.
3. **Verisure** — a smoke detector's temperature stands by in any room with a
   fresh Zigbee thermometer *or* a fresh weather-station module.

A module that is standing by is shown in the room, marked as such, and is not
used for the room's reading, limits or warnings until the source above it goes
quiet. So a Kitchen with both a Zigbee sensor and a Netatmo module shows the
Zigbee reading, and the module is a backup.

The outdoor module is never offered as a room thermometer: outside is not a
room, and it has its own place on the front page.

## Warnings

Three alerts, under **Alerts**, each off until chosen there like any other:

| Event | What it means |
| --- | --- |
| **Very cold outside** | The outdoor module is below the limit set under Settings → Weather Station (−15 °C by default). Cleared one degree above the limit, so a reading hovering at the limit does not send an email every five minutes. |
| **Weather station battery low** | A module reports 20 % or less. Cleared above 30 %. |
| **The weather station cannot be read** | Netatmo has ended the connection (at once), or the station has not been read for an hour. The heating is not affected either way. |

A module used as a room thermometer raises the room's own *too cold*, *too
warm*, *damp* and *near freezing* alerts like any thermometer. Its battery and
radio are the station's: *battery low* and *not heard from* for a module come
from the weather station's alerts above, not from the sensor ones.

## Connecting a Netatmo station

Netatmo has no local API: the station uploads to Netatmo, and is read from
there with Netatmo's official API. You need **HTTPS** first (see
[INSTALL.md](INSTALL.md#optional-a-proper-address-instead-of-an-ip)); the app
secret and tokens are refused over plain http.

1. Sign in at **[dev.netatmo.com/apps](https://dev.netatmo.com/apps)** with your
   usual Netatmo account and **create an app**. Name and description can be
   anything.
2. Set its **redirect URI** to your address followed by
   `/api/weather/netatmo/callback` — for example
   `https://nobo.example.no/api/weather/netatmo/callback`. Settings shows the
   exact one to use.
3. Under **Settings → Weather Station**, choose **On**, then **Netatmo**, and
   paste the app's **client ID** and **client secret**. Save.
4. Press **Connect to Netatmo**. Netatmo's own page asks you to allow the app to
   read your weather station; agree, and you are sent back to the front page.

**Without the redirect** — if the page is not opened at the address registered
for the app — open the app on dev.netatmo.com, choose the scope
`read_station` in its token generator, and paste the **refresh token** under
*Connect with a token instead*.

To use a module in a room: open the room, **Add** a sensor, choose **From the
weather station**, pick the module and the room. Door, window and temperature
sensors must be switched on under Settings for this.

### How your Netatmo account is protected

- **No Netatmo password ever reaches this system.** You sign in on Netatmo's
  page; this system receives only a one-time code, tied to your session here
  and valid for ten minutes, which it exchanges for tokens.
- **The tokens can only read the weather station.** The only scope asked for is
  `read_station`. It cannot change a setting, and it does not reach cameras,
  thermostats or anything else in the account.
- **Two endpoints, and nothing else.** The client refuses, before any request
  is made, anything but `oauth2/token` and `api/getstationsdata` on
  `api.netatmo.com` over HTTPS. The tests check that anything else — another
  Netatmo endpoint, plain http, a look-alike host — is refused before a
  connection is made.
- **Kept privately, and not backed up.** The client ID and secret and the
  tokens are in `data/netatmo/account.json`, readable only by the application
  (0600, in a 0700 folder). `backup.sh` leaves the folder out. The secret is
  never sent back to the browser; Settings shows a masked client ID only.
- **Turned off, it is gone.** Disconnecting deletes the tokens; turning the
  weather station off deletes the app's secret too. To withdraw access at
  Netatmo's end as well, remove the app under your Netatmo account's settings.
- **Netatmo's error messages are never shown or logged**, only this system's
  own words, and tokens are never logged.

## How often it reads

Every five minutes — Netatmo's modules report about that often, so reading
faster would only repeat the same values. After errors it backs off (1, 2, 5
and 10 minutes), after Netatmo's rate limit much further (15, 30, 60 minutes),
and once Netatmo has ended the connection it only checks hourly until you
connect again.

## Demo mode

In demo mode the source can be **Demo**: an invented station with a base in the
Living Room, indoor modules in the Tech Room and the Kitchen, and an outdoor
module. Its readings are set by hand under Settings → Weather Station, and a
module can be made unreachable to see what that looks like. Nothing leaves the
Pi.

## Files

| File | What |
| --- | --- |
| `data/weather_settings.json` | On/off, which source, the cold limit. Backed up. |
| `data/weather_state.json` | Which alerts are currently raised, so a restart does not send them again. Backed up. |
| `data/weather_sensors.json` | Which modules are used in which rooms, and their names. Backed up. |
| `data/weather_pressure_history.json`, `data/weather_outdoor_history.json` | 24 hours of station pressure and outdoor temperature. Backed up. |
| `data/simulated_weather.json` | The demo station's readings. |
| `data/netatmo/account.json` | The Netatmo app and tokens. **Private, not backed up.** After a restore, connect again. |

## For developers

| Module | Role |
| --- | --- |
| `app/weather_provider.py` | The provider contract (`WeatherReading`, `WeatherModule`) and the simulated station. |
| `app/weather_netatmo.py` | The Netatmo client: OAuth, the endpoint allow-list, token refresh, error mapping. |
| `app/weather_persistence.py` | Settings, state, histories and the private account file. |
| `app/sensor_weather.py` | Modules as room thermometers, and their place in the precedence. |
| `app/server.py` | `weather_loop` / `weather_poll_once`, the alerts, and `/api/weather/*`. |

`tests/conftest.py` makes every test unable to reach Netatmo: an autouse fixture
replaces the HTTP call with one that raises. Tests that exercise the client use
`FakeNetatmo` in `tests/test_weather_netatmo.py`.
