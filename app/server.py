"""
Nobø Energy Hub Web Control Server
FastAPI backend for local control of Nobø heating system via pynobo library
"""

import os
import re
import asyncio
import ipaddress
import logging
import threading
from collections import deque
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse
from pydantic import BaseModel
import copy
import pynobo
import auth
import away_schedule
import config_persistence

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ===== CONFIGURATION =====
# These are the *defaults*, taken from environment variables (set via .env).
# They can be overridden at runtime from the web interface, in which case the
# chosen values are stored in data/hub_config.json and reloaded on every start
# (see _load_persisted_hub_config below). That file always wins over the
# environment, so a setting made in the UI survives restarts and reboots.
NOBO_SERIAL = os.environ.get('NOBO_SERIAL', '111111111111')  # Replace with your hub's 12-digit serial number
NOBO_IP = os.environ.get('NOBO_IP', '10.0.0.100')  # Replace with your hub's IP address

# Demo mode - set to True to use simulated data instead of connecting to real hub
# Can be enabled via environment variable or using the test serial number
DEMO_MODE = os.environ.get('NOBO_DEMO', '').lower() in ('true', '1', 'yes') or NOBO_SERIAL == '111111111111'

# Where the currently active settings came from: "environment" or "web interface".
HUB_CONFIG_SOURCE = "environment"
DEMO_SOFTWARE_VERSION = "1.4.0 (Simulated)"  # Software version shown in demo mode

# Demo mode zone data - 8 grouped zones with realistic Norwegian indoor temperatures
# Hardcoded defaults (used on first run or if the persisted file is missing/corrupt).
_DEFAULT_DEMO_ZONES = [
    {
        "zone_id": "1",
        "name": "Large Bathroom",
        "icon": "🛁",
        "rooms": ["Large Bathroom"],
        "components": ["210000016247"],  # NTB-2R device
        "component_names": ["Large Bathroom Heater"],
        "current_temp": 24.2,
        "comfort_temp": 24.0,
        "eco_temp": 21.0,
        "mode": "comfort",
        "override_id": None
    },
    {
        "zone_id": "2",
        "name": "Small Bathroom",
        "icon": "🛁",
        "rooms": ["Small Bathroom"],
        "components": ["210000016248"],  # NTB-2R device
        "component_names": ["Small Bathroom Heater"],
        "current_temp": 23.8,
        "comfort_temp": 23.5,
        "eco_temp": 20.5,
        "mode": "comfort",
        "override_id": None
    },
    {
        "zone_id": "3",
        "name": "Hallway",
        "icon": "🚪",
        "rooms": ["Hallway"],
        "components": ["000000016249"],  # NTB-2R device (000-prefix)
        "component_names": ["Hallway Heater"],
        "current_temp": 21.5,
        "comfort_temp": 21.0,
        "eco_temp": 19.0,
        "mode": "normal",
        "override_id": None
    },
    {
        "zone_id": "4",
        "name": "Upstairs Bedrooms",
        "icon": "🛏️",
        "rooms": ["North", "South"],
        "components": ["160004028112", "160004028113"],  # R80 RDC 700 devices
        "component_names": ["North Room Heater", "South Room Heater"],
        "current_temp": None,  # R80 has no built-in temperature sensor
        "comfort_temp": 21.0,
        "eco_temp": 18.0,
        "mode": "eco",
        "override_id": None
    },
    {
        "zone_id": "5",
        "name": "Living Area",
        "icon": "🍳🛋️",
        "rooms": ["Kitchen", "Living Room"],
        "components": ["160004028114", "160004028115"],  # R80 RDC 700 devices
        "component_names": ["Kitchen Heater", "Living Room Heater"],
        "current_temp": None,  # R80 has no built-in temperature sensor
        "comfort_temp": 21.0,
        "eco_temp": 19.0,
        "mode": "normal",
        "override_id": None
    },
    {
        "zone_id": "6",
        "name": "Tech Room",
        "icon": "💻",
        "rooms": ["Tech Room"],
        "components": ["160004028116"],  # R80 RDC 700 device
        "component_names": ["Tech Room Heater"],
        "current_temp": None,  # R80 has no built-in temperature sensor
        "comfort_temp": 21.5,
        "eco_temp": 19.0,
        "mode": "comfort",
        "override_id": None
    },
    {
        "zone_id": "7",
        "name": "Downstairs Bedrooms",
        "icon": "🛏️",
        "rooms": ["Master", "North", "South"],
        "components": ["160004028117", "160004028118", "160004028119"],  # R80 RDC 700 devices
        "component_names": ["Master Heater", "North Heater", "South Heater"],
        "current_temp": None,  # R80 has no built-in temperature sensor
        "comfort_temp": 20.5,
        "eco_temp": 18.0,
        "mode": "eco",
        "override_id": None
    },
    {
        "zone_id": "8",
        "name": "Laundry Room",
        "icon": "🧺",
        "rooms": ["Laundry Room"],
        "components": ["000000016250", "160004028120"],  # Mixed: NTB-2R + R80 RDC 700
        "component_names": ["Laundry Heater", "Drying Area Controller"],
        "current_temp": 18.5,  # NTB-2R provides temperature reading
        "comfort_temp": 22.0,
        "eco_temp": 18.0,
        "mode": "normal",
        "override_id": None
    },
]

# Load persisted demo zones from disk, falling back to the hardcoded defaults on first run
# or when the persisted file is missing/corrupt.  DEMO_ZONES is always the same list
# object so in-place mutations (append / remove / clear+extend in tests) work correctly.
_loaded_zones = config_persistence.load_demo_zones()
if _loaded_zones is not None:
    DEMO_ZONES: list = _loaded_zones
    logger.info("Demo zones: loaded from %s", config_persistence.DEMO_ZONES_FILE)
else:
    DEMO_ZONES = copy.deepcopy(_DEFAULT_DEMO_ZONES)
    logger.info("Demo zones: using hardcoded defaults (no persisted store found)")

# Away temperature (set by Nobø, not configurable)
AWAY_TEMPERATURE = 7.0

# Default demo schedule — shared by get_current_schedule_mode() and get_zone_schedule()
DEFAULT_DEMO_SCHEDULE = {
    'monday':    [{'start': '00:00', 'end': '07:00', 'mode': 'eco'},
                  {'start': '07:00', 'end': '22:00', 'mode': 'comfort'},
                  {'start': '22:00', 'end': '24:00', 'mode': 'eco'}],
    'tuesday':   [{'start': '00:00', 'end': '07:00', 'mode': 'eco'},
                  {'start': '07:00', 'end': '22:00', 'mode': 'comfort'},
                  {'start': '22:00', 'end': '24:00', 'mode': 'eco'}],
    'wednesday': [{'start': '00:00', 'end': '07:00', 'mode': 'eco'},
                  {'start': '07:00', 'end': '22:00', 'mode': 'comfort'},
                  {'start': '22:00', 'end': '24:00', 'mode': 'eco'}],
    'thursday':  [{'start': '00:00', 'end': '07:00', 'mode': 'eco'},
                  {'start': '07:00', 'end': '22:00', 'mode': 'comfort'},
                  {'start': '22:00', 'end': '24:00', 'mode': 'eco'}],
    'friday':    [{'start': '00:00', 'end': '07:00', 'mode': 'eco'},
                  {'start': '07:00', 'end': '22:00', 'mode': 'comfort'},
                  {'start': '22:00', 'end': '24:00', 'mode': 'eco'}],
    'saturday':  [{'start': '00:00', 'end': '09:00', 'mode': 'eco'},
                  {'start': '09:00', 'end': '23:00', 'mode': 'comfort'},
                  {'start': '23:00', 'end': '24:00', 'mode': 'eco'}],
    'sunday':    [{'start': '00:00', 'end': '09:00', 'mode': 'eco'},
                  {'start': '09:00', 'end': '23:00', 'mode': 'comfort'},
                  {'start': '23:00', 'end': '24:00', 'mode': 'eco'}],
}

# ========================

# Global variables
hub: Optional[pynobo.nobo] = None
connected_websockets: List[WebSocket] = []
hub_connected = False
hub_thread: Optional[threading.Thread] = None
main_event_loop: Optional[asyncio.AbstractEventLoop] = None
websocket_lock = asyncio.Lock()  # Lock for thread-safe websocket list access
connection_lock = threading.Lock()  # Lock for thread-safe hub_connected access
log_lock = threading.Lock()  # Lock for thread-safe command log access

# Command log buffer — keeps the last 500 entries
command_log: deque = deque(maxlen=500)

# In-memory store for demo-mode schedule changes (keyed by zone_id).
# Populated from disk on startup; persisted to disk on every write.
demo_schedules: Dict[str, dict] = config_persistence.load_demo_schedules()

# Tracks whether the current global mode was set manually or by the away schedule.
# Loaded from disk on startup; persisted to disk on every change.
_server_state = config_persistence.load_server_state()
global_mode_source: str = _server_state.get("global_mode_source", "manual")  # "manual" | "schedule"


def local_now() -> datetime:
    """Current time in the machine's local timezone, as an aware datetime.

    Everything the user sees or schedules is wall-clock time: "comfort from
    07:00 on weekdays" means 07:00 on the kitchen clock. The container, however,
    runs in UTC unless it is told otherwise, so a plain datetime.now() silently
    ran the week schedule in the wrong timezone (QA defect D-06). compose.yml now
    shares the host's timezone with the container, and this helper returns an
    aware datetime so timestamps sent to the browser carry their offset instead
    of being guessed at.
    """
    return datetime.now().astimezone()


def local_timezone_name() -> str:
    """Human-readable name of the timezone the app is running in, e.g. 'CEST'."""
    return local_now().strftime("%Z") or "UTC"


def add_log_entry(direction: str, description: str, command: str = "", source: str = "api"):
    """Add an entry to the command log buffer (thread-safe)."""
    entry = {
        "timestamp": local_now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
        "direction": direction,       # "sent" | "received" | "error"
        "command": command,
        "description": description,
        "source": source,             # "api" | "hub" | "websocket"
    }
    with log_lock:
        command_log.append(entry)


# ===== Helper Functions =====
def detect_device_type(serial: str) -> tuple[str, bool, bool]:
    """
    Detect device type from serial number prefix using pynobo MODELS.
    
    Args:
        serial: 12-digit serial number (with or without spaces)
    
    Returns:
        tuple: (device_name, supports_comfort, supports_eco)
    """
    # Remove spaces and ensure it's a string
    serial_clean = str(serial).replace(' ', '').strip()
    
    # Get first 3 digits (model prefix)
    if len(serial_clean) < 3:
        return ("Unknown", False, False)
    
    model_prefix = serial_clean[:3]
    
    # Look up in pynobo MODELS
    if model_prefix in pynobo.nobo.MODELS:
        model = pynobo.nobo.MODELS[model_prefix]
        return (model.name, model.supports_comfort, model.supports_eco)
    
    # Fallback: some devices use 000 prefix (legacy firmware or manufacturing variant) and are NTB-2R compatible
    if model_prefix == '000':
        return ("NTB-2R", True, True)
    
    # Default for unknown models
    return ("Unknown", False, False)


