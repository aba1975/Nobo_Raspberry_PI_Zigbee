"""The weather forecast from MET Norway, and MET's terms of service.

Nothing here reaches the network: ``conftest`` makes ``forecast.http_get``
fail every test that calls it, and each test hands the service a fake
transport that records what would have been sent.

The terms tested are MET's own (api.met.no/doc/TermsOfService and the
Locationforecast HowTO): identify yourself, never ask again before Expires,
use If-Modified-Since exactly as Last-Modified was sent, add jitter, cut
coordinates to four decimals, back off at once on 429, log a 203, and credit
MET under CC BY 4.0.
"""

import asyncio
import gzip
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import auth
import forecast
import server

OSLO = ZoneInfo("Europe/Oslo")
APP = Path(__file__).resolve().parent.parent / "app"
CABIN = APP / "static" / "ui" / "cabin"
SJUSJOEN = {"name": "Sjusjøen", "detail": "Hamlet · Ringsaker, Innlandet",
            "lat": 61.15296, "lon": 10.69184, "altitude": 842}
# 21 October 2026, 11:00 UTC: 13:00 in Oslo.
NOW = datetime(2026, 10, 21, 11, 0, tzinfo=timezone.utc).timestamp()


def series(start: datetime, hourly: int = 60, six_hourly: int = 20) -> list:
    """Rows shaped like MET's compact answer: hourly, then every six hours."""
    rows = []
    at = start
    for index in range(hourly + six_hourly):
        step = 1 if index < hourly else 6
        if step == 6 and at.hour % 6:
            at += timedelta(hours=6 - at.hour % 6)
        details = {"air_temperature": round(2 + (index % 10) / 2, 1),
                   "wind_speed": 3.0 + index % 4, "wind_from_direction": 200.0}
        data = {"instant": {"details": details},
                "next_6_hours": {"summary": {"symbol_code": "rain"},
                                 "details": {"precipitation_amount": 6.0}}}
        if step == 1:
            data["next_1_hours"] = {"summary": {"symbol_code": "cloudy"},
                                    "details": {"precipitation_amount": 0.5}}
        rows.append({"time": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "data": data})
        at += timedelta(hours=step)
    return rows


def met_answer(start=None, updated="2026-10-21T10:30:00Z") -> bytes:
    start = start or datetime.fromtimestamp(NOW, timezone.utc)
    return json.dumps({"type": "Feature", "properties": {
        "meta": {"updated_at": updated}, "timeseries": series(start)}}).encode()


def http_date(stamp: float) -> str:
    return format_datetime(datetime.fromtimestamp(stamp, timezone.utc), usegmt=True)


class FakeMet:
    """Records every request; answers with whatever is queued next."""

    def __init__(self):
        self.requests = []
        self.queue = []

    def answer(self, status=200, body=None, headers=None, raises=None):
        self.queue.append((status, body, headers, raises))

    def __call__(self, url, headers):
        forecast.check_allowed(url)
        self.requests.append({"url": url, "headers": dict(headers)})
        if "ws.geonorge.no" in url:
            if "hoydedata" in url:
                return 200, {}, json.dumps({"punkter": [{"z": 841.85}]}).encode()
            return 200, {}, json.dumps({"navn": [
                {"skrivemåte": "Sjusjøen", "navneobjekttype": "Innsjø",
                 "kommuner": [{"kommunenavn": "Ringsaker"}], "fylker": [{"fylkesnavn": "Innlandet"}],
                 "representasjonspunkt": {"nord": 61.14, "øst": 10.70}},
                {"skrivemåte": "Sjusjøen", "navneobjekttype": "Grend",
                 "kommuner": [{"kommunenavn": "Ringsaker"}], "fylker": [{"fylkesnavn": "Innlandet"}],
                 "representasjonspunkt": {"nord": 61.152961234, "øst": 10.691849999}},
            ]}).encode()
        status, body, extra, raises = self.queue.pop(0) if self.queue else (200, None, None, None)
        if raises is not None:
            raise raises
        sent = self.now if hasattr(self, "now") else NOW
        found = {"date": http_date(sent), "expires": http_date(sent + 1800),
                 "last-modified": "Wed, 21 Oct 2026 10:31:00 GMT"}
        found.update(extra or {})
        return status, found, met_answer() if body is None and status in (200, 203) else (body or b"")


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def met():
    return FakeMet()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(monkeypatch, met, clock):
    value = forecast.ForecastService(transport=met, clock=clock, rng=lambda low, high: low)
    monkeypatch.setattr(server, "forecast_service", value)
    return value


