"""The Verisure account: signing in, staying signed in, and never writing.

Nothing here reaches Verisure. A fake stands in for vsure's Session with the
same shape — the same method names and the same exceptions — so what is
tested is this application's handling of it: what is stored, what is
forgotten, and what can never be sent.
"""

import asyncio
import json
import os
import re
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import verisure

import alarm_persistence
import alarm_verisure
from alarm_provider import AlarmUnavailable
from alarm_verisure import (
    ReadOnlyViolation, VerisureAlarm, VerisureError, check_read_only, mask_email,
)

run = asyncio.run

PASSWORD = "correct horse battery staple"

INSTALLATIONS = {"data": {"account": {"installations": [
    {"giid": "111", "alias": "Mostugu", "address": {"street": "Somewhere 1"}},
]}}}
TWO_INSTALLATIONS = {"data": {"account": {"installations": [
    {"giid": "111", "alias": "Mostugu"}, {"giid": "222", "alias": "Cabin"},
]}}}

READING = [
    {"data": {"installation": {"armState": {
        "statusType": "ARMED_AWAY", "date": "2026-09-30T08:00:00.000Z",
        "changedVia": "CODE", "name": "Somebody",
    }}}},
    {"data": {"installation": {"smartLocks": [{
        "device": {"deviceLabel": "ABCD", "area": "Front door"},
        "lockStatus": "LOCKED", "lockMethod": "CODE",
        "eventTime": "2026-09-30T08:01:00.000Z",
    }]}}},
]


class FakeSession:
    """vsure's Session, as far as this application uses it."""

    instances = []
    mfa = True
    login_error = None
    code_error = None
    read_error = None
    refresh_error = None
    trusted_error = None
    trusted_answer = "{}"
    answer = READING

    def __init__(self, username, password, cookie_file_name=None):
        self._username = username
        self._password = password
        self._cookie_file_name = cookie_file_name
        self._cookies = None
        self._trust_token = None
        self._mfa_login_pending = False
        self._giid = None
        self.calls = []
        FakeSession.instances.append(self)

    def _pickle(self):
        # vsure writes a pickle beside every step; this proves it is removed.
        Path(self._cookie_file_name).write_bytes(b"pickle")

    def login(self):
        self.calls.append("login")
        if self.login_error:
            raise self.login_error
        if self.mfa:
            self._mfa_login_pending = True
            raise verisure.LoginError("Multifactor authentication enabled")
        self._cookies = {"vid": "session", "vs-refresh": "refresh"}
        self._pickle()
        return INSTALLATIONS

    def request_mfa(self):
        self.calls.append("request_mfa")

    def validate_mfa(self, code):
        self.calls.append(("validate_mfa", code))
        if self.code_error:
            raise self.code_error
        self._cookies = {"vid": "session", "vs-refresh": "refresh", "vs-trust": "t"}
        self._trust_token = {"trustTokenValue": "trust-me"}
        self._pickle()
        return INSTALLATIONS

    def _post(self, url, headers=None, auth=None, cookies=None):
        """The trusted sign-in after the code: vsure's /auth/login with the
        password and the trust cookie, as Home Assistant signs in."""
        self.calls.append(("post", url, auth == (self._username, PASSWORD),
                           sorted(dict(cookies.items()))))
        if self.trusted_error:
            raise self.trusted_error
        return SimpleNamespace(
            text=self.trusted_answer,
            cookies={"vid": "trusted", "vs-access": "a", "vs-refresh": "trusted-refresh"},
        )

    def get_installations(self):
        self.calls.append("get_installations")
        return INSTALLATIONS

    def set_giid(self, giid):
        self._giid = giid

    def update_cookie(self):
        self.calls.append("update_cookie")
        if self.refresh_error:
            raise self.refresh_error
        self._cookies.set("vid", "fresh")
        self._pickle()

    def arm_state(self):
        return {"operationName": "ArmState", "query": "query ArmState { }"}

    def smart_lock(self):
        return {"operationName": "SmartLock", "query": "query SmartLock { }"}

    def door_window(self):
        return {"operationName": "DoorWindow", "query": "query DoorWindow { }"}

    def climate(self):
        return {"operationName": "Climate", "query": "query Climate { }"}

    def request(self, *operations):
        for operation in operations:
            check_read_only(operation)
        self.calls.append(("request", tuple(op["operationName"] for op in operations)))
        if self.read_error:
            raise self.read_error
        return self.answer

    def logout(self):
        self.calls.append("logout")