def format_serial_display(serial: str) -> str:
    """
    Format serial number for display with spaces: XXX XXX XXX XXX
    
    Args:
        serial: 12-digit serial number
    
    Returns:
        Formatted serial with spaces
    """
    serial_clean = str(serial).replace(' ', '').strip()
    if len(serial_clean) == 12:
        return f"{serial_clean[0:3]} {serial_clean[3:6]} {serial_clean[6:9]} {serial_clean[9:12]}"
    return serial_clean


def parse_serial_input(serial: str) -> str:
    """
    Parse serial number input (with or without spaces) to 12-digit format.
    
    Args:
        serial: Serial number input
    
    Returns:
        12-digit serial without spaces
    """
    return str(serial).replace(' ', '').strip()


def validate_serial(serial: str) -> tuple[bool, str]:
    """Validate and clean a device serial number.
    Returns (is_valid, cleaned_serial_or_error_message)."""
    clean = str(serial).replace(' ', '').strip()
    if not re.fullmatch(r'\d{12}', clean):
        return False, "Serial number must be exactly 12 digits (0-9 only)"
    return True, clean


def validate_ip(ip: str) -> tuple[bool, str]:
    """Validate and clean an IPv4 address.
    Returns (is_valid, cleaned_ip_or_error_message)."""
    clean = str(ip).strip()
    try:
        return True, str(ipaddress.IPv4Address(clean))
    except ValueError:
        return False, "IP address must be a valid IPv4 address (e.g. 192.168.1.100)"


def _load_persisted_hub_config() -> None:
    """Apply data/hub_config.json over the environment defaults, if it exists.

    Called once at import time so the settings chosen in the web interface are
    restored automatically on every start, including after a reboot.
    """
    global NOBO_SERIAL, NOBO_IP, DEMO_MODE, HUB_CONFIG_SOURCE

    stored = config_persistence.load_hub_config()
    if not stored:
        return

    serial_ok, serial = validate_serial(stored.get("serial", ""))
    ip_ok, ip = validate_ip(stored.get("ip", ""))
    demo = bool(stored.get("demo_mode", False))

    # Only trust a stored real-hub config if the values are actually usable;
    # otherwise fall back to demo mode rather than looping on a bad address.
    if not demo and not (serial_ok and ip_ok):
        logger.warning(
            "Stored hub config is invalid (serial_ok=%s, ip_ok=%s) — falling back to demo mode",
            serial_ok, ip_ok,
        )
        demo = True

    if serial_ok:
        NOBO_SERIAL = serial
    if ip_ok:
        NOBO_IP = ip
    DEMO_MODE = demo
    HUB_CONFIG_SOURCE = "web interface"
    logger.info(
        "Loaded hub config from disk: demo_mode=%s serial=%s ip=%s",
        DEMO_MODE, NOBO_SERIAL if not DEMO_MODE else "(demo)", NOBO_IP if not DEMO_MODE else "(demo)",
    )


_load_persisted_hub_config()


# ===== Lifespan Context Manager =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events"""
    global main_event_loop
    # Startup
    mode_label = "demo" if DEMO_MODE else "production"
    logger.info("Starting Nobø Web Control Server — mode=%s", mode_label)
    logger.info(
        "Config data dir: %s  |  demo_zones=%s  |  global_mode_source=%s",
        config_persistence.DATA_DIR,
        "loaded from disk" if config_persistence.DEMO_ZONES_FILE.exists() else "defaults",
        global_mode_source,
    )
    # Week schedules run on wall-clock time, so log the timezone the app believes
    # it is in. If this says UTC on a machine that is not, schedules will fire at
    # the wrong hour (QA defect D-06) and the /etc/localtime mount in compose.yml
    # is missing.
    logger.info(
        "Local time: %s (%s)",
        local_now().strftime("%Y-%m-%d %H:%M:%S %z"),
        local_timezone_name(),
    )
    main_event_loop = asyncio.get_running_loop()
    try:
        await connect_to_hub()
    except Exception as e:
        logger.error(f"Failed to connect to hub on startup: {e}")
        # Don't fail startup - allow server to run and show disconnected state
    
    # Start background reconnection task (no-op in demo mode)
    reconnect_task = asyncio.create_task(reconnect_loop())
    # Start background away-schedule checker
    schedule_task = asyncio.create_task(away_schedule_loop())

    yield
    
    # Shutdown
    reconnect_task.cancel()
    schedule_task.cancel()
    logger.info("Shutting down server...")
    # Close all websocket connections
    for ws in connected_websockets:
        try:
            await ws.close()
        except:
            pass
    connected_websockets.clear()
    
    # Disconnect from hub
    with connection_lock:
        current_hub = hub
    if current_hub:
        try:
            current_hub.stop()
        except:
            pass


