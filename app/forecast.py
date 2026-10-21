"""A weather forecast for the cabin, from MET Norway's Locationforecast 2.0.

For reading only. Nothing here may change the heating: the forecast is shown
on the front page and in its own sheet, and that is all.

Whose data, and on what terms
-----------------------------
The forecast is MET Norway's, from ``api.met.no`` — the same forecast
MET's consumer weather service shows, as data. It is free and needs no account, on terms
(https://api.met.no/doc/TermsOfService) that this module keeps to the letter:

* **Identification.** Every request carries a User-Agent naming this
  application, its version and where to find it, plus the contact address
  entered under Settings when there is one. Never a stock or invented one.
* **No unnecessary traffic.** The answer is kept on disk with its
  ``Expires`` and ``Last-Modified`` headers. No request is made before
  ``Expires``; the next one sends ``If-Modified-Since`` with exactly the
  ``Last-Modified`` that came back, and a 304 keeps the copy already held.
* **Only while somebody is looking.** There is no background poll. A request
  is made only when a browser asks this system for the forecast *and* the copy
  held has expired, so a cabin nobody is looking at sends nothing at all.
  Browsers only ask while their page is visible. Every browser and phone reads
  this system's copy; none of them contacts MET.
* **Spread out.** A random few minutes are added to every ``Expires``, and
  never fewer than ten minutes pass between two requests, whatever a header
  says.
* **Coordinates truncated to four decimals**, as MET requires.
* **gzip and redirects** are supported; a redirect is followed only to an
  address this module is allowed to call.
* **203** means the product version is deprecated: it is logged and shown in
  Settings. **429** means slow down, and the next request waits an hour,
  doubling each time it happens again. **403** usually means the
  identification was refused, and it waits six hours.
* **Attribution.** The data is CC BY 4.0. The interface credits MET Norway,
  links the licence, says the daily summaries are made here, and shows when the
  forecast was fetched.
* **No consumer brand.** The feature is called Weather Forecast and carries
  no brand name or logo of MET's consumer service. MET's own weather symbols are used, from ``metno/weathericons``
  (MIT), copied into the application rather than linked.

Finding the place
-----------------
MET has no place search. Places come from Kartverket, the Norwegian Mapping
Authority, whose place-name register is open data (CC BY 4.0, credited
"© Kartverket") and is where MET's own consumer service takes its place names from. Its elevation
service gives the height above sea level, which MET uses to correct the
temperature for a cabin up a hill. Both are asked only when an admin presses
Search or picks a place, never as they type, at most once a second, and
answers are kept in memory. Coordinates typed in are used as they are and need
no service at all, which is also how a place outside Norway is chosen.

What it may call
----------------
``check_allowed`` permits exactly MET's compact Locationforecast 2.0 and
Kartverket's place-name and elevation lookups, over HTTPS, and refuses
anything else before a request is made. The tests prove the list is
exhaustive, and ``tests/conftest.py`` makes every test unable to reach them.
"""

from __future__ import annotations

import asyncio
import email.utils
import gzip
import http.client
import json
import logging
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from alarm_persistence import _atomic_write

logger = logging.getLogger(__name__)

APP_NAME = "NoboControl"
APP_VERSION = "1.0"
APP_HOME = "github.com/aba1975/Nobo_Raspberry_PI_Zigbee"

MET_HOST = "api.met.no"
MET_PATH = "weatherapi/locationforecast/2.0/compact"
PLACE_HOST = "ws.geonorge.no"
PLACE_SEARCH_PATH = "stedsnavn/v1/navn"
ELEVATION_PATH = "hoydedata/v1/punkt"
ALLOWED = frozenset({
    (MET_HOST, MET_PATH),
    (PLACE_HOST, PLACE_SEARCH_PATH),
    (PLACE_HOST, ELEVATION_PATH),
})
TIMEOUT_SECONDS = 10

SCHEMA_VERSION = 1
DATA_DIR = Path(__file__).resolve().parent / "data"
SETTINGS_FILE = DATA_DIR / "forecast.json"
CACHE_FILE = DATA_DIR / "forecast_cache.json"
ICON_DIR = Path(__file__).resolve().parent / "static" / "ui" / "cabin" / "wx"

FRONT_PAGE_CHOICES = ("top", "bottom", "off")

# Never two requests to MET closer than this, whatever Expires says.
MIN_INTERVAL_SECONDS = 600
PLACE_CHANGE_INTERVAL_SECONDS = 60
# When MET sends no usable Expires, the copy is kept this long.
DEFAULT_LIFETIME_SECONDS = 3600
MAX_LIFETIME_SECONDS = 6 * 3600
# Added to every Expires, so this installation never asks on the dot.
JITTER_SECONDS = (60, 600)
UNREACHABLE_BACKOFF = (600, 900, 1800, 3600)
THROTTLED_BACKOFF_SECONDS = 3600
THROTTLED_BACKOFF_MAX = 24 * 3600
REFUSED_BACKOFF_SECONDS = 6 * 3600
# A forecast fetched longer ago than this is shown as old.
STALE_SECONDS = 6 * 3600
MAX_DAYS = 10

