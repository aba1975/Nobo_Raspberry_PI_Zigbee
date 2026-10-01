"""The Netatmo account: what is called, what is kept, and what is never said.

Nothing here reaches Netatmo. The account is handed a fake transport that
records each request and answers from a script; conftest makes the real
``http_post`` fail any test that gets past it.
"""

import asyncio
import json
import logging
import stat

import pytest

import weather_netatmo
import weather_persistence
from weather_netatmo import (
    ForbiddenEndpoint, NetatmoAccount, NetatmoError, check_allowed, mask, parse_stations,
)
from weather_provider import WeatherUnavailable

CLIENT_ID = "5f0c0ffee0123456789abcde"
CLIENT_SECRET = "s3cr3t-client-value-xyz"
NOW = 1_800_000_000.0
# Taken at import, before conftest's guard replaces it for each test.
REAL_HTTP_POST = weather_netatmo.http_post


def station_payload(now=NOW, outdoor_temp=-3.4, outdoor_battery=64, indoor_reachable=True):
    """Shaped like Netatmo's getstationsdata answer, as pyatmo's fixture is."""
    return {
        "status": "ok",
        "body": {
            "devices": [{
                "_id": "70:ee:50:22:a3:00",
                "type": "NAMain",
                "station_name": "Mostugu",
                "module_name": "Stue",
                "reachable": True,
                "dashboard_data": {
                    "time_utc": now - 120, "Temperature": 21.5, "Humidity": 41,
                    "CO2": 612, "Pressure": 1009.4, "AbsolutePressure": 960.1,
                    "min_temp": 20.1, "max_temp": 22.0, "temp_trend": "stable",
                    "pressure_trend": "down",
                },
                "modules": [
                    {
                        "_id": "02:00:00:22:a3:01", "type": "NAModule1", "module_name": "Ute",
                        "battery_percent": outdoor_battery, "reachable": True,
                        "dashboard_data": {
                            "time_utc": now - 200, "Temperature": outdoor_temp, "Humidity": 88,
                            "min_temp": -6.0, "max_temp": 1.5, "temp_trend": "down",
                        },
                    },
                    {
                        "_id": "03:00:00:05:aa:02", "type": "NAModule4", "module_name": "Teknisk rom",
                        "battery_percent": 15, "reachable": indoor_reachable,
                        "dashboard_data": {
                            "time_utc": now - 300, "Temperature": 12.0, "Humidity": 55,
                            "CO2": 480, "min_temp": 11.5, "max_temp": 12.4,
                        } if indoor_reachable else {},
                    },
                    {"_id": "05:00:00:00:00:09", "type": "NAModule3", "module_name": "Regn",
                     "battery_percent": 90, "reachable": True,
                     "dashboard_data": {"time_utc": now - 100, "Rain": 0}},
                    {"_id": "ignored", "type": "NACamera"},
                ],
            }],
        },
    }


class FakeNetatmo:
    """Answers the token and station endpoints from a script, and records each call."""

    def __init__(self):
        self.calls = []
        self.token_answers = []
        self.station_answers = []
        self.issued = 0

    def issue(self, rotate=True):
        self.issued += 1
        answer = {"access_token": f"access-{self.issued}", "expires_in": 10800,
                  "scope": ["read_station"]}
        if rotate:
            answer["refresh_token"] = f"refresh-{self.issued}"
        return 200, answer

    def __call__(self, url, form, headers):
        check_allowed(url)
        self.calls.append((url, dict(form), dict(headers)))
        if url.endswith("oauth2/token"):
            if self.token_answers:
                return self.token_answers.pop(0)
            return self.issue()
        if self.station_answers:
            return self.station_answers.pop(0)
        return 200, station_payload()


@pytest.fixture
def fake():
    return FakeNetatmo()


@pytest.fixture
def clock():
    class Clock:
        now = NOW

        def __call__(self):
            return self.now

    return Clock()


@pytest.fixture
def account(fake, clock):
    return NetatmoAccount(transport=fake, clock=clock)


def connect(account):
    account.set_app(CLIENT_ID, CLIENT_SECRET)
    asyncio.run(account.exchange_code("the-code", "https://nobo.example/api/weather/netatmo/callback"))


# -- only Netatmo, and only two endpoints -------------------------------------


@pytest.mark.parametrize("url", [
    "https://api.netatmo.com/oauth2/token",
    "https://api.netatmo.com/api/getstationsdata",
])
def test_the_two_endpoints_are_allowed(url):
    check_allowed(url)