app = FastAPI(title="Nobø Web Control", version="1.0.0", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Authentication — initialise user store and attach middleware
# ---------------------------------------------------------------------------
auth.init_user_store()


# ---------------------------------------------------------------------------
# Authentication policy
# ---------------------------------------------------------------------------
# Deny-by-default: every path needs a valid session except these. /api/health is
# public so container healthchecks and monitoring keep working without a login.
PUBLIC_PATHS = frozenset({"/login", "/auth/login", "/favicon.ico", "/api/health"})

# Escape hatch for headless integrations. Off unless explicitly enabled.
ALLOW_ANON_API = os.environ.get('NOBO_ALLOW_ANON_API', '').lower() in ('true', '1', 'yes')
if ALLOW_ANON_API:
    logger.warning(
        "NOBO_ALLOW_ANON_API is enabled: /api/* and /ws are reachable without a login. "
        "Only do this on a trusted network."
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """Require a valid session for every request except a small public allow-list.

    The policy is deny-by-default: anything not explicitly listed in
    ``PUBLIC_PATHS`` needs a valid ``session_id`` cookie. Browsers asking for a
    page are redirected to the login screen; API clients get a JSON 401 so they
    can tell the difference between "not logged in" and "no such endpoint".

    ``NOBO_ALLOW_ANON_API=true`` re-opens ``/api/*`` and ``/ws`` for headless
    integrations (Home Assistant, scripts, dashboards). It is off by default and
    should only be enabled on a network you trust.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in PUBLIC_PATHS:
            return await call_next(request)

        if ALLOW_ANON_API and (path.startswith("/api/") or path == "/ws"):
            return await call_next(request)

        session_id = request.cookies.get("session_id")
        if session_id and auth.get_session(session_id):
            return await call_next(request)

        if path.startswith("/api/") or path.startswith("/auth/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return RedirectResponse(url="/login", status_code=302)


app.add_middleware(AuthMiddleware)


# ---------------------------------------------------------------------------
# Live update policy
# ---------------------------------------------------------------------------
# Anything that changes heating state has to reach every other open browser and
# phone, otherwise a second screen keeps showing stale zones until the page is
# reloaded (QA defect D-02).
#
# This is done in one middleware rather than in each handler on purpose: there
# are more than a dozen mutating endpoints and any new one would silently
# inherit the bug if it forgot the call. Broadcasting from here means every
# successful write is covered, including endpoints added later.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Writes that cannot change zone state, so there is nothing to push.
# /api/hub/config is excluded because switching hub or demo mode already
# broadcasts from apply_hub_config once the new zones are actually loaded.
NO_BROADCAST_PATHS = frozenset({"/api/log/clear", "/api/hub/config"})


class ZoneBroadcastMiddleware(BaseHTTPMiddleware):
    """Push fresh zone data to all WebSocket clients after a successful write."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        path = request.url.path
        if (
            request.method in MUTATING_METHODS
            and path.startswith("/api/")
            and path not in NO_BROADCAST_PATHS
            and response.status_code < 400
        ):
            # Fire and forget so the caller is not kept waiting for every other
            # client to be written to.
            asyncio.create_task(broadcast_zone_update())

        return response


app.add_middleware(ZoneBroadcastMiddleware)


# ===== Pydantic Models =====
class TemperatureUpdate(BaseModel):
    comfort: Optional[float] = None
    eco: Optional[float] = None


class ZoneInfo(BaseModel):
    zone_id: str
    name: str
    current_temperature: float
    comfort_temperature: float
    eco_temperature: float
    current_mode: str
    active_override_id: Optional[str] = None
    device_type: str
    supports_temp_adjust: bool


class ZoneAdd(BaseModel):
    name: str
    icon: str = ""


class ZoneUpdate(BaseModel):
    name: Optional[str] = None
    icon: Optional[str] = None


class HubConfigUpdate(BaseModel):
    demo_mode: bool
    serial: Optional[str] = None
    ip: Optional[str] = None


VALID_SCHEDULE_MODES = {'comfort', 'eco', 'away'}
SCHEDULE_DAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
_TIME_RE = re.compile(r'^(?:[01]\d|2[0-3]):[0-5]\d$|^24:00$')


class ScheduleBlock(BaseModel):
    start: str
    end: str
    mode: str

    @classmethod
    def _parse_minutes(cls, t: str) -> int:
        h, m = t.split(':')
        return int(h) * 60 + int(m)

    def validate_fields(self) -> None:
        """Raise ValueError if any field is invalid."""
        if not _TIME_RE.match(self.start):
            raise ValueError(f"Invalid start time: {self.start!r}")
        if not _TIME_RE.match(self.end):
            raise ValueError(f"Invalid end time: {self.end!r}")
        if self.mode not in VALID_SCHEDULE_MODES:
            raise ValueError(f"Invalid mode {self.mode!r}; must be one of {sorted(VALID_SCHEDULE_MODES)}")
        if self._parse_minutes(self.end) <= self._parse_minutes(self.start):
            raise ValueError(f"Block end ({self.end}) must be after start ({self.start})")


class ScheduleUpdate(BaseModel):
    """Validated weekly schedule payload for POST /api/zones/{zone_id}/schedule."""
    schedule: Dict[str, List[ScheduleBlock]]

    def validate_schedule(self) -> None:
        """Raise ValueError describing the first problem found."""
        missing = [d for d in SCHEDULE_DAYS if d not in self.schedule]
        if missing:
            raise ValueError(f"Missing days: {missing}")
        extra = [d for d in self.schedule if d not in SCHEDULE_DAYS]
        if extra:
            raise ValueError(f"Unknown days: {extra}")

        for day, blocks in self.schedule.items():
            if not blocks:
                raise ValueError(f"Day {day!r} has no time blocks")
            # Individual block validation
            for b in blocks:
                b.validate_fields()
            # Sort by start time and check coverage 00:00 → 24:00 without gaps/overlaps
            sorted_blocks = sorted(blocks, key=lambda b: b._parse_minutes(b.start))
            if sorted_blocks[0].start != '00:00':
                raise ValueError(f"Day {day!r} must start at 00:00 (got {sorted_blocks[0].start!r})")
            if sorted_blocks[-1].end != '24:00':
                raise ValueError(f"Day {day!r} must end at 24:00 (got {sorted_blocks[-1].end!r})")
            for i in range(len(sorted_blocks) - 1):
                if sorted_blocks[i].end != sorted_blocks[i + 1].start:
                    raise ValueError(
                        f"Day {day!r}: gap/overlap between block ending {sorted_blocks[i].end!r} "
                        f"and block starting {sorted_blocks[i + 1].start!r}"
                    )


# ===== Hub Connection & Callbacks =====
def connect_to_hub_sync():
    """Connect to the Nobø Hub (synchronous, runs in thread)"""
    global hub, hub_connected
    
    try:
        logger.info(f"Connecting to Nobø Hub at {NOBO_IP} with serial {NOBO_SERIAL}...")
        new_hub = pynobo.nobo(NOBO_SERIAL, NOBO_IP, discover=False)
        with connection_lock:
            hub = new_hub
            hub_connected = True
        logger.info("Successfully connected to Nobø Hub")
        
        # Register callback for hub updates
        new_hub.register_callback(hub_update_callback)
        
    except Exception as e:
        logger.error(f"Failed to connect to Nobø Hub: {e}")
        with connection_lock:
            hub_connected = False
            hub = None
        raise


async def connect_to_hub():
    """Connect to the Nobø Hub (async wrapper)"""
    global hub_thread, hub_connected
    
    # Check if demo mode is enabled
    if DEMO_MODE:
        logger.info("Demo mode enabled - using simulated data")
        with connection_lock:
            hub_connected = True
        return
    
    # Run the synchronous connection in a thread to avoid event loop conflicts
    hub_thread = threading.Thread(target=connect_to_hub_sync, daemon=True)
    hub_thread.start()
    
    # Wait a moment for connection to establish
    await asyncio.sleep(2)


def disconnect_from_hub() -> None:
    """Stop and clear any live hub connection. Safe to call when not connected."""
    global hub, hub_connected

    with connection_lock:
        current_hub = hub

    if current_hub:
        try:
            current_hub.stop()
            logger.info("Disconnected from Nobø Hub")
        except Exception as exc:
            logger.warning("Error while disconnecting from hub: %s", exc)

    with connection_lock:
        hub = None
        hub_connected = False


async def apply_hub_config(demo_mode: bool, serial: str, ip: str) -> dict:
    """Switch the running server to a new hub configuration.

    Persists the settings first (so they survive a restart or reboot), then
    rebinds the module-level globals and rebuilds the hub connection in place.
    Returns a dict describing the resulting state.
    """
    global NOBO_SERIAL, NOBO_IP, DEMO_MODE, HUB_CONFIG_SOURCE

    config = {"demo_mode": bool(demo_mode), "serial": serial, "ip": ip}
    config_persistence.save_hub_config(config)

    previous_mode = "demo" if DEMO_MODE else "production"

    # Always drop the existing connection before changing the target.
    disconnect_from_hub()

    NOBO_SERIAL = serial
    NOBO_IP = ip
    DEMO_MODE = bool(demo_mode)
    HUB_CONFIG_SOURCE = "web interface"

    new_mode = "demo" if DEMO_MODE else "production"
    logger.info("Hub configuration changed: %s -> %s", previous_mode, new_mode)
    add_log_entry(
        "sent",
        f"Hub configuration changed to {new_mode} mode"
        + ("" if DEMO_MODE else f" (serial {format_serial_display(serial)} at {ip})"),
        source="api",
    )

    try:
        await connect_to_hub()
    except Exception as exc:
        logger.error("Failed to connect using the new hub configuration: %s", exc)

    with connection_lock:
        connected = hub_connected

    # Push the new zone list to every open browser tab.
    try:
        await broadcast_zone_update()
    except Exception as exc:
        logger.warning("Could not broadcast zone update after config change: %s", exc)

    return {
        "demo_mode": DEMO_MODE,
        "serial": NOBO_SERIAL,
        "ip": NOBO_IP,
        "connected": connected,
        "source": HUB_CONFIG_SOURCE,
    }


async def reconnect_loop():
    """Background task that monitors hub connectivity and reconnects with exponential backoff.

    The demo-mode check happens on every iteration rather than once at start-up,
    so the loop keeps working when the user switches between demo mode and a real
    hub from the web interface without restarting the server.
    """
    MIN_INTERVAL = 5      # Start at 5 seconds
    MAX_INTERVAL = 300     # Cap at 5 minutes
    interval = MIN_INTERVAL
    attempt = 0

    while True:
        await asyncio.sleep(interval)

        if DEMO_MODE:
            # Nothing to reconnect to while simulating; reset backoff state.
            interval = MIN_INTERVAL
            attempt = 0
            continue

        with connection_lock:
            currently_connected = hub_connected

        if not currently_connected:
            attempt += 1
            logger.warning(f"Hub disconnected — reconnect attempt #{attempt} (next retry in {interval}s)")
            add_log_entry("error", f"Hub disconnected — reconnect attempt #{attempt} (delay: {interval}s)", source="hub")
            try:
                await connect_to_hub()
                with connection_lock:
                    reconnected = hub_connected
                if reconnected:
                    logger.info(f"Hub reconnected successfully after {attempt} attempt(s)")
                    add_log_entry("received", f"Hub reconnected after {attempt} attempt(s)", source="hub")
                    interval = MIN_INTERVAL  # Reset on success
                    attempt = 0
                    await broadcast_zone_update()
                else:
                    interval = min(interval * 2, MAX_INTERVAL)  # Exponential backoff
            except Exception as exc:
                logger.error(f"Reconnection attempt #{attempt} failed: {exc}")
                interval = min(interval * 2, MAX_INTERVAL)  # Exponential backoff
        else:
            # Connected — reset backoff state
            if interval != MIN_INTERVAL:
                interval = MIN_INTERVAL
                attempt = 0


def hub_update_callback(hub_instance):
    """Callback function triggered when hub data changes"""
    logger.info("Hub data updated - broadcasting to websocket clients")
    add_log_entry("received", "Hub data update received", source="hub")
    
    # Schedule the broadcast in the main event loop
    if main_event_loop is not None and main_event_loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast_zone_update(), main_event_loop)
    else:
        logger.warning("Cannot broadcast zone update: main event loop not available")


async def broadcast_zone_update():
    """Send updated zone data to all connected WebSocket clients"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub
    
    if not connected:
        return
    if not DEMO_MODE and not current_hub:
        return
    
    try:
        zones_data = get_zones_data()
        message = {
            "type": "zones_update",
            "data": zones_data,
            "timestamp": local_now().isoformat()
        }
        
        # Send to all connected clients (thread-safe)
        async with websocket_lock:
            disconnected = []
            for websocket in connected_websockets:
                try:
                    await websocket.send_json(message)
                except Exception as e:
                    logger.warning(f"Failed to send to websocket: {e}")
                    disconnected.append(websocket)
            
            # Remove disconnected clients
            for ws in disconnected:
                if ws in connected_websockets:
                    connected_websockets.remove(ws)
                
    except Exception as e:
        logger.error(f"Error broadcasting zone update: {e}")


def get_current_schedule_mode(zone_id: str) -> str:
    """Determine which schedule mode is currently active for a zone.

    Checks the current day of the week and time against the zone's week
    profile to find the active schedule block.  Falls back to 'comfort'
    when no matching block is found.
    """
    def _time_to_minutes(t: str) -> int:
        """Convert HH:MM time string to minutes since midnight. '24:00' → 1440."""
        try:
            h, m = t.split(':')
            return int(h) * 60 + int(m)
        except (ValueError, AttributeError):
            return 0

    now = local_now()
    day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    current_day = day_names[now.weekday()]
    current_minutes = now.hour * 60 + now.minute

    # Build a day schedule regardless of demo / real-hub mode
    day_schedule = None

    if DEMO_MODE:
        # Check for user-saved schedule first, fall back to default
        saved = demo_schedules.get(zone_id, DEFAULT_DEMO_SCHEDULE)
        day_schedule = saved.get(current_day)
    else:
        with connection_lock:
            current_hub = hub
        if current_hub:
            try:
                zone = current_hub.zones.get(zone_id)
                if zone:
                    week_profile_id = zone.get('week_profile_id')
                    if week_profile_id and week_profile_id in current_hub.week_profiles:
                        status = current_hub.get_week_profile_status(week_profile_id)
                        # get_week_profile_status() may return a pynobo API integer
                        # constant instead of a string — map it back to the string
                        # values used throughout the rest of the application.
                        if isinstance(status, str) and status in ('comfort', 'eco', 'away'):
                            return status
                        mode_reverse_map = {
                            pynobo.nobo.API.OVERRIDE_MODE_COMFORT: 'comfort',
                            pynobo.nobo.API.OVERRIDE_MODE_ECO: 'eco',
                            pynobo.nobo.API.OVERRIDE_MODE_AWAY: 'away',
                        }
                        return mode_reverse_map.get(status, 'comfort')
            except Exception as e:
                logger.error(f"Error reading week profile for zone {zone_id}: {e}")
            return 'comfort'

    if day_schedule:
        for block in day_schedule:
            start_min = _time_to_minutes(block.get('start', '00:00'))
            end_min = _time_to_minutes(block.get('end', '24:00'))
            if start_min <= current_minutes < end_min:
                return block.get('mode', 'comfort')

    return 'comfort'


def get_zones_data() -> List[Dict[str, Any]]:
    """Get current data for all zones"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub
    
    if not connected:
        return []
    
    # Demo mode - return simulated data
    if DEMO_MODE:
        zones = []
        for demo_zone in DEMO_ZONES:
            # Detect device type for EACH component individually
            components_types = []
            any_supports_temp = False
            any_manual = False
            for comp_serial in demo_zone['components']:
                cname, csupports_comfort, csupports_eco = detect_device_type(comp_serial)
                components_types.append(cname)
                if csupports_comfort or csupports_eco:
                    any_supports_temp = True
                else:
                    any_manual = True

            # Use first component's type for the zone-level device_type field
            device_name = components_types[0] if components_types else "Unknown"

            # Format components for display
            components_display = [format_serial_display(c) for c in demo_zone['components']]

            # Component friendly names
            components_names = demo_zone.get('component_names', [''] * len(demo_zone['components']))

            zones.append({
                'zone_id': demo_zone['zone_id'],
                'name': demo_zone['name'],
                'icon': demo_zone.get('icon', ''),
                'rooms': demo_zone.get('rooms', []),
                'components': demo_zone['components'],
                'components_display': components_display,
                'components_types': components_types,
                'components_names': components_names,
                'current_temperature': demo_zone['current_temp'],
                'comfort_temperature': demo_zone['comfort_temp'],
                'eco_temperature': demo_zone['eco_temp'],
                'away_temperature': AWAY_TEMPERATURE,
                'current_mode': demo_zone['mode'],
                'schedule_mode': get_current_schedule_mode(demo_zone['zone_id']) if demo_zone['mode'] == 'normal' else None,
                'active_override_id': demo_zone.get('override_id'),
                'device_type': device_name,
                'supports_comfort': any_supports_temp,
                'supports_eco': any_supports_temp,
                'supports_temp_adjust': any_supports_temp,
                'has_manual_devices': any_manual,
            })
        return zones
    
    # Real hub mode
    if not current_hub:
        return []
    
    zones = []
    try:
        for zone_id, zone in current_hub.zones.items():
            zone_name = zone.get('name', f'Zone {zone_id}')
            
            # Get components for this zone
            zone_components = []
            for comp_id, comp in current_hub.components.items():
                if comp.get('zone_id', '') == zone_id:
                    zone_components.append(comp_id)
            
            # Detect device type for EACH component individually
            components_types = []
            any_supports_temp = False
            any_manual = False
            for comp_serial in zone_components:
                cname, csupports_comfort, csupports_eco = detect_device_type(comp_serial)
                components_types.append(cname)
                if csupports_comfort or csupports_eco:
                    any_supports_temp = True
                else:
                    any_manual = True

            # Use first component's type for zone-level device_type field
            if zone_components:
                device_name = components_types[0]
            else:
                device_name = "Unknown"
            
            # Format components for display
            components_display = [format_serial_display(c) for c in zone_components]
            
            # Get current temperature using pynobo's helper (reads hub.temperatures dict)
            current_temp_raw = current_hub.get_current_zone_temperature(zone_id)
            if current_temp_raw is not None:
                try:
                    current_temp = float(current_temp_raw)
                except (ValueError, TypeError):
                    current_temp = None
            else:
                current_temp = None
            
            # Get comfort and eco temperatures (pynobo stores as whole-degree integers)
            comfort_temp = float(zone.get('temp_comfort_c', 21))
            eco_temp = float(zone.get('temp_eco_c', 17))
            
            # Determine current mode
            mode = determine_zone_mode(zone_id, zone)
            
            zones.append({
                'zone_id': str(zone_id),
                'name': zone_name,
                'icon': '',  # Could be configured per zone
                'rooms': [zone_name],  # Default to zone name
                'components': zone_components,
                'components_display': components_display,
                'components_types': components_types,
                'components_names': [''] * len(zone_components),
                'current_temperature': current_temp,
                'comfort_temperature': comfort_temp,
                'eco_temperature': eco_temp,
                'away_temperature': AWAY_TEMPERATURE,
                'current_mode': mode,
                'schedule_mode': get_current_schedule_mode(str(zone_id)) if mode == 'normal' else None,
                'active_override_id': zone.get('deprecated_override_id'),
                'device_type': device_name,
                'supports_comfort': any_supports_temp,
                'supports_eco': any_supports_temp,
                'supports_temp_adjust': any_supports_temp,
                'has_manual_devices': any_manual,
            })
    except Exception as e:
        logger.error(f"Error getting zones data: {e}")
    
    return zones


def determine_zone_mode(zone_id: str, zone: Dict) -> str:
    """Determine the current mode of a zone.

    Uses pynobo's built-in helper which correctly handles both zone-specific
    and global overrides, and returns 'normal' when no override is active.
    """
    with connection_lock:
        current_hub = hub
    if current_hub is None:
        return 'normal'
    try:
        return current_hub.get_zone_override_mode(zone_id)
    except Exception as e:
        logger.error(f"Error determining zone mode for {zone_id}: {e}")
        return 'normal'


# ===== API Endpoints =====
@app.get("/api/health")
async def health_check():
    """Health check endpoint for monitoring and load balancers"""
    with connection_lock:
        connected = hub_connected
    return {
        "status": "ok",
        "connected": connected,
        "demo_mode": DEMO_MODE,
        "timestamp": local_now().isoformat(),
    }


@app.get("/api/status")
async def get_status():
    """Get connection status"""
    with connection_lock:
        connected = hub_connected

    schedule = away_schedule.load_schedule()
    now = datetime.now(timezone.utc)
    currently_active = away_schedule.is_schedule_active(schedule, now)

    return {
        "connected": connected,
        "demo_mode": DEMO_MODE,
        "hub_serial": NOBO_SERIAL if connected else None,
        "timestamp": local_now().isoformat(),
        # Week schedules run on wall-clock time, so the timezone the app thinks
        # it is in decides when they fire. Reporting it makes a misconfigured
        # container obvious instead of silently shifting every schedule.
        "timezone": local_timezone_name(),
        "away_schedule": {
            "enabled": schedule["enabled"],
            "start_at": schedule["start_at"],
            "end_at": schedule["end_at"],
            "currently_active": currently_active,
        },
        "global_mode_source": global_mode_source,
    }


@app.get("/api/hub/config")
async def get_hub_config():
    """Return the current hub connection settings, for the settings form."""
    with connection_lock:
        connected = hub_connected

    return {
        "demo_mode": DEMO_MODE,
        "serial": NOBO_SERIAL,
        "serial_display": format_serial_display(NOBO_SERIAL),
        "ip": NOBO_IP,
        "connected": connected,
        "source": HUB_CONFIG_SOURCE,
    }


@app.post("/api/hub/config")
async def update_hub_config(request: Request, body: HubConfigUpdate):
    """Switch between demo mode and a real hub from the web interface.

    Requires an admin session — unlike the read-only endpoints this changes how
    the whole system behaves, so it is not left open for local integrations.
    """
    session = _get_session_or_401(request)
    _require_admin(session)

    demo_mode = bool(body.demo_mode)

    if demo_mode:
        # Keep whatever serial/IP the user typed so the form is still populated
        # when they switch back, but do not demand that they be valid.
        serial = parse_serial_input(body.serial or NOBO_SERIAL)
        ip = str(body.ip or NOBO_IP).strip()
    else:
        serial_ok, serial = validate_serial(body.serial or "")
        if not serial_ok:
            raise HTTPException(status_code=400, detail=serial)

        ip_ok, ip = validate_ip(body.ip or "")
        if not ip_ok:
            raise HTTPException(status_code=400, detail=ip)

    result = await apply_hub_config(demo_mode, serial, ip)

    if not demo_mode and not result["connected"]:
        result["warning"] = (
            "Settings saved, but the hub could not be reached. Check the serial "
            "number and IP address, and make sure the official Nobo app is not "
            "connected to the hub."
        )

    # Switching data source changes zones, devices and schedules wholesale. Rather
    # than trying to patch every open page back into a consistent state, end the
    # session so the browser returns to the login page and starts clean.
    result["signed_out"] = True

    response = JSONResponse(result)
    session_id = request.cookies.get("session_id")
    if session_id:
        auth.delete_session(session_id)
    response.delete_cookie(key="session_id", path="/")
    return response


@app.get("/api/hub")
async def get_hub_info():
    """Get hub information"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub
    
    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")
    
    try:
        # Demo mode - return simulated hub info
        if DEMO_MODE:
            return {
                "name": "Nobø Hub",
                "serial": NOBO_SERIAL,
                "software_version": DEMO_SOFTWARE_VERSION,
                "connected": True,
                "demo_mode": True
            }
        
        # Real hub mode
        if not current_hub:
            raise HTTPException(status_code=503, detail="Hub not connected")
        
        # Get hub info from pynobo
        hub_info = {
            "name": getattr(current_hub, 'hub_name', 'Nobø Hub'),
            "serial": NOBO_SERIAL,
            "software_version": getattr(current_hub, 'hub_version', 'Unknown'),
            "connected": connected
        }
        return hub_info
    except Exception as e:
        logger.error(f"Error getting hub info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/zones")
async def get_zones():
    """Get all zones with current status"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub
    
    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")
    
    try:
        zones_data = get_zones_data()
        return {"zones": zones_data}
    except Exception as e:
        logger.error(f"Error getting zones: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/zones")
async def add_zone(zone: ZoneAdd):
    """Create a new zone"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub

    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")

    try:
        if DEMO_MODE:
            # Auto-increment zone_id based on current max
            new_id = str(max((int(z['zone_id']) for z in DEMO_ZONES), default=0) + 1)
            DEMO_ZONES.append({
                "zone_id": new_id,
                "name": zone.name.strip(),
                "icon": zone.icon.strip(),
                "rooms": [],
                "components": [],
                "component_names": [],
                "current_temp": None,
                "comfort_temp": 21.0,
                "eco_temp": 18.0,
                "mode": "normal",
                "override_id": None,
            })
            logger.info(f"Demo mode: Zone '{zone.name}' created with id {new_id}")
            config_persistence.save_demo_zones(DEMO_ZONES)
            return {"status": "success", "zone_id": new_id, "name": zone.name}

        # Real hub mode - not yet implemented
        raise HTTPException(status_code=501, detail="Add zone not yet implemented for real hub")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding zone: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/zones/{zone_id}")
async def update_zone(zone_id: str, update: ZoneUpdate):
    """Rename a zone and/or change its icon"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub

    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")

    try:
        if DEMO_MODE:
            demo_zone = next((z for z in DEMO_ZONES if z['zone_id'] == zone_id), None)
            if not demo_zone:
                raise HTTPException(status_code=404, detail="Zone not found")

            old_name = demo_zone['name']
            if update.name is not None:
                demo_zone['name'] = update.name.strip()
            if update.icon is not None:
                demo_zone['icon'] = update.icon.strip()

            add_log_entry(
                "sent",
                f"[DEMO] Zone '{old_name}' updated: name='{demo_zone['name']}' icon='{demo_zone['icon']}'",
                source="api",
            )
            logger.info(f"Demo mode: Zone {zone_id} updated")
            config_persistence.save_demo_zones(DEMO_ZONES)
            return {"status": "success", "zone_id": zone_id, "name": demo_zone['name'], "icon": demo_zone['icon']}

        # Real hub mode — icon is stored locally only (pynobo doesn't support icons)
        if not current_hub:
            raise HTTPException(status_code=503, detail="Hub not connected")

        if zone_id not in current_hub.zones:
            raise HTTPException(status_code=404, detail="Zone not found")

        zone = current_hub.zones[zone_id]
        old_name = zone.get('name', zone_id)

        if update.name is not None:
            current_hub.update_zone(zone_id, update.name.strip())
            add_log_entry(
                "sent",
                f"update_zone({zone_id}, '{update.name.strip()}')",
                command=f"update_zone zone_id={zone_id} name={update.name.strip()}",
                source="api",
            )

        await asyncio.sleep(0.3)
        return {"status": "success", "zone_id": zone_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating zone: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/zones/{zone_id}")
async def delete_zone(zone_id: str):
    """Delete a zone"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub

    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")

    try:
        if DEMO_MODE:
            demo_zone = next((z for z in DEMO_ZONES if z['zone_id'] == zone_id), None)
            if not demo_zone:
                raise HTTPException(status_code=404, detail="Zone not found")

            zone_name = demo_zone['name']
            DEMO_ZONES.remove(demo_zone)
            add_log_entry(
                "sent",
                f"[DEMO] Zone '{zone_name}' (id={zone_id}) deleted",
                source="api",
            )
            logger.info(f"Demo mode: Zone '{zone_name}' deleted")
            config_persistence.save_demo_zones(DEMO_ZONES)
            return {"status": "success", "zone_id": zone_id}

        # Real hub mode — not yet implemented
        raise HTTPException(status_code=501, detail="Delete zone not yet implemented for real hub")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting zone: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/zones/{zone_id}/override/{mode}")
async def set_zone_override(zone_id: str, mode: str):
    """Set override mode for a specific zone"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub
    
    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")
    
    # Validate mode — 'off' is not a valid Nobø Eco Hub override mode
    mode_map = {
        'comfort': pynobo.nobo.API.OVERRIDE_MODE_COMFORT,
        'eco': pynobo.nobo.API.OVERRIDE_MODE_ECO,
        'away': pynobo.nobo.API.OVERRIDE_MODE_AWAY,
        'normal': -1  # Special case: remove override
    }
    
    if mode not in mode_map:
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")
    
    try:
        # Demo mode - update simulated data
        if DEMO_MODE:
            demo_zone = next((z for z in DEMO_ZONES if z['zone_id'] == zone_id), None)
            if not demo_zone:
                raise HTTPException(status_code=404, detail="Zone not found")
            
            demo_zone['mode'] = mode
            add_log_entry(
                "sent",
                f"[DEMO] Would send: create_override(now, 0, {mode.upper()}, zone_{zone_id})",
                command=f"create_override now 0 {mode} {zone_id}",
                source="api",
            )
            add_log_entry(
                "received",
                f"[DEMO] Zone '{demo_zone['name']}' mode set to {mode}",
                source="api",
            )
            config_persistence.save_demo_zones(DEMO_ZONES)
            return {"status": "success", "zone_id": zone_id, "mode": mode}
        
        # Real hub mode
        if not current_hub:
            raise HTTPException(status_code=503, detail="Hub not connected")
        
        if mode == 'normal':
            # Remove override - return to schedule
            current_hub.create_override(
                pynobo.nobo.API.OVERRIDE_MODE_NORMAL,
                pynobo.nobo.API.OVERRIDE_TYPE_NOW,
                pynobo.nobo.API.OVERRIDE_TARGET_ZONE,
                zone_id,
            )
            add_log_entry(
                "sent",
                f"create_override(NORMAL, NOW, ZONE, zone_{zone_id}) — cancel override",
                command=f"create_override NORMAL NOW ZONE {zone_id}",
                source="api",
            )
        else:
            # Set override mode
            current_hub.create_override(
                mode_map[mode],
                pynobo.nobo.API.OVERRIDE_TYPE_NOW,
                pynobo.nobo.API.OVERRIDE_TARGET_ZONE,
                zone_id,
            )
            add_log_entry(
                "sent",
                f"create_override({mode.upper()}, NOW, ZONE, zone_{zone_id})",
                command=f"create_override {mode} NOW ZONE {zone_id}",
                source="api",
            )
        
        # Wait a moment for hub to update
        await asyncio.sleep(0.5)
        
        return {"status": "success", "zone_id": zone_id, "mode": mode}
    except Exception as e:
        logger.error(f"Error setting zone override: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/zones/{zone_id}/temperature")
async def set_zone_temperature(zone_id: str, temps: TemperatureUpdate):
    """Set comfort and/or eco temperature for a zone"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub
    
    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")
    
    try:
        # Demo mode - validate device type from DEMO_ZONES
        if DEMO_MODE:
            demo_zone = next((z for z in DEMO_ZONES if z['zone_id'] == zone_id), None)
            if not demo_zone:
                raise HTTPException(status_code=404, detail="Zone not found")
            
            # Check if any device in the zone supports temperature adjustment
            any_supports = False
            device_name = "Unknown"
            for i, comp in enumerate(demo_zone['components']):
                cname, csupports_comfort, csupports_eco = detect_device_type(comp)
                if i == 0:
                    device_name = cname
                if csupports_comfort or csupports_eco:
                    any_supports = True
                    break
            
            if not any_supports:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Temperature cannot be adjusted remotely for {device_name} devices. Temperature is set manually on the physical device."
                )
            
            # In demo mode, just validate and return success
            if temps.comfort is not None:
                if not 7 <= temps.comfort <= 30:
                    raise HTTPException(status_code=400, detail="Comfort temperature must be between 7 and 30°C")
                demo_zone['comfort_temp'] = temps.comfort
            if temps.eco is not None:
                if not 7 <= temps.eco <= 30:
                    raise HTTPException(status_code=400, detail="Eco temperature must be between 7 and 30°C")
                demo_zone['eco_temp'] = temps.eco
            
            add_log_entry(
                "sent",
                f"[DEMO] Would send: update_zone(zone_{zone_id}, comfort={temps.comfort}, eco={temps.eco})",
                command=f"update_zone {zone_id} comfort={temps.comfort} eco={temps.eco}",
                source="api",
            )
            config_persistence.save_demo_zones(DEMO_ZONES)
            return {"status": "success", "zone_id": zone_id, "comfort": temps.comfort, "eco": temps.eco}
        
        # Real hub mode
        if not current_hub:
            raise HTTPException(status_code=503, detail="Hub not connected")
        
        # Get current zone
        if zone_id not in current_hub.zones:
            raise HTTPException(status_code=404, detail="Zone not found")
        
        zone = current_hub.zones[zone_id]
        
        # Get components for this zone and auto-detect device type
        zone_components = []
        for comp_id, comp in current_hub.components.items():
            if comp.get('zone_id', '') == zone_id:
                zone_components.append(comp_id)
        
        any_supports = False
        device_name = "Unknown"
        for i, comp_serial in enumerate(zone_components):
            cname, csupports_comfort, csupports_eco = detect_device_type(comp_serial)
            if i == 0:
                device_name = cname
            if csupports_comfort or csupports_eco:
                any_supports = True
                break
        
        # Check if any device supports temperature adjustment
        if not any_supports:
            raise HTTPException(
                status_code=400, 
                detail=f"Temperature cannot be adjusted remotely for {device_name} devices. Temperature is set manually on the physical device."
            )
        
        # Validate temperatures (7-30°C range)
        if temps.comfort is not None:
            if not 7 <= temps.comfort <= 30:
                raise HTTPException(status_code=400, detail="Comfort temperature must be between 7 and 30°C")
        if temps.eco is not None:
            if not 7 <= temps.eco <= 30:
                raise HTTPException(status_code=400, detail="Eco temperature must be between 7 and 30°C")
        
        # Get current temperatures to avoid overwriting (pynobo stores as whole-degree integers)
        current_comfort = int(zone.get('temp_comfort_c', 21))
        current_eco = int(zone.get('temp_eco_c', 17))
        
        # Update temperatures using keyword arguments (pynobo expects whole-degree integers)
        if temps.comfort is not None and temps.eco is not None:
            current_hub.update_zone(zone_id, name=zone['name'], temp_comfort_c=int(temps.comfort), temp_eco_c=int(temps.eco))
        elif temps.comfort is not None:
            current_hub.update_zone(zone_id, name=zone['name'], temp_comfort_c=int(temps.comfort), temp_eco_c=current_eco)
        elif temps.eco is not None:
            current_hub.update_zone(zone_id, name=zone['name'], temp_comfort_c=current_comfort, temp_eco_c=int(temps.eco))
        
        # Wait for update
        await asyncio.sleep(0.5)
        
        return {"status": "success", "zone_id": zone_id, "comfort": temps.comfort, "eco": temps.eco}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting zone temperature: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/global/override/{mode}")
async def set_global_override(mode: str):
    """Set global override mode for all zones"""
    global global_mode_source
    with connection_lock:
        connected = hub_connected
        current_hub = hub
    
    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")
    
    # Validate mode — 'off' is not a valid Nobø Eco Hub override mode
    # 'home' is an alias for 'normal' (cancel all overrides)
    mode_map = {
        'comfort': pynobo.nobo.API.OVERRIDE_MODE_COMFORT,
        'eco': pynobo.nobo.API.OVERRIDE_MODE_ECO,
        'away': pynobo.nobo.API.OVERRIDE_MODE_AWAY,
        'normal': -1,
        'home': -1  # Home mode = cancel all overrides, return to schedules
    }
    
    if mode not in mode_map:
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")
    
    try:
        # Demo mode - update all simulated zones
        if DEMO_MODE:
            for demo_zone in DEMO_ZONES:
                # For home mode, set to 'normal' which means following schedule
                demo_zone['mode'] = 'normal' if mode == 'home' else mode
            add_log_entry(
                "sent",
                f"[DEMO] Would send: create_override(now, 0, {mode.upper()}, all zones)",
                command=f"create_override now 0 {mode} all",
                source="api",
            )
            add_log_entry(
                "received",
                f"[DEMO] All zones set to {mode}",
                source="api",
            )
            global_mode_source = "manual"
            config_persistence.save_demo_zones(DEMO_ZONES)
            config_persistence.save_server_state({"global_mode_source": global_mode_source})
            return {"status": "success", "mode": mode, "source": "manual"}
        
        # Real hub mode — use a single global override command instead of per-zone
        if not current_hub:
            raise HTTPException(status_code=503, detail="Hub not connected")
        
        if mode == 'normal' or mode == 'home':
            current_hub.create_override(
                pynobo.nobo.API.OVERRIDE_MODE_NORMAL,
                pynobo.nobo.API.OVERRIDE_TYPE_NOW,
                pynobo.nobo.API.OVERRIDE_TARGET_GLOBAL,
            )
            add_log_entry(
                "sent",
                "create_override(NORMAL, NOW, GLOBAL) — cancel all overrides",
                command="create_override NORMAL NOW GLOBAL",
                source="api",
            )
        else:
            current_hub.create_override(
                mode_map[mode],
                pynobo.nobo.API.OVERRIDE_TYPE_NOW,
                pynobo.nobo.API.OVERRIDE_TARGET_GLOBAL,
            )
            add_log_entry(
                "sent",
                f"create_override({mode.upper()}, NOW, GLOBAL)",
                command=f"create_override {mode} NOW GLOBAL",
                source="api",
            )
        
        # Wait for updates
        await asyncio.sleep(0.5)
        
        global_mode_source = "manual"
        config_persistence.save_server_state({"global_mode_source": global_mode_source})
        return {"status": "success", "mode": mode, "source": "manual"}
    except Exception as e:
        logger.error(f"Error setting global override: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===== Away Schedule Endpoints =====

@app.get("/api/global-mode/away-schedule")
async def get_away_schedule():
    """Return the current away schedule configuration."""
    schedule = away_schedule.load_schedule()
    now = datetime.now(timezone.utc)
    currently_active = away_schedule.is_schedule_active(schedule, now)
    return {
        "enabled": schedule["enabled"],
        "start_at": schedule["start_at"],
        "end_at": schedule["end_at"],
        "currently_active": currently_active,
    }


class AwayScheduleUpdate(BaseModel):
    enabled: bool
    start_at: Optional[str] = None
    end_at: Optional[str] = None


@app.put("/api/global-mode/away-schedule")
async def update_away_schedule(body: AwayScheduleUpdate):
    """Save a new away schedule configuration."""
    global global_mode_source
    is_valid, error_msg = away_schedule.validate_schedule(body.enabled, body.start_at, body.end_at)
    if not is_valid:
        logger.warning(f"Invalid away schedule input rejected: {error_msg}")
        add_log_entry("error", f"Away schedule input rejected: {error_msg}", source="api")
        raise HTTPException(status_code=400, detail=error_msg)

    schedule = {
        "enabled": body.enabled,
        "start_at": body.start_at if body.enabled else None,
        "end_at": body.end_at if body.enabled else None,
    }
    away_schedule.save_schedule(schedule)
    logger.info(f"Away schedule saved: enabled={body.enabled}, start={body.start_at}, end={body.end_at}")
    add_log_entry(
        "sent",
        f"Away schedule updated: enabled={body.enabled}, start={body.start_at}, end={body.end_at}",
        source="api",
    )

    # If enabling and we're inside the window right now, immediately switch to Away
    now = datetime.now(timezone.utc)
    if away_schedule.is_schedule_active(schedule, now):
        logger.info("Away schedule activated immediately (currently inside window) — entering GLOBAL Away")
        add_log_entry("sent", "Away schedule activated — entering GLOBAL Away", source="schedule")
        try:
            await _apply_global_mode_internal("away", source="schedule")
            global_mode_source = "schedule"
            config_persistence.save_server_state({"global_mode_source": global_mode_source})
        except Exception as e:
            logger.error(f"Error applying immediate Away on schedule save: {e}")

    currently_active = away_schedule.is_schedule_active(schedule, now)
    return {
        "enabled": schedule["enabled"],
        "start_at": schedule["start_at"],
        "end_at": schedule["end_at"],
        "currently_active": currently_active,
    }


@app.delete("/api/global-mode/away-schedule")
async def delete_away_schedule():
    """Clear the away schedule; if it was active, return to Home mode."""
    global global_mode_source
    old_schedule = away_schedule.load_schedule()
    now = datetime.now(timezone.utc)
    was_active = away_schedule.is_schedule_active(old_schedule, now)

    away_schedule.clear_schedule()
    logger.info("Away schedule cleared")
    add_log_entry("sent", "Away schedule cleared", source="api")

    if was_active:
        logger.info("Away schedule was active — returning to GLOBAL Home")
        add_log_entry("sent", "Away schedule cleared — returning to GLOBAL Home", source="schedule")
        try:
            await _apply_global_mode_internal("home", source="schedule")
            global_mode_source = "manual"
            config_persistence.save_server_state({"global_mode_source": global_mode_source})
        except Exception as e:
            logger.error(f"Error applying Home on schedule clear: {e}")

    return {"status": "cleared"}


async def _apply_global_mode_internal(mode: str, source: str = "schedule") -> None:
    """
    Internal helper to apply a global mode without going through the HTTP endpoint.
    Used by the scheduler and schedule save/delete endpoints.
    """
    global global_mode_source
    with connection_lock:
        connected = hub_connected
        current_hub = hub

    if not connected:
        logger.warning(f"Cannot apply global mode '{mode}': hub not connected")
        return

    if DEMO_MODE:
        for demo_zone in DEMO_ZONES:
            demo_zone['mode'] = 'normal' if mode == 'home' else mode
        add_log_entry(
            "sent",
            f"[DEMO] Schedule: create_override(now, 0, {mode.upper()}, all zones)",
            command=f"create_override now 0 {mode} all",
            source=source,
        )
        global_mode_source = source
        config_persistence.save_demo_zones(DEMO_ZONES)
        config_persistence.save_server_state({"global_mode_source": global_mode_source})
        return

    if not current_hub:
        return

    mode_map = {
        'comfort': pynobo.nobo.API.OVERRIDE_MODE_COMFORT,
        'eco': pynobo.nobo.API.OVERRIDE_MODE_ECO,
        'away': pynobo.nobo.API.OVERRIDE_MODE_AWAY,
        'normal': -1,
        'home': -1,
    }
    if mode == 'normal' or mode == 'home':
        current_hub.create_override(
            pynobo.nobo.API.OVERRIDE_MODE_NORMAL,
            pynobo.nobo.API.OVERRIDE_TYPE_NOW,
            pynobo.nobo.API.OVERRIDE_TARGET_GLOBAL,
        )
    else:
        current_hub.create_override(
            mode_map[mode],
            pynobo.nobo.API.OVERRIDE_TYPE_NOW,
            pynobo.nobo.API.OVERRIDE_TARGET_GLOBAL,
        )
    global_mode_source = source
    config_persistence.save_server_state({"global_mode_source": global_mode_source})
    await asyncio.sleep(0.5)


async def away_schedule_loop():
    """
    Background task that checks the away schedule every 30 seconds.
    Transitions the global mode to Away when inside the window and back to
    Home when the window expires.
    """
    global global_mode_source

    # Track the last known activation state to detect transitions
    last_active = False

    # On startup — check immediately
    schedule = away_schedule.load_schedule()
    now = datetime.now(timezone.utc)

    if away_schedule.is_schedule_expired(schedule, now):
        logger.info("Away schedule expired on boot — disabling schedule and ensuring Home mode")
        add_log_entry("received", "Away schedule expired on boot — ensuring GLOBAL Home", source="schedule")
        schedule["enabled"] = False
        away_schedule.save_schedule(schedule)
        try:
            await _apply_global_mode_internal("home", source="schedule")
            global_mode_source = "manual"
            config_persistence.save_server_state({"global_mode_source": global_mode_source})
        except Exception as e:
            logger.error(f"Error applying Home on boot (expired schedule): {e}")
    elif away_schedule.is_schedule_active(schedule, now):
        logger.info("Away schedule active on boot — entering GLOBAL Away")
        add_log_entry("received", "Away schedule active on boot — entering GLOBAL Away", source="schedule")
        try:
            await _apply_global_mode_internal("away", source="schedule")
            global_mode_source = "schedule"
            config_persistence.save_server_state({"global_mode_source": global_mode_source})
        except Exception as e:
            logger.error(f"Error applying Away on boot: {e}")
        last_active = True

    while True:
        await asyncio.sleep(30)

        schedule = away_schedule.load_schedule()
        now = datetime.now(timezone.utc)

        if away_schedule.is_schedule_expired(schedule, now):
            # Window just ended (or already ended)
            if last_active or schedule.get("enabled"):
                logger.info("Away schedule ended — returning to GLOBAL Home")
                add_log_entry("received", "Away schedule ended — returning to GLOBAL Home", source="schedule")
                schedule["enabled"] = False
                away_schedule.save_schedule(schedule)
                try:
                    await _apply_global_mode_internal("home", source="schedule")
                    global_mode_source = "manual"
                    config_persistence.save_server_state({"global_mode_source": global_mode_source})
                except Exception as e:
                    logger.error(f"Error applying Home on schedule expiry: {e}")
            last_active = False
            continue

        currently_active = away_schedule.is_schedule_active(schedule, now)

        if currently_active and not last_active:
            # Transition into window
            logger.info("Away schedule activated — entering GLOBAL Away")
            add_log_entry("received", "Away schedule activated — entering GLOBAL Away", source="schedule")
            try:
                await _apply_global_mode_internal("away", source="schedule")
                global_mode_source = "schedule"
                config_persistence.save_server_state({"global_mode_source": global_mode_source})
            except Exception as e:
                logger.error(f"Error applying Away on schedule activation: {e}")

        elif currently_active and last_active:
            # Still inside window — re-assert Away in case of manual override
            try:
                await _apply_global_mode_internal("away", source="schedule")
                global_mode_source = "schedule"
                config_persistence.save_server_state({"global_mode_source": global_mode_source})
            except Exception as e:
                logger.error(f"Error re-asserting Away during active schedule: {e}")

        last_active = currently_active


@app.get("/api/week_profiles")
async def get_week_profiles():
    """Get all week profiles"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub
    
    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")
    
    try:
        # Demo mode — return the default schedule as a sample week profile
        if DEMO_MODE:
            return {
                "week_profiles": [
                    {
                        "profile_id": "1",
                        "name": "Default",
                        "profile": DEFAULT_DEMO_SCHEDULE,
                    }
                ]
            }

        if not current_hub:
            raise HTTPException(status_code=503, detail="Hub not connected")

        profiles = []
        for profile_id, profile in current_hub.week_profiles.items():
            profiles.append({
                'profile_id': str(profile_id),
                'name': profile.get('name', f'Profile {profile_id}'),
                'profile': profile
            })
        return {"week_profiles": profiles}
    except Exception as e:
        logger.error(f"Error getting week profiles: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===== Schedule API Endpoints =====
@app.get("/api/zones/{zone_id}/schedule")
async def get_zone_schedule(zone_id: str):
    """Get the weekly schedule for a specific zone"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub
    
    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")
    
    try:
        # Demo mode — use saved schedule if available, otherwise DEFAULT_DEMO_SCHEDULE
        if DEMO_MODE:
            demo_zone = next((z for z in DEMO_ZONES if z['zone_id'] == zone_id), None)
            if not demo_zone:
                raise HTTPException(status_code=404, detail="Zone not found")
            
            saved = demo_schedules.get(zone_id, DEFAULT_DEMO_SCHEDULE)
            return {
                "zone_id": zone_id,
                "zone_name": demo_zone['name'],
                "schedule": saved,
            }
        
        # Real hub mode - get week profile from pynobo
        if not current_hub:
            raise HTTPException(status_code=503, detail="Hub not connected")
        
        if zone_id not in current_hub.zones:
            raise HTTPException(status_code=404, detail="Zone not found")
        
        zone = current_hub.zones[zone_id]
        week_profile_id = zone.get('week_profile_id')
        
        if not week_profile_id or week_profile_id not in current_hub.week_profiles:
            raise HTTPException(status_code=404, detail="Week profile not found for zone")
        
        week_profile = current_hub.week_profiles[week_profile_id]
        
        return {
            "zone_id": zone_id,
            "zone_name": zone.get('name', f'Zone {zone_id}'),
            "week_profile_id": week_profile_id,
            "week_profile": week_profile
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting zone schedule: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/zones/{zone_id}/schedule")
async def update_zone_schedule(zone_id: str, schedule: ScheduleUpdate):
    """Update the weekly schedule for a specific zone"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub
    
    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")
    
    try:
        # Validate schedule structure
        try:
            schedule.validate_schedule()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # Demo mode - store schedule and return success
        if DEMO_MODE:
            demo_zone = next((z for z in DEMO_ZONES if z['zone_id'] == zone_id), None)
            if not demo_zone:
                raise HTTPException(status_code=404, detail="Zone not found")
            
            # Serialise ScheduleBlock objects to plain dicts for storage
            demo_schedules[zone_id] = {
                day: [b.model_dump() for b in blocks]
                for day, blocks in schedule.schedule.items()
            }
            logger.info(f"Demo mode: Schedule updated for zone {zone_id}")
            config_persistence.save_demo_schedules(demo_schedules)
            return {"status": "success", "zone_id": zone_id, "message": "Schedule updated (demo mode)"}
        
        # Real hub mode - update week profile using pynobo
        if not current_hub:
            raise HTTPException(status_code=503, detail="Hub not connected")
        
        if zone_id not in current_hub.zones:
            raise HTTPException(status_code=404, detail="Zone not found")
        
        # This would need to be implemented based on pynobo's week profile format
        # For now, return not implemented
        raise HTTPException(status_code=501, detail="Schedule update not yet implemented for real hub")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating zone schedule: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===== Device Management API Endpoints =====
@app.get("/api/devices")
async def get_devices():
    """Get all registered devices with their zone assignments"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub
    
    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")
    
    try:
        zones_data = get_zones_data()
        devices = []
        
        for zone in zones_data:
            for i, serial in enumerate(zone['components']):
                device_name, supports_comfort, supports_eco = detect_device_type(serial)
                devices.append({
                    'serial': serial,
                    'serial_display': zone['components_display'][i] if i < len(zone['components_display']) else format_serial_display(serial),
                    'device_type': device_name,
                    'zone_id': zone['zone_id'],
                    'zone_name': zone['name'],
                    'supports_comfort': supports_comfort,
                    'supports_eco': supports_eco,
                    'supports_temp_adjust': supports_comfort or supports_eco,
                    'current_mode': zone.get('current_mode', 'normal'),
                })
        
        return {"devices": devices}
    except Exception as e:
        logger.error(f"Error getting devices: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class DeviceAdd(BaseModel):
    serial: str
    zone_id: str
    name: Optional[str] = None


@app.post("/api/devices")
async def add_device(device: DeviceAdd):
    """Add a new device to a zone"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub
    
    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")
    
    try:
        # Parse and validate serial
        is_valid, result = validate_serial(device.serial)
        if not is_valid:
            raise HTTPException(status_code=400, detail=result)
        serial = result
        
        # Auto-detect device type
        device_name, supports_comfort, supports_eco = detect_device_type(serial)
        if device_name == "Unknown":
            raise HTTPException(status_code=400, detail=f"Unknown device model for serial prefix {serial[:3]}")
        
        # Demo mode - add to DEMO_ZONES
        if DEMO_MODE:
            demo_zone = next((z for z in DEMO_ZONES if z['zone_id'] == device.zone_id), None)
            if not demo_zone:
                raise HTTPException(status_code=404, detail="Zone not found")

            # Global duplicate check across all zones
            for z in DEMO_ZONES:
                if serial in z['components']:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Device with serial {serial} is already registered in zone '{z['name']}'"
                    )

            if serial in demo_zone['components']:
                raise HTTPException(status_code=400, detail="Device already registered in this zone")
            
            demo_zone['components'].append(serial)
            if 'component_names' not in demo_zone:
                demo_zone['component_names'] = [''] * (len(demo_zone['components']) - 1)
            demo_zone['component_names'].append(device.name or '')
            logger.info(f"Demo mode: Device {serial} added to zone {device.zone_id}")
            config_persistence.save_demo_zones(DEMO_ZONES)
            return {
                "status": "success",
                "serial": serial,
                "serial_display": format_serial_display(serial),
                "device_type": device_name,
                "zone_id": device.zone_id,
                "name": device.name or ''
            }
        
        # Real hub mode
        if not current_hub:
            raise HTTPException(status_code=503, detail="Hub not connected")
        
        raise HTTPException(status_code=501, detail="Add device not yet implemented for real hub")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding device: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class DeviceReplace(BaseModel):
    new_serial: str


class DeviceRename(BaseModel):
    name: str


@app.patch("/api/devices/{serial}/name")
async def rename_device(serial: str, body: DeviceRename):
    """Update the friendly name of a device"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub

    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")

    try:
        is_valid, result = validate_serial(serial)
        if not is_valid:
            raise HTTPException(status_code=400, detail=result)
        clean_serial = result
        new_name = body.name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="Name cannot be empty")

        # Demo mode - update component_names in DEMO_ZONES
        if DEMO_MODE:
            for demo_zone in DEMO_ZONES:
                if clean_serial in demo_zone['components']:
                    idx = demo_zone['components'].index(clean_serial)
                    if 'component_names' not in demo_zone:
                        demo_zone['component_names'] = [''] * len(demo_zone['components'])
                    demo_zone['component_names'][idx] = new_name
                    logger.info(f"Demo mode: Device {clean_serial} renamed to '{new_name}'")
                    config_persistence.save_demo_zones(DEMO_ZONES)
                    return {"status": "success", "serial": clean_serial, "name": new_name}
            raise HTTPException(status_code=404, detail="Device not found")

        # Real hub mode
        if not current_hub:
            raise HTTPException(status_code=503, detail="Hub not connected")

        raise HTTPException(status_code=501, detail="Device renaming is not yet supported for connected hubs")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error renaming device: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/devices/{serial}")
async def replace_device(serial: str, replacement: DeviceReplace):
    """Replace a device with a new one"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub
    
    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")
    
    try:
        # Parse and validate serials
        is_valid_old, old_result = validate_serial(serial)
        if not is_valid_old:
            raise HTTPException(status_code=400, detail=old_result)
        old_serial = old_result
        is_valid, result = validate_serial(replacement.new_serial)
        if not is_valid:
            raise HTTPException(status_code=400, detail=result)
        new_serial = result
        
        # Auto-detect new device type
        device_name, _, _ = detect_device_type(new_serial)
        if device_name == "Unknown":
            raise HTTPException(status_code=400, detail=f"Unknown device model for serial prefix {new_serial[:3]}")
        
        # Demo mode - replace in DEMO_ZONES
        if DEMO_MODE:
            # Find the source zone for old_serial, normalizing stored serials
            src_zone = next(
                (z for z in DEMO_ZONES if old_serial in [c.replace(' ', '') for c in z['components']]),
                None
            )
            if not src_zone:
                raise HTTPException(status_code=404, detail="Device not found")

            # Check new serial doesn't already exist in any other zone
            for z in DEMO_ZONES:
                z_components_normalized = [c.replace(' ', '') for c in z['components']]
                if new_serial in z_components_normalized and z['zone_id'] != src_zone['zone_id']:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Device with serial {new_serial} is already registered in zone '{z['name']}'"
                    )

            # Replace the serial in the source zone
            components_normalized = [c.replace(' ', '') for c in src_zone['components']]
            idx = components_normalized.index(old_serial)
            # Ensure component_names is properly sized before updating
            if 'component_names' not in src_zone:
                src_zone['component_names'] = [''] * len(src_zone['components'])
            elif len(src_zone['component_names']) < len(src_zone['components']):
                src_zone['component_names'].extend(
                    [''] * (len(src_zone['components']) - len(src_zone['component_names']))
                )
            src_zone['components'][idx] = new_serial
            logger.info(f"Demo mode: Device {old_serial} replaced with {new_serial} in zone {src_zone['zone_id']}")
            config_persistence.save_demo_zones(DEMO_ZONES)
            return {
                "status": "success",
                "old_serial": old_serial,
                "new_serial": new_serial,
                "serial_display": format_serial_display(new_serial),
                "device_type": device_name
            }
        
        # Real hub mode
        if not current_hub:
            raise HTTPException(status_code=503, detail="Hub not connected")
        
        raise HTTPException(status_code=501, detail="Replace device not yet implemented for real hub")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error replacing device: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/devices/{serial}")
async def remove_device(serial: str):
    """Remove a device from its zone"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub
    
    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")
    
    try:
        # Parse serial
        is_valid, result = validate_serial(serial)
        if not is_valid:
            raise HTTPException(status_code=400, detail=result)
        serial_clean = result
        
        # Demo mode - remove from DEMO_ZONES
        if DEMO_MODE:
            found = False
            for demo_zone in DEMO_ZONES:
                # Normalize stored serials to handle any spaces
                components_normalized = [c.replace(' ', '') for c in demo_zone['components']]
                if serial_clean in components_normalized:
                    idx = components_normalized.index(serial_clean)
                    demo_zone['components'].pop(idx)
                    if 'component_names' in demo_zone and idx < len(demo_zone['component_names']):
                        demo_zone['component_names'].pop(idx)
                    found = True
                    logger.info(f"Demo mode: Device {serial_clean} removed from zone {demo_zone['zone_id']}")
                    break
            
            if not found:
                raise HTTPException(status_code=404, detail="Device not found")
            
            config_persistence.save_demo_zones(DEMO_ZONES)
            return {"status": "success", "serial": serial_clean}
        
        # Real hub mode
        if not current_hub:
            raise HTTPException(status_code=503, detail="Hub not connected")
        
        raise HTTPException(status_code=501, detail="Remove device not yet implemented for real hub")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing device: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class DeviceMove(BaseModel):
    new_zone_id: str


@app.post("/api/devices/{serial}/move")
async def move_device(serial: str, move: DeviceMove):
    """Move a device from its current zone to a different zone"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub

    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")

    try:
        is_valid, result = validate_serial(serial)
        if not is_valid:
            raise HTTPException(status_code=400, detail=result)
        serial_clean = result

        # Demo mode - move between DEMO_ZONES
        if DEMO_MODE:
            # Find source zone
            src_zone = None
            for z in DEMO_ZONES:
                if serial_clean in z['components']:
                    src_zone = z
                    break

            if not src_zone:
                raise HTTPException(status_code=404, detail="Device not found")

            # Validate target zone
            dst_zone = next((z for z in DEMO_ZONES if z['zone_id'] == move.new_zone_id), None)
            if not dst_zone:
                raise HTTPException(status_code=404, detail="Target zone not found")

            if src_zone['zone_id'] == dst_zone['zone_id']:
                raise HTTPException(status_code=400, detail="Device is already in the target zone")

            # Remove from source zone (preserve component name)
            idx = src_zone['components'].index(serial_clean)
            src_zone['components'].pop(idx)
            component_name = ''
            if 'component_names' in src_zone and idx < len(src_zone['component_names']):
                component_name = src_zone['component_names'].pop(idx)

            # Add to destination zone
            dst_zone['components'].append(serial_clean)
            if 'component_names' not in dst_zone:
                dst_zone['component_names'] = [''] * (len(dst_zone['components']) - 1)
            dst_zone['component_names'].append(component_name)

            logger.info(
                f"Demo mode: Device {serial_clean} moved from zone {src_zone['zone_id']} "
                f"to zone {dst_zone['zone_id']}"
            )
            config_persistence.save_demo_zones(DEMO_ZONES)
            return {
                "status": "success",
                "serial": serial_clean,
                "old_zone_id": src_zone['zone_id'],
                "old_zone_name": src_zone['name'],
                "new_zone_id": dst_zone['zone_id'],
                "new_zone_name": dst_zone['name'],
            }

        # Real hub mode
        raise HTTPException(status_code=501, detail="Move device not yet implemented for real hub")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error moving device: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===== Command Log Endpoints =====

@app.get("/api/log")
async def get_log(limit: int = 500):
    """Return the last N entries from the command log buffer"""
    with log_lock:
        entries = list(command_log)
    # Return most-recent first; honour the limit
    entries = entries[-limit:]
    entries_reversed = list(reversed(entries))
    return {
        "entries": entries_reversed,
        "total": len(entries_reversed),
        "demo_mode": DEMO_MODE,
    }


@app.post("/api/log/clear")
async def clear_log():
    """Clear the command log buffer"""
    with log_lock:
        command_log.clear()
    return {"status": "success", "message": "Log cleared"}


# ===== WebSocket Endpoint =====
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates.

    BaseHTTPMiddleware does not see WebSocket handshakes, so the session check
    that AuthMiddleware performs for HTTP has to be repeated here. Closing
    before accepting makes Starlette reject the handshake with HTTP 403.
    """
    if not ALLOW_ANON_API:
        if not auth.get_session(websocket.cookies.get("session_id")):
            await websocket.close(code=1008)
            return

    await websocket.accept()
    
    async with websocket_lock:
        connected_websockets.append(websocket)
        total = len(connected_websockets)
    
    logger.info(f"WebSocket client connected. Total clients: {total}")
    
    try:
        # Send initial data
        with connection_lock:
            connected = hub_connected
            current_hub = hub
        
        if connected and (current_hub or DEMO_MODE):
            zones_data = get_zones_data()
            await websocket.send_json({
                "type": "zones_update",
                "data": zones_data,
                "timestamp": local_now().isoformat()
            })
        
        # Keep connection alive and handle incoming messages
        while True:
            try:
                # Wait for messages from client (e.g., ping/pong)
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_text("pong")
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                break
    
    finally:
        async with websocket_lock:
            if websocket in connected_websockets:
                connected_websockets.remove(websocket)
            total = len(connected_websockets)
        logger.info(f"WebSocket client disconnected. Total clients: {total}")


# ===== Authentication Endpoints =====

# Inline login-page HTML — served directly (not via /static) to avoid auth loop
_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Nobø Control — Login</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #1a1a2e; --card: #16213e; --border: #0f3460;
    --accent: #e94560; --text: #eee; --muted: #aaa;
    --input-bg: #0f3460; --radius: 8px;
  }
  body { background: var(--bg); color: var(--text); font-family: system-ui, sans-serif;
    min-height: 100vh; display: flex; align-items: center; justify-content: center; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 2rem; width: 100%; max-width: 380px; }
  h1 { text-align: center; margin-bottom: 1.5rem; font-size: 1.5rem; }
  .form-group { margin-bottom: 1rem; }
  label { display: block; margin-bottom: .4rem; font-size: .875rem; color: var(--muted); }
  input { width: 100%; padding: .6rem .8rem; background: var(--input-bg);
    border: 1px solid var(--border); border-radius: var(--radius);
    color: var(--text); font-size: 1rem; }
  input:focus { outline: 2px solid var(--accent); }
  button { width: 100%; margin-top: .5rem; padding: .75rem;
    background: var(--accent); color: #fff; font-size: 1rem; font-weight: 600;
    border: none; border-radius: var(--radius); cursor: pointer; }
  button:hover { opacity: .9; }
  .error { background: rgba(233,69,96,.15); border: 1px solid var(--accent);
    color: var(--accent); border-radius: var(--radius); padding: .6rem .8rem;
    margin-bottom: 1rem; font-size: .875rem; display: none; }
  .error.show { display: block; }
</style>
</head>
<body>
<div class="card">
  <h1>🔒 Nobø Control</h1>
  <div class="error" id="errorMsg"></div>
  <form id="loginForm">
    <div class="form-group">
      <label for="username">Username</label>
      <input type="text" id="username" name="username" autocomplete="username" required autofocus>
    </div>
    <div class="form-group">
      <label for="password">Password</label>
      <input type="password" id="password" name="password" autocomplete="current-password" required>
    </div>
    <button type="submit">Sign in</button>
  </form>
</div>
<script>
document.getElementById('loginForm').addEventListener('submit', async e => {
  e.preventDefault();
  const err = document.getElementById('errorMsg');
  err.classList.remove('show');
  const body = new URLSearchParams({
    username: document.getElementById('username').value,
    password: document.getElementById('password').value,
  });
  try {
    const r = await fetch('/auth/login', { method: 'POST', body,
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' } });
    if (r.ok) {
      window.location.href = '/';
    } else {
      const data = await r.json().catch(() => ({}));
      err.textContent = data.detail || 'Login failed';
      err.classList.add('show');
    }
  } catch {
    err.textContent = 'Network error — please try again.';
    err.classList.add('show');
  }
});
</script>
</body>
</html>"""


def _get_session_or_401(request: Request) -> dict:
    """Return session dict or raise HTTP 401."""
    session_id = request.cookies.get("session_id")
    session = auth.get_session(session_id) if session_id else None
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return session


def _require_admin(session: dict) -> None:
    users = auth.load_users()
    user = users.get(session["username"], {})
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")


@app.get("/login")
async def login_page():
    """Serve the login HTML page (exempt from auth middleware)."""
    return HTMLResponse(content=_LOGIN_HTML)


@app.post("/auth/login")
async def auth_login(request: Request):
    """Validate credentials, create session, set cookie."""
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required")

    # Rate-limit by username
    allowed, wait = auth.check_rate_limit(username)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {wait} seconds.",
        )

    users = auth.load_users()
    user = users.get(username)
    if not user or not auth.verify_password(password, user["password_hash"]):
        auth.record_failed_attempt(username)
        raise HTTPException(status_code=401, detail="Invalid username or password")

    auth.clear_attempts(username)
    session_id = auth.create_session(username)

    is_https = (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").lower() == "https"
    )
    response = JSONResponse(
        {"username": username, "role": user.get("role", "user")}
    )
    response.set_cookie(
        key="session_id",
        value=session_id,
        httponly=True,
        samesite="lax",
        secure=is_https,
        max_age=86400,
        path="/",
    )
    return response


