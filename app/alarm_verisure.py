"""Reading a Verisure alarm and Yale Doorman, and nothing else.

Verisure has no public API. This goes through ``vsure`` (python-verisure), the
same library Home Assistant's Verisure integration uses, which speaks the
cloud API the Verisure app itself uses. That makes it unofficial: Verisure can
change it without notice, and the integration fails safe when it does — the
heating is left exactly as it is and Settings says Verisure cannot be read.

How the account is protected (docs/ALARM.md has the full account):

* **The password is never stored.** It is held in memory for at most five
  minutes, between typing it and entering the code Verisure sends, and then
  dropped. What is kept is the session Verisure hands back — the same thing a
  phone keeps after you sign in to the app — so a stolen copy of it can be
  revoked by signing out, and it never reveals the password.
* The session lives in ``data/verisure/session.json``: directory 0700, file
  0600, excluded from backups, deleted on sign-out or when the integration is
  turned off. vsure's own cookie file is a pickle; it is written to a scratch
  file inside the same private directory, deleted after every call, and never
  read back, so there is no file this process ever unpickles.
* **Read-only, enforced.** Every request goes through ``check_read_only``,
  which allows three named GraphQL *queries* and rejects everything else before
  it leaves the Pi. Arming, disarming, locking and unlocking cannot be sent
  from here even by a bug. A test also fails if this module names any of
  vsure's commands.
* Nothing from Verisure's servers is logged or shown: their error bodies can
  contain account details, so only the error's type is logged and the user
  sees one of our own sentences. vsure's own logger is turned down to ERROR,
  because at INFO it logs the e-mail address.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import alarm_persistence
from alarm_provider import AlarmDevice, AlarmReading, AlarmUnavailable, LockReading

logger = logging.getLogger(__name__)
logging.getLogger("verisure").setLevel(logging.ERROR)

# The only GraphQL operations this application may send. All of them are
# queries. Everything else vsure offers is refused by ``check_read_only``.
ALLOWED_OPERATIONS = frozenset({
    "fetchAllInstallations", "ArmState", "SmartLock", "DoorWindow", "Climate",
})

# Verisure's cookie lasts about fifteen minutes; refresh a little before that,
# as Home Assistant does.
COOKIE_REFRESH_SECONDS = 10 * 60
PENDING_LOGIN_SECONDS = 5 * 60
# What each door and window is called. The name the Verisure app shows
# ("Bod ute") is on the device, which vsure's door query does not ask for, so
# it is read on its own: hourly, and sooner when a contact appears that has
# no name yet, but never more than once in ten minutes.
NAMES_REFRESH_SECONDS = 60 * 60
NAMES_RETRY_SECONDS = 10 * 60
# Our own limit on sign-in attempts, well inside anything Verisure would call
# abuse: a locked Verisure account is a much worse outcome than a wait here.
LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 10 * 60

_CODE = re.compile(r"^\d{4,8}$")

ARM_STATE_NAMES = {
    "DISARMED": "disarmed",
    "ARMED_HOME": "armed_home",
    "ARMED_AWAY": "armed_away",
}


class ReadOnlyViolation(RuntimeError):
    pass


def check_read_only(operation: Any) -> None:
    """Refuse anything but the read-only queries this module needs."""
    if not isinstance(operation, dict):
        raise ReadOnlyViolation("operation must be a GraphQL document")
    name = operation.get("operationName")
    query = operation.get("query")
    if name not in ALLOWED_OPERATIONS:
        raise ReadOnlyViolation(f"operation {name!r} is not allowed")
    if not isinstance(query, str) or not query.lstrip().startswith("query "):
        raise ReadOnlyViolation(f"operation {name!r} is not a query")


def read_only_session_class():
    """vsure's Session, restricted. Imported lazily: vsure is only needed when
    somebody actually signs in to Verisure."""
    import verisure

    class ReadOnlySession(verisure.Session):
        def request(self, *operations):
            for operation in operations:
                check_read_only(operation)
            return super().request(*operations)

        def _load_cookie_file_into_memory(self):
            # vsure would unpickle its cookie file here. This application
            # keeps cookies in its own JSON store and never loads a pickle.
            raise verisure.CookieReadError("cookie file is not used")

    return ReadOnlySession


def mask_email(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def _installation_parts(response: Any) -> List[dict]:
    """``request`` answers a dict for one operation and a list for several."""
    items = response if isinstance(response, list) else [response]
    return [item for item in items if isinstance(item, dict)]


def _unpack(response: Any, *path: str) -> Any:
    for item in _installation_parts(response):
        value: Any = item.get("data")
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if value is not None:
            return value
    return None


def parse_installations(response: Any) -> List[Dict[str, str]]:
    found = _unpack(response, "account", "installations")
    if not isinstance(found, list):
        raise ValueError("no installations in the answer")
    return [
        {"giid": str(item["giid"]), "alias": str(item.get("alias") or "")}
        for item in found if isinstance(item, dict) and item.get("giid")
    ]


def parse_reading(response: Any, now: float) -> AlarmReading:
    arm = _unpack(response, "installation", "armState")
    if not isinstance(arm, dict):
        raise ValueError("no arm state in the answer")
    locks = _unpack(response, "installation", "smartLocks") or []
    readings = []
    for lock in locks if isinstance(locks, list) else []:
        if not isinstance(lock, dict):
            continue
        device = lock.get("device") or {}
        label = str(device.get("deviceLabel") or "")
        status = lock.get("lockStatus")
        method = lock.get("lockMethod")
        readings.append(LockReading(
            lock_id=label,
            name=str(device.get("area") or label or "Lock"),
            locked=True if status == "LOCKED" else False if status == "UNLOCKED" else None,
            method=str(method).lower() if method else None,
            changed_at=lock.get("eventTime"),
        ))
    return AlarmReading(
        arm_state=ARM_STATE_NAMES.get(arm.get("statusType")),
        arm_changed_at=arm.get("date"),
        locks=tuple(readings),
        read_at=now,
        devices=parse_devices(response),
    )


def contact_names_query(giid: str) -> Dict[str, Any]:
    """vsure's door query, plus the name each device has in the Verisure app.

    Kept apart from the reading: if Verisure ever refuses it, the doors keep
    their labels and nothing else changes.
    """
    return {
        "operationName": "DoorWindow",
        "variables": {"giid": giid},
        "query": (
            "query DoorWindow($giid: String!) {\n"
            "  installation(giid: $giid) {\n"
            "    doorWindows {\n"
            "      device {\n        deviceLabel\n        area\n        __typename\n      }\n"
            "      area\n      __typename\n"
            "    }\n    __typename\n  }\n}\n"
        ),
    }


def parse_contact_names(response: Any) -> Dict[str, str]:
    """label -> name, for the doors and windows that have a name."""
    names: Dict[str, str] = {}
    doors = _unpack(response, "installation", "doorWindows")
    if not isinstance(doors, list):
        raise ValueError("no doors and windows in the answer")
    for item in doors:
        if not isinstance(item, dict):
            continue
        device = item.get("device") if isinstance(item.get("device"), dict) else {}
        label = _label(device.get("deviceLabel"))
        name = _label(item.get("area")) or _label(device.get("area"))
        if label and name:
            names[label] = name
    return names


def with_contact_names(reading: AlarmReading, names: Dict[str, str]) -> AlarmReading:
    """Name the contacts the reading could only label."""
    if not names:
        return reading
    devices = []
    for device in reading.devices:
        label = device.device_id.partition(":")[2]
        if device.kind == "contact" and device.name == label and names.get(label):
            device = dataclasses.replace(device, name=names[label])
        devices.append(device)
    return dataclasses.replace(reading, devices=tuple(devices))


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 1)


def _label(value: Any) -> str:
    return str(value or "").strip()[:64]


def parse_devices(response: Any) -> Tuple[AlarmDevice, ...]:
    """The alarm's door and window contacts and its climate readings.

    Both lists are optional: an installation with no such devices, or an
    answer without them, is simply one with no sensors to offer. A device
    without a label cannot be told apart from the next one and is skipped.
    """
    devices: List[AlarmDevice] = []
    doors = _unpack(response, "installation", "doorWindows") or []
    for item in doors if isinstance(doors, list) else []:
        if not isinstance(item, dict):
            continue
        label = _label((item.get("device") or {}).get("deviceLabel"))
        if not label:
            continue
        state = item.get("state")
        devices.append(AlarmDevice(
            device_id=f"contact:{label}",
            kind="contact",
            name=_label(item.get("area")) or label,
            model=_label(item.get("type")).replace("_", " ").capitalize() or "Door/window",
            open=True if state == "OPEN" else False if state == "CLOSE" else None,
            reported_at=item.get("reportTime") if isinstance(item.get("reportTime"), str) else None,
        ))
    climates = _unpack(response, "installation", "climates") or []
    for item in climates if isinstance(climates, list) else []:
        if not isinstance(item, dict):
            continue
        device = item.get("device") or {}
        label = _label(device.get("deviceLabel"))
        if not label:
            continue
        stamp = item.get("temperatureTimestamp")
        devices.append(AlarmDevice(
            device_id=f"climate:{label}",
            kind="climate",
            name=_label(device.get("area")) or label,
            model=_label((device.get("gui") or {}).get("label")).capitalize() or None,
            temperature=_number(item.get("temperatureValue")),
            humidity=_number(item.get("humidityValue")) if item.get("humidityEnabled") else None,
            reported_at=stamp if isinstance(stamp, str) else None,
        ))
    return tuple(devices)


class VerisureError(Exception):
    """A sign-in step failed. ``status`` is the HTTP status to answer with;
    the message is ours and safe to show."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class VerisureAlarm:
    """The Verisure account, signed in once and then read every minute."""

    name = "verisure"

    def __init__(
        self,
        session_factory: Optional[Callable[..., Any]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._factory = session_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: Optional[Tuple[Any, float]] = None
        self._attempts: Deque[float] = deque()
        # The doors' names: (giid, label -> name, labels the last answer
        # covered, when read, when last tried). Memory only.
        self._names: Tuple[Optional[str], Dict[str, str], frozenset, float, float] = (
            None, {}, frozenset(), 0.0, float("-inf"))

    # -- plumbing -----------------------------------------------------------

    @property
    def _scratch(self) -> Path:
        return alarm_persistence.VERISURE_DIR / "vsure.cookie"

    def _session_class(self):
        return self._factory or read_only_session_class()

    def _new_session(self, email: str, password: str):
        alarm_persistence.secure_dir()
        return self._session_class()(email, password, cookie_file_name=str(self._scratch))

    def _clean_scratch(self) -> None:
        self._scratch.unlink(missing_ok=True)

    @staticmethod
    def _errors():
        import verisure

        return verisure

    def _restore(self, stored: Dict[str, Any]):
        import requests

        session = self._new_session(stored["email"], "")
        jar = requests.sessions.RequestsCookieJar()
        for name, value in stored["cookies"].items():
            jar.set(name, value)
        session._cookies = jar
        if stored.get("trust_token"):
            session._trust_token = {"trustTokenValue": stored["trust_token"]}
        if stored.get("giid"):
            session.set_giid(stored["giid"])
        return session

    @staticmethod
    def _cookies_of(session) -> Dict[str, str]:
        cookies = getattr(session, "_cookies", None)
        if cookies is None:
            return {}
        return {str(name): str(value) for name, value in cookies.items()}

    def _store(self, session, stored: Dict[str, Any]) -> Dict[str, Any]:
        stored = {**stored, "cookies": self._cookies_of(session)}
        alarm_persistence.save_session(stored)
        return stored

    # -- what Settings shows -------------------------------------------------

    def public_state(self) -> Dict[str, Any]:
        stored = alarm_persistence.load_session()
        pending = self._pending is not None and self._pending[1] > self._clock()
        if stored is None:
            return {
                "signed_in": False, "email": None, "installation": None,
                "installations": [], "awaiting_code": pending,
            }
        chosen = next(
            (item for item in stored["installations"] if item["giid"] == stored.get("giid")),
            None,
        )
        return {
            "signed_in": bool(stored["cookies"]),
            "email": mask_email(stored["email"]),
            "installation": chosen["alias"] if chosen else None,
            "installations": [
                {"giid": item["giid"], "alias": item["alias"] or "Unnamed"}
                for item in stored["installations"]
            ],
            "awaiting_code": pending,
        }

    # -- signing in -----------------------------------------------------------

    def _count_attempt(self) -> None:
        now = self._clock()
        while self._attempts and now - self._attempts[0] > LOGIN_WINDOW_SECONDS:
            self._attempts.popleft()
        if len(self._attempts) >= LOGIN_ATTEMPTS:
            raise VerisureError(
                429, "Too many sign-in attempts. Wait ten minutes before trying again."
            )
        self._attempts.append(now)

    def _map_login_error(self, exc: Exception, wrong: str) -> VerisureError:
        errors = self._errors()
        logger.warning("Verisure sign-in failed: %s", type(exc).__name__)
        if isinstance(exc, errors.RateLimitError):
            return VerisureError(429, "Verisure is limiting sign-ins. Try again later.")
        if isinstance(exc, (errors.RequestError, errors.ResponseError)):
            return VerisureError(502, "Verisure could not be reached. Try again later.")
        if isinstance(exc, errors.Error):
            return VerisureError(401, wrong)
        return VerisureError(502, "Verisure gave an answer this app does not understand.")

    def _finish(self, session, installations_response) -> Dict[str, Any]:
        installations = parse_installations(installations_response)
        trust = getattr(session, "_trust_token", None)
        stored = {
            "email": session._username,
            "cookies": {},
            "trust_token": trust.get("trustTokenValue") if isinstance(trust, dict) else None,
            "giid": self._giid_for(session._username, installations),
            "installations": installations,
            "refreshed_at": self._clock(),
        }
        # The password goes no further than this.
        session._password = ""
        return self._store(session, stored)

    @staticmethod
    def _giid_for(email: str, installations: List[dict]) -> Optional[str]:
        """The installation to read after signing in. Signing in again is
        usually because Verisure ended the last sign-in, and asking again
        which house this is would leave the alarm unread until somebody
        noticed. So the earlier choice stands, if it is still on the account."""
        if len(installations) == 1:
            return installations[0]["giid"]
        previous = alarm_persistence.load_session()
        if (
            previous is not None and previous.get("giid")
            and (previous.get("email") or "").lower() == (email or "").lower()
            and any(item["giid"] == previous["giid"] for item in installations)
        ):
            return previous["giid"]
        return None

    def _login_trusted(self, session) -> Any:
        """Sign in once more, with the password and the trust the code just
        earned, and keep that session rather than the code's.

        This is vsure's ``login_cookie``, from the cookies in memory instead
        of its pickle. Home Assistant signs in this way after the code, and
        refreshes that session every ten minutes; the session the code step
        itself returns was refused at its first refresh on the real account.
        The password is still only the one typed a moment ago, and is
        dropped straight after, as before."""
        import requests

        errors = self._errors()
        trusted = requests.sessions.RequestsCookieJar()
        for name, value in (session._cookies or {}).items():
            if "vs-trust" in name:
                trusted.set(name, value)
        if not len(trusted):
            raise errors.LoginError("Verisure returned no trust cookie")
        response = session._post(
            url="/auth/login",
            headers={"APPLICATION_ID": "PS_PYTHON"},
            auth=(session._username, session._password),
            cookies=trusted,
        )
        if "stepUpToken" in response.text:
            raise errors.LoginError("Verisure asked for a code again")
        # A fresh jar: the trust cookie and this sign-in's own cookies, with
        # nothing of the code step's session left to be confused with them.
        trusted.update(response.cookies)
        session._cookies = trusted
        installations = session.get_installations()
        if not isinstance(installations, dict) or "errors" in installations:
            raise errors.LoginError("Failed to log in")
        return installations

    def _begin_sync(self, email: str, password: str) -> str:
        with self._lock:
            self._count_attempt()
            self._pending = None
            session = self._new_session(email, password)
            errors = self._errors()
            try:
                try:
                    response = session.login()
                except errors.LoginError as exc:
                    if not getattr(session, "_mfa_login_pending", False):
                        raise
                    session.request_mfa()
                    self._pending = (session, self._clock() + PENDING_LOGIN_SECONDS)
                    return "code_sent"
                self._finish(session, response)
                return "signed_in"
            except (errors.Error, ValueError, KeyError, TypeError) as exc:
                session._password = ""
                raise self._map_login_error(exc, "Verisure did not accept that e-mail and password.")
            finally:
                self._clean_scratch()

    def _code_sync(self, code: str) -> str:
        with self._lock:
            pending = self._pending
            if pending is None or pending[1] <= self._clock():
                self._pending = None
                raise VerisureError(409, "The sign-in has expired. Start again.")
            session = pending[0]
            errors = self._errors()
            try:
                # Answers with the installations, as a plain login does.
                response = session.validate_mfa(code)
                try:
                    response = self._login_trusted(session)
                except errors.Error as exc:
                    # The code's own session still reads the alarm; it may
                    # just not outlive its first refresh.
                    logger.warning(
                        "Verisure trusted sign-in failed, keeping the code's session: %s",
                        type(exc).__name__,
                    )
                self._finish(session, response)
                self._pending = None
                return "signed_in"
            except (errors.Error, ValueError, KeyError, TypeError) as exc:
                raise self._map_login_error(exc, "Verisure did not accept that code.")
            finally:
                self._clean_scratch()

    async def begin_login(self, email: str, password: str) -> str:
        email = (email or "").strip()
        if not email or "@" not in email or not password:
            raise VerisureError(400, "Enter the e-mail and password you use for the Verisure app.")
        return await asyncio.to_thread(self._begin_sync, email, password)

    async def submit_code(self, code: str) -> str:
        code = (code or "").strip().replace(" ", "")
        if not _CODE.match(code):
            raise VerisureError(400, "Enter the code Verisure sent you, digits only.")
        return await asyncio.to_thread(self._code_sync, code)

    def cancel_login(self) -> None:
        with self._lock:
            if self._pending is not None:
                self._pending[0]._password = ""
            self._pending = None

    def choose_installation(self, giid: str) -> None:
        with self._lock:
            stored = alarm_persistence.load_session()
            if stored is None:
                raise VerisureError(409, "Sign in to Verisure first.")
            if not any(item["giid"] == giid for item in stored["installations"]):
                raise VerisureError(400, "That installation is not on this account.")
            alarm_persistence.save_session({**stored, "giid": giid})

    def _sign_out_sync(self) -> None:
        with self._lock:
            if self._pending is not None:
                self._pending[0]._password = ""
            self._pending = None
            stored = alarm_persistence.load_session()
            try:
                if stored is not None and stored["cookies"]:
                    errors = self._errors()
                    try:
                        # Revokes the trust and the session at Verisure, so a
                        # copy of the file taken earlier stops working too.
                        self._restore(stored).logout()
                    except errors.Error as exc:
                        logger.warning("Verisure sign-out was not confirmed: %s", type(exc).__name__)
            finally:
                alarm_persistence.delete_session()
                self._clean_scratch()

    async def sign_out(self) -> None:
        await asyncio.to_thread(self._sign_out_sync)

    # -- reading ----------------------------------------------------------------

    @staticmethod
    def _queries(session: Any) -> tuple:
        # One request for all four, as Home Assistant does: the rate limit is
        # counted in requests, so the sensors cost nothing extra.
        return (
            session.arm_state(), session.smart_lock(),
            session.door_window(), session.climate(),
        )

    def _read_sync(self) -> AlarmReading:
        with self._lock:
            stored = alarm_persistence.load_session()
            if stored is None:
                # Never signed in: something still to do, not something wrong.
                raise AlarmUnavailable("not_configured", "Sign in to Verisure in Settings.")
            if not stored["cookies"]:
                raise AlarmUnavailable("signed_out", "Verisure ended the sign-in. Sign in again in Settings.")
            if not stored.get("giid"):
                raise AlarmUnavailable("not_configured", "Choose which Verisure installation to use.")
            errors = self._errors()
            session = self._restore(stored)
            try:
                try:
                    if self._clock() - stored["refreshed_at"] >= COOKIE_REFRESH_SECONDS:
                        session.update_cookie()
                        stored = self._store(session, {**stored, "refreshed_at": self._clock()})
                    response = session.request(*self._queries(session))
                    try:
                        reading = parse_reading(response, self._clock())
                    except ValueError:
                        # Most likely the access cookie lapsed early. One
                        # refresh, then it is a real failure.
                        session.update_cookie()
                        stored = self._store(session, {**stored, "refreshed_at": self._clock()})
                        response = session.request(*self._queries(session))
                        reading = parse_reading(response, self._clock())
                    return with_contact_names(reading, self._contact_names(session, stored["giid"], reading))
                except (errors.AuthenticationError, errors.CookieReadError) as exc:
                    logger.warning("Verisure session ended: %s", type(exc).__name__)
                    alarm_persistence.save_session({**stored, "cookies": {}, "trust_token": None})
                    raise AlarmUnavailable(
                        "signed_out", "The Verisure sign-in has expired. Sign in again in Settings."
                    ) from None
                except errors.RateLimitError:
                    raise AlarmUnavailable(
                        "rate_limited", "Verisure is limiting requests; reading less often."
                    ) from None
                except errors.Error as exc:
                    logger.warning("Verisure could not be read: %s", type(exc).__name__)
                    raise AlarmUnavailable("unreachable", "Verisure could not be reached.") from None
                except ValueError:
                    raise AlarmUnavailable(
                        "unreachable", "Verisure gave an answer this app does not understand."
                    ) from None
            finally:
                self._clean_scratch()

    def _contact_names(self, session: Any, giid: str, reading: AlarmReading) -> Dict[str, str]:
        """The doors' names, read again when due. Never fails the reading."""
        known_giid, names, seen, read_at, tried_at = self._names
        if known_giid != giid:
            names, seen, read_at, tried_at = {}, frozenset(), 0.0, float("-inf")
        labels = {
            device.device_id.partition(":")[2]
            for device in reading.devices if device.kind == "contact"
        }
        now = self._clock()
        due = bool(labels) and now - tried_at >= NAMES_RETRY_SECONDS and (
            now - read_at >= NAMES_REFRESH_SECONDS or not labels <= seen
        )
        if due:
            tried_at = now
            errors = self._errors()
            try:
                answer = session.request(contact_names_query(giid))
                names, seen, read_at = parse_contact_names(answer), frozenset(labels), now
            except (errors.Error, ValueError) as exc:
                # The doors keep their labels; the next try is ten minutes away.
                logger.warning("Verisure door names could not be read: %s", type(exc).__name__)
        self._names = (giid, names, seen, read_at, tried_at)
        return names

    async def read(self) -> AlarmReading:
        return await asyncio.to_thread(self._read_sync)