@pytest.fixture(autouse=True)
def fresh_fake():
    FakeSession.instances = []
    FakeSession.mfa = True
    FakeSession.login_error = None
    FakeSession.code_error = None
    FakeSession.read_error = None
    FakeSession.refresh_error = None
    FakeSession.answer = READING
    yield


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def account(clock):
    return VerisureAlarm(session_factory=FakeSession, clock=clock)


def _stored_text():
    return alarm_persistence.VERISURE_SESSION_FILE.read_text(encoding="utf-8")


def _sign_in(account):
    assert run(account.begin_login("anders@example.com", PASSWORD)) == "code_sent"
    assert run(account.submit_code("123456")) == "signed_in"


# -- signing in ---------------------------------------------------------------


def test_signing_in_with_a_code_stores_the_session_and_not_the_password(account):
    _sign_in(account)

    text = _stored_text()
    assert PASSWORD not in text
    stored = json.loads(text)
    assert stored["email"] == "anders@example.com"
    assert stored["giid"] == "111", "one installation is chosen by itself"
    assert stored["trust_token"] == "trust-me"
    assert stored["cookies"]["vs-refresh"] == "trusted-refresh"
    assert stored["cookies"]["vs-trust"] == "t"
    assert "password" not in stored
    session = FakeSession.instances[0]
    assert session._password == "", "the password must not outlive the sign-in"


def test_after_the_code_it_signs_in_once_more_with_the_trust(account):
    """The session the code step returns was refused at its first refresh on
    the real account. Home Assistant keeps the one a trusted sign-in gives."""
    _sign_in(account)
    session = FakeSession.instances[0]
    assert session.calls == [
        "login", "request_mfa", ("validate_mfa", "123456"),
        ("post", "/auth/login", True, ["vs-trust"]),
        "get_installations",
    ]


def test_if_the_trusted_sign_in_fails_the_codes_session_is_kept(account):
    FakeSession.trusted_error = verisure.RateLimitError("slow down")
    try:
        _sign_in(account)
    finally:
        FakeSession.trusted_error = None
    stored = json.loads(_stored_text())
    assert stored["cookies"]["vs-refresh"] == "refresh"
    assert FakeSession.instances[0]._password == ""


def test_a_trust_that_is_not_accepted_keeps_the_codes_session(account):
    FakeSession.trusted_answer = '{"stepUpToken": "again"}'
    try:
        _sign_in(account)
    finally:
        FakeSession.trusted_answer = "{}"
    assert json.loads(_stored_text())["cookies"]["vs-refresh"] == "refresh"


def test_signing_in_again_remembers_which_installation(account, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "INSTALLATIONS", TWO_INSTALLATIONS)
    _sign_in(account)
    assert json.loads(_stored_text())["giid"] is None, "two houses: the user chooses"
    account.choose_installation("222")
    # Verisure ends the sign-in; the file keeps the choice, without cookies.
    stored = json.loads(_stored_text())
    alarm_persistence.save_session({**stored, "cookies": {}, "trust_token": None})
    FakeSession.instances.clear()
    _sign_in(account)
    assert json.loads(_stored_text())["giid"] == "222"


def test_a_different_account_does_not_inherit_the_choice(account, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "INSTALLATIONS", TWO_INSTALLATIONS)
    _sign_in(account)
    account.choose_installation("222")
    stored = json.loads(_stored_text())
    alarm_persistence.save_session({**stored, "email": "someone@else.no", "cookies": {}})
    _sign_in(account)
    assert json.loads(_stored_text())["giid"] is None


def test_an_account_without_a_code_signs_straight_in(account):
    FakeSession.mfa = False
    assert run(account.begin_login("anders@example.com", PASSWORD)) == "signed_in"
    assert PASSWORD not in _stored_text()


def test_the_session_file_is_private(account):
    _sign_in(account)
    folder = alarm_persistence.VERISURE_DIR
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(alarm_persistence.VERISURE_SESSION_FILE).st_mode) == 0o600


