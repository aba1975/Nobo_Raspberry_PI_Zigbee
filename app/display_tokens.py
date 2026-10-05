"""
display_tokens.py — read-only keys for wall displays.

A wall display such as the M5Stack PaperColor cannot sign in: it has no
keyboard, and a session cookie expires. It is given a key instead, created by
an admin under Settings → Displays and shown exactly once.

What a key can do is deliberately tiny. It opens the display picture and the
display summary (``DISPLAY_TOKEN_PATHS`` in server.py) and nothing else — no
heating, no settings, no WebSocket. That is the whole reason this exists rather
than ``NOBO_ALLOW_ANON_API``, which opens the heating to everyone on the
network.

Only a SHA-256 hash of each key is stored (``data/displays.json``). A key is
32 random bytes, so a slow password hash would add nothing but latency on every
poll; the hash is there so that a copy of the data folder — a backup, say —
does not hand out working keys.

``last_seen_at`` and the reported battery are kept in memory on every request
and written to disk at most every ``SEEN_WRITE_INTERVAL`` seconds, so a display
polling every minute does not rewrite a file on the Pi's SD card every minute.
"""

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent / "data"
DISPLAYS_FILE = DATA_DIR / "displays.json"

SCHEMA_VERSION = 1
TOKEN_PREFIX = "nd_"
MAX_DISPLAYS = 10
MAX_NAME_LENGTH = 40
SEEN_WRITE_INTERVAL = 600.0

# How often a display on battery wakes to ask, in minutes. Set per display on
# the Pi and handed to the display in the X-Display-Interval header, so it can
# be changed without touching the device. Every wake is a Wi-Fi join, which is
# nearly all of what the battery goes on, so the choices are kept coarse.
BATTERY_MINUTES_CHOICES = (2, 5, 10, 15, 30, 60)
DEFAULT_BATTERY_MINUTES = 15
POWER_SOURCES = ("usb", "battery")


@dataclass
class Display:
    display_id: str
    name: str
    token_hash: str
    created_at: float
    last_seen_at: Optional[float] = None
    battery: Optional[int] = None
    firmware: Optional[str] = None
    battery_minutes: int = DEFAULT_BATTERY_MINUTES
    power: Optional[str] = None
    # Whether its lights blink the house's state while it is on USB power.
    # On battery they are always off: the board is asleep between asks.
    light: bool = True

    def public(self) -> Dict[str, object]:
        """What the settings page may see. Never the hash."""
        return {
            "display_id": self.display_id,
            "name": self.name,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "battery": self.battery,
            "firmware": self.firmware,
            "battery_minutes": self.battery_minutes,
            "power": self.power,
            "light": self.light,
        }


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clean_name(value: object) -> str:
    name = " ".join(str(value or "").split())[:MAX_NAME_LENGTH]
    return name


def clean_battery(value: object) -> Optional[int]:
    try:
        level = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return level if 0 <= level <= 100 else None


def clean_firmware(value: object) -> Optional[str]:
    text = "".join(ch for ch in str(value or "") if ch.isalnum() or ch in ".-_+")
    return text[:32] or None


def clean_power(value: object) -> Optional[str]:
    text = str(value or "").strip().lower()
    return text if text in POWER_SOURCES else None


