"""
notifications.py — telling somebody when the heating needs attention.

Why this exists
---------------
A cabin stands empty. Nobody is there to notice that a thermostat was switched
off in November and never switched back on, and the first sign of trouble is a
burst pipe in March. The application already knows enough to have said
something; it just never did.

What the hub can and cannot tell us
-----------------------------------
This module is built around four hard limits in the Nobø Hub API (v1.1), and it
is worth writing them down because they decide the shape of everything here:

1. **An override carries no source.** The struct is
   ``<Id> <Mode> <Type> <End> <Start> <Target> <TargetID>``. Nothing says who
   set it. So "who changed this?" can only ever be answered by elimination:
   we know what *we* sent, and anything else came from somewhere else.

2. **A component's ``Status`` field is "not yet implemented, always 0"**, and
   component-level overrides are "not yet supported". There is therefore *no*
   signal when somebody presses the button on a thermostat, and none when a
   heater loses power.

3. **Almost nothing measures temperature.** Of the 25 models pynobo knows, only
   the SW4 control panel carries a thermometer, and it is no longer sold. Every
   heater receiver — NTB-2R, R80 RDC 700 and the rest — controls temperature
   without ever reporting it. Alerts that need a room temperature were removed
   rather than left switched on looking useful.

4. **The keep-alive is between this Pi and the hub, and nowhere else.** The
   client sends ``HANDSHAKE`` every 14 seconds and the hub echoes it; if nothing
   comes back within 28 the link is declared dead. That covers the hub losing
   power or the network dropping, and it is the ``hub_offline`` alert.

   It says nothing about the heaters, and the reason is worth stating precisely.
   The hub is not deaf — ``X00`` starts a receiver search and the hub answers
   with ``Y04`` for every component it hears, so devices do transmit during
   pairing. But no *liveness* signal ever reaches this application: the
   component ``Status`` field is "not yet implemented, always 0", there is no
   command to ask whether a component is alive, and no unsolicited message
   reports that one has gone away. Whether a paired NTB-2R beacons on the radio
   is undocumented and unknowable from here; it also does not matter, because
   there is no channel through which the answer could arrive.

Which leaves an awkward truth: on this hardware the system cannot detect a cold
room, a dead heater, or a heater running flat out. What it *can* do is be
honest about the things it does see, and — through the heartbeat — make its own
silence meaningful, so that a Pi which has lost power still tells you something
by failing to write.

Design rules
------------
- **Never block the heating.** Every send happens on a worker thread with a
  timeout, and every failure is swallowed and logged. A broken mail server must
  not stop an away period from being applied.
- **Say it once.** Conditions are level-triggered, not edge-triggered: a room
  left off is *continuously* off, and re-sending that every 30 seconds would
  train the user to ignore the alerts. Each condition alerts on the way in,
  stays quiet while it persists, and alerts again on recovery.
- **Never log or return the password.**
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import threading
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Redirected by tests, in the same way as the other persistence modules.
DATA_DIR = Path(__file__).resolve().parent / "data"
NOTIFICATIONS_FILE = DATA_DIR / "notifications.json"
STATE_FILE = DATA_DIR / "notify_state.json"

SMTP_TIMEOUT_SECONDS = 20

# How long a send is given before we give up on it. Generous, because a slow
# mail server is normal; hard, because nothing may hang a heating loop.
SEND_TIMEOUT_SECONDS = 45


# ---------------------------------------------------------------------------
# The events we can actually detect
# ---------------------------------------------------------------------------
# key -> (label, default_on, explanation shown in the UI)
#
# Every alert here works on ordinary Nobø hardware. There used to be three more
# — a cold-room alarm, a silent-thermostat alarm and a cannot-get-warm alarm —
# and they were removed rather than left switched on: all three need a room
# temperature, only the SW4 reports one, and the SW4 is no longer sold. An alert
# that cannot fire is worse than no alert, because the owner believes the cabin
# is watched when nothing is watching it.
#
# Defaults are chosen for a cabin that is empty most of the year: the things
# that mean "go and look" are on, and the things that happen many times a day
# are off. A notification the user has learned to ignore is worse than none.
EVENT_TYPES: Dict[str, Dict[str, Any]] = {
    "hub_offline": {
        "label": "Hub goes offline",
        "default": True,
        "help": "The Pi has lost contact with the hub — the hub has lost power, or the "
                "network is down. Noticed within about half a minute.",
    },
    "hub_online": {
        "label": "Hub comes back",
        "default": True,
        "help": "Sent after an offline alert so you know it fixed itself.",
    },
    "heartbeat": {
        "label": "A regular “still here” email",
        "default": True,
        "help": "Sent on a schedule whether or not anything is wrong, so that silence "
                "means something. If the Pi loses power it cannot warn you — but a "
                "heartbeat that stops arriving does.",
    },
    "zone_off": {
        "label": "A room is left switched off",
        "default": True,
        "help": "A room whose schedule holds it Off will not heat at all, whatever the "
                "weather. Off is below Away: no anti-frost either.",
    },
    "changed_elsewhere": {
        "label": "Something is changed from another app",
        "default": True,
        "help": "A zone's mode or temperature changed and it was not this system that did it.",
    },
    "away_period": {
        "label": "An away period starts or ends",
        "default": True,
        "help": "Confirms the planned trip actually took effect.",
    },
    "schedule_event": {
        "label": "A weekly schedule event starts",
        "default": False,
        "help": "Every comfort/eco switch, in every room. Many a day — off unless you want a diary.",
    },
}

SEVERITIES = {"info", "warning", "critical"}

_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "email": {
        "host": "",
        "port": 587,
        "security": "starttls",     # "starttls" | "ssl" | "none"
        "username": "",
        "password": "",
        "from_addr": "",
        "to_addrs": [],
    },
    "events": {k: v["default"] for k, v in EVENT_TYPES.items()},
    # A room left switched off will not heat whatever the weather. A day is long
    # enough that turning a room off to air it is not an incident.
    "off_for_hours": 24,
    # How often the "still here" email goes out. Weekly suits a cabin: often
    # enough that a dead Pi is noticed within days, rare enough to stay read.
    "heartbeat_days": 7,
    "heartbeat_hour": 9,
    "quiet_hours": {"enabled": False, "start": "23:00", "end": "07:00"},
    # A floor on how often the same condition may speak, whatever happens.
    "min_minutes_between": 10,
}


def _clean_port(value: Any, fallback: int = 587) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return fallback
    return port if 1 <= port <= 65535 else fallback


def _clean_addrs(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        addr = str(item).strip()
        # Not full RFC validation - just enough to reject obvious nonsense
        # before we hand it to the mail server.
        if addr and "@" in addr and " " not in addr:
            out.append(addr)
    return out


def _merge(stored: Any) -> Dict[str, Any]:
    """Fold stored settings onto the defaults, so a new event key is simply adopted."""
    cfg = json.loads(json.dumps(_DEFAULTS))  # deep copy
    if not isinstance(stored, dict):
        return cfg

    cfg["enabled"] = bool(stored.get("enabled", cfg["enabled"]))

    email = stored.get("email")
    if isinstance(email, dict):
        cfg["email"]["host"] = str(email.get("host", "")).strip()
        cfg["email"]["port"] = _clean_port(email.get("port"), cfg["email"]["port"])
        sec = str(email.get("security", "starttls")).lower()
        cfg["email"]["security"] = sec if sec in ("starttls", "ssl", "none") else "starttls"
        cfg["email"]["username"] = str(email.get("username", "")).strip()
        cfg["email"]["password"] = str(email.get("password", ""))
        cfg["email"]["from_addr"] = str(email.get("from_addr", "")).strip()
        cfg["email"]["to_addrs"] = _clean_addrs(email.get("to_addrs"))

    events = stored.get("events")
    if isinstance(events, dict):
        for key in cfg["events"]:
            if key in events:
                cfg["events"][key] = bool(events[key])

    for key, lo, hi in (
        ("min_minutes_between", 0, 24 * 60),
        ("off_for_hours", 1, 30 * 24),
        ("heartbeat_days", 1, 90),
        ("heartbeat_hour", 0, 23),
    ):
        if key in stored:
            try:
                val = float(stored[key])
                if lo <= val <= hi:
                    cfg[key] = int(val)
            except (TypeError, ValueError):
                pass

    quiet = stored.get("quiet_hours")
    if isinstance(quiet, dict):
        cfg["quiet_hours"]["enabled"] = bool(quiet.get("enabled", False))
        for k in ("start", "end"):
            val = str(quiet.get(k, _DEFAULTS["quiet_hours"][k]))
            if _valid_hhmm(val):
                cfg["quiet_hours"][k] = val
    return cfg


def _valid_hhmm(value: str) -> bool:
    parts = str(value).split(":")
    if len(parts) != 2:
        return False
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 0 <= h <= 23 and 0 <= m <= 59


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_settings() -> Dict[str, Any]:
    """Load the settings, falling back to "notifications off" for anything unusable."""
    try:
        with NOTIFICATIONS_FILE.open("r", encoding="utf-8") as fh:
            return _merge(json.load(fh))
    except FileNotFoundError:
        return _merge(None)
    except json.JSONDecodeError as exc:
        logger.warning("notifications.json is corrupt (%s) — notifications are off until it is set again", exc)
        return _merge(None)
    except Exception as exc:
        logger.error("Could not read notifications.json: %s", exc)
        return _merge(None)


def save_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Validate, persist atomically, and return the cleaned settings."""
    cfg = _merge(settings)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = NOTIFICATIONS_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    tmp.replace(NOTIFICATIONS_FILE)
    try:
        # The file holds a mail password. Keep it to the owner where the OS
        # allows it; on filesystems that do not support it this is a no-op.
        NOTIFICATIONS_FILE.chmod(0o600)
    except Exception:
        pass
    return cfg