@app.post("/auth/logout")
async def auth_logout(request: Request):
    """Invalidate session and clear cookie."""
    session_id = request.cookies.get("session_id")
    if session_id:
        auth.delete_session(session_id)
    response = JSONResponse({"status": "logged out"})
    response.delete_cookie(key="session_id", path="/")
    return response


@app.get("/auth/me")
async def auth_me(request: Request):
    """Return info about the currently authenticated user."""
    session = _get_session_or_401(request)
    users = auth.load_users()
    user = users.get(session["username"], {})
    return {"username": session["username"], "role": user.get("role", "user")}


@app.post("/auth/change-password")
async def auth_change_password(request: Request):
    """Change the current user's password (requires new password confirmed twice)."""
    session = _get_session_or_401(request)
    data = await request.json()
    current = data.get("current_password", "")
    new_pw = data.get("new_password", "")
    confirm = data.get("confirm_password", "")

    if not current or not new_pw or not confirm:
        raise HTTPException(status_code=400, detail="All password fields are required")
    if new_pw != confirm:
        raise HTTPException(status_code=400, detail="New passwords do not match")
    if len(new_pw) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    users = auth.load_users()
    username = session["username"]
    user = users.get(username)
    if not user or not auth.verify_password(current, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    users[username]["password_hash"] = auth.hash_password(new_pw)
    auth.save_users(users)
    return {"status": "password changed"}


@app.post("/auth/rename")
async def auth_rename(request: Request):
    """Rename the current user's username."""
    session = _get_session_or_401(request)
    data = await request.json()
    new_name = str(data.get("new_username", "")).strip()

    if not new_name:
        raise HTTPException(status_code=400, detail="New username is required")
    if len(new_name) < 2:
        raise HTTPException(status_code=400, detail="Username must be at least 2 characters")

    users = auth.load_users()
    old_name = session["username"]

    if new_name == old_name:
        return {"status": "no change"}
    if new_name in users:
        raise HTTPException(status_code=409, detail="Username already exists")

    users[new_name] = users.pop(old_name)
    auth.save_users(users)

    # Update the active session
    session["username"] = new_name
    return {"status": "renamed", "username": new_name}


# ----- Admin-only endpoints -----

@app.get("/auth/admin/users")
async def admin_list_users(request: Request):
    """List all users (admin only)."""
    session = _get_session_or_401(request)
    _require_admin(session)
    users = auth.load_users()
    return [
        {"username": u, "role": info.get("role", "user")}
        for u, info in users.items()
    ]


@app.post("/auth/admin/users")
async def admin_add_user(request: Request):
    """Add a new user (admin only)."""
    session = _get_session_or_401(request)
    _require_admin(session)
    data = await request.json()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    role = str(data.get("role", "user"))

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if role not in ("admin", "user"):
        role = "user"

    users = auth.load_users()
    if username in users:
        raise HTTPException(status_code=409, detail="Username already exists")

    users[username] = {"password_hash": auth.hash_password(password), "role": role}
    auth.save_users(users)
    return {"status": "created", "username": username}


@app.patch("/auth/admin/users/{username}")
async def admin_update_user(request: Request, username: str):
    """Rename a user or change their role (admin only)."""
    session = _get_session_or_401(request)
    _require_admin(session)
    data = await request.json()

    users = auth.load_users()
    if username not in users:
        raise HTTPException(status_code=404, detail="User not found")

    new_name = str(data.get("new_username", "")).strip() or None
    new_role = data.get("role")

    if new_name and new_name != username:
        if new_name in users:
            raise HTTPException(status_code=409, detail="Username already exists")
        users[new_name] = users.pop(username)
        username = new_name

    if new_role in ("admin", "user"):
        users[username]["role"] = new_role

    auth.save_users(users)
    return {"status": "updated", "username": username}


@app.delete("/auth/admin/users/{username}")
async def admin_delete_user(request: Request, username: str):
    """Delete a user (admin only; cannot delete yourself)."""
    session = _get_session_or_401(request)
    _require_admin(session)

    if username == session["username"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    users = auth.load_users()
    if username not in users:
        raise HTTPException(status_code=404, detail="User not found")

    del users[username]
    auth.save_users(users)
    return {"status": "deleted"}


# ===== Static Files =====
# Resolved from this file's location, not the working directory, so the app and
# the test suite behave the same no matter where they are started from
# (QA defect D-03).
STATIC_DIR = Path(__file__).resolve().parent / "static"

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def read_root():
    """Serve the main HTML page"""
    try:
        with open(STATIC_DIR / "index.html", "r", encoding="utf-8") as f:
            content = f.read()
        return HTMLResponse(content=content)
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Error: index.html not found</h1>", status_code=404)


# ===== Main Entry Point =====
if __name__ == "__main__":
    import uvicorn
    
    logger.info("Starting Nobø Web Control Server...")
    logger.info(f"Hub Serial: {NOBO_SERIAL}")
    logger.info(f"Hub IP: {NOBO_IP}")
    logger.info("Access the web interface at http://localhost:8000")
    
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