def clean_battery_minutes(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        minutes = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return minutes if minutes in BATTERY_MINUTES_CHOICES else None


class DisplayRegistry:
    """The displays that may fetch the picture, and when each last did."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._displays: Dict[str, Display] = {}
        self._written_seen: Dict[str, float] = {}

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        displays: Dict[str, Display] = {}
        path = DISPLAYS_FILE
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for item in data.get("displays", []):
                    display = Display(
                        display_id=str(item["display_id"]),
                        name=clean_name(item.get("name")) or "Display",
                        token_hash=str(item["token_hash"]),
                        created_at=float(item.get("created_at") or 0.0),
                        last_seen_at=item.get("last_seen_at"),
                        battery=clean_battery(item.get("battery")),
                        firmware=clean_firmware(item.get("firmware")),
                        battery_minutes=clean_battery_minutes(item.get("battery_minutes"))
                        or DEFAULT_BATTERY_MINUTES,
                        power=clean_power(item.get("power")),
                        light=item.get("light") is not False,
                    )
                    if len(display.token_hash) == 64:
                        displays[display.display_id] = display
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                # A damaged file must not leave an old key working, so nothing
                # is trusted from it: every display has to be added again.
                logger.error("Could not read %s, no display keys loaded: %s", path, exc)
                displays = {}
        with self._lock:
            self._displays = displays
            self._written_seen = {
                key: item.last_seen_at or 0.0 for key, item in displays.items()
            }

    def _save_locked(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = DISPLAYS_FILE
        tmp = path.with_suffix(".tmp")
        payload = {
            "version": SCHEMA_VERSION,
            "displays": [asdict(item) for item in self._displays.values()],
        }
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        tmp.chmod(0o600)
        tmp.replace(path)
        for key, item in self._displays.items():
            self._written_seen[key] = item.last_seen_at or 0.0

    # -- administration ----------------------------------------------------

    def list(self) -> List[Dict[str, object]]:
        with self._lock:
            items = sorted(self._displays.values(), key=lambda item: item.created_at)
            return [item.public() for item in items]

    def create(self, name: str, now: Optional[float] = None) -> Tuple[Dict[str, object], str]:
        """Add a display. Returns its public record and the key, once."""
        cleaned = clean_name(name)
        if not cleaned:
            raise ValueError("Give the display a name.")
        with self._lock:
            if len(self._displays) >= MAX_DISPLAYS:
                raise ValueError(f"At most {MAX_DISPLAYS} displays can be added.")
            token = TOKEN_PREFIX + secrets.token_urlsafe(32)
            display = Display(
                display_id=secrets.token_hex(6),
                name=cleaned,
                token_hash=_hash(token),
                created_at=time.time() if now is None else now,
            )
            self._displays[display.display_id] = display
            self._save_locked()
            return display.public(), token

    def revoke(self, display_id: str) -> bool:
        with self._lock:
            if self._displays.pop(display_id, None) is None:
                return False
            self._written_seen.pop(display_id, None)
            self._save_locked()
            return True

    def set_battery_minutes(self, display_id: str, minutes: object) -> Dict[str, object]:
        cleaned = clean_battery_minutes(minutes)
        if cleaned is None:
            choices = ", ".join(str(item) for item in BATTERY_MINUTES_CHOICES)
            raise ValueError(f"Choose one of {choices} minutes.")
        with self._lock:
            item = self._displays.get(display_id)
            if item is None:
                raise KeyError(display_id)
            item.battery_minutes = cleaned
            self._save_locked()
            return item.public()

    def set_light(self, display_id: str, on: bool) -> Dict[str, object]:
        with self._lock:
            item = self._displays.get(display_id)
            if item is None:
                raise KeyError(display_id)
            item.light = bool(on)
            self._save_locked()
            return item.public()

    # -- use ---------------------------------------------------------------

    def verify(self, token: Optional[str]) -> Optional[str]:
        """The display id a key belongs to, or None.

        Every stored hash is compared, in constant time, so neither a match
        nor its position in the list shows up in how long the answer takes.
        """
        if not token or not token.startswith(TOKEN_PREFIX) or len(token) > 128:
            return None
        digest = _hash(token)
        found: Optional[str] = None
        with self._lock:
            for display_id, item in self._displays.items():
                if hmac.compare_digest(digest, item.token_hash):
                    found = display_id
        return found

    def seen(
        self, display_id: str, *, battery: object = None, firmware: object = None,
        power: object = None, now: Optional[float] = None,
    ) -> None:
        when = time.time() if now is None else now
        with self._lock:
            item = self._displays.get(display_id)
            if item is None:
                return
            level = clean_battery(battery)
            version = clean_firmware(firmware)
            source = clean_power(power)
            changed = (
                (level is not None and level != item.battery)
                or (version is not None and version != item.firmware)
                or (source is not None and source != item.power)
            )
            item.last_seen_at = when
            if level is not None:
                item.battery = level
            if version is not None:
                item.firmware = version
            if source is not None:
                item.power = source
            due = when - self._written_seen.get(display_id, 0.0) >= SEEN_WRITE_INTERVAL
            if changed or due:
                try:
                    self._save_locked()
                except OSError as exc:
                    logger.warning("Could not record when a display was last seen: %s", exc)

    def battery(self, display_id: str) -> Optional[int]:
        with self._lock:
            item = self._displays.get(display_id)
            return item.battery if item else None

    def battery_minutes(self, display_id: str) -> int:
        with self._lock:
            item = self._displays.get(display_id)
            return item.battery_minutes if item else DEFAULT_BATTERY_MINUTES

    def light(self, display_id: str) -> bool:
        with self._lock:
            item = self._displays.get(display_id)
            return item.light if item else True