def public_settings(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    The settings as the API is allowed to return them.

    The password is replaced by a boolean. It is never sent back to the browser,
    so it cannot leak through a screenshot, a proxy log or a support bundle.
    """
    cfg = cfg if cfg is not None else load_settings()
    out = json.loads(json.dumps(cfg))
    out["email"].pop("password", None)
    out["email"]["password_set"] = bool(cfg["email"].get("password"))
    out["event_types"] = {
        k: {"label": v["label"], "help": v["help"], "default": v["default"]}
        for k, v in EVENT_TYPES.items()
    }
    out["last_heartbeat"] = load_state().get("last_heartbeat")
    return out


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------
# Kept apart from the settings so that saving preferences cannot disturb it, and
# so that a corrupt settings file cannot cost the heartbeat its history. It has
# to survive a restart: a Pi that reboots weekly would otherwise never reach the
# interval and never send, which is the one failure this feature must not have.

def load_state() -> Dict[str, Any]:
    try:
        with STATE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state: Dict[str, Any]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        tmp.replace(STATE_FILE)
    except Exception as exc:
        logger.warning("Could not save notification state: %s", exc)


def heartbeat_due(now: float, settings: Dict[str, Any],
                  state: Optional[Dict[str, Any]] = None) -> bool:
    """
    Whether the "still here" email is due.

    First run counts as due, so the user finds out immediately that it works
    rather than waiting a week to discover the mail settings were wrong.
    """
    if not settings.get("enabled") or not settings.get("events", {}).get("heartbeat"):
        return False
    state = state if state is not None else load_state()
    last = state.get("last_heartbeat")
    if not isinstance(last, (int, float)):
        return True
    interval = max(1, int(settings.get("heartbeat_days", 7))) * 86400
    return (now - last) >= interval


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _send_email_blocking(cfg: Dict[str, Any], subject: str, body: str) -> None:
    """Actually talk to the mail server. Runs on a worker thread, never the loop."""
    email = cfg["email"]
    host, port = email["host"], _clean_port(email["port"])
    to_addrs = email["to_addrs"]
    from_addr = email["from_addr"] or email["username"] or "nobo@localhost"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)

    security = email.get("security", "starttls")
    if security == "ssl":
        context = ssl.create_default_context()
        server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SECONDS, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS)

    try:
        server.ehlo()
        if security == "starttls":
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        if email.get("username"):
            server.login(email["username"], email.get("password", ""))
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def send_test(cfg: Dict[str, Any], site_name: str = "Nobø Control") -> None:
    """Send a test message, letting the error through so the UI can show it."""
    problems = validate_email_config(cfg)
    if problems:
        raise ValueError(problems[0])
    _send_email_blocking(
        cfg,
        f"[{site_name}] Test message",
        "This is a test from your heating system.\n\n"
        "If you are reading this, alerts will reach you.\n",
    )


def validate_email_config(cfg: Dict[str, Any]) -> List[str]:
    """Human-readable reasons the mail settings cannot work yet."""
    email = cfg.get("email", {})
    problems = []
    if not email.get("host"):
        problems.append("No mail server is set.")
    if not email.get("to_addrs"):
        problems.append("There is nobody to send to.")
    if email.get("username") and not email.get("password"):
        problems.append("A username is set but no password.")
    if not email.get("from_addr") and not email.get("username"):
        problems.append("No 'from' address is set.")
    return problems


# ---------------------------------------------------------------------------
# The notifier
# ---------------------------------------------------------------------------

@dataclass
class _Condition:
    """Whether a level-triggered condition is currently raised, and when it last spoke."""
    active: bool = False
    last_sent: float = 0.0


@dataclass
class Notifier:
    """
    Decides what is worth saying, and says it without ever blocking the caller.

    Everything is keyed by a stable string (``room_cold:4``), so a condition can
    be raised and cleared independently per room or per device.
    """
    settings: Dict[str, Any] = field(default_factory=load_settings)
    _conditions: Dict[str, _Condition] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    site_name: str = "Nobø Control"
    # Set by the server so alerts show up in the same place as everything else.
    log_hook: Optional[Any] = None
    # Overridden in tests so nothing tries to reach a mail server.
    send_impl: Optional[Any] = None

    # -- configuration ----------------------------------------------------

    def reload(self, settings: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self.settings = settings if settings is not None else load_settings()

    # -- gating -----------------------------------------------------------

    def _quiet_now(self, now: Optional[time.struct_time] = None) -> bool:
        quiet = self.settings.get("quiet_hours", {})
        if not quiet.get("enabled"):
            return False
        lt = now or time.localtime()
        minutes = lt.tm_hour * 60 + lt.tm_min
        start = _hhmm_to_minutes(quiet.get("start", "23:00"))
        end = _hhmm_to_minutes(quiet.get("end", "07:00"))
        if start == end:
            return False
        if start < end:
            return start <= minutes < end
        # Wraps midnight, which is the normal case for "quiet at night".
        return minutes >= start or minutes < end

    def _wants(self, event_type: str) -> bool:
        if not self.settings.get("enabled"):
            return False
        return bool(self.settings.get("events", {}).get(event_type))

    # -- the two ways to raise something ----------------------------------

    def notify(self, event_type: str, subject: str, body: str,
               severity: str = "info", key: Optional[str] = None) -> bool:
        """
        A one-off thing happened. Send it, unless it is muted or too soon.

        Returns whether it was sent, which is what the tests assert on.
        """
        if not self._wants(event_type):
            return False

        # A critical alert is the whole point of the feature, so it ignores
        # quiet hours. A frost warning that waits until 07:00 is not a warning.
        if severity != "critical" and self._quiet_now():
            logger.info("Suppressed %s during quiet hours", event_type)
            return False

        dedup_key = key or event_type
        floor = float(self.settings.get("min_minutes_between", 10)) * 60
        now = time.time()
        with self._lock:
            cond = self._conditions.setdefault(dedup_key, _Condition())
            if floor and (now - cond.last_sent) < floor:
                logger.info("Suppressed %s — sent %.0fs ago", dedup_key, now - cond.last_sent)
                return False
            cond.last_sent = now

        self._dispatch(subject, body, severity, event_type)
        return True

    def set_condition(self, event_type: str, key: str, raised: bool,
                      subject: str = "", body: str = "",
                      severity: str = "warning",
                      recovery_subject: str = "", recovery_body: str = "",
                      recovery_event_type: Optional[str] = None) -> bool:
        """
        A *continuing* state changed.

        This is the important one. A cold room is cold for hours; the user wants
        to hear once that it went cold and once that it recovered, not every
        time a loop ticks. Returns whether anything was sent.

        ``recovery_event_type`` exists because "the hub is back" is a different
        thing to be told than "the hub is gone", and the user can want one
        without the other.
        """
        with self._lock:
            cond = self._conditions.setdefault(key, _Condition())
            was = cond.active
            if was == raised:
                return False
            cond.active = raised

        if raised:
            return self.notify(event_type, subject, body, severity=severity, key=key + ":on")
        if recovery_subject:
            return self.notify(recovery_event_type or event_type,
                               recovery_subject, recovery_body,
                               severity="info", key=key + ":off")
        return False

    def is_raised(self, key: str) -> bool:
        with self._lock:
            cond = self._conditions.get(key)
            return bool(cond and cond.active)

    # -- delivery ---------------------------------------------------------

    def _dispatch(self, subject: str, body: str, severity: str, event_type: str) -> None:
        prefix = {"critical": "⚠ ", "warning": "", "info": ""}.get(severity, "")
        full_subject = f"[{self.site_name}] {prefix}{subject}"
        footer = (
            "\n\n---\n"
            f"Sent by your Nobø heating system ({self.site_name}).\n"
            "Turn this off or change what it reports under Settings → Notifications.\n"
        )
        full_body = body + footer

        if self.log_hook:
            try:
                self.log_hook("sent", f"Notification: {subject}", command=f"notify {event_type}", source="api")
            except Exception:
                pass

        sender = self.send_impl or _send_email_blocking
        cfg = json.loads(json.dumps(self.settings))

        def run():
            try:
                sender(cfg, full_subject, full_body)
                logger.info("Notification sent: %s", subject)
            except Exception as exc:
                # Deliberately swallowed. A mail server being down must never
                # become a heating fault.
                logger.error("Could not send notification %r: %s", subject, exc)
                if self.log_hook:
                    try:
                        self.log_hook("error", f"Notification failed: {exc}",
                                      command=f"notify {event_type}", source="api")
                    except Exception:
                        pass

        threading.Thread(target=run, name="nobo-notify", daemon=True).start()


def _hhmm_to_minutes(value: str) -> int:
    try:
        h, m = str(value).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0