@pytest.fixture
def client():
    with TestClient(server.app, base_url="https://testserver") as value:
        value.cookies.set("session_id", "pytest-fixed-session-id")
        yield value


def as_user(monkeypatch):
    original = auth.load_users

    def users():
        data = dict(original())
        data["admin"] = {**data["admin"], "role": "user"}
        return data

    monkeypatch.setattr(auth, "load_users", users)


def turn_on(client, **extra):
    response = client.put("/api/forecast/settings",
                          json={"enabled": True, "place": SJUSJOEN, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def settings(**extra):
    return forecast.ForecastSettings(
        enabled=True, place=forecast.parse_place(SJUSJOEN), **extra)


def run(coroutine):
    return asyncio.run(coroutine)


# -- only MET and Kartverket, only https -----------------------------------


@pytest.mark.parametrize("url", [
    "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=61&lon=10",
    "https://ws.geonorge.no/stedsnavn/v1/navn?sok=x",
    "https://ws.geonorge.no/hoydedata/v1/punkt?nord=61&ost=10",
])
def test_allowed_endpoints(url):
    forecast.check_allowed(url)


@pytest.mark.parametrize("url", [
    "http://api.met.no/weatherapi/locationforecast/2.0/compact?lat=61&lon=10",
    "https://api.met.no/weatherapi/locationforecast/2.0/complete?lat=61&lon=10",
    "https://api.met.no/weatherapi/nowcast/2.0/complete?lat=61&lon=10",
    "https://www.yr.no/api/v0/locations/search?q=x",
    "https://evil.example/weatherapi/locationforecast/2.0/compact",
    "https://api.met.no.evil.example/weatherapi/locationforecast/2.0/compact",
    "https://ws.geonorge.no/stedsnavn/v1/sted",
])
def test_everything_else_refused(url):
    with pytest.raises(forecast.ForbiddenEndpoint):
        forecast.check_allowed(url)


def test_a_redirect_elsewhere_is_refused():
    handler = forecast._CheckedRedirect()
    with pytest.raises(forecast.ForbiddenEndpoint):
        handler.redirect_request(None, None, 301, "Moved", {}, "https://evil.example/x")


def test_http_get_refuses_before_sending(monkeypatch, real_forecast_http_get):
    class NoNetwork:
        def open(self, *args, **kwargs):
            raise AssertionError("a request was sent")

    monkeypatch.setattr(forecast, "_opener", NoNetwork())
    for url in ("https://example.com/", "http://api.met.no/" + forecast.MET_PATH):
        with pytest.raises(forecast.ForbiddenEndpoint):
            real_forecast_http_get(url, {})


def test_gzip_answers_are_unpacked():
    assert forecast._decompress(gzip.compress(b"{}"), "gzip") == b"{}"
    assert forecast._decompress(b"{}", "") == b"{}"


def test_the_module_names_no_other_service():
    source = (APP / "forecast.py").read_text()
    hosts = set(re.findall(r'"([a-z0-9.-]+\.(?:no|com|org|net))"', source))
    assert hosts <= {"api.met.no", "ws.geonorge.no"}, hosts


# -- identification and the request itself ---------------------------------


def test_user_agent_identifies_the_application():
    agent = forecast.user_agent()
    assert agent.startswith("NoboControl/")
    assert "github.com/aba1975/Nobo_Raspberry_PI_Zigbee" in agent
    assert forecast.user_agent("me@example.com").endswith(" me@example.com")


def test_coordinates_are_cut_to_four_decimals_not_rounded():
    place = forecast.parse_place({"lat": 61.152969, "lon": 10.691849})
    assert (place.lat, place.lon) == (61.1529, 10.6918)
    url = forecast.met_url(place)
    assert "lat=61.1529" in url and "lon=10.6918" in url
    assert "altitude" not in url
    with_height = forecast.parse_place({"lat": 61.15, "lon": 10.69, "altitude": 841.85})
    assert "altitude=842" in forecast.met_url(with_height)


@pytest.mark.parametrize("payload", [
    {"lat": 91, "lon": 10}, {"lat": 61, "lon": 181}, {"lat": "x", "lon": 10},
    {"lat": True, "lon": 10}, {"lat": 61, "lon": 10, "altitude": 10000},
    {"lat": float("nan"), "lon": 10}, "Oslo",
])
def test_bad_places_are_refused(payload):
    with pytest.raises(forecast.InvalidForecastData):
        forecast.parse_place(payload)


def test_contact_must_be_an_email_address():
    assert forecast.parse_contact("") == ""
    assert forecast.parse_contact(" me@example.com ") == "me@example.com"
    for bad in ("me", "me@", "<me@example.com>", "a b@example.com"):
        with pytest.raises(forecast.InvalidForecastData):
            forecast.parse_contact(bad)


def test_first_request_carries_the_user_agent_and_no_if_modified_since(met):
    cache = forecast.fetch(settings(contact="me@example.com"), forecast.ForecastCache(), NOW, met,
                           rng=lambda low, high: low)
    sent = met.requests[0]
    assert sent["headers"]["User-Agent"] == forecast.user_agent("me@example.com")
    assert "If-Modified-Since" not in sent["headers"]
    assert cache.timeseries and cache.fetched_at == NOW
    assert cache.updated_at == "2026-10-21T10:30:00Z"
    assert cache.last_modified == "Wed, 21 Oct 2026 10:31:00 GMT"


def test_next_request_waits_for_expires_plus_jitter(met):
    cache = forecast.fetch(settings(), forecast.ForecastCache(), NOW, met, rng=lambda low, high: 120)
    assert cache.next_at == NOW + 1800 + 120


def test_lifetime_is_measured_on_mets_clock():
    # The Pi's clock is ten minutes slow; MET's Date and Expires decide.
    headers = {"date": http_date(NOW + 600), "expires": http_date(NOW + 600 + 2400)}
    assert forecast.lifetime(headers, NOW) == 2400
    assert forecast.lifetime({}, NOW) == forecast.DEFAULT_LIFETIME_SECONDS
    long = {"date": http_date(NOW), "expires": http_date(NOW + 48 * 3600)}
    assert forecast.lifetime(long, NOW) == forecast.MAX_LIFETIME_SECONDS


def test_never_sooner_than_the_minimum_even_if_expires_has_passed(met):
    met.answer(headers={"expires": http_date(NOW - 60)})
    cache = forecast.fetch(settings(), forecast.ForecastCache(), NOW, met, rng=lambda low, high: low)
    assert cache.next_at == NOW + forecast.MIN_INTERVAL_SECONDS


def test_if_modified_since_is_last_modified_exactly_and_304_keeps_the_copy(met):
    first = forecast.fetch(settings(), forecast.ForecastCache(), NOW, met, rng=lambda low, high: low)
    met.answer(304, b"", {"last-modified": "Wed, 21 Oct 2026 10:31:00 GMT"})
    later = NOW + 3600
    second = forecast.fetch(settings(), first, later, met, rng=lambda low, high: low)
    assert met.requests[1]["headers"]["If-Modified-Since"] == "Wed, 21 Oct 2026 10:31:00 GMT"
    assert second.timeseries == first.timeseries
    assert second.fetched_at == later and second.failure is None


def test_if_modified_since_is_not_sent_for_another_place(met):
    first = forecast.fetch(settings(), forecast.ForecastCache(), NOW, met, rng=lambda low, high: low)
    elsewhere = forecast.ForecastSettings(enabled=True, place=forecast.parse_place(
        {"name": "Oslo", "lat": 59.91, "lon": 10.75}))
    forecast.fetch(elsewhere, first, NOW + 3600, met, rng=lambda low, high: low)
    assert "If-Modified-Since" not in met.requests[1]["headers"]


def test_203_is_kept_logged_and_flagged(met, caplog):
    met.answer(203)
    cache = forecast.fetch(settings(), forecast.ForecastCache(), NOW, met, rng=lambda low, high: low)
    assert cache.deprecated and cache.timeseries
    assert "deprecated" in caplog.text


def test_429_backs_off_an_hour_then_doubles_to_a_day(met):
    cache = forecast.ForecastCache()
    waits = []
    now = NOW
    for _ in range(7):
        met.answer(429)
        cache = forecast.fetch(settings(), cache, now, met)
        waits.append(cache.next_at - now)
        now = cache.next_at
    assert waits[:5] == [3600, 7200, 14400, 28800, 57600]
    assert waits[5:] == [86400, 86400]
    assert cache.failure == "throttled"


def test_refused_waits_six_hours(met):
    met.answer(403)
    cache = forecast.fetch(settings(), forecast.ForecastCache(), NOW, met)
    assert cache.failure == "refused" and cache.next_at == NOW + 6 * 3600


def test_unreachable_backs_off_and_keeps_the_copy(met):
    good = forecast.fetch(settings(), forecast.ForecastCache(), NOW, met, rng=lambda low, high: low)
    met.answer(raises=OSError("down"))
    cache = forecast.fetch(settings(), good, NOW + 3600, met)
    assert cache.failure == "unreachable" and cache.timeseries == good.timeseries
    assert cache.next_at == NOW + 3600 + 600
    met.answer(503)
    cache = forecast.fetch(settings(), cache, NOW + 7200, met)
    assert cache.next_at == NOW + 7200 + 900 and cache.failures == 2


def test_an_unreadable_answer_is_not_stored(met):
    met.answer(200, b"<html>")
    cache = forecast.fetch(settings(), forecast.ForecastCache(), NOW, met)
    assert cache.failure == "bad_answer" and not cache.timeseries


# -- when a request is due -------------------------------------------------


def test_due_rules():
    on = settings()
    fresh = forecast.ForecastCache()
    assert forecast.due(on, fresh, NOW)
    assert not forecast.due(forecast.ForecastSettings(), fresh, NOW)
    assert not forecast.due(forecast.ForecastSettings(enabled=True), fresh, NOW)
    held = forecast.ForecastCache(place_key=forecast.place_key(on.place), timeseries=({},),
                                  asked_at=NOW, next_at=NOW + 1800)
    assert not forecast.due(on, held, NOW + 1799)
    assert forecast.due(on, held, NOW + 1800)


def test_never_twice_within_the_minimum_interval():
    on = settings()
    held = forecast.ForecastCache(place_key=forecast.place_key(on.place), asked_at=NOW, next_at=NOW)
    assert not forecast.due(on, held, NOW + forecast.MIN_INTERVAL_SECONDS - 1)


def test_a_new_place_is_asked_at_once_but_not_more_than_once_a_minute():
    on = settings()
    held = forecast.ForecastCache(place_key="1.0000,2.0000,", asked_at=NOW, next_at=NOW + 3600)
    assert not forecast.due(on, held, NOW + 59)
    assert forecast.due(on, held, NOW + 60)


@pytest.mark.parametrize("failure", ["throttled", "refused"])
def test_changing_place_does_not_escape_a_backoff(failure):
    on = settings()
    held = forecast.ForecastCache(place_key="1.0000,2.0000,", asked_at=NOW,
                                  next_at=NOW + 3600, failure=failure, failures=1)
    assert not forecast.due(on, held, NOW + 1800)


def test_turning_off_keeps_a_backoff_but_drops_the_copy(service, met):
    service.apply_settings(settings())
    met.answer(429)
    run(service.refresh_if_due())
    service.apply_settings(forecast.ForecastSettings())
    assert service.cache.failure == "throttled" and service.cache.next_at is not None
    service.apply_settings(settings())
    assert not service.due()


def test_turning_off_after_success_drops_everything(service):
    service.apply_settings(settings())
    run(service.refresh_if_due())
    service.apply_settings(forecast.ForecastSettings())
    assert service.cache.timeseries == () and service.cache.next_at is None
    assert service.cache.asked_at == NOW


# -- the service -----------------------------------------------------------


def test_refresh_asks_once_and_then_waits(service, met, clock):
    service.apply_settings(settings())
    assert run(service.refresh_if_due()) is True
    assert run(service.refresh_if_due()) is False
    clock.now += 1799
    assert run(service.refresh_if_due()) is False
    assert len(met.requests) == 1


def test_the_copy_survives_a_restart(service, met, clock):
    service.apply_settings(settings())
    run(service.refresh_if_due())
    again = forecast.ForecastService(transport=met, clock=clock)
    assert again.settings == service.settings
    assert again.cache == service.cache
    assert not again.due()


def test_a_damaged_cache_file_is_set_aside(tmp_path):
    forecast.CACHE_FILE.write_text("{not json")
    assert forecast.load_cache() == forecast.ForecastCache()
    assert forecast.CACHE_FILE.with_suffix(".backup").exists()


def test_an_answer_for_a_place_no_longer_chosen_is_discarded(service, met):
    service.apply_settings(settings())
    real_fetch = forecast.fetch

    def fetch_while_changing(*args):
        service.settings = forecast.ForecastSettings(enabled=True, place=forecast.parse_place(
            {"name": "Oslo", "lat": 59.91, "lon": 10.75}))
        return real_fetch(*args)

    import unittest.mock
    with unittest.mock.patch.object(forecast, "fetch", fetch_while_changing):
        assert run(service.refresh_if_due()) is False
    assert service.cache.timeseries == () and service.cache.asked_at == NOW


def test_status_and_payload(service, clock):
    service.apply_settings(settings())
    assert service.status()["state"] == "starting"
    run(service.refresh_if_due())
    body = service.payload(OSLO)
    assert body["state"] == "ok" and body["place"]["name"] == "Sjusjøen"
    assert body["fetched_at"] and body["stale"] is False
    clock.now += forecast.STALE_SECONDS + 1
    assert service.status()["stale"] is True


# -- days, periods and rain ------------------------------------------------


def summary(start=None, now=NOW):
    rows = series(start or datetime.fromtimestamp(NOW, timezone.utc))
    return forecast.summarise(tuple(rows), datetime.fromtimestamp(now, timezone.utc), OSLO)


def test_days_are_local_and_start_today():
    days = summary()["days"]
    assert days[0]["date"] == "2026-10-21"
    assert [day["date"] for day in days] == sorted(day["date"] for day in days)
    assert len(days) <= forecast.MAX_DAYS
    first_hour = days[0]["hours"][0]
    assert first_hour["time"] == "13:00" and first_hour["at"].endswith("+02:00")


def test_hourly_rain_is_not_counted_twice():
    today = summary()["days"][0]
    # 11 hourly rows today (13:00–23:00), half a millimetre each, ignoring the
    # six-hour totals that every hourly row also carries.
    assert len(today["hours"]) == 11
    assert today["precipitation"] == 5.5


def test_six_hour_rows_count_their_six_hours():
    days = summary()["days"]
    late = days[-1]
    assert {hour["step"] for hour in late["hours"]} == {6}
    assert late["precipitation"] == 6.0 * len(late["hours"])


def test_rows_already_over_are_dropped():
    two_hours_on = summary(now=NOW + 2 * 3600 + 1)
    assert two_hours_on["days"][0]["hours"][0]["time"] == "15:00"
    assert two_hours_on["now"]["at"].startswith("2026-10-21T15:00")


def test_periods_and_the_days_symbol():
    tomorrow = summary()["days"][1]
    names = [period and period["name"] for period in tomorrow["periods"]]
    assert names == ["night", "morning", "afternoon", "evening"]
    assert tomorrow["symbol"] == tomorrow["periods"][2]["symbol"]
    today = summary()["days"][0]
    assert today["periods"][0] is None and today["periods"][1] is None


def test_a_last_day_with_only_a_night_is_left_out():
    days = summary()["days"]
    for day in days[1:]:
        named = {period["name"] for period in day["periods"] if period}
        assert named & {"morning", "afternoon"}


def test_symbols_come_with_words():
    words = summary()["symbols"]
    assert words["rain"] == "Rain" and words["cloudy"] == "Cloudy"
    assert forecast.symbol_words("lightssleetshowersandthunder_day")
    assert forecast.symbol_words("<script>") == ""


def test_every_symbol_has_an_icon():
    icons = {path.stem for path in (CABIN / "wx").glob("*.svg")}
    for code in forecast.SYMBOL_WORDS:
        variants = [code] if code in icons else [f"{code}_day", f"{code}_night"]
        assert all(name in icons for name in variants), code


# -- places ----------------------------------------------------------------


def test_typed_coordinates_need_no_lookup(met):
    finder = forecast.PlaceFinder(met)
    found = finder.search("61.152969, 10.691849")
    assert found[0]["lat"] == 61.1529 and found[0]["lon"] == 10.6918
    assert met.requests == []
    assert forecast.parse_point("61.15 N, 10.69 Ø") == (61.15, 10.69)
    assert forecast.parse_point("33.9 S 18.4 E") == (-33.9, 18.4)
    assert forecast.parse_point("Sjusjøen") is None


def test_place_search_lists_settlements_first_and_remembers(met):
    times = iter([100.0, 100.5, 101.5, 102.0])
    finder = forecast.PlaceFinder(met, clock=lambda: next(times, 200.0))
    found = finder.search("Sjusjøen")
    assert [place["detail"].split(" · ")[0] for place in found] == ["Hamlet", "Lake"]
    assert found[0]["lat"] == 61.1529 and found[0]["lon"] == 10.6918
    assert "fuzzy=false" in met.requests[0]["url"]
    finder.search("sjusjøen")
    assert len(met.requests) == 1


def test_place_search_waits_a_second_between_requests(met, monkeypatch):
    slept = []
    monkeypatch.setattr(forecast.time, "sleep", slept.append)
    finder = forecast.PlaceFinder(met, clock=lambda: 50.0)
    finder._last = 49.7
    finder.search("Sjusjøen")
    assert slept and abs(slept[0] - 0.7) < 1e-9


def test_elevation_only_in_norway(met):
    finder = forecast.PlaceFinder(met)
    assert finder.elevation(61.1529, 10.6918) == 842
    assert finder.elevation(48.85, 2.35) is None
    assert len(met.requests) == 1


# -- the endpoints ---------------------------------------------------------


def test_off_by_default_and_absent(client, service, met):
    assert client.get("/api/status").json()["forecast"] is None
    assert client.get("/api/forecast").json() == {"enabled": False}
    assert client.get("/api/forecast/settings").json()["enabled"] is False
    assert met.requests == []


def test_turning_on_looks_up_the_height_and_saves(client, service, met):
    body = turn_on(client, place={**SJUSJOEN, "altitude": None}, contact="me@example.com")
    assert body["place"]["altitude"] == 842
    assert body["place"]["lat"] == 61.1529
    assert body["user_agent"].endswith("me@example.com")
    saved = json.loads(forecast.SETTINGS_FILE.read_text())
    assert saved["enabled"] is True and saved["schema_version"] == 1


def test_forecast_endpoint_fetches_once(client, service, met):
    turn_on(client)
    body = client.get("/api/forecast").json()
    assert body["enabled"] and body["state"] == "ok" and body["days"]
    assert body["refreshing"] is False
    client.get("/api/forecast")
    assert len([r for r in met.requests if "api.met.no" in r["url"]]) == 1
    brief = client.get("/api/status").json()["forecast"]
    assert brief["front_page"] == "top" and brief["fetched_at"]


def test_status_never_asks_met(client, service, met):
    turn_on(client)
    for _ in range(3):
        client.get("/api/status")
    assert met.requests == []


def test_settings_are_admin_only(client, service, monkeypatch):
    turn_on(client)
    as_user(monkeypatch)
    assert client.get("/api/forecast/settings").status_code == 403
    assert client.put("/api/forecast/settings", json={"enabled": False}).status_code == 403
    assert client.get("/api/forecast/places", params={"q": "x"}).status_code == 403
    assert client.get("/api/forecast").status_code == 200


def test_needs_a_session(service):
    with TestClient(server.app, base_url="https://testserver") as anonymous:
        assert anonymous.get("/api/forecast", follow_redirects=False).status_code in (302, 401)


@pytest.mark.parametrize("body", [
    {"front_page": "middle"}, {"contact": "nope"}, {"place": {"lat": 100, "lon": 10}},
])
def test_bad_settings_are_400(client, service, body):
    assert client.put("/api/forecast/settings", json=body).status_code == 400


def test_place_search_endpoint(client, service, met):
    found = client.get("/api/forecast/places", params={"q": "Sjusjøen"}).json()["places"]
    assert found[0]["name"] == "Sjusjøen"
    assert client.get("/api/forecast/places", params={"q": "x" * 201}).status_code == 400


def test_place_search_failure_is_502(client, service, monkeypatch):
    def down(url, headers):
        raise OSError("down")
    service.places = forecast.PlaceFinder(down)
    assert client.get("/api/forecast/places", params={"q": "Sjusjøen"}).status_code == 502


def test_front_page_choice(client, service):
    turn_on(client, front_page="bottom")
    assert client.get("/api/status").json()["forecast"]["front_page"] == "bottom"


# -- the cabin interface ---------------------------------------------------


def cabin_js():
    return (CABIN / "cabin.js").read_text()


def test_credit_and_licence_are_shown():
    source = cabin_js()
    assert "MET Norway" in source
    assert "creativecommons.org/licenses/by/4.0" in source
    assert "Kartverket" in source


def test_yr_is_not_named_or_linked():
    for path in (CABIN / "cabin.js", CABIN / "index.html", CABIN / "cabin.css",
                 APP / "forecast.py", APP / "static" / "ui" / "shared" / "core.js"):
        text = path.read_text()
        assert "yr.no" not in text.lower(), path.name
        assert not re.search(r"\bYr\b", text), path.name


def test_the_browser_asks_only_when_the_page_is_visible():
    source = cabin_js()
    start = source.index("async function loadForecast")
    body = source[start:start + 1500]
    assert "document.hidden" in body


def test_no_browser_code_talks_to_met_directly():
    for path in (CABIN / "cabin.js", APP / "static" / "ui" / "shared" / "core.js"):
        text = path.read_text()
        assert "api.met.no" not in text and "geonorge" not in text, path.name


def test_icons_are_plain_svgs_with_their_licence():
    assert (CABIN / "wx" / "LICENSE").exists()
    for path in (CABIN / "wx").glob("*.svg"):
        text = path.read_text()
        assert "<script" not in text.lower() and "href=\"http" not in text, path.name


def test_front_page_slots_start_hidden():
    html = (CABIN / "index.html").read_text()
    for slot in ("forecastTop", "forecastBottom"):
        tag = re.search(r'<section[^>]*id="%s"[^>]*>' % slot, html)
        assert tag, slot
        assert "hidden" in tag.group(0), slot
    assert html.index('id="forecastTop"') < html.index('id="forecastBottom"')


def test_settings_card_is_admin_only_and_wired():
    source = cabin_js()
    assert "${renderForecastSettingsCard(isAdmin)}" in source
    assert "wireForecastSettings(root);" in source
    start = source.index("function renderForecastSettingsCard(isAdmin)")
    assert "if (!isAdmin" in source[start:start + 200]


def test_place_search_runs_on_a_press_never_while_typing():
    source = cabin_js()
    start = source.index("function wireForecastSettings(root)")
    body = source[start:source.index("\n  }\n", start)]
    assert "forecastPlaces(" in body
    assert "onsubmit" in body
    assert "oninput" not in body and "'input'" not in body and "keyup" not in body


def test_place_results_are_unwrapped_exactly_once():
    # GET /api/forecast/places answers {"places": [...]}; core.js unwraps it,
    # so cabin.js must take the list as it comes.
    core = (APP / "static" / "ui" / "shared" / "core.js").read_text()
    start = core.index("forecastPlaces:")
    assert ".then(r => r.places || [])" in core[start:start + 200]
    source = cabin_js()
    assert "results = await Nobo.api.forecastPlaces(query);" in source
    assert ".places" not in source[source.index("function wireForecastSettings"):]\
        .split("\n  }\n", 1)[0]