# Kartverket: a person pressing Search, so once a second is plenty.
PLACE_MIN_INTERVAL_SECONDS = 1.0
PLACE_CACHE_SIZE = 32
MAX_PLACE_RESULTS = 10
# Roughly mainland Norway and Svalbard: where Kartverket has heights.
NORWAY_BOUNDS = ((57.5, 81.0), (4.0, 32.0))

CONTACT_PATTERN = re.compile(r"^[^@\s<>\"]{1,64}@[^@\s<>\"]{1,190}\.[A-Za-z]{2,}$")
MAX_CONTACT_LENGTH = 120
MAX_NAME_LENGTH = 80
SYMBOL_PATTERN = re.compile(r"^[a-z]+(?:_(?:day|night|polartwilight))?$")

# MET's own words for each symbol, from metno/weathericons' legend.csv. The
# "lightssleet…"/"lightssnow…" spellings are MET's, and the codes the API
# sends really are spelt that way.
SYMBOL_WORDS = {
    "clearsky": "Clear sky",
    "fair": "Fair",
    "partlycloudy": "Partly cloudy",
    "cloudy": "Cloudy",
    "fog": "Fog",
    "lightrainshowers": "Light rain showers",
    "rainshowers": "Rain showers",
    "heavyrainshowers": "Heavy rain showers",
    "lightrainshowersandthunder": "Light rain showers and thunder",
    "rainshowersandthunder": "Rain showers and thunder",
    "heavyrainshowersandthunder": "Heavy rain showers and thunder",
    "lightsleetshowers": "Light sleet showers",
    "sleetshowers": "Sleet showers",
    "heavysleetshowers": "Heavy sleet showers",
    "lightssleetshowersandthunder": "Light sleet showers and thunder",
    "sleetshowersandthunder": "Sleet showers and thunder",
    "heavysleetshowersandthunder": "Heavy sleet showers and thunder",
    "lightsnowshowers": "Light snow showers",
    "snowshowers": "Snow showers",
    "heavysnowshowers": "Heavy snow showers",
    "lightssnowshowersandthunder": "Light snow showers and thunder",
    "snowshowersandthunder": "Snow showers and thunder",
    "heavysnowshowersandthunder": "Heavy snow showers and thunder",
    "lightrain": "Light rain",
    "rain": "Rain",
    "heavyrain": "Heavy rain",
    "lightrainandthunder": "Light rain and thunder",
    "rainandthunder": "Rain and thunder",
    "heavyrainandthunder": "Heavy rain and thunder",
    "lightsleet": "Light sleet",
    "sleet": "Sleet",
    "heavysleet": "Heavy sleet",
    "lightsleetandthunder": "Light sleet and thunder",
    "sleetandthunder": "Sleet and thunder",
    "heavysleetandthunder": "Heavy sleet and thunder",
    "lightsnow": "Light snow",
    "snow": "Snow",
    "heavysnow": "Heavy snow",
    "lightsnowandthunder": "Light snow and thunder",
    "snowandthunder": "Snow and thunder",
    "heavysnowandthunder": "Heavy snow and thunder",
}

# Kartverket's kinds of place, in English, and which ones a cabin is likely
# to be named after — those are listed first.
PLACE_KINDS = {
    "By": "Town", "Tettsted": "Town", "Tettbebyggelse": "Built-up area",
    "Tettsteddel": "Part of town", "Bydel": "District", "Bygd": "Village",
    "Grend": "Hamlet", "Boligfelt": "Housing area", "Hyttefelt": "Cabin area",
    "Gard": "Farm", "Bruk": "Smallholding", "Seter": "Summer farm",
    "Vann": "Lake", "Tjern": "Tarn", "Innsjø": "Lake", "Elv": "River",
    "Fjell": "Mountain", "Topp": "Summit", "Ås": "Ridge", "Dal": "Valley",
    "Øy": "Island", "Halvøy": "Peninsula", "Nes": "Headland", "Vik": "Bay",
    "Fjord": "Fjord", "Skog": "Forest", "Myr": "Bog", "Kommune": "Municipality",
    "Adressenavn": "Street", "Alpinanlegg": "Ski resort", "Skiheis": "Ski lift",
}
SETTLEMENT_KINDS = frozenset({
    "By", "Tettsted", "Tettbebyggelse", "Tettsteddel", "Bydel", "Bygd", "Grend",
    "Boligfelt", "Hyttefelt", "Gard", "Bruk", "Seter", "Alpinanlegg",
})

PERIODS = (("night", 0), ("morning", 6), ("afternoon", 12), ("evening", 18))