@pytest.mark.parametrize("url", [
    "http://api.netatmo.com/oauth2/token",
    "https://api.netatmo.com/api/setthermmode",
    "https://api.netatmo.com/api/homesdata",
    "https://evil.example/oauth2/token",
    "https://api.netatmo.com.evil.example/oauth2/token",
])
def test_anything_else_is_refused(url):
    with pytest.raises(ForbiddenEndpoint):
        check_allowed(url)


def test_the_real_transport_refuses_before_connecting(monkeypatch):
    monkeypatch.setattr(weather_netatmo.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must not connect"))
    with pytest.raises(ForbiddenEndpoint):
        REAL_HTTP_POST("https://api.netatmo.com/api/setroomthermpoint", {}, {})


def test_sign_in_asks_only_to_read_the_station(account):
    account.set_app(CLIENT_ID, CLIENT_SECRET)
    url = account.authorize_url("https://nobo.example/api/weather/netatmo/callback", "abc")
    assert url.startswith("https://api.netatmo.com/oauth2/authorize?")
    assert "scope=read_station" in url
    assert "state=abc" in url
    assert CLIENT_SECRET not in url


# -- the station's answer --------------------------------------------------


def test_parse_reads_the_base_the_outdoor_and_the_indoor_module():
    reading = parse_stations(station_payload(), NOW)
    assert reading.station_name == "Mostugu"
    base = reading.first("base")
    assert base.name == "Stue" and base.pressure == 1009.4 and base.co2 == 612
    assert base.battery is None
    outdoor = reading.first("outdoor")
    assert outdoor.temperature == -3.4 and outdoor.humidity == 88
    assert outdoor.min_temperature == -6.0 and outdoor.max_temperature == 1.5
    assert outdoor.battery == 64 and outdoor.temperature_trend == "down"
    assert outdoor.reported_at == NOW - 200
    indoor = reading.module("03:00:00:05:aa:02")
    assert indoor.kind == "indoor" and indoor.name == "Teknisk rom" and indoor.co2 == 480
    assert reading.first("rain") is not None
    assert reading.module("ignored") is None


def test_an_unreachable_module_has_no_reading():
    reading = parse_stations(station_payload(indoor_reachable=False), NOW)
    indoor = reading.module("03:00:00:05:aa:02")
    assert indoor.reachable is False
    assert indoor.temperature is None


@pytest.mark.parametrize("payload", [None, {}, {"body": {}}, {"body": {"devices": []}}])
def test_an_account_without_a_station_says_so(payload):
    with pytest.raises(WeatherUnavailable) as raised:
        parse_stations(payload, NOW)
    assert raised.value.kind == "not_configured"


# -- tokens ----------------------------------------------------------------


def test_connecting_keeps_the_tokens_privately(account, fake):
    connect(account)
    path = weather_persistence.NETATMO_ACCOUNT_FILE
    saved = json.loads(path.read_text())
    assert saved["refresh_token"] == "refresh-1"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    state = account.public_state()
    assert state["connected"] is True
    assert CLIENT_ID not in json.dumps(state)
    assert state["client_id"] == mask(CLIENT_ID)
    url, form, _ = fake.calls[0]
    assert form["grant_type"] == "authorization_code"
    assert form["redirect_uri"].endswith("/api/weather/netatmo/callback")


def test_a_read_uses_the_access_token_in_a_header(account, fake):
    connect(account)
    reading = asyncio.run(account.read())
    assert reading.first("outdoor").temperature == -3.4
    url, form, headers = fake.calls[-1]
    assert url.endswith("api/getstationsdata")
    assert headers["Authorization"] == "Bearer access-1"
    assert "access-1" not in json.dumps(form)


def test_an_expiring_token_is_refreshed_and_the_new_refresh_token_saved_first(account, fake, clock):
    connect(account)
    clock.now = NOW + 10800 - 60
    saved_before_read = []
    original = fake.__call__

    def watching(url, form, headers):
        if url.endswith("getstationsdata"):
            saved_before_read.append(
                json.loads(weather_persistence.NETATMO_ACCOUNT_FILE.read_text())["refresh_token"]
            )
        return original(url, form, headers)

    account._transport = watching
    asyncio.run(account.read())
    refresh = [form for url, form, _ in fake.calls if form.get("grant_type") == "refresh_token"]
    assert refresh and refresh[0]["refresh_token"] == "refresh-1"
    assert saved_before_read == ["refresh-2"]
    assert fake.calls[-1][2]["Authorization"] == "Bearer access-2"


def test_a_refresh_without_rotation_keeps_the_old_refresh_token(account, fake, clock):
    connect(account)
    clock.now = NOW + 20000
    fake.token_answers.append(fake.issue(rotate=False))
    asyncio.run(account.read())
    saved = json.loads(weather_persistence.NETATMO_ACCOUNT_FILE.read_text())
    assert saved["refresh_token"] == "refresh-1"
    assert saved["access_token"] == "access-2"


def test_an_expired_token_answer_is_refreshed_once(account, fake):
    connect(account)
    fake.station_answers.append((403, {"error": {"code": 3, "message": "Access token expired"}}))
    reading = asyncio.run(account.read())
    assert reading.station_name == "Mostugu"
    grants = [form.get("grant_type") for url, form, _ in fake.calls if url.endswith("token")]
    assert grants == ["authorization_code", "refresh_token"]


def test_a_revoked_refresh_token_signs_out_and_forgets_the_tokens(account, fake, clock):
    connect(account)
    clock.now = NOW + 20000
    fake.token_answers.append((400, {"error": "invalid_grant"}))
    with pytest.raises(WeatherUnavailable) as raised:
        asyncio.run(account.read())
    assert raised.value.kind == "signed_out"
    saved = json.loads(weather_persistence.NETATMO_ACCOUNT_FILE.read_text())
    assert saved["refresh_token"] is None and saved["access_token"] is None
    assert saved["client_id"] == CLIENT_ID
    # And it does not keep trying a token it no longer has.
    calls = len(fake.calls)
    with pytest.raises(WeatherUnavailable):
        asyncio.run(account.read())
    assert len(fake.calls) == calls


@pytest.mark.parametrize("answer", [
    (429, None),
    (403, {"error": {"code": 26, "message": "User usage reached"}}),
])
def test_being_throttled_is_rate_limited(account, fake, answer):
    connect(account)
    fake.station_answers.append(answer)
    with pytest.raises(WeatherUnavailable) as raised:
        asyncio.run(account.read())
    assert raised.value.kind == "rate_limited"


def test_no_answer_at_all_is_unreachable(account, fake):
    connect(account)

    def broken(url, form, headers):
        raise OSError(f"connection refused while sending {form}")

    account._transport = broken
    with pytest.raises(WeatherUnavailable) as raised:
        asyncio.run(account.read())
    assert raised.value.kind == "unreachable"
    assert "refresh" not in str(raised.value)


def test_not_set_up_and_not_connected_say_which(account):
    with pytest.raises(WeatherUnavailable) as raised:
        asyncio.run(account.read())
    assert raised.value.kind == "not_configured"
    account.set_app(CLIENT_ID, CLIENT_SECRET)
    with pytest.raises(WeatherUnavailable) as raised:
        asyncio.run(account.read())
    assert raised.value.kind == "signed_out"


def test_a_wrong_client_secret_is_explained(account, fake):
    account.set_app(CLIENT_ID, CLIENT_SECRET)
    fake.token_answers.append((400, {"error": "invalid_client"}))
    with pytest.raises(NetatmoError) as raised:
        asyncio.run(account.exchange_code("c", "https://x/cb"))
    assert "client ID and secret" in str(raised.value)


def test_a_pasted_refresh_token_is_checked_by_using_it(account, fake):
    account.set_app(CLIENT_ID, CLIENT_SECRET)
    asyncio.run(account.use_refresh_token("  pasted-token  "))
    assert fake.calls[0][1] == {
        "grant_type": "refresh_token", "refresh_token": "pasted-token",
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
    }
    assert account.public_state()["connected"] is True


def test_disconnect_keeps_the_app_and_forget_deletes_everything(account):
    connect(account)
    account.disconnect()
    state = account.public_state()
    assert state["app_configured"] is True and state["connected"] is False
    account.forget()
    assert not weather_persistence.NETATMO_ACCOUNT_FILE.exists()
    assert account.public_state()["app_configured"] is False


def test_a_different_app_starts_unconnected(account):
    connect(account)
    account.set_app("another-client-id-0000", "another-secret")
    assert account.public_state()["connected"] is False


def test_secrets_and_tokens_never_reach_the_log(account, fake, clock, caplog):
    caplog.set_level(logging.DEBUG)
    connect(account)
    clock.now = NOW + 20000
    asyncio.run(account.read())
    fake.token_answers.append((400, {"error": "invalid_grant"}))
    clock.now = NOW + 50000
    with pytest.raises(WeatherUnavailable):
        asyncio.run(account.read())
    text = caplog.text
    for secret in (CLIENT_SECRET, "access-1", "access-2", "refresh-1", "refresh-2", "the-code"):
        assert secret not in text


def test_a_damaged_account_file_is_deleted(account):
    connect(account)
    weather_persistence.NETATMO_ACCOUNT_FILE.write_text("{not json")
    assert account.public_state()["app_configured"] is False
    assert not weather_persistence.NETATMO_ACCOUNT_FILE.exists()
