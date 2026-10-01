"""The user's own Netatmo weather station, read through Netatmo's official API.

How it is reached
-----------------
Netatmo has no local API for the weather station: the station uploads to
Netatmo's cloud and is read from there. The API is official and documented,
and access is by OAuth2 with the ``read_station`` scope, which can read the
station and nothing else — it cannot change a setting, and it is not the
scope that reads cameras, thermostats or anything else in a Netatmo account.

Each installation registers **its own app** at https://dev.netatmo.com and
enters that app's client id and secret under Settings. Nothing is shared
between installations, and no Netatmo password ever reaches this system: the
user signs in on Netatmo's own page, which sends the browser back here with a
one-time code. Netatmo's developer page can also generate a token directly,
and that can be pasted in instead.

What is kept
------------
``data/netatmo/account.json`` (0600, in a 0700 directory, left out of
backups): the client id and secret, the access token, which lasts about three
hours, and the refresh token, which Netatmo may replace each time it is used.
The newest refresh token is therefore written to disk *before* anything else
is done with it: losing it would mean signing in again.

What it may call
----------------
Exactly two endpoints, ``oauth2/token`` and ``api/getstationsdata``, on
``api.netatmo.com`` over HTTPS. ``check_allowed`` refuses anything else before
a request is made, and the tests prove the list is exhaustive.

Errors
------
Netatmo's own messages are never shown or logged — only our own words, chosen
by the HTTP status and Netatmo's numeric error code. Tokens are never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import weather_persistence
from weather_provider import WeatherModule, WeatherReading, WeatherUnavailable

logger = logging.getLogger(__name__)

API_HOST = "api.netatmo.com"
API_BASE = f"https://{API_HOST}/"
AUTHORIZE_URL = f"{API_BASE}oauth2/authorize"
TOKEN_PATH = "oauth2/token"
STATIONS_PATH = "api/getstationsdata"
ALLOWED_PATHS = frozenset({TOKEN_PATH, STATIONS_PATH})
SCOPE = "read_station"
TIMEOUT_SECONDS = 20
# Refreshed this long before it would expire, so a slow request never
# carries a token that runs out on the way.
REFRESH_MARGIN_SECONDS = 300

# Netatmo's numeric error codes that mean the token, not the request, is bad.
TOKEN_ERROR_CODES = frozenset({1, 2, 3})
THROTTLED_ERROR_CODE = 26

MODULE_TYPES = {
    "NAMain": "base",
    "NAModule1": "outdoor",
    "NAModule4": "indoor",
    "NAModule3": "rain",
    "NAModule2": "wind",
}


class ForbiddenEndpoint(RuntimeError):
    """A request this integration has no business making."""


class NetatmoError(Exception):
    """A failure the interface can show. ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def check_allowed(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.lstrip("/")
    if parsed.scheme != "https" or parsed.hostname != API_HOST or path not in ALLOWED_PATHS:
        raise ForbiddenEndpoint("Only Netatmo's token and station endpoints may be called")


# (status, parsed JSON or None). Raises OSError when nothing came back at all.
Transport = Callable[[str, Mapping[str, str], Mapping[str, str]], Tuple[int, Optional[dict]]]


def http_post(url: str, form: Mapping[str, str], headers: Mapping[str, str]) -> Tuple[int, Optional[dict]]:
    """POST a form and return the status and the JSON answer.

    The standard library only: one request every few minutes does not need a
    client library, and a dependency is one more thing to keep current.
    """
    check_allowed(url)
    data = urllib.parse.urlencode(dict(form)).encode("ascii")
    request = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Accept": "application/json",
        **headers,
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    try:
        parsed = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    return status, parsed if isinstance(parsed, dict) else None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _module(raw: Mapping[str, Any], fallback_name: str) -> Optional[WeatherModule]:
    kind = MODULE_TYPES.get(str(raw.get("type")))
    module_id = raw.get("_id")
    if kind is None or not isinstance(module_id, str):
        return None
    data = raw.get("dashboard_data") if isinstance(raw.get("dashboard_data"), dict) else {}
    reachable = raw.get("reachable") is not False and bool(data)
    name = raw.get("module_name")
    battery = _number(raw.get("battery_percent"))
    co2 = _number(data.get("CO2"))
    return WeatherModule(
        module_id=module_id,
        kind=kind,
        name=name.strip() if isinstance(name, str) and name.strip() else fallback_name,
        temperature=_number(data.get("Temperature")),
        humidity=_number(data.get("Humidity")),
        co2=int(co2) if co2 is not None else None,
        # Corrected to sea level by the station, from the altitude it was set
        # up with. Only the change matters here, so either would do.
        pressure=_number(data.get("Pressure")),
        min_temperature=_number(data.get("min_temp")),
        max_temperature=_number(data.get("max_temp")),
        temperature_trend=data.get("temp_trend") if isinstance(data.get("temp_trend"), str) else None,
        battery=int(battery) if battery is not None else None,
        reachable=reachable,
        reported_at=_number(data.get("time_utc")),
    )


def _station_name(station: Mapping[str, Any]) -> str:
    """The home the station is in, which heads the weather sheet.

    Netatmo's ``station_name`` is "Home (base station module)", so renaming the
    base station in Netatmo's app renamed the whole sheet to an indoor room.
    ``home_name`` is the home alone; without it, the base module's name is
    taken off the end of ``station_name``.
    """
    home = str(station.get("home_name") or "").strip()
    if home:
        return home
    name = str(station.get("station_name") or "").strip()
    base = str(station.get("module_name") or "").strip()
    suffix = f" ({base})"
    if base and name.endswith(suffix) and len(name) > len(suffix):
        name = name[:-len(suffix)].strip()
    return name or "Weather station"


def parse_stations(payload: Any, now: float) -> WeatherReading:
    """The first station in an account, as one reading.

    An account with more than one station is read from the first; a second
    house would be a second installation of this application anyway.
    """
    body = payload.get("body") if isinstance(payload, dict) else None
    devices = body.get("devices") if isinstance(body, dict) else None
    if not isinstance(devices, list) or not devices or not isinstance(devices[0], dict):
        raise WeatherUnavailable(
            "not_configured", "Netatmo reports no weather station on this account.",
        )
    station = devices[0]
    modules: List[WeatherModule] = []
    base = _module(station, "Base station")
    if base is not None:
        modules.append(base)
    for raw in station.get("modules") or []:
        if isinstance(raw, dict):
            module = _module(raw, MODULE_TYPES.get(str(raw.get("type")), "module").capitalize())
            if module is not None:
                modules.append(module)
    return WeatherReading(
        station_name=_station_name(station)[:80], modules=tuple(modules[:weather_persistence.MAX_MODULES]),
        read_at=now,
    )


def mask(value: Optional[str]) -> Optional[str]:
    """Enough of a client id to recognise it, and no more."""
    if not value:
        return None
    return f"{value[:4]}\u2026{value[-2:]}" if len(value) > 8 else "\u2026"


class NetatmoAccount:
    """The Netatmo app and its tokens, and the reading of the station."""

    name = "netatmo"

    def __init__(
        self,
        transport: Optional[Transport] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._lock = threading.Lock()

    def _post(self, path: str, form: Mapping[str, str], headers: Optional[Mapping[str, str]] = None):
        url = API_BASE + path
        check_allowed(url)
        transport = self._transport or http_post
        try:
            return transport(url, form, headers or {})
        except ForbiddenEndpoint:
            raise
        except OSError as exc:
            # Never the exception's text: a URL error can carry the request.
            logger.warning("Netatmo did not answer: %s", type(exc).__name__)
            raise WeatherUnavailable(
                "unreachable", "Netatmo is not answering. It will be tried again shortly.",
            ) from None

    # --- what Settings shows ------------------------------------------------

    def public_state(self) -> Dict[str, Any]:
        account = weather_persistence.load_account()
        return {
            "app_configured": account is not None,
            "client_id": mask(account["client_id"]) if account else None,
            "connected": bool(account and account.get("refresh_token")),
            "connected_at": account.get("connected_at") if account else None,
        }

    # --- setting up ---------------------------------------------------------

    def set_app(self, client_id: str, client_secret: str) -> None:
        """Keep the app's id and secret. A different app starts unconnected."""
        client_id, client_secret = client_id.strip(), client_secret.strip()
        if not client_id or not client_secret:
            raise NetatmoError(400, "Enter both the client ID and the client secret.")
        with self._lock:
            current = weather_persistence.load_account()
            keep = current if current and current["client_id"] == client_id else None
            weather_persistence.save_account({
                "client_id": client_id,
                "client_secret": client_secret,
                "access_token": keep.get("access_token") if keep else None,
                "refresh_token": keep.get("refresh_token") if keep else None,
                "expires_at": keep.get("expires_at") if keep else None,
                "connected_at": keep.get("connected_at") if keep else None,
            })

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        account = weather_persistence.load_account()
        if account is None:
            raise NetatmoError(409, "Enter the Netatmo app's client ID and secret first.")
        return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
            "client_id": account["client_id"],
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "state": state,
        })

    def _token_request(self, account: Mapping[str, Any], grant: Mapping[str, str]) -> Dict[str, Any]:
        status, answer = self._post(TOKEN_PATH, {
            **grant,
            "client_id": account["client_id"],
            "client_secret": account["client_secret"],
        })
        if status == 200 and answer and isinstance(answer.get("access_token"), str):
            refresh = answer.get("refresh_token")
            expires_in = _number(answer.get("expires_in")) or 10800.0
            scope = answer.get("scope")
            if isinstance(scope, list) and SCOPE not in scope:
                raise NetatmoError(
                    403, "That sign-in does not allow reading the weather station. "
                         "Choose read_station when connecting.",
                )
            return {
                **account,
                "access_token": answer["access_token"],
                "refresh_token": refresh if isinstance(refresh, str) and refresh
                else account.get("refresh_token"),
                "expires_at": self._clock() + expires_in,
            }
        error = answer.get("error") if answer else None
        if status == 429:
            raise WeatherUnavailable(
                "rate_limited", "Netatmo has asked this system to wait before trying again.",
            )
        if status >= 500 or answer is None:
            raise WeatherUnavailable(
                "unreachable", "Netatmo is not answering. It will be tried again shortly.",
            )
        if error == "invalid_client":
            raise NetatmoError(
                400, "Netatmo did not recognise the client ID and secret. Check them on dev.netatmo.com.",
            )
        raise NetatmoError(
            400, "Netatmo refused the sign-in. Connect again from Settings.",
        )

    def _connect_sync(self, grant: Mapping[str, str]) -> None:
        with self._lock:
            account = weather_persistence.load_account()
            if account is None:
                raise NetatmoError(409, "Enter the Netatmo app's client ID and secret first.")
            try:
                updated = self._token_request(account, grant)
            except WeatherUnavailable as exc:
                raise NetatmoError(503, str(exc)) from None
            updated["connected_at"] = self._clock()
            weather_persistence.save_account(updated)

    async def exchange_code(self, code: str, redirect_uri: str) -> None:
        """Finish the sign-in Netatmo's page sent the browser back with."""
        await asyncio.to_thread(self._connect_sync, {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
        })

    async def use_refresh_token(self, refresh_token: str) -> None:
        """A token generated on Netatmo's developer page, checked by using it once."""
        token = refresh_token.strip()
        if not token:
            raise NetatmoError(400, "Paste the refresh token.")
        await asyncio.to_thread(self._connect_sync, {
            "grant_type": "refresh_token", "refresh_token": token,
        })

    def disconnect(self) -> None:
        """Forget the tokens. The app's id and secret stay, to connect again.

        Netatmo has no endpoint to revoke a token; the user can remove the
        app's access from their Netatmo account, and Settings says so.
        """
        with self._lock:
            account = weather_persistence.load_account()
            if account is None:
                return
            weather_persistence.save_account({
                **account, "access_token": None, "refresh_token": None,
                "expires_at": None, "connected_at": None,
            })

    def forget(self) -> None:
        """Delete everything, the app's secret included."""
        with self._lock:
            weather_persistence.delete_account()

    # --- reading -----------------------------------------------------------

    def _fresh_account(self, force: bool = False) -> Dict[str, Any]:
        account = weather_persistence.load_account()
        if account is None:
            raise WeatherUnavailable(
                "not_configured", "Enter the Netatmo app's details under Settings.",
            )
        if not account.get("refresh_token"):
            raise WeatherUnavailable(
                "signed_out", "Connect the weather station to Netatmo under Settings.",
            )
        expires = account.get("expires_at") or 0.0
        if force or not account.get("access_token") or expires - REFRESH_MARGIN_SECONDS <= self._clock():
            try:
                account = self._token_request(account, {
                    "grant_type": "refresh_token", "refresh_token": account["refresh_token"],
                })
            except NetatmoError:
                # The refresh token is spent or revoked: only signing in again
                # helps. The tokens are dropped so nothing keeps trying them.
                weather_persistence.save_account({
                    **(weather_persistence.load_account() or account),
                    "access_token": None, "refresh_token": None, "expires_at": None,
                })
                raise WeatherUnavailable(
                    "signed_out", "Netatmo ended this system's access. Connect again under Settings.",
                ) from None
            # Written before it is used: Netatmo may have replaced the refresh
            # token, and the old one may no longer work.
            weather_persistence.save_account(account)
        return account

    def _stations(self, account: Mapping[str, Any]) -> Tuple[int, Optional[dict]]:
        return self._post(STATIONS_PATH, {"get_favorites": "false"}, {
            "Authorization": "Bearer " + account["access_token"],
        })

    def _read_sync(self) -> WeatherReading:
        with self._lock:
            account = self._fresh_account()
            status, answer = self._stations(account)
            code = None
            if answer and isinstance(answer.get("error"), dict):
                code = answer["error"].get("code")
            if status in (401, 403) and code in TOKEN_ERROR_CODES:
                # Expired early, or revoked: one refresh, then believe it.
                account = self._fresh_account(force=True)
                status, answer = self._stations(account)
                code = answer["error"].get("code") if answer and isinstance(
                    answer.get("error"), dict) else None
            if status == 200 and answer is not None:
                return parse_stations(answer, self._clock())
            if status == 429 or code == THROTTLED_ERROR_CODE:
                raise WeatherUnavailable(
                    "rate_limited", "Netatmo has asked this system to wait before reading again.",
                )
            if status in (401, 403):
                raise WeatherUnavailable(
                    "signed_out", "Netatmo refused to share the station. Connect again under Settings.",
                )
            raise WeatherUnavailable(
                "unreachable", "Netatmo is not answering. It will be tried again shortly.",
            )

    async def read(self) -> WeatherReading:
        return await asyncio.to_thread(self._read_sync)