class ForbiddenEndpoint(RuntimeError):
    """A request this integration has no business making."""


class InvalidForecastData(ValueError):
    pass


class PlaceSearchError(Exception):
    """The place search could not be done. The message is ours, for the interface."""


def check_allowed(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.lstrip("/")
    if parsed.scheme != "https" or (parsed.hostname, path) not in ALLOWED:
        raise ForbiddenEndpoint("Only MET's forecast and Kartverket's place names may be called")


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """MET asks clients to follow redirects; only to somewhere allowed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_allowed(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirect())

# (status, headers with lower-case names, body). Raises OSError when nothing
# came back at all.
Response = Tuple[int, Dict[str, str], bytes]
Transport = Callable[[str, Mapping[str, str]], Response]


def _decompress(body: bytes, encoding: str) -> bytes:
    encoding = encoding.strip().lower()
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    return body


def http_get(url: str, headers: Mapping[str, str]) -> Response:
    """GET with gzip, as MET requires of every client. The standard library only."""
    check_allowed(url)
    request = urllib.request.Request(url, method="GET", headers={
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        **headers,
    })
    try:
        with _opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            status, raw_headers, body = response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        status, raw_headers, body = exc.code, exc.headers, exc.read()
    found = {name.lower(): value for name, value in (raw_headers or {}).items()}
    try:
        body = _decompress(body, found.get("content-encoding", ""))
    except (OSError, zlib.error, EOFError):
        body = b""
    return status, found, body


def user_agent(contact: str = "") -> str:
    """Who is asking, as MET's terms require: name/version, then where to reach us."""
    base = f"{APP_NAME}/{APP_VERSION} {APP_HOME}"
    return f"{base} {contact}" if contact else base


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float
    # "Hamlet · Ringsaker, Innlandet", or the coordinates for a typed point.
    detail: str = ""
    # Metres above sea level. None lets MET use its own terrain.
    altitude: Optional[int] = None


@dataclass(frozen=True)
class ForecastSettings:
    enabled: bool = False
    place: Optional[Place] = None
    # Sent to MET in the User-Agent, so they can write before they block.
    contact: str = ""
    # Where the week goes on the front page: under the top card, under the
    # rooms, or nowhere (the sheet is still reached from the weather station).
    front_page: str = "top"


def truncate_coordinate(value: Any, limit: float, where: str) -> float:
    """Four decimals, cut rather than rounded, as MET's terms ask."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise InvalidForecastData(f"{where} must be a number")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise InvalidForecastData(f"{where} must be a number") from None
    if not number.is_finite() or abs(number) > Decimal(str(limit)):
        raise InvalidForecastData(f"{where} must be between -{limit:g} and {limit:g}")
    return float(number.quantize(Decimal("0.0001"), rounding=ROUND_DOWN))


def parse_altitude(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidForecastData("altitude must be a number of metres")
    if not -500 <= value <= 9000:
        raise InvalidForecastData("altitude must be between -500 and 9000 metres")
    return int(round(value))


def _text(value: Any, where: str, limit: int) -> str:
    if not isinstance(value, str):
        raise InvalidForecastData(f"{where} must be text")
    text = " ".join(value.split())
    if len(text) > limit:
        raise InvalidForecastData(f"{where} is too long")
    return text


def parse_place(payload: Any) -> Optional[Place]:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise InvalidForecastData("place must be an object")
    lat = truncate_coordinate(payload.get("lat"), 90, "lat")
    lon = truncate_coordinate(payload.get("lon"), 180, "lon")
    name = _text(payload.get("name", ""), "name", MAX_NAME_LENGTH) or format_point(lat, lon)
    detail = _text(payload.get("detail", ""), "detail", 160)
    return Place(name=name, lat=lat, lon=lon, detail=detail,
                 altitude=parse_altitude(payload.get("altitude")))


def parse_contact(value: Any) -> str:
    if value is None:
        return ""
    text = _text(value, "contact", MAX_CONTACT_LENGTH)
    if text and not CONTACT_PATTERN.match(text):
        raise InvalidForecastData("The contact must be an email address")
    return text


def parse_front_page(value: Any) -> str:
    if value not in FRONT_PAGE_CHOICES:
        raise InvalidForecastData("front_page must be top, bottom or off")
    return value


def _document(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise InvalidForecastData("document must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise InvalidForecastData("unsupported or missing schema_version")
    return payload


def _load(path: Path, default: Any, parse):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return parse(json.load(handle))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, InvalidForecastData, TypeError, ValueError, KeyError) as exc:
        logger.warning("Invalid forecast data in %s: %s", path.name, type(exc).__name__)
        try:
            path.replace(path.with_suffix(".backup"))
        except OSError:
            pass
        return default


def parse_settings(payload: Any) -> ForecastSettings:
    doc = _document(payload)
    enabled = doc.get("enabled", False)
    if not isinstance(enabled, bool):
        raise InvalidForecastData("enabled must be a boolean")
    return ForecastSettings(
        enabled=enabled,
        place=parse_place(doc.get("place")),
        contact=parse_contact(doc.get("contact", "")),
        front_page=parse_front_page(doc.get("front_page", "top")),
    )


def load_settings(path: Optional[Path] = None) -> ForecastSettings:
    return _load(path or SETTINGS_FILE, ForecastSettings(), parse_settings)


def save_settings(settings: ForecastSettings, path: Optional[Path] = None) -> None:
    _atomic_write(path or SETTINGS_FILE, {"schema_version": SCHEMA_VERSION, **asdict(settings)})


# ---------------------------------------------------------------------------
# The copy of MET's answer, and when we may ask again
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ForecastCache:
    # Which request the copy answers: "lat,lon,altitude".
    place_key: str = ""
    timeseries: Tuple[dict, ...] = field(default_factory=tuple)
    updated_at: Optional[str] = None       # MET's model run, from meta
    last_modified: Optional[str] = None    # exactly as MET sent it
    fetched_at: Optional[float] = None     # when the copy last came back, 200 or 304
    asked_at: Optional[float] = None       # the last request of any outcome
    next_at: Optional[float] = None        # not a request before this
    failure: Optional[str] = None          # unreachable | throttled | refused | bad_answer
    failures: int = 0
    deprecated: bool = False               # MET answered 203


def place_key(place: Place) -> str:
    return f"{place.lat:.4f},{place.lon:.4f},{'' if place.altitude is None else place.altitude}"


def _parse_cache(payload: Any) -> ForecastCache:
    doc = _document(payload)
    series = doc.get("timeseries", [])
    if not isinstance(series, list) or not all(isinstance(row, dict) for row in series):
        raise InvalidForecastData("timeseries must be a list")

    def number(name: str) -> Optional[float]:
        value = doc.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidForecastData(f"{name} must be a number")
        return float(value)

    def text(name: str) -> Optional[str]:
        value = doc.get(name)
        if value is not None and not isinstance(value, str):
            raise InvalidForecastData(f"{name} must be text")
        return value

    failures = doc.get("failures", 0)
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        raise InvalidForecastData("failures must be a count")
    return ForecastCache(
        place_key=text("place_key") or "",
        timeseries=tuple(series),
        updated_at=text("updated_at"),
        last_modified=text("last_modified"),
        fetched_at=number("fetched_at"),
        asked_at=number("asked_at"),
        next_at=number("next_at"),
        failure=text("failure"),
        failures=failures,
        deprecated=bool(doc.get("deprecated", False)),
    )


def load_cache(path: Optional[Path] = None) -> ForecastCache:
    return _load(path or CACHE_FILE, ForecastCache(), _parse_cache)


def save_cache(cache: ForecastCache, path: Optional[Path] = None) -> None:
    payload = asdict(cache)
    payload["timeseries"] = list(cache.timeseries)
    _atomic_write(path or CACHE_FILE, {"schema_version": SCHEMA_VERSION, **payload})


def met_url(place: Place) -> str:
    query = {"lat": f"{place.lat:.4f}", "lon": f"{place.lon:.4f}"}
    if place.altitude is not None:
        query["altitude"] = str(place.altitude)
    return f"https://{MET_HOST}/{MET_PATH}?{urllib.parse.urlencode(query)}"


def _http_time(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def lifetime(headers: Mapping[str, str], now: float) -> float:
    """How long MET says the answer holds, measured on MET's clock.

    Expires minus MET's own Date, so a Pi whose clock is a few minutes out
    neither asks early nor waits too long.
    """
    expires = _http_time(headers.get("expires"))
    if expires is None:
        return float(DEFAULT_LIFETIME_SECONDS)
    sent = _http_time(headers.get("date"))
    seconds = expires - (sent if sent is not None else now)
    return float(min(max(seconds, 0.0), MAX_LIFETIME_SECONDS))


def fetch(
    settings: ForecastSettings,
    cache: ForecastCache,
    now: float,
    transport: Optional[Transport] = None,
    rng: Callable[[float, float], float] = random.uniform,
) -> ForecastCache:
    """Ask MET once and return the new copy. Blocking: run it off the event loop.

    The caller decides that a request is due (``due``); this only makes it and
    works out when the next one may be.
    """
    place = settings.place
    if place is None:
        return cache
    key = place_key(place)
    same_place = cache.place_key == key and bool(cache.timeseries)
    headers = {"User-Agent": user_agent(settings.contact)}
    if same_place and cache.last_modified:
        headers["If-Modified-Since"] = cache.last_modified
    send = transport or http_get

    def failed(kind: str, wait: float) -> ForecastCache:
        kept = cache if same_place else replace(
            cache, place_key=key, timeseries=(), updated_at=None, last_modified=None, fetched_at=None,
        )
        return replace(
            kept, asked_at=now, failure=kind, failures=cache.failures + 1,
            next_at=now + max(wait, MIN_INTERVAL_SECONDS),
        )

    try:
        status, answer_headers, body = send(met_url(place), headers)
    except (OSError, http.client.HTTPException) as exc:
        logger.info("Weather forecast: MET could not be reached (%s)", type(exc).__name__)
        table = UNREACHABLE_BACKOFF
        return failed("unreachable", table[min(cache.failures, len(table) - 1)])

    def next_time() -> float:
        wait = lifetime(answer_headers, now) + rng(*JITTER_SECONDS)
        return now + max(wait, MIN_INTERVAL_SECONDS)

    if status in (200, 203):
        try:
            document = json.loads(body.decode("utf-8"))
            properties = document["properties"]
            series = properties["timeseries"]
            if not isinstance(series, list) or not series:
                raise ValueError("empty")
            updated = (properties.get("meta") or {}).get("updated_at")
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("Weather forecast: MET's answer could not be read")
            return failed("bad_answer", UNREACHABLE_BACKOFF[-1])
        if status == 203 and not cache.deprecated:
            # MET's terms ask for this to be logged and shown, not ignored.
            logger.warning(
                "Weather forecast: MET reports Locationforecast 2.0 is deprecated (203). "
                "This application needs updating before MET turns it off."
            )
        return ForecastCache(
            place_key=key,
            timeseries=tuple(row for row in series if isinstance(row, dict)),
            updated_at=updated if isinstance(updated, str) else None,
            last_modified=answer_headers.get("last-modified"),
            fetched_at=now, asked_at=now, next_at=next_time(),
            failure=None, failures=0, deprecated=status == 203,
        )
    if status == 304 and same_place:
        return replace(
            cache, fetched_at=now, asked_at=now, next_at=next_time(),
            failure=None, failures=0,
            last_modified=answer_headers.get("last-modified", cache.last_modified),
        )
    if status == 429:
        logger.warning("Weather forecast: MET asked this installation to slow down (429)")
        wait = THROTTLED_BACKOFF_SECONDS * (2 ** min(cache.failures, 5))
        return failed("throttled", min(wait, THROTTLED_BACKOFF_MAX))
    if status in (400, 401, 403, 404, 422):
        logger.warning("Weather forecast: MET refused the request (%s)", status)
        return failed("refused", REFUSED_BACKOFF_SECONDS)
    logger.info("Weather forecast: MET answered %s", status)
    table = UNREACHABLE_BACKOFF
    return failed("unreachable", table[min(cache.failures, len(table) - 1)])


def due(settings: ForecastSettings, cache: ForecastCache, now: float) -> bool:
    """Whether MET may be asked now. Never before Expires, never twice in ten minutes."""
    if not settings.enabled or settings.place is None:
        return False
    since = None if cache.asked_at is None else now - cache.asked_at
    holding = cache.failure in ("throttled", "refused")
    if cache.place_key != place_key(settings.place) and not holding:
        # A different place is a different forecast, which nothing held
        # answers and no Expires covers. Still never more than once a minute,
        # however often somebody changes their mind.
        return since is None or since >= PLACE_CHANGE_INTERVAL_SECONDS
    if since is not None and since < MIN_INTERVAL_SECONDS:
        return False
    return cache.next_at is None or now >= cache.next_at


# ---------------------------------------------------------------------------
# Days, periods and hours, in local time
# ---------------------------------------------------------------------------

def _symbol(block: Any) -> Optional[str]:
    if not isinstance(block, dict):
        return None
    code = (block.get("summary") or {}).get("symbol_code")
    return code if isinstance(code, str) and SYMBOL_PATTERN.match(code) else None


def _amount(block: Any) -> Optional[float]:
    if not isinstance(block, dict):
        return None
    value = (block.get("details") or {}).get("precipitation_amount")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _num(details: Mapping[str, Any], name: str) -> Optional[float]:
    value = details.get(name)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def symbol_words(code: Optional[str]) -> str:
    if not code:
        return ""
    return SYMBOL_WORDS.get(code.split("_", 1)[0], "")


def _rows(timeseries: Tuple[dict, ...], zone: Optional[tzinfo]) -> List[dict]:
    """Each row with its start, its step to the next and what it covers."""
    parsed: List[Tuple[datetime, dict]] = []
    for row in timeseries:
        try:
            when = datetime.fromisoformat(str(row["time"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        parsed.append((when, row.get("data") or {}))
    parsed.sort(key=lambda item: item[0])
    rows: List[dict] = []
    for index, (when, data) in enumerate(parsed):
        one, six, twelve = data.get("next_1_hours"), data.get("next_6_hours"), data.get("next_12_hours")
        if index + 1 < len(parsed):
            step = (parsed[index + 1][0] - when).total_seconds() / 3600
        else:
            step = 1.0 if one and not six else 6.0
        # Rain for exactly the hours up to the next row, so a day's total
        # never counts an hour twice where hourly steps become six-hourly.
        if step <= 1.01 and _amount(one) is not None:
            precipitation = _amount(one)
        elif _amount(six) is not None:
            precipitation = _amount(six) * min(step, 6.0) / 6.0
        else:
            precipitation = _amount(one) or 0.0
        symbol = _symbol(one) if step <= 1.01 else None
        symbol = symbol or _symbol(six) or _symbol(one) or _symbol(twelve)
        details = (data.get("instant") or {}).get("details") or {}
        local = when.astimezone(zone) if zone is not None else when.astimezone()
        rows.append({
            "start": when, "local": local, "step": step,
            "temperature": _num(details, "air_temperature"),
            "wind": _num(details, "wind_speed"),
            "wind_direction": _num(details, "wind_from_direction"),
            "precipitation": round(precipitation, 2),
            "symbol": symbol,
            "symbol_6h": _symbol(six) or symbol,
        })
    return rows


def _range(values: List[float]) -> Tuple[Optional[float], Optional[float]]:
    return (min(values), max(values)) if values else (None, None)


def summarise(
    timeseries: Tuple[dict, ...], now: datetime, zone: Optional[tzinfo] = None,
) -> Dict[str, Any]:
    """MET's rows as days the interface can draw.

    Rows already over are dropped. Each day has its high and low, its rain,
    its strongest wind, a symbol for each quarter (night, morning, afternoon,
    evening) and one for the day, and its hours: by the hour for the first two
    or three days and every six hours after that, as MET forecasts them.
    """
    rows = [row for row in _rows(timeseries, zone)
            if row["start"] + timedelta(hours=row["step"]) > now]
    by_day: Dict[date, List[dict]] = {}
    for row in rows:
        by_day.setdefault(row["local"].date(), []).append(row)
    today = (now.astimezone(zone) if zone is not None else now.astimezone()).date()

    days: List[Dict[str, Any]] = []
    used: set = set()
    for day, items in sorted(by_day.items()):
        if day < today:
            continue
        periods: List[Optional[Dict[str, Any]]] = []
        for name, first_hour in PERIODS:
            inside = [row for row in items if first_hour <= row["local"].hour < first_hour + 6]
            if not inside:
                periods.append(None)
                continue
            temps = [row["temperature"] for row in inside if row["temperature"] is not None]
            low, high = _range(temps)
            periods.append({
                "name": name,
                "symbol": inside[0]["symbol_6h"],
                "min": low, "max": high,
                "precipitation": round(sum(row["precipitation"] for row in inside), 1),
            })
        by_name = {period["name"]: period for period in periods if period}
        # A day that only starts — the last one MET sends is often a single
        # row after midnight — is not worth a column of its own.
        if day != today and not ({"morning", "afternoon"} & set(by_name)):
            continue
        main = next((by_name[name]["symbol"] for name in ("afternoon", "morning", "evening", "night")
                     if name in by_name and by_name[name]["symbol"]), None)
        temps = [row["temperature"] for row in items if row["temperature"] is not None]
        winds = [row["wind"] for row in items if row["wind"] is not None]
        low, high = _range(temps)
        hours = [{
            "at": row["local"].isoformat(),
            "time": row["local"].strftime("%H:%M"),
            "step": 1 if row["step"] <= 1.01 else round(row["step"]),
            "temperature": row["temperature"],
            "symbol": row["symbol"],
            "precipitation": row["precipitation"],
            "wind": row["wind"],
            "wind_direction": row["wind_direction"],
        } for row in items]
        for code in [main, *(row["symbol"] for row in items), *(p["symbol"] for p in by_name.values())]:
            if code:
                used.add(code)
        days.append({
            "date": day.isoformat(),
            "symbol": main,
            "min": low, "max": high,
            "precipitation": round(sum(row["precipitation"] for row in items), 1),
            "wind_max": max(winds) if winds else None,
            "periods": periods,
            "hours": hours,
        })
        if len(days) >= MAX_DAYS:
            break

    current = rows[0] if rows else None
    return {
        "now": None if current is None else {
            "temperature": current["temperature"],
            "symbol": current["symbol"],
            "at": current["local"].isoformat(),
        },
        "days": days,
        "symbols": {code: symbol_words(code) for code in sorted(used)},
    }


# ---------------------------------------------------------------------------
# Places: Kartverket's names and heights, or typed coordinates
# ---------------------------------------------------------------------------

POINT_PATTERN = re.compile(
    r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*°?\s*([NS])?\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*°?\s*([EWØ])?\s*$",
    re.IGNORECASE,
)


def format_point(lat: float, lon: float) -> str:
    return f"{lat:.4f}, {lon:.4f}"


def parse_point(text: str) -> Optional[Tuple[float, float]]:
    """"61.1529, 10.6918" (or with N/E) as a point, truncated. None if it is not one."""
    found = POINT_PATTERN.match(text or "")
    if not found:
        return None
    lat_text, north_south, lon_text, east_west = found.groups()
    try:
        lat = truncate_coordinate(lat_text, 90, "lat")
        lon = truncate_coordinate(lon_text, 180, "lon")
    except InvalidForecastData:
        return None
    if north_south and north_south.upper() == "S":
        lat = -abs(lat)
    if east_west and east_west.upper() == "W":
        lon = -abs(lon)
    return lat, lon


def in_norway(lat: float, lon: float) -> bool:
    (south, north), (west, east) = NORWAY_BOUNDS
    return south <= lat <= north and west <= lon <= east


def _place_from_name(entry: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    point = entry.get("representasjonspunkt") or {}
    name = entry.get("skrivemåte")
    try:
        lat = truncate_coordinate(point.get("nord"), 90, "lat")
        lon = truncate_coordinate(point.get("øst"), 180, "lon")
    except InvalidForecastData:
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    kind = entry.get("navneobjekttype") if isinstance(entry.get("navneobjekttype"), str) else ""
    where = []
    for key, inner in (("kommuner", "kommunenavn"), ("fylker", "fylkesnavn")):
        values = entry.get(key) or []
        if isinstance(values, list) and values and isinstance(values[0], dict):
            text = values[0].get(inner)
            if isinstance(text, str) and text and text not in where:
                where.append(text)
    detail = " · ".join(part for part in (PLACE_KINDS.get(kind, kind), ", ".join(where)) if part)
    return {
        "name": " ".join(name.split())[:MAX_NAME_LENGTH], "detail": detail[:160],
        "lat": lat, "lon": lon, "altitude": None,
        "settlement": kind in SETTLEMENT_KINDS,
    }


class PlaceFinder:
    """Kartverket, asked politely: on a button, once a second, remembered."""

    def __init__(self, transport: Optional[Transport] = None, clock: Callable[[], float] = time.monotonic):
        self.transport = transport
        self.clock = clock
        self._lock = threading.Lock()
        self._last = 0.0
        self._answers: Dict[str, Any] = {}

    def _ask(self, url: str, contact: str) -> dict:
        send = self.transport or http_get
        with self._lock:
            wait = PLACE_MIN_INTERVAL_SECONDS - (self.clock() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                status, _headers, body = send(url, {"User-Agent": user_agent(contact)})
            except (OSError, http.client.HTTPException):
                raise PlaceSearchError("Kartverket could not be reached just now") from None
            finally:
                self._last = self.clock()
        if status != 200:
            raise PlaceSearchError("Kartverket's place names could not be searched just now")
        try:
            answer = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PlaceSearchError("Kartverket's answer could not be read") from None
        if not isinstance(answer, dict):
            raise PlaceSearchError("Kartverket's answer could not be read")
        return answer

    def _remember(self, key: str, value: Any) -> Any:
        if len(self._answers) >= PLACE_CACHE_SIZE:
            self._answers.pop(next(iter(self._answers)))
        self._answers[key] = value
        return value

    def search(self, query: str, contact: str = "") -> List[Dict[str, Any]]:
        text = " ".join((query or "").split())
        if not text:
            return []
        point = parse_point(text)
        if point is not None:
            lat, lon = point
            return [{"name": format_point(lat, lon), "detail": "Coordinates",
                     "lat": lat, "lon": lon, "altitude": None, "settlement": True}]
        if len(text) > MAX_NAME_LENGTH:
            raise PlaceSearchError("That is too long to be a place name")
        key = f"search:{text.casefold()}"
        if key in self._answers:
            return self._answers[key]
        found: List[Dict[str, Any]] = []
        for fuzzy in ("false", "true"):
            query_string = urllib.parse.urlencode({
                "sok": text, "fuzzy": fuzzy, "utkoordsys": "4258",
                "treffPerSide": str(MAX_PLACE_RESULTS * 2), "side": "1",
            })
            answer = self._ask(f"https://{PLACE_HOST}/{PLACE_SEARCH_PATH}?{query_string}", contact)
            names = answer.get("navn") or []
            found = [place for place in (
                _place_from_name(entry) for entry in names if isinstance(entry, dict)
            ) if place]
            if found:
                break
        # Towns and hamlets before lakes and hills of the same name.
        found.sort(key=lambda place: not place["settlement"])
        return self._remember(key, found[:MAX_PLACE_RESULTS])

    def elevation(self, lat: float, lon: float, contact: str = "") -> Optional[int]:
        if not in_norway(lat, lon):
            return None
        key = f"height:{lat:.4f},{lon:.4f}"
        if key in self._answers:
            return self._answers[key]
        query_string = urllib.parse.urlencode({
            "nord": f"{lat:.4f}", "ost": f"{lon:.4f}", "koordsys": "4258", "geojson": "false",
        })
        try:
            answer = self._ask(f"https://{PLACE_HOST}/{ELEVATION_PATH}?{query_string}", contact)
        except PlaceSearchError:
            return None
        points = answer.get("punkter") or []
        height = points[0].get("z") if points and isinstance(points[0], dict) else None
        if isinstance(height, bool) or not isinstance(height, (int, float)):
            return None
        try:
            return self._remember(key, parse_altitude(height))
        except InvalidForecastData:
            return None


# ---------------------------------------------------------------------------
# The service the application holds
# ---------------------------------------------------------------------------

class ForecastService:
    """Settings, the copy held, and asking MET only when it is due."""

    def __init__(
        self,
        transport: Optional[Transport] = None,
        clock: Callable[[], float] = time.time,
        rng: Callable[[float, float], float] = random.uniform,
    ):
        self.transport = transport
        self.clock = clock
        self.rng = rng
        self.settings = load_settings()
        self.cache = load_cache()
        self.places = PlaceFinder(transport)
        self._lock: Optional[asyncio.Lock] = None

    def apply_settings(self, updated: ForecastSettings) -> None:
        previous = self.settings
        save_settings(updated)
        self.settings = updated
        if not updated.enabled and previous.enabled:
            # Off means off: the copy goes, but not the record of being told
            # to slow down, which MET expects to be honoured.
            holding = self.cache.failure in ("throttled", "refused")
            self.cache = ForecastCache(
                asked_at=self.cache.asked_at,
                next_at=self.cache.next_at if holding else None,
                failure=self.cache.failure if holding else None,
                failures=self.cache.failures if holding else 0,
            )
            save_cache(self.cache)

    def due(self) -> bool:
        return due(self.settings, self.cache, self.clock())

    async def refresh_if_due(self) -> bool:
        """Ask MET if, and only if, the terms allow it now. True if anything changed."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if not self.due():
                return False
            settings, cache = self.settings, self.cache
            updated = await asyncio.to_thread(
                fetch, settings, cache, self.clock(), self.transport, self.rng,
            )
            if not self.settings.enabled or self.settings.place != settings.place:
                # Changed while the request was out: the answer is for a place
                # no longer chosen, but the time it was asked still counts.
                self.cache = replace(self.cache, asked_at=updated.asked_at)
                save_cache(self.cache)
                return False
            self.cache = updated
            save_cache(updated)
            return True

    def brief(self) -> Optional[Dict[str, Any]]:
        """For /api/status: enough for a browser to know whether to ask."""
        settings = self.settings
        if not settings.enabled or settings.place is None:
            return None
        return {
            "front_page": settings.front_page,
            "fetched_at": _iso(self.cache.fetched_at),
        }

    def status(self) -> Dict[str, Any]:
        cache, settings, now = self.cache, self.settings, self.clock()
        current = settings.place is not None and cache.place_key == place_key(settings.place)
        has_copy = current and bool(cache.timeseries)
        if settings.place is None:
            state = "no_place"
        elif cache.failure:
            state = cache.failure
        elif has_copy:
            state = "ok"
        else:
            state = "starting"
        return {
            "state": state,
            "message": FAILURE_WORDS.get(state),
            "fetched_at": _iso(cache.fetched_at) if has_copy else None,
            "updated_at": cache.updated_at if has_copy else None,
            "next_at": _iso(cache.next_at),
            "stale": has_copy and cache.fetched_at is not None and now - cache.fetched_at > STALE_SECONDS,
            "deprecated": cache.deprecated,
        }

    def payload(self, zone: Optional[tzinfo] = None) -> Dict[str, Any]:
        settings = self.settings
        if not settings.enabled:
            return {"enabled": False}
        status = self.status()
        place = settings.place
        current = place is not None and self.cache.place_key == place_key(place)
        summary = summarise(
            self.cache.timeseries if current else (),
            datetime.fromtimestamp(self.clock(), timezone.utc), zone,
        )
        return {
            "enabled": True,
            "front_page": settings.front_page,
            "place": None if place is None else {
                "name": place.name, "detail": place.detail, "altitude": place.altitude,
            },
            **status,
            **summary,
        }


FAILURE_WORDS = {
    "no_place": "Choose a place for the forecast under Settings.",
    "unreachable": "MET Norway could not be reached. The forecast is tried again later.",
    "throttled": "MET Norway asked this system to ask less often, so it is waiting before it asks again.",
    "refused": "MET Norway refused the request. Check the place and the contact address under Settings.",
    "bad_answer": "MET Norway's answer could not be read. It is tried again later.",
}


def _iso(stamp: Optional[float]) -> Optional[str]:
    if stamp is None:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).astimezone().isoformat()