def test_vsures_pickle_never_stays_on_disk(account):
    """vsure writes its cookies as a pickle after every step. Nothing may be
    left for anything to load later."""
    _sign_in(account)
    run(account.read())
    assert not (alarm_persistence.VERISURE_DIR / "vsure.cookie").exists()


def test_what_settings_shows_hides_the_address(account):
    _sign_in(account)
    shown = account.public_state()
    assert shown["signed_in"] is True
    assert shown["email"] == "a***@example.com"
    assert shown["installation"] == "Mostugu"
    assert "trust" not in json.dumps(shown).lower()
    assert "refresh" not in json.dumps(shown)


def test_mask_email():
    assert mask_email("someone@example.no") == "s***@example.no"
    assert mask_email(None) is None
    assert mask_email("nodomain") == "***"


def test_a_wrong_password_is_our_own_words(account):
    FakeSession.login_error = verisure.LoginError("server said: account 1234 locked")
    with pytest.raises(VerisureError) as caught:
        run(account.begin_login("anders@example.com", PASSWORD))
    assert caught.value.status == 401
    assert "1234" not in str(caught.value), "Verisure's text is never passed on"
    assert not alarm_persistence.VERISURE_SESSION_FILE.exists()


def test_a_wrong_code(account):
    run(account.begin_login("anders@example.com", PASSWORD))
    FakeSession.code_error = verisure.LoginError("bad code")
    with pytest.raises(VerisureError) as caught:
        run(account.submit_code("000000"))
    assert caught.value.status == 401


def test_a_pending_sign_in_expires(account, clock):
    run(account.begin_login("anders@example.com", PASSWORD))
    clock.now += alarm_verisure.PENDING_LOGIN_SECONDS + 1
    with pytest.raises(VerisureError) as caught:
        run(account.submit_code("123456"))
    assert caught.value.status == 409


def test_a_code_without_a_sign_in_is_refused(account):
    with pytest.raises(VerisureError) as caught:
        run(account.submit_code("123456"))
    assert caught.value.status == 409


@pytest.mark.parametrize("code", ["12", "abcdef", "123456789", ""])
def test_only_digits_are_accepted_as_a_code(account, code):
    with pytest.raises(VerisureError) as caught:
        run(account.submit_code(code))
    assert caught.value.status == 400


def test_sign_in_attempts_are_limited(account, clock):
    """A locked Verisure account is far worse than a ten-minute wait."""
    FakeSession.login_error = verisure.LoginError("no")
    for _ in range(alarm_verisure.LOGIN_ATTEMPTS):
        with pytest.raises(VerisureError):
            run(account.begin_login("anders@example.com", "wrong"))
    with pytest.raises(VerisureError) as caught:
        run(account.begin_login("anders@example.com", PASSWORD))
    assert caught.value.status == 429
    calls = len(FakeSession.instances)
    assert calls == alarm_verisure.LOGIN_ATTEMPTS, "the refused attempt never reached Verisure"
    clock.now += alarm_verisure.LOGIN_WINDOW_SECONDS + 1
    FakeSession.login_error = None
    assert run(account.begin_login("anders@example.com", PASSWORD)) == "code_sent"


def test_verisure_rate_limit_on_sign_in(account):
    FakeSession.login_error = verisure.RateLimitError("slow down")
    with pytest.raises(VerisureError) as caught:
        run(account.begin_login("anders@example.com", PASSWORD))
    assert caught.value.status == 429


# -- reading ------------------------------------------------------------------


def test_reading_the_alarm(account):
    _sign_in(account)
    reading = run(account.read())
    assert reading.arm_state == "armed_away"
    assert reading.arm_changed_at == "2026-09-30T08:00:00.000Z"
    (lock,) = reading.locks
    assert (lock.name, lock.locked, lock.method) == ("Front door", True, "code")


def test_reading_before_signing_in_is_not_a_fault(account):
    with pytest.raises(AlarmUnavailable) as caught:
        run(account.read())
    assert caught.value.kind == "not_configured"


def test_the_cookie_is_refreshed_before_it_lapses(account, clock):
    _sign_in(account)
    clock.now += alarm_verisure.COOKIE_REFRESH_SECONDS
    run(account.read())
    assert "update_cookie" in FakeSession.instances[-1].calls
    assert json.loads(_stored_text())["cookies"]["vid"] == "fresh"


