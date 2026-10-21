# Weather forecast (optional)

A week's weather for one place, from the Norwegian Meteorological Institute
(MET Norway) through its free API at `api.met.no`. It appears as one compact row
on the front page, and tapping a day opens a forecast sheet laid out for a
phone.

It is **off** unless an administrator turns it on under **Settings → Weather
Forecast**. While it is off, nothing about it appears anywhere and MET is never
asked. It is **for reading only**: nothing in it changes the heating, and the
heating does not depend on it. If MET cannot be reached, the front page simply
shows the last copy, marked as old, or nothing at all.

It needs no account. MET's API is open, and the forecast is not tied to any
login on a consumer weather site.

## What it shows

- **On the front page:** a row of days, each with the day's weather symbol, its
  highest and lowest temperature and the precipitation, under the top card,
  under the rooms, or not at all (**Settings → Weather Forecast → On the front
  page**). With the row turned off, the forecast is still reachable from the
  weather station's sheet and from Settings.
- **The forecast sheet:** day tabs across the top (or swipe), then for the
  chosen day the main symbol and temperature, its night, morning, afternoon and
  evening, a temperature and precipitation chart, and hour by hour below.
  About two and a half days ahead MET forecasts by the hour; after that, every
  six hours, and the sheet says so.
- Days and the four parts of a day are **worked out by this system** from MET's
  hourly and six-hourly data, in the Pi's own time zone. The credit says so,
  as the licence requires changes to be indicated.

MET's own weather symbols are used, copied into the application from
[`metno/weathericons`](https://github.com/metno/weathericons) (MIT, licence in
`app/static/ui/cabin/wx/LICENSE`). Nothing is loaded from anywhere else.

## Choosing the place

Type a place name and press **Search**. Names are looked up in Kartverket's
place-name register (the Norwegian Mapping Authority, open data under CC BY
4.0), and the height above sea level comes from Kartverket's elevation service.
MET uses the height to correct the temperature, so the right altitude matters in
the mountains.

Anywhere outside Norway, or to be exact, type coordinates instead, as
`latitude, longitude` — for example `61.1529, 10.6918`. No search is made for
those.

The search runs **only when Search is pressed**, never as you type.

## Keeping to MET's terms

MET's API is free on [terms](https://api.met.no/doc/TermsOfService) that ask
users not to put needless load on it. This system keeps to them:

| MET asks | What this system does |
| --- | --- |
| Identify yourself | Every request carries the User-Agent `NoboControl/1.0 github.com/aba1975/Nobo_Raspberry_PI_Zigbee`, plus the contact email from Settings when one is entered. MET encourages a contact so they can write before blocking anyone. |
| Respect `Expires` | No request is made before the copy has expired — usually about half an hour to an hour — plus a random 1 to 10 minutes, so that installations do not all ask on the minute. |
| Use `If-Modified-Since` | Every repeat request sends the `Last-Modified` MET gave, exactly as received. An unchanged forecast comes back as a short 304. |
| No more than needed | Never two requests within ten minutes, whatever `Expires` says. Only one request at a time, shared by everybody looking. |
| Ask only when needed | **There is no background polling.** MET is asked only when somebody has the app open, the page is visible, and the copy has expired. Phones are always given the Pi's copy; no phone or browser ever talks to MET. A cabin nobody is looking at makes no requests at all. |
| Coordinates to at most four decimals | Truncated to four before every request. |
| Compression and HTTPS | `Accept-Encoding: gzip` and https only. Redirects are followed only to the same allowed addresses. |
| Slow down when told | A `429` waits an hour, then doubles up to a day. A refusal (`400`, `401`, `403`, `404`, `422`) waits six hours. Unreachable waits 10, 15, 30, then 60 minutes. These waits are kept on disk, so a restart or a change of place does not cut them short. |
| Deprecation | A `203` means MET will withdraw this version. It is logged and shown under Settings. |
| Credit | "Forecast data from MET Norway, under CC BY 4.0", with links, on the strip's sheet and in Settings. Place names are credited to Kartverket. |

A new place may be fetched at most once a minute, so trying several places in
a row cannot hammer MET either.

The only addresses this feature can reach are listed in `ALLOWED` in
`app/forecast.py`: MET's compact location forecast, and Kartverket's place
search and elevation. `check_allowed` refuses anything else, including a
redirect elsewhere.

## Demo mode

There is no simulated forecast. Turned on in demo mode, it asks MET for real,
under the same rules — it is read-only and harmless, and a fake would only show
invented weather.

## Files

| File | What |
| --- | --- |
| `data/forecast.json` | On/off, the place, the front-page position and the contact email. Backed up. |
| `data/forecast_cache.json` | The last answer from MET with its `Expires` and `Last-Modified`, and when MET may next be asked. Backed up, so a restore does not ask early. |

## For developers

| Module | Role |
| --- | --- |
| `app/forecast.py` | The allow-list and HTTP call, `fetch` and `due` (the rules above), `summarise` (days and parts of days), `PlaceFinder` (Kartverket) and `ForecastService`. |
| `app/server.py` | `GET /api/forecast` (any signed-in user; the only path that asks MET), `GET`/`PUT /api/forecast/settings` and `GET /api/forecast/places` (administrators). `/api/status` carries a brief `forecast` field so the front page knows when to reload. |
| `app/static/ui/cabin/cabin.js` | The front-page row, the sheet and the Settings card. The classic interface does not show the forecast. |

`tests/conftest.py` makes every test unable to reach MET or Kartverket: an
autouse fixture replaces `forecast.http_get` with one that raises. Tests use
`FakeMet` in `tests/test_forecast.py`, and the `real_forecast_http_get`
fixture when they need the real function with a fake transport.

**Do not name or link the consumer weather site** that MET runs. The data is
MET's, under CC BY 4.0; the consumer brand is not part of that licence.
`tests/test_forecast.py` fails if its name or address appears in the UI or the
module.