def test_a_lapsed_sign_in_forgets_its_tokens(account, clock):
    _sign_in(account)
    clock.now += alarm_verisure.COOKIE_REFRESH_SECONDS
    FakeSession.refresh_error = verisure.AuthenticationError("expired", 401)
    with pytest.raises(AlarmUnavailable) as caught:
        run(account.read())
    assert caught.value.kind == "signed_out"
    stored = json.loads(_stored_text())
    assert stored["cookies"] == {} and stored["trust_token"] is None
    assert account.public_state()["signed_in"] is False
    with pytest.raises(AlarmUnavailable) as again:
        run(account.read())
    assert again.value.kind == "signed_out"


def test_rate_limited_reads(account):
    _sign_in(account)
    FakeSession.read_error = verisure.RateLimitError("slow")
    with pytest.raises(AlarmUnavailable) as caught:
        run(account.read())
    assert caught.value.kind == "rate_limited"


def test_an_outage_says_nothing_of_verisures_own(account):
    _sign_in(account)
    FakeSession.read_error = verisure.ResponseError(503, "internal: user anders@example.com")
    with pytest.raises(AlarmUnavailable) as caught:
        run(account.read())
    assert caught.value.kind == "unreachable"
    assert "anders" not in str(caught.value)


def test_an_answer_that_cannot_be_read_is_retried_once_after_a_refresh(account):
    _sign_in(account)
    FakeSession.answer = [{"data": {}}]
    with pytest.raises(AlarmUnavailable) as caught:
        run(account.read())
    assert caught.value.kind == "unreachable"
    calls = FakeSession.instances[-1].calls
    assert calls.count("update_cookie") == 1
    assert sum(1 for c in calls if isinstance(c, tuple) and c[0] == "request") == 2


# -- signing out --------------------------------------------------------------


def test_signing_out_revokes_and_deletes(account):
    _sign_in(account)
    run(account.sign_out())
    assert "logout" in FakeSession.instances[-1].calls
    assert not alarm_persistence.VERISURE_SESSION_FILE.exists()
    assert account.public_state()["signed_in"] is False


def test_the_file_is_deleted_even_if_verisure_cannot_be_reached(account, monkeypatch):
    _sign_in(account)

    def unreachable(self):
        raise verisure.RequestError("down")

    monkeypatch.setattr(FakeSession, "logout", unreachable)
    run(account.sign_out())
    assert not alarm_persistence.VERISURE_SESSION_FILE.exists()


def test_a_damaged_session_file_is_deleted_not_kept():
    """A .backup of a token file is one more copy of the tokens."""
    alarm_persistence.secure_dir()
    alarm_persistence.VERISURE_SESSION_FILE.write_text('{"schema_version": 1}')
    assert alarm_persistence.load_session() is None
    assert not alarm_persistence.VERISURE_SESSION_FILE.exists()
    assert not alarm_persistence.VERISURE_SESSION_FILE.with_suffix(".backup").exists()


# -- read-only ----------------------------------------------------------------


def test_the_real_session_refuses_anything_but_the_three_queries():
    session_class = alarm_verisure.read_only_session_class()
    session = session_class("a@example.com", "", cookie_file_name="/nonexistent/x")
    session._giid = "111"
    for command in (
        session.arm_away("0000"), session.disarm("0000"), session.arm_home("0000"),
        session.door_lock("ABCD", "0000"), session.door_unlock("ABCD", "0000"),
    ):
        with pytest.raises(ReadOnlyViolation):
            session.request(command)
    for query in (session.arm_state(), session.smart_lock(), session.fetch_all_installations()):
        check_read_only(query)


def test_the_real_session_never_loads_a_pickle(tmp_path):
    scratch = tmp_path / "cookie"
    scratch.write_bytes(b"not a pickle anyone should load")
    session = alarm_verisure.read_only_session_class()("a@example.com", "",
                                                       cookie_file_name=str(scratch))
    with pytest.raises(verisure.CookieReadError):
        session._load_cookie_file_into_memory()


@pytest.mark.parametrize("operation", [
    {"operationName": "ArmState", "query": "mutation ArmState { disarm }"},
    {"operationName": "armAway", "query": "query armAway { }"},
    "not a dict",
    {"operationName": "SmartLock"},
])
def test_the_guard_rejects_anything_dressed_up(operation):
    with pytest.raises(ReadOnlyViolation):
        check_read_only(operation)


def test_no_command_is_named_anywhere_in_the_application():
    """The guard stops a command being sent; this stops one being written."""
    app = Path(alarm_verisure.__file__).resolve().parent
    forbidden = re.compile(
        r"\.(arm_away|arm_home|disarm|door_lock|door_unlock|set_autolock_enabled|"
        r"set_smartplug|door_lock_configuration|set_arm_state)\s*\("
    )
    for source in app.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        if source.name == "alarm_provider.py":
            # The simulated alarm's own panel, not Verisure's.
            text = text.replace("def set_arm_state(", "")
        if source.name == "server.py":
            text = text.replace("provider.set_arm_state(", "")
        assert not forbidden.search(text), source.name


# -- the alarm's own sensors ----------------------------------------------------


DEVICES = [
    {"data": {"installation": {"doorWindows": [
        {"device": {"deviceLabel": "D001"}, "type": "DOOR_WINDOW", "area": "Woodshed",
         "state": "OPEN", "wired": False, "reportTime": "2026-09-30T08:02:00.000Z"},
        {"device": {"deviceLabel": "D002"}, "area": "Patio door", "state": "CLOSE"},
        {"device": {"deviceLabel": "D003"}, "area": "Tech room", "state": "SOMETHING"},
        {"device": {}, "area": "No label, so no way to tell it from the next"},
    ]}}},
    {"data": {"installation": {"climates": [
        {"device": {"deviceLabel": "S001", "area": "Hallway", "gui": {"label": "SMOKE"}},
         "humidityEnabled": False, "humidityValue": 55,
         "temperatureValue": 21.5, "temperatureTimestamp": "2026-09-30T07:40:00.000Z"},
        {"device": {"deviceLabel": "S002", "area": "Bathroom", "gui": {"label": "WATER"}},
         "humidityEnabled": True, "humidityValue": 61.0, "temperatureValue": "hot"},
    ]}}},
]


def test_door_window_and_climate_devices_are_read():
    devices = {item.device_id: item for item in alarm_verisure.parse_devices(DEVICES)}
    assert set(devices) == {"contact:D001", "contact:D002", "contact:D003",
                            "climate:S001", "climate:S002"}
    woodshed = devices["contact:D001"]
    assert (woodshed.kind, woodshed.name, woodshed.open) == ("contact", "Woodshed", True)
    assert woodshed.reported_at == "2026-09-30T08:02:00.000Z"
    assert devices["contact:D002"].open is False
    # A state this does not know is unknown, never closed.
    assert devices["contact:D003"].open is None
    hallway = devices["climate:S001"]
    assert (hallway.temperature, hallway.model) == (21.5, "Smoke")
    # Humidity only where the device says it measures it.
    assert hallway.humidity is None
    assert devices["climate:S002"].humidity == 61.0
    # A reading that is not a number is no reading.
    assert devices["climate:S002"].temperature is None


def test_an_installation_without_sensors_has_none():
    assert alarm_verisure.parse_devices(READING) == ()


def test_a_reading_carries_the_devices(account):
    FakeSession.answer = READING + DEVICES
    _sign_in(account)
    reading = run(account.read())
    assert reading.device("contact:D001").open is True
    assert reading.device("climate:S001").temperature == 21.5
    (request,) = [c for c in FakeSession.instances[-1].calls if isinstance(c, tuple)
                  and c[0] == "request"]
    assert request[1] == ("ArmState", "SmartLock", "DoorWindow", "Climate")


def test_the_sensor_queries_are_allowed_and_nothing_else_is_added():
    assert alarm_verisure.ALLOWED_OPERATIONS >= {"DoorWindow", "Climate"}
    for name in ("DoorWindow", "Climate"):
        check_read_only({"operationName": name, "query": f"query {name} {{ }}"})


def test_vsures_own_sensor_queries_pass_the_guard():
    """Checked against vsure itself, so a renamed operation is caught here
    rather than by a refused read on the Pi."""
    session_class = alarm_verisure.read_only_session_class()
    session = session_class("a@example.com", "", cookie_file_name="/nonexistent/x")
    session._giid = "111"
    for query in (session.door_window(), session.climate()):
        check_read_only(query)
