"""
Nobø Energy Hub Web Control Server
FastAPI backend for local control of Nobø heating system via pynobo library
"""

import os
import re
import asyncio
import html
import ipaddress
import json
import logging
import math
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Any, Set
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse
from pydantic import BaseModel
import copy
import pynobo
import auth
import away_schedule
import config_persistence
import notifications
import notify_watch

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
#
# The temperatures here are deliberately sparse, because that is the truth about
# this hardware: of the 25 models pynobo knows, only the SW4 control panel has a
# thermometer. An NTB-2R controls temperature perfectly well without ever telling
# the hub what the room is. This file used to give every NTB-2R zone a reading,
# which made the demo house look like a fully instrumented building and is not
# what anybody's cabin looks like.
#
# One SW4 is included, in the Living Area, so the features that do need a reading
# can still be seen working - and so the difference between a measured room and
# an unmeasured one is visible side by side.
_DEFAULT_DEMO_ZONES = [
    {
        "zone_id": "1",
        "name": "Large Bathroom",
        "icon": "🛁",
        "rooms": ["Large Bathroom"],
        "components": ["210000016247"],  # NTB-2R device
        "component_names": ["Large Bathroom Heater"],
        "current_temp": None,  # NTB-2R controls temperature but never reports it
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
        "current_temp": None,  # NTB-2R controls temperature but never reports it
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
        "current_temp": None,  # NTB-2R controls temperature but never reports it
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
        "components": ["160004028114", "160004028115", "234000012006"],  # 2x R80 RDC 700 + an SW4 panel
        "component_names": ["Kitchen Heater", "Living Room Heater", "Living Room Panel"],
        # The only measured room in the demo house, because the SW4 is the only
        # model in the range that carries a thermometer.
        "current_temp": 20.4,
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
        "current_temp": None,  # neither model reports a temperature
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


def _drop_impossible_demo_temperatures(zones: list) -> int:
    """
    Strip room temperatures from demo zones whose devices cannot measure one.

    Earlier versions of this file gave every NTB-2R zone a reading, which is not
    something an NTB-2R can produce. Anybody who ran those versions has that
    fiction saved in ``data/demo_zones.json``, where it would outlive the fix and
    contradict the capability the same zone now reports.

    Cheap to repair and self-healing, so it is done on load rather than by
    editing files on individual machines.
    """
    fixed = 0
    for zone in zones:
        if zone.get("current_temp") is None:
            continue
        if not any(model_has_temp_sensor(c) for c in zone.get("components", [])):
            zone["current_temp"] = None
            fixed += 1
    return fixed

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
# Bumped every time the hub configuration changes. Connection attempts run on
# background threads and can finish long after the user has switched modes
# again; they carry the generation they started under so a stale result cannot
# overwrite the current state.
hub_config_generation = 0
main_event_loop: Optional[asyncio.AbstractEventLoop] = None
websocket_lock = asyncio.Lock()  # Lock for thread-safe websocket list access
connection_lock = threading.Lock()  # Lock for thread-safe hub_connected access
log_lock = threading.Lock()  # Lock for thread-safe command log access

# Notifications. The notifier decides what is worth saying and sends it on a
# worker thread; the watcher turns a stream of zone snapshots into events. Both
# are created here so every code path can reach them, and both are harmless
# until the user turns notifications on in Settings.
notifier = notifications.Notifier()
zone_watcher = notify_watch.ZoneWatcher(notifier=notifier)

# Command log buffer — keeps the last 500 entries
command_log: deque = deque(maxlen=500)

# In-memory store for demo-mode schedule changes (keyed by zone_id).
# Populated from disk on startup; persisted to disk on every write.
demo_schedules: Dict[str, dict] = config_persistence.load_demo_schedules()

# Zone icons are this app's own idea — the hub does not store them — so they are
# kept locally and apply in both demo and real-hub mode. Keyed by zone id.
zone_icons: Dict[str, str] = config_persistence.load_zone_icons()

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


def model_has_temp_sensor(serial: str) -> bool:
    """
    Whether this model measures room temperature and reports it to the hub.

    This is the difference between an alert that can fire and one that never
    will, so it is read from pynobo's own model table rather than guessed.

    It is worth knowing how lopsided the answer is: of the 25 models pynobo
    knows, exactly one — the SW4 control panel — has a thermometer. Every
    heater receiver and thermostat, including the NTB-2R and the R80 RDC 700,
    controls temperature without ever measuring it for the hub. A house can
    therefore be fully working and report no temperature at all, which is why
    the temperature-based alerts have to be offered conditionally instead of
    sitting there looking available.
    """
    serial_clean = str(serial).replace(' ', '').strip()
    if len(serial_clean) < 3:
        return False
    model = pynobo.nobo.MODELS.get(serial_clean[:3])
    return bool(model and getattr(model, "has_temp_sensor", False))


# Repair any saved demo house that predates the discovery above. Done here
# rather than at the load site because it needs model_has_temp_sensor, and in
# memory only - the corrected values persist the next time anything saves.
_repaired = _drop_impossible_demo_temperatures(DEMO_ZONES)
if _repaired:
    logger.info(
        "Demo zones: cleared %d invented room temperature(s) from devices that cannot measure one",
        _repaired,
    )


# The hub protocol is space-delimited, so a space inside a name would break the
# field count. Names therefore travel with non-breaking spaces (U+00A0) instead.
# pynobo encodes on the way out but does not decode on the way in, so names read
# back from a real hub contain U+00A0 wherever the user typed a space. On screen
# that is nearly invisible, but it makes comparison, search and sorting fail.
HUB_NAME_SPACE = "\u00a0"


def decode_hub_name(name: Any) -> Any:
    """Turn a name from the hub back into ordinary text."""
    if isinstance(name, str):
        return name.replace(HUB_NAME_SPACE, " ")
    return name


def encode_hub_name(name: str) -> str:
    """Prepare a name for the hub. Only needed where pynobo does not do it."""
    return name.replace(" ", HUB_NAME_SPACE)


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

    # Wire up notifications. Done here rather than at import time so that tests
    # importing the module do not pick up whatever is in the real data dir.
    notifier.log_hook = add_log_entry
    refresh_notifier_identity()

    try:
        await connect_to_hub()
    except Exception as e:
        logger.error(f"Failed to connect to hub on startup: {e}")
        # Don't fail startup - allow server to run and show disconnected state
    
    # Start background reconnection task (no-op in demo mode)
    reconnect_task = asyncio.create_task(reconnect_loop())
    # Start background away-schedule checker
    schedule_task = asyncio.create_task(away_schedule_loop())
    # Watches for cold rooms and silent thermostats. Separate from the hub push
    # because both are conditions that persist rather than events that arrive -
    # a room that stopped reporting sends nothing to react to.
    watch_task = asyncio.create_task(notification_watch_loop())

    yield
    
    # Shutdown
    reconnect_task.cancel()
    schedule_task.cancel()
    watch_task.cancel()
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
            stop_hub_client(current_hub)
        except:
            pass

    hub_loop.shutdown()


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

# The sign-in page's own artwork. These have to be readable without a session
# for the obvious reason: they appear *on* the sign-in page, which is shown to
# somebody who by definition has not logged in yet. Behind the wall they answer
# 302 to /login, the browser gets HTML where it expected an image, and falls
# back to drawing a letter from the hostname — which is where the mysterious
# "N" came from.
#
# Listed one by one rather than opening /static/ui/, so this stays a decision
# about two specific files and not a hole that grows quietly. The sign-in pages
# are otherwise entirely self-contained — their CSS is inline — so this is the
# whole list, and a test keeps it matching what the page actually asks for.
PUBLIC_ASSET_PATHS = frozenset({
    "/static/ui/cabin/icon.svg",
    "/static/ui/cabin/icon-180.png",
})

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

        if path in PUBLIC_PATHS or path in PUBLIC_ASSET_PATHS:
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
# /api/site only changes what the place is called, which no zone card shows.
NO_BROADCAST_PATHS = frozenset({"/api/log/clear", "/api/hub/config", "/api/site"})


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


# The hub allows 100 bytes for a zone name (API_Nobo.pdf).
ZONE_NAME_MAX_BYTES = 100

# Starting temperatures for a zone created from this app. The hub requires
# comfort >= eco, and these match the defaults elsewhere in the application.
DEFAULT_NEW_ZONE_COMFORT = 21
DEFAULT_NEW_ZONE_ECO = 18


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


class SiteUpdate(BaseModel):
    """What this place is called, and whether to say so before sign-in."""
    name: Optional[str] = None
    show_on_login: Optional[bool] = None
    locale: Optional[str] = None


VALID_SCHEDULE_MODES = {'comfort', 'eco', 'away', 'off'}
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


# ===== Week profile conversion =====
#
# The app models a schedule as seven days of contiguous blocks with a start, an
# end and a mode. A Nobø hub models it as one flat, comma-separated list of
# "HHMMS" stamps covering the whole week, where a stamp only says "from this
# moment, be in state S" and every day begins with a 0000 stamp. Converting
# between the two is what makes schedule editing work against real hardware.
#
# The state digit is documented inconsistently: page 6 of API_Nobo.pdf lists
# "3: Off" while page 13 lists "4 = Off". pynobo — the library that actually
# talks to hubs in the field — only accepts 0, 1, 2 and 4, so 4 is used here.
WEEK_PROFILE_STATE_BY_MODE = {
    'eco': '0',
    'comfort': '1',
    'away': '2',
    'off': '4',
}
MODE_BY_WEEK_PROFILE_STATE = {v: k for k, v in WEEK_PROFILE_STATE_BY_MODE.items()}

# The hub stores at most 672 stamps per profile (API_Nobo.pdf) and only accepts
# quarter-hour boundaries.
MAX_WEEK_PROFILE_ENTRIES = 672


def schedule_to_week_profile(schedule: Dict[str, List["ScheduleBlock"]]) -> List[str]:
    """Convert the app's weekly block schedule into hub "HHMMS" stamps.

    Assumes the schedule has already passed ``ScheduleUpdate.validate_schedule``,
    so each day is contiguous from 00:00 to 24:00. Only block *starts* become
    stamps — an end time is implied by the next stamp.
    """
    entries: List[str] = []

    for day in SCHEDULE_DAYS:
        blocks = sorted(schedule[day], key=lambda b: b._parse_minutes(b.start))
        for block in blocks:
            hours, minutes = block.start.split(':')
            if int(minutes) % 15 != 0:
                raise ValueError(
                    f"Day {day!r}: the hub only accepts times on a quarter hour, "
                    f"but a block starts at {block.start}"
                )
            state = WEEK_PROFILE_STATE_BY_MODE.get(block.mode)
            if state is None:
                raise ValueError(f"Day {day!r}: mode {block.mode!r} cannot be sent to a hub")
            entries.append(f"{hours}{minutes}{state}")

    midnights = sum(1 for e in entries if e[:4] == '0000')
    if midnights != 7:
        # Should be unreachable after validate_schedule, but a wrong profile is
        # worse than a rejected one — a hub will happily accept a corrupt week.
        raise ValueError(
            f"Converted schedule has {midnights} midnight entries; the hub requires exactly 7"
        )
    if len(entries) > MAX_WEEK_PROFILE_ENTRIES:
        raise ValueError(
            f"Schedule has {len(entries)} switch points; the hub allows at most "
            f"{MAX_WEEK_PROFILE_ENTRIES}"
        )
    return entries


def week_profile_to_schedule(entries: List[str]) -> Dict[str, List[Dict[str, str]]]:
    """Convert hub "HHMMS" stamps back into the app's weekly block schedule."""
    days: List[List[Dict[str, str]]] = []

    for entry in entries:
        entry = entry.strip()
        if len(entry) != 5 or not entry[:4].isdigit():
            raise ValueError(f"Malformed week profile entry: {entry!r}")
        start = f"{entry[0:2]}:{entry[2:4]}"
        mode = MODE_BY_WEEK_PROFILE_STATE.get(entry[4])
        if mode is None:
            raise ValueError(f"Unknown state {entry[4]!r} in week profile entry {entry!r}")
        if entry[:4] == '0000':
            days.append([])
        if not days:
            raise ValueError("Week profile does not start at midnight")
        days[-1].append({'start': start, 'end': '24:00', 'mode': mode})

    if len(days) != 7:
        raise ValueError(f"Week profile covers {len(days)} days; expected 7")

    # A stamp runs until the next one, or to the end of the day.
    for blocks in days:
        for i in range(len(blocks) - 1):
            blocks[i]['end'] = blocks[i + 1]['start']

    return {day: days[i] for i, day in enumerate(SCHEDULE_DAYS)}


# ===== Hub Connection & Callbacks =====

# How long to wait for the hub to accept a command before giving up. The hub is
# on the local network and answers in milliseconds, so anything slower means the
# link is in trouble and the user is better served by an error than by a hang.
HUB_COMMAND_TIMEOUT = 10


class HubLoop:
    """A dedicated asyncio event loop, on its own thread, that owns the hub client.

    pynobo's socket belongs to whichever event loop created it, and asyncio
    streams may only be used from that loop. pynobo's deprecated synchronous
    wrappers guess at a loop instead: called from inside a request handler they
    pick up the *web server's* loop, so the command is written to a socket that
    belongs to a different loop. That either does nothing or corrupts the
    connection, and it fails silently — which is why real-hub writes could not
    be relied on before.

    Everything that talks to the hub therefore goes through here.
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the loop thread if it is not already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()

            def run() -> None:
                loop = asyncio.new_event_loop()
                self._loop = loop
                asyncio.set_event_loop(loop)
                self._ready.set()
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            self._thread = threading.Thread(target=run, name="nobo-hub-loop", daemon=True)
            self._thread.start()

        if not self._ready.wait(timeout=10):
            raise RuntimeError("Hub event loop failed to start")

    @property
    def running(self) -> bool:
        loop = self._loop
        return loop is not None and loop.is_running()

    def run(self, coro, timeout: float = HUB_COMMAND_TIMEOUT):
        """Run a coroutine on the hub loop and wait for its result."""
        loop = self._loop
        if loop is None or not loop.is_running():
            coro.close()
            raise RuntimeError("Hub event loop is not running")
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)

    def shutdown(self) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._loop = None
        self._thread = None


hub_loop = HubLoop()


async def hub_command(coro, timeout: float = HUB_COMMAND_TIMEOUT):
    """Await a pynobo coroutine from a request handler, on the hub's own loop.

    The blocking wait is pushed to a worker thread so the web server keeps
    serving other requests while the hub thinks about it.
    """
    return await asyncio.to_thread(hub_loop.run, coro, timeout)


# Serialises connection attempts. A configuration change and the reconnect loop
# can otherwise start one each at the same moment: both succeed, the second
# overwrites the first, and the first is left holding an open socket and a
# running keep-alive task for ever. Nothing ever closes it, and because the
# keep-alive keeps answering, the hub never times it out either. The hub accepts
# only two LAN connections, so a couple of leaked clients lock the user out of
# their own heating until the server is restarted.
hub_connect_lock = threading.Lock()


def stop_hub_client(client: "pynobo.nobo") -> None:
    """Shut a pynobo client down properly.

    ``nobo.stop()`` is a coroutine. Calling it without awaiting — as this code
    used to — creates a coroutine that is immediately discarded, so the socket
    and its keep-alive task are never cleaned up and every mode switch leaks a
    connection to the hub.
    """
    try:
        if hub_loop.running:
            hub_loop.run(client.stop(), timeout=5)
        else:
            asyncio.run(client.stop())
    except Exception as exc:
        logger.warning("Error while stopping hub client: %s", exc)


# Responses pynobo has no handling for. Left to it, these reach a "behavior
# undefined for this response" warning and are then discarded.
SEARCH_RESPONSES = {"Y00", "Y01", "Y03", "Y04"}

# Components are reported by these commands: initial info, added, updated.
COMPONENT_RESPONSES = {"H02", "B01", "V01"}


class HubProtocolTap:
    """Observes the raw hub protocol alongside pynobo.

    Two things need raw access that pynobo does not offer:

    * When a component is only a temperature sensor for a zone, pynobo copies
      ``tempsensor_for_zone_id`` over ``zone_id`` in its own dictionary. That is
      convenient for display but destructive for editing: an update built from
      pynobo's copy would tell the hub the device really belongs to that zone.
      The raw rows kept here are what the hub actually said.

    * Receiver search and pairing (``Y00``/``Y01``/``Y03``/``Y04``) are not
      implemented in pynobo at all, so discovered devices would simply be lost.

    Installed by replacing ``response_handler`` on the client, which is a plain
    attribute lookup, so the original still runs for everything else.
    """

    # Discovered devices are forgotten after this long, so a stale list is never
    # presented as if the search were still running.
    DISCOVERY_TTL = 300

    def __init__(self) -> None:
        self.raw_components: Dict[str, List[str]] = {}
        self.search_active = False
        self.discovered: Dict[str, float] = {}
        self.pair_results: Dict[str, bool] = {}
        self.last_error: Optional[List[str]] = None
        self._lock = threading.Lock()

    def attach(self, client) -> None:
        original = client.response_handler

        def handler(response: List[str]) -> None:
            try:
                self.observe(response)
            except Exception as exc:  # never let the tap break the connection
                logger.warning("Hub protocol tap error on %s: %s", response, exc)
            if response and response[0] in SEARCH_RESPONSES:
                return
            original(response)

        client.response_handler = handler

    def observe(self, response: List[str]) -> None:
        if not response:
            return
        code = response[0]

        with self._lock:
            if code == "H00":
                # A fresh dump of everything; drop what we thought we knew.
                self.raw_components.clear()
            elif code in COMPONENT_RESPONSES and len(response) >= 8:
                self.raw_components[response[1]] = list(response[1:8])
            elif code == "S01" and len(response) >= 2:
                self.raw_components.pop(response[1], None)
            elif code == "Y00":
                self.search_active = True
                self.discovered.clear()
            elif code == "Y01":
                self.search_active = False
            elif code == "Y04" and len(response) >= 2:
                self.discovered[response[1]] = time.monotonic()
            elif code == "Y03" and len(response) >= 3:
                self.pair_results[response[1]] = response[2] == "1"
            elif code.startswith("E") and len(code) == 3 and code[1:].isdigit():
                self.last_error = list(response)
                logger.warning("Hub reported an error: %s", " ".join(response))

    def component_row(self, serial: str) -> Optional[List[str]]:
        with self._lock:
            row = self.raw_components.get(serial)
            return list(row) if row else None

    def discovered_serials(self) -> List[str]:
        cutoff = time.monotonic() - self.DISCOVERY_TTL
        with self._lock:
            return sorted(s for s, seen in self.discovered.items() if seen >= cutoff)

    def take_pair_result(self, serial: str) -> Optional[bool]:
        with self._lock:
            return self.pair_results.pop(serial, None)

    def take_error(self) -> Optional[List[str]]:
        with self._lock:
            error, self.last_error = self.last_error, None
            return error


hub_tap: Optional[HubProtocolTap] = None


def connect_to_hub_sync():
    """Connect to the Nobø Hub (synchronous, runs in thread)"""
    global hub, hub_connected, hub_tap

    # One attempt at a time. See hub_connect_lock for why this matters.
    with hub_connect_lock:
        with connection_lock:
            generation = hub_config_generation
            if hub is not None and hub_connected:
                # Another attempt won the race while this one was queued.
                # Connecting again would take a second slot on the hub for a
                # client nobody would ever use.
                logger.info("Hub is already connected — skipping duplicate attempt")
                return

        try:
            logger.info(f"Connecting to Nobø Hub at {NOBO_IP} with serial {NOBO_SERIAL}...")
            hub_loop.start()
            new_hub = pynobo.nobo(NOBO_SERIAL, NOBO_IP, discover=False, synchronous=False)
            # Attach before connecting, so the initial data dump is captured too.
            tap = HubProtocolTap()
            tap.attach(new_hub)
            hub_loop.run(new_hub.start(), timeout=30)
            with connection_lock:
                if generation != hub_config_generation:
                    stale = True
                    displaced = None
                else:
                    stale = False
                    # Whatever was here before is being replaced. Hold on to it
                    # so it can be shut down rather than abandoned.
                    displaced = hub if hub is not new_hub else None
                    hub = new_hub
                    hub_tap = tap
                    hub_connected = True

            if stale:
                # The user changed the configuration while we were connecting. This
                # hub is not the one they asked for, so drop it silently.
                logger.info("Discarding hub connection from a superseded configuration")
                try:
                    stop_hub_client(new_hub)
                except Exception as exc:
                    logger.warning("Error while stopping superseded hub connection: %s", exc)
                return

            if displaced is not None:
                # A previous client was still installed — usually one whose
                # socket died without us noticing. Closing it frees its slot.
                logger.info("Closing the hub connection this one replaces")
                try:
                    stop_hub_client(displaced)
                except Exception as exc:
                    logger.warning("Error while stopping the replaced hub connection: %s", exc)

            logger.info("Successfully connected to Nobø Hub")

            # Register callback for hub updates
            new_hub.register_callback(hub_update_callback)

        except Exception as e:
            logger.error(f"Failed to connect to Nobø Hub: {e}")
            with connection_lock:
                # Two things have to be true before a failure is allowed to
                # declare the system disconnected.
                #
                # The configuration must still be the one this attempt was for.
                # Without that, a doomed attempt against an old address marks
                # demo mode as disconnected seconds after the user switched to
                # it, leaving the UI stuck on "Hub not connected".
                #
                # And nothing else may have connected in the meantime. The
                # generation only moves when the *configuration* changes, so it
                # cannot see a hub installed by any other route — which is
                # exactly what happens when an attempt against an unreachable
                # address is still timing out while a working connection is
                # established beside it. That live client is the truth; a stale
                # failure must not contradict it, and clearing `hub` here would
                # disconnect a hub this attempt never owned.
                superseded = generation != hub_config_generation
                someone_else_connected = hub is not None
                if not superseded and not someone_else_connected:
                    hub_connected = False
                    hub = None
                    hub_tap = None
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
    global hub, hub_connected, hub_tap

    with connection_lock:
        current_hub = hub

    if current_hub:
        try:
            stop_hub_client(current_hub)
            logger.info("Disconnected from Nobø Hub")
        except Exception as exc:
            logger.warning("Error while disconnecting from hub: %s", exc)

    with connection_lock:
        hub = None
        hub_tap = None
        hub_connected = False


async def apply_hub_config(demo_mode: bool, serial: str, ip: str) -> dict:
    """Switch the running server to a new hub configuration.

    Persists the settings first (so they survive a restart or reboot), then
    rebinds the module-level globals and rebuilds the hub connection in place.
    Returns a dict describing the resulting state.
    """
    global NOBO_SERIAL, NOBO_IP, DEMO_MODE, HUB_CONFIG_SOURCE, hub_config_generation

    config = {"demo_mode": bool(demo_mode), "serial": serial, "ip": ip}
    config_persistence.save_hub_config(config)

    previous_mode = "demo" if DEMO_MODE else "production"

    # Always drop the existing connection before changing the target.
    disconnect_from_hub()

    # Invalidate any connection attempt still running against the old settings.
    with connection_lock:
        hub_config_generation += 1

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
    global hub_connected

    MIN_INTERVAL = 5      # Start at 5 seconds
    MAX_INTERVAL = 300     # Cap at 5 minutes
    interval = MIN_INTERVAL
    attempt = 0

    while True:
        await asyncio.sleep(interval)

        if DEMO_MODE:
            # Nothing to reconnect to while simulating; reset backoff state.
            # Demo data is always available, so repair the flag if a late-
            # finishing connection attempt cleared it. Belt and braces: the
            # generation check in connect_to_hub_sync should prevent this, but
            # a stuck flag here means zones return 503 forever.
            with connection_lock:
                if not hub_connected:
                    logger.info("Demo mode is active — marking the data source as available")
                    hub_connected = True
            interval = MIN_INTERVAL
            attempt = 0
            continue

        with connection_lock:
            currently_connected = hub_connected

        if not currently_connected:
            attempt += 1
            logger.warning(f"Hub disconnected — reconnect attempt #{attempt} (next retry in {interval}s)")
            add_log_entry("error", f"Hub disconnected — reconnect attempt #{attempt} (delay: {interval}s)", source="hub")

            # Alert once, on the way down, not on every retry. Deliberately not
            # on the first attempt: a hub that blinks and comes straight back is
            # not worth an email, and the retry is usually quicker than the mail.
            if attempt >= 2:
                notifier.set_condition(
                    "hub_offline", "hub_offline", True,
                    subject="Lost contact with the hub",
                    body=(
                        "The Raspberry Pi can no longer reach the Nobø hub.\n\n"
                        "While this lasts, nothing can be switched or scheduled — not by this\n"
                        "system and not by the Nobø app. Any override already on the hub stays\n"
                        "in force, and heaters keep following whatever they were last told.\n\n"
                        "Usually this is the hub losing power or the network going down.\n\n"
                        "Reconnection is being retried automatically."
                    ),
                    severity="critical",
                )
            try:
                await connect_to_hub()
                with connection_lock:
                    reconnected = hub_connected
                if reconnected:
                    logger.info(f"Hub reconnected successfully after {attempt} attempt(s)")
                    add_log_entry("received", f"Hub reconnected after {attempt} attempt(s)", source="hub")
                    if notifier.is_raised("hub_offline"):
                        notifier.set_condition(
                            "hub_offline", "hub_offline", False,
                            recovery_event_type="hub_online",
                            recovery_subject="The hub is back",
                            recovery_body=(
                                f"Contact with the Nobø hub was restored after {attempt} attempt(s).\n"
                                "Everything can be controlled again."
                            ),
                        )
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

        # Every hub push is also the chance to notice that somebody else moved
        # something. Guarded, because a fault in the notifier must never stop
        # the browsers from being updated.
        try:
            zone_watcher.observe(zones_data)
        except Exception as exc:
            logger.error("Notification watcher failed on a hub update: %s", exc)

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


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def refresh_notifier_identity() -> None:
    """Give the notifier the name the user chose, so alerts are recognisable."""
    try:
        site = config_persistence.load_site()
        name = (site.get("name") or "").strip()
        notifier.site_name = name or "Nobø Control"
    except Exception:
        notifier.site_name = "Nobø Control"


def note_local_write(zone_id: Any, field_name: str, value: Any) -> None:
    """
    Record that this application made a change, before it is sent.

    This is the whole basis of "somebody else changed it": the hub tells us what
    changed but never who, so the only way to recognise our own echo is to have
    written it down first. Called before the hub command, because the push can
    come back faster than the call returns.
    """
    try:
        zone_watcher.record_local_write(str(zone_id), field_name, value)
    except Exception as exc:
        logger.debug("Could not record a local write: %s", exc)


async def notification_watch_loop():
    """
    Re-check the house every minute for changes made somewhere else.

    The hub pushes most changes, and ``broadcast_zone_update`` observes those as
    they arrive. This loop is the safety net for anything that does not produce
    a push — a missed message, or a reconnection that resynchronised quietly.
    """
    INTERVAL = 60
    while True:
        await asyncio.sleep(INTERVAL)
        try:
            if not notifier.settings.get("enabled"):
                continue
            with connection_lock:
                connected = hub_connected
            if connected:
                zone_watcher.observe(get_zones_data())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Notification watch loop error: %s", exc)


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
                # Whether anything in this room can measure the temperature. The
                # demo house deliberately contains both kinds, so the UI can be
                # exercised without a hub.
                'has_temp_sensor': any(model_has_temp_sensor(c) for c in demo_zone['components']),
            })
        return zones
    
    # Real hub mode
    if not current_hub:
        return []
    
    zones = []
    try:
        for zone_id, zone in current_hub.zones.items():
            zone_name = decode_hub_name(zone.get('name', f'Zone {zone_id}'))
            
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
                'icon': zone_icons.get(str(zone_id), ''),
                'rooms': [zone_name],  # Default to zone name
                'components': zone_components,
                'components_display': components_display,
                'components_types': components_types,
                'components_names': [
                    decode_hub_name(current_hub.components.get(c, {}).get('name', ''))
                    for c in zone_components
                ],
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
                # Read from the model table rather than from whether a reading
                # has arrived, so a room is known to be unmeasurable straight
                # away instead of after waiting for a temperature that is never
                # coming.
                'has_temp_sensor': any(model_has_temp_sensor(c) for c in zone_components),
            })
    except Exception as e:
        logger.error(f"Error getting zones data: {e}")
    
    return zones


# ---------------------------------------------------------------------------
# Feature capabilities
# ---------------------------------------------------------------------------
# Several editing features are implemented for demo mode only and answer 501
# against a real hub. The web UI used to offer them anyway, so the buttons were
# there, did nothing, and returned a raw error (QA defect D-04).
#
# This map is the single source of truth: the endpoints raise from it and the UI
# reads the same values from /api/capabilities, so the two cannot drift apart.
DEMO_ONLY_FEATURES: Dict[str, str] = {}
"""Features this application only implements against the built-in demo data.

This is now empty: every editing feature works against a real hub as well.

It used to list zone creation and deletion, schedule editing, zone icons and all
five device operations. Those were never hub limitations — the protocol has
commands for all of them (A00/R00 for zones, A01/U01/R01 for components,
A02/U02/R02 for week profiles, X00/X01/X03 for pairing) — the real-hub branches
of the endpoints had simply never been written. They have been now.

The map is kept rather than deleted because it is the mechanism that keeps the
UI honest: anything added here is refused by the endpoint *and* greyed out in
the browser, from the same source of truth.
"""

HUB_ONLY_FEATURES = {
    "discover_devices": "Searching for nearby devices needs the hub's radio, so "
                        "it only works when a real hub is connected. In demo mode "
                        "there is no hardware to listen for.",
}
"""Features that need real hardware and cannot be simulated."""


def get_capabilities() -> Dict[str, Dict[str, Any]]:
    """What this installation can actually do right now.

    Everything not listed here works in both modes.
    """
    capabilities = {
        name: {
            "supported": DEMO_MODE,
            "reason": None if DEMO_MODE else reason,
        }
        for name, reason in DEMO_ONLY_FEATURES.items()
    }
    capabilities.update({
        name: {
            "supported": not DEMO_MODE,
            "reason": reason if DEMO_MODE else None,
        }
        for name, reason in HUB_ONLY_FEATURES.items()
    })
    return capabilities


def require_capability(name: str) -> None:
    """Refuse a feature that is not available in the current mode.

    Answers 501 (not implemented here) with the same explanation the UI shows,
    so the message the user reads is the message the API gives.
    """
    capability = get_capabilities().get(name)
    if capability is not None and not capability["supported"]:
        raise HTTPException(status_code=501, detail=capability["reason"])


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
@app.get("/api/capabilities")
async def get_capabilities_endpoint():
    """Which editing features work in the current mode.

    The web UI reads this on load and greys out the controls that would only
    return 501, showing the reason instead of letting the user press a button
    that cannot work (QA defect D-04).
    """
    return {"demo_mode": DEMO_MODE, "features": get_capabilities()}


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
            "number and IP address, and that the hub is powered on and on this "
            "network. The hub accepts two connections over the local network at "
            "once, so the official Nobo app can stay connected — but a second "
            "phone on the same network is one too many."
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


# ---------------------------------------------------------------------------
# Site identity
# ---------------------------------------------------------------------------
# The hub has no concept of a house name, so this lives entirely on the Pi.
# It is cosmetic, but it is the difference between an app that belongs to you
# and one that calls your home "the cabin".

# Used wherever a name stands on its own: page titles, the header, the trip
# heading. Title case, because it is a proper noun in that position.
SITE_NAME_FALLBACK = "Cabin"
# Used mid-sentence: "Warm all of the cabin?", "returns the cabin to its normal
# schedules". A named site substitutes cleanly into the same phrasings, which is
# why every string is written to take a name rather than to be a name.
SITE_INLINE_FALLBACK = "the cabin"


def site_settings() -> dict:
    """Current site identity, with both display forms already resolved.

    Resolving here rather than in each caller means the fallback wording exists
    in exactly one place, and a page cannot accidentally render "Warm all of
    Cabin?".
    """
    site = config_persistence.load_site()
    name = site.get("name") or ""
    return {
        "name": name,
        "show_on_login": bool(site.get("show_on_login", True)),
        "display_name": name or SITE_NAME_FALLBACK,
        "inline_name": name or SITE_INLINE_FALLBACK,
        "is_named": bool(name),
        "max_length": config_persistence.SITE_NAME_MAX,
        # "" means "follow the browser". Sent to every interface so dates are
        # written the same way on every device in the house.
        "locale": site.get("locale") or "",
        # Stated rather than offered as choices. Times are 24-hour because the
        # hub's own week profiles are "HHMM" and its handshake is
        # "yyyyMMddHHmmss"; temperatures are Celsius because API_Nobo.pdf says
        # "temperatures are in celsius" and there is no other unit to ask for.
        "clock": "24h",
        "temperature_unit": "C",
    }


@app.get("/api/site")
async def get_site():
    """What this place is called. Read by every interface on load."""
    return site_settings()


@app.put("/api/site")
async def update_site(request: Request, body: SiteUpdate):
    """Rename the system, or change how dates are written.

    Admin only, for the same reason as the hub settings: it changes what every
    user of this installation sees, including on the sign-in page, so it is not
    left open to an ordinary account or a headless integration.
    """
    session = _get_session_or_401(request)
    _require_admin(session)

    current = config_persistence.load_site()

    # Absent fields keep their current value, so a client can change the name
    # without having to know or resend the login-page preference.
    name = current["name"] if body.name is None else body.name
    show = current["show_on_login"] if body.show_on_login is None else bool(body.show_on_login)
    locale = current.get("locale", "") if body.locale is None else body.locale

    cleaned = config_persistence._clean_site_name(name)
    if body.name is not None and name.strip() and not cleaned:
        # They typed something, and nothing usable survived normalisation.
        raise HTTPException(
            status_code=400,
            detail="That name contains no usable characters. Try letters, numbers or spaces.",
        )

    cleaned_locale = config_persistence._clean_locale(locale)
    if body.locale is not None and locale.strip() and not cleaned_locale:
        raise HTTPException(
            status_code=400,
            detail="That is not a valid language tag. Use something like nb-NO, sv-SE or en-GB.",
        )

    previous_locale = current.get("locale", "")
    config_persistence.save_site(
        {"name": cleaned, "show_on_login": show, "locale": cleaned_locale}
    )

    # Alerts are signed with the system name, so keep the notifier in step.
    refresh_notifier_identity()

    if body.name is not None and cleaned != current["name"]:
        add_log_entry(
            "sent",
            f"System renamed to '{cleaned}'" if cleaned else "System name cleared",
            source="api",
        )
    if cleaned_locale != previous_locale:
        add_log_entry(
            "sent",
            f"Date format set to {cleaned_locale}" if cleaned_locale
            else "Date format now follows the browser",
            source="api",
        )
    return site_settings()


# ---------------------------------------------------------------------------
# Notification settings
# ---------------------------------------------------------------------------
# Admin only, all three. The settings hold a mail password and decide where
# alerts about this building are sent, which is not something an ordinary
# account should be able to read, redirect or silence.

class NotificationEmail(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = None
    security: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    from_addr: Optional[str] = None
    to_addrs: Optional[List[str]] = None


class NotificationUpdate(BaseModel):
    enabled: Optional[bool] = None
    email: Optional[NotificationEmail] = None
    events: Optional[Dict[str, bool]] = None
    min_minutes_between: Optional[int] = None
    quiet_hours: Optional[Dict[str, Any]] = None


def _merge_notification_body(body: NotificationUpdate) -> Dict[str, Any]:
    """
    Fold a partial update onto what is stored.

    Absent fields keep their value so a client can flip one toggle without
    having to resend the whole configuration — and, crucially, without having
    to know the password, which it is never given.
    """
    current = notifications.load_settings()
    if body.enabled is not None:
        current["enabled"] = bool(body.enabled)
    if body.events:
        for key, value in body.events.items():
            if key in notifications.EVENT_TYPES:
                current["events"][key] = bool(value)
    for field_name in ("min_minutes_between",):
        value = getattr(body, field_name)
        if value is not None:
            current[field_name] = value
    if body.quiet_hours is not None:
        current["quiet_hours"] = body.quiet_hours
    if body.email is not None:
        for field_name in ("host", "port", "security", "username", "from_addr", "to_addrs"):
            value = getattr(body.email, field_name)
            if value is not None:
                current["email"][field_name] = value
        # An omitted password means "keep the one you have". An empty string is
        # an explicit clear, which is how the user removes it.
        if body.email.password is not None:
            current["email"]["password"] = body.email.password
    return current


def _temperature_capability() -> Dict[str, Any]:
    """
    Whether anything in this installation measures room temperature.

    No alert depends on this any more — the ones that did were removed, because
    only the SW4 reports a temperature and it is no longer sold. It is still
    reported so the interface can explain a blank room temperature instead of
    leaving it looking broken.
    """
    try:
        zones = get_zones_data()
    except Exception:
        zones = []
    with_sensor = [z["name"] for z in zones if z.get("has_temp_sensor")]
    return {
        "available": bool(with_sensor),
        "zones_with_sensor": with_sensor,
        "zones_total": len(zones),
        "sensor_models": sorted(
            m.name for m in pynobo.nobo.MODELS.values()
            if getattr(m, "has_temp_sensor", False)
        ),
    }


@app.get("/api/notifications")
async def get_notifications(request: Request):
    """The current notification settings, with the password redacted."""
    session = _get_session_or_401(request)
    _require_admin(session)
    out = notifications.public_settings()
    out["temperature"] = _temperature_capability()
    return out


@app.put("/api/notifications")
async def update_notifications(request: Request, body: NotificationUpdate):
    """Save notification settings and apply them immediately."""
    session = _get_session_or_401(request)
    _require_admin(session)

    merged = _merge_notification_body(body)

    # Turning it on with settings that cannot deliver would look like it worked
    # and then quietly never send anything, which is the worst outcome for a
    # feature whose entire job is to speak up.
    if merged.get("enabled"):
        problems = notifications.validate_email_config(merged)
        if problems:
            raise HTTPException(status_code=400, detail=" ".join(problems))

    saved = notifications.save_settings(merged)
    notifier.reload(saved)
    refresh_notifier_identity()

    add_log_entry(
        "sent",
        "Notifications turned on" if saved["enabled"] else "Notifications turned off",
        command="notifications update",
        source="api",
    )
    out = notifications.public_settings(saved)
    out["temperature"] = _temperature_capability()
    return out


@app.post("/api/notifications/test")
async def test_notification(request: Request, body: Optional[NotificationUpdate] = None):
    """
    Send a test email, reporting the real reason if it fails.

    Accepts the settings in the body so the user can test before saving — the
    alternative is making them save a broken configuration to find out it is
    broken. The error text comes straight from the mail server, because
    "authentication failed" and "name or service not known" need different
    fixes and a generic failure message helps nobody.
    """
    session = _get_session_or_401(request)
    _require_admin(session)

    cfg = _merge_notification_body(body) if body is not None else notifications.load_settings()

    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None, notifications.send_test, cfg, notifier.site_name
            ),
            timeout=notifications.SEND_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        add_log_entry("error", "Test notification timed out", source="api")
        raise HTTPException(
            status_code=504,
            detail=f"The mail server did not answer within {notifications.SEND_TIMEOUT_SECONDS} seconds. "
                   "Check the server address and port.",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        # Deliberate status codes must reach the browser as themselves, not be
        # flattened into a 500 by the catch-all below.
        raise
    except Exception as exc:
        add_log_entry("error", f"Test notification failed: {exc}", source="api")
        raise HTTPException(status_code=502, detail=f"The mail server refused it: {exc}")

    add_log_entry("sent", "Test notification sent", command="notifications test", source="api")
    return {"status": "success", "sent_to": cfg["email"]["to_addrs"]}


@app.get("/manifest.webmanifest")
async def site_manifest():
    """The installed-app manifest, carrying the user's chosen name.

    Built from the static manifest rather than written out here, so the icons,
    colours and — critically — ``start_url``/``scope`` stay defined in one
    place. Only the three name fields are overridden. A manifest scoped to a
    static path would install a home-screen icon that opens a file instead of
    the app.
    """
    base = {}
    static_manifest = STATIC_DIR / "ui" / ACTIVE_UI / "manifest.webmanifest"
    try:
        with static_manifest.open("r", encoding="utf-8") as fh:
            base = json.load(fh)
    except FileNotFoundError:
        logger.warning("No static manifest at %s — serving a minimal one", static_manifest)
    except json.JSONDecodeError as exc:
        logger.warning("Static manifest is not valid JSON: %s — serving a minimal one", exc)

    site = site_settings()
    base.setdefault("start_url", "/")
    base.setdefault("scope", "/")
    base["name"] = f"{site['display_name']} — Nobø Control"
    base["short_name"] = site["display_name"][:12]
    base["description"] = f"Heating control for {site['inline_name']}."

    return JSONResponse(
        content=base,
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/favicon.ico")
async def favicon():
    """Serve the interface's icon at the address browsers ask for by default.

    ``/favicon.ico`` has been on the public allow-list all along but nothing
    ever answered it, so it returned 404. Browsers request it unprompted for
    any page that does not declare an icon — the classic interface declares
    none — and on a 404 they draw a letter from the hostname instead.

    An SVG is returned rather than a real .ico. The extension in the URL is a
    convention, not a promise; every browser that still asks for this path
    honours the Content-Type. Serving the same file the interface already uses
    means there is one icon to keep, not two.
    """
    icon = STATIC_DIR / "ui" / ACTIVE_UI / "icon.svg"
    if not icon.is_file():
        # A missing icon is not worth an error page.
        return Response(status_code=204)
    return FileResponse(
        icon,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-cache"},
    )


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
    except HTTPException:
        raise
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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting zones: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/zones")
async def add_zone(zone: ZoneAdd):
    """Create a new zone"""
    require_capability("add_zone")

    with connection_lock:
        connected = hub_connected
        current_hub = hub

    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")

    try:
        name = zone.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Zone name cannot be empty")
        if len(encode_hub_name(name).encode('utf-8')) > ZONE_NAME_MAX_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Zone name is too long for the hub (maximum "
                       f"{ZONE_NAME_MAX_BYTES} bytes)",
            )

        if DEMO_MODE:
            # Demo mode has to refuse exactly what the hub refuses, or it
            # teaches the wrong limits.
            if any(z.get('name', '') == name for z in DEMO_ZONES):
                raise HTTPException(status_code=400, detail=f"A zone named '{name}' already exists")

            # Auto-increment zone_id based on current max
            new_id = str(max((int(z['zone_id']) for z in DEMO_ZONES), default=0) + 1)
            DEMO_ZONES.append({
                "zone_id": new_id,
                "name": name,
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
            logger.info(f"Demo mode: Zone '{name}' created with id {new_id}")
            add_log_entry(
                "sent",
                f"[DEMO] Zone '{name}' created with id {new_id}",
                f"A00 0 {name} {DEFAULT_WEEK_PROFILE_ID}",
            )
            config_persistence.save_demo_zones(DEMO_ZONES)
            return {"status": "success", "zone_id": new_id, "name": name}

        # Real hub mode
        if not current_hub:
            raise HTTPException(status_code=503, detail="Hub not connected")

        if any(decode_hub_name(z.get('name', '')) == name for z in current_hub.zones.values()):
            raise HTTPException(status_code=400, detail=f"A zone named '{name}' already exists")

        encoded = encode_hub_name(name)
        before = set(current_hub.zones)
        # The hub assigns the real id and ignores the one sent, but a placeholder
        # still has to occupy the field.
        await hub_command(current_hub.async_send_command([
            "A00",
            "0",
            encoded,
            DEFAULT_WEEK_PROFILE_ID,
            str(DEFAULT_NEW_ZONE_COMFORT),
            str(DEFAULT_NEW_ZONE_ECO),
            pynobo.nobo.API.OVERRIDE_ALLOWED,
            pynobo.nobo.API.OVERRIDE_ID_NONE,
        ]))

        new_ids = await wait_for_hub_state(lambda: set(current_hub.zones) - before)
        if not new_ids:
            raise HTTPException(
                status_code=502,
                detail="The hub did not confirm the new zone. Please try again.",
            )
        new_id = sorted(new_ids)[0]

        add_log_entry(
            "sent",
            f"Zone '{name}' created with id {new_id}",
            command=f"A00 {encoded}",
            source="api",
        )
        await broadcast_zone_update()
        return {"status": "success", "zone_id": new_id, "name": name}

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

        # Real hub mode. The name lives on the hub; the icon is this app's own
        # setting and is stored locally, exactly as it is in demo mode.
        if not current_hub:
            raise HTTPException(status_code=503, detail="Hub not connected")

        if zone_id not in current_hub.zones:
            raise HTTPException(status_code=404, detail="Zone not found")

        zone = current_hub.zones[zone_id]
        old_name = decode_hub_name(zone.get('name', zone_id))

        if update.name is not None:
            await hub_command(current_hub.async_update_zone(zone_id, update.name.strip()))
            add_log_entry(
                "sent",
                f"update_zone({zone_id}, '{update.name.strip()}')",
                command=f"update_zone zone_id={zone_id} name={update.name.strip()}",
                source="api",
            )

        if update.icon is not None:
            zone_icons[str(zone_id)] = update.icon.strip()
            config_persistence.save_zone_icons(zone_icons)

        await asyncio.sleep(0.3)
        return {
            "status": "success",
            "zone_id": zone_id,
            "name": update.name.strip() if update.name is not None else old_name,
            "icon": zone_icons.get(str(zone_id), ''),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating zone: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/zones/{zone_id}")
async def delete_zone(zone_id: str):
    """Delete a zone"""
    require_capability("delete_zone")

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

        # Real hub mode
        if not current_hub:
            raise HTTPException(status_code=503, detail="Hub not connected")

        if zone_id not in current_hub.zones:
            raise HTTPException(status_code=404, detail="Zone not found")

        # Deleting a zone that still contains heaters would leave them
        # unassigned and unheatable, which is not something to do by accident.
        occupants = [
            serial for serial, comp in current_hub.components.items()
            if comp.get('zone_id') == zone_id
        ]
        if occupants:
            names = ", ".join(format_serial_display(s) for s in occupants)
            raise HTTPException(
                status_code=409,
                detail=f"This zone still contains {len(occupants)} device(s): {names}. "
                       f"Move or remove them first.",
            )

        zone = current_hub.zones[zone_id]
        zone_name = decode_hub_name(zone.get('name', zone_id))
        await hub_command(current_hub.async_send_command(
            ["R00"] + list(zone.values())
        ))
        gone = await wait_for_hub_state(lambda: zone_id not in current_hub.zones)
        if not gone:
            raise HTTPException(
                status_code=502,
                detail="The hub did not confirm the deletion. Please try again.",
            )

        add_log_entry(
            "sent",
            f"Zone '{zone_name}' (id={zone_id}) deleted",
            command=f"R00 {zone_id}",
            source="api",
        )
        await broadcast_zone_update()
        return {"status": "success", "zone_id": zone_id}

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
    
    # Written down before the command goes out, because the hub can push the
    # change back to us faster than this function returns. Without this, every
    # change we make would be reported as "changed from another app".
    note_local_write(zone_id, "mode", mode)

    try:
        # Demo mode - update simulated data
        if DEMO_MODE:
            demo_zone = next((z for z in DEMO_ZONES if z['zone_id'] == zone_id), None)
            if not demo_zone:
                raise HTTPException(status_code=404, detail="Zone not found")
            
            demo_zone['mode'] = mode
            if mode == 'normal':
                DEMO_ZONE_OVERRIDES.discard(zone_id)
            else:
                DEMO_ZONE_OVERRIDES.add(zone_id)
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
            await hub_command(current_hub.async_create_override(
                pynobo.nobo.API.OVERRIDE_MODE_NORMAL,
                pynobo.nobo.API.OVERRIDE_TYPE_NOW,
                pynobo.nobo.API.OVERRIDE_TARGET_ZONE,
                zone_id,
            ))
            add_log_entry(
                "sent",
                f"create_override(NORMAL, NOW, ZONE, zone_{zone_id}) — cancel override",
                command=f"create_override NORMAL NOW ZONE {zone_id}",
                source="api",
            )
        else:
            # Set override mode
            await hub_command(current_hub.async_create_override(
                mode_map[mode],
                pynobo.nobo.API.OVERRIDE_TYPE_NOW,
                pynobo.nobo.API.OVERRIDE_TARGET_ZONE,
                zone_id,
            ))
            add_log_entry(
                "sent",
                f"create_override({mode.upper()}, NOW, ZONE, zone_{zone_id})",
                command=f"create_override {mode} NOW ZONE {zone_id}",
                source="api",
            )
        
        # Wait a moment for hub to update
        await asyncio.sleep(0.5)
        
        return {"status": "success", "zone_id": zone_id, "mode": mode}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting zone override: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def round_to_whole_degree(value: Any) -> int:
    """
    The Eco Hub stores set points as whole degrees. Truncating turned a
    requested 20.6°C into 20°C, so a room ran colder than the user asked for;
    round to the nearest degree instead.

    Values reach this from two places with different types. The API supplies
    floats, but the hub supplies *strings* — the Nobø protocol is text on the
    wire and pynobo keeps zone values exactly as they arrived. Coercing here is
    what stops that difference reaching the arithmetic.

    That difference was a real fault: setting one set point on its own filled
    the other in from the hub, so ``'15' + 0.5`` raised TypeError and every
    temperature change from the web interface failed with a 500 against a real
    hub. Demo mode stores floats, so it only ever broke on real hardware.
    """
    try:
        return math.floor(float(value) + 0.5)
    except (TypeError, ValueError) as exc:
        # A set point the hub gave us that is not a number is the hub's
        # problem, not the caller's, so say so rather than returning a 500.
        raise HTTPException(
            status_code=502,
            detail=f"The hub reported a temperature this app could not read: {value!r}",
        ) from exc


def resolve_temperature_update(
    temps: "TemperatureUpdate",
    current_comfort: float,
    current_eco: float,
) -> tuple:
    """
    Validate a temperature change and return the (comfort, eco) whole degrees
    to send to the hub.

    Values that are left out keep their current setting, which is why the
    comfort/eco ordering can only be checked here, once both are known.
    """
    if temps.comfort is None and temps.eco is None:
        raise HTTPException(
            status_code=400,
            detail="Provide a comfort and/or eco temperature to set",
        )

    for label, value in (("Comfort", temps.comfort), ("Eco", temps.eco)):
        if value is not None and not 7 <= value <= 30:
            raise HTTPException(
                status_code=400,
                detail=f"{label} temperature must be between 7 and 30°C",
            )

    comfort = (
        round_to_whole_degree(temps.comfort)
        if temps.comfort is not None
        else round_to_whole_degree(current_comfort)
    )
    eco = (
        round_to_whole_degree(temps.eco)
        if temps.eco is not None
        else round_to_whole_degree(current_eco)
    )

    if eco >= comfort:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Eco temperature ({eco}°C) must be lower than the comfort "
                f"temperature ({comfort}°C). Otherwise the zone never saves any "
                f"energy when it drops to eco."
            ),
        )

    return comfort, eco


@app.post("/api/zones/{zone_id}/temperature")
async def set_zone_temperature(zone_id: str, temps: TemperatureUpdate):
    """Set comfort and/or eco temperature for a zone"""
    with connection_lock:
        connected = hub_connected
        current_hub = hub
    
    if not connected:
        raise HTTPException(status_code=503, detail="Hub not connected")
    
    # Recorded up front so our own change is not mistaken for somebody else's.
    if temps.comfort is not None:
        note_local_write(zone_id, "comfort", temps.comfort)
    if temps.eco is not None:
        note_local_write(zone_id, "eco", temps.eco)

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
            comfort, eco = resolve_temperature_update(
                temps,
                demo_zone.get('comfort_temp') or 21.0,
                demo_zone.get('eco_temp') or 17.0,
            )
            if temps.comfort is not None:
                demo_zone['comfort_temp'] = float(comfort)
            if temps.eco is not None:
                demo_zone['eco_temp'] = float(eco)
            
            add_log_entry(
                "sent",
                f"[DEMO] Would send: update_zone(zone_{zone_id}, comfort={comfort}, eco={eco})",
                command=f"update_zone {zone_id} comfort={comfort} eco={eco}",
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
        
        # Validate, and fill in whichever set point was not supplied
        comfort, eco = resolve_temperature_update(
            temps,
            zone.get('temp_comfort_c', 21),
            zone.get('temp_eco_c', 17),
        )
        
        await hub_command(current_hub.async_update_zone(zone_id, name=zone['name'], temp_comfort_c=comfort, temp_eco_c=eco))
        
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
    
    # One record covering every zone, since a global override moves them all.
    note_local_write("*", "mode", 'normal' if mode == 'home' else mode)

    try:
        # Demo mode - update all simulated zones
        if DEMO_MODE:
            for demo_zone in DEMO_ZONES:
                # A zone override outranks the global one on the hub, so a zone
                # that has its own override is left alone here â€” same as the
                # hardware. Whoever set it is responsible for clearing it.
                if str(demo_zone.get('zone_id')) in DEMO_ZONE_OVERRIDES:
                    continue
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
            exceptions = await _sync_away_exceptions(mode)
            return {"status": "success", "mode": mode, "source": "manual",
                    "away_exceptions_applied": exceptions}
        
        # Real hub mode — use a single global override command instead of per-zone
        if not current_hub:
            raise HTTPException(status_code=503, detail="Hub not connected")
        
        if mode == 'normal' or mode == 'home':
            await hub_command(current_hub.async_create_override(
                pynobo.nobo.API.OVERRIDE_MODE_NORMAL,
                pynobo.nobo.API.OVERRIDE_TYPE_NOW,
                pynobo.nobo.API.OVERRIDE_TARGET_GLOBAL,
            ))
            add_log_entry(
                "sent",
                "create_override(NORMAL, NOW, GLOBAL) — cancel all overrides",
                command="create_override NORMAL NOW GLOBAL",
                source="api",
            )
        else:
            await hub_command(current_hub.async_create_override(
                mode_map[mode],
                pynobo.nobo.API.OVERRIDE_TYPE_NOW,
                pynobo.nobo.API.OVERRIDE_TARGET_GLOBAL,
            ))
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
        exceptions = await _sync_away_exceptions(mode)
        return {"status": "success", "mode": mode, "source": "manual",
                "away_exceptions_applied": exceptions}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting global override: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===== Away Schedule Endpoints =====

@app.get("/api/global-mode/away-schedule")
async def get_away_schedule():
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


class AwayExceptionsUpdate(BaseModel):
    zone_ids: List[str] = []


@app.get("/api/global-mode/away-exceptions")
async def get_away_exceptions():
    """
    Zones kept on Eco while the rest of the house is Away.

    Nobø's Away is a fixed 7 °C anti-frost temperature and cannot be changed,
    so ``away_temperature`` is returned alongside the list to make it clear what
    the exception is protecting the room from.
    """
    zone_ids = config_persistence.load_away_exceptions()
    try:
        zones = await get_zones()
    except HTTPException:
        # No hub, so no zone names. The stored list is still the truth.
        return {
            "zone_ids": zone_ids,
            "zone_names": [],
            "away_temperature": AWAY_TEMPERATURE,
            "unknown_zone_ids": [],
        }
    known = {str(z['zone_id']): z['name'] for z in zones.get('zones', [])}
    return {
        "zone_ids": [z for z in zone_ids if z in known],
        "zone_names": [known[z] for z in zone_ids if z in known],
        "away_temperature": AWAY_TEMPERATURE,
        # Stale ids are reported rather than silently dropped, so a room that
        # was deleted while listed does not quietly stop being protected.
        "unknown_zone_ids": [z for z in zone_ids if z not in known],
    }


@app.put("/api/global-mode/away-exceptions")
async def update_away_exceptions(body: AwayExceptionsUpdate):
    """
    Replace the list of zones kept on Eco during Away.

    Applies immediately when the house is already Away, so the setting does not
    appear to do nothing until the next trip.
    """
    zones = await get_zones()
    known = {str(z['zone_id']) for z in zones.get('zones', [])}
    requested = [str(z) for z in body.zone_ids]

    unknown = [z for z in requested if z not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown zone ids: {', '.join(unknown)}")

    # De-duplicate but keep the order the user chose.
    seen = set()
    zone_ids = [z for z in requested if not (z in seen or seen.add(z))]

    config_persistence.save_away_exceptions(zone_ids)
    add_log_entry(
        "sent",
        f"Away exceptions set to {', '.join(zone_ids) if zone_ids else 'none'}",
        command=f"away_exceptions {','.join(zone_ids)}",
        source="api",
    )

    applied: List[str] = []
    schedule = away_schedule.load_schedule()
    now = datetime.now(timezone.utc)
    house_is_away = (
        away_schedule.is_schedule_active(schedule, now)
        or any(z.get('current_mode') == 'away' for z in zones.get('zones', []))
    )
    if house_is_away and zone_ids:
        applied = await _apply_away_exceptions()

    return {"status": "success", "zone_ids": zone_ids, "applied_now": applied}


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


# Zone ids currently under a zone-level override in demo mode.
#
# The real hub ranks a zone override above the global one, and cancelling the
# global override does not touch the zone. Demo mode used to blanket-assign
# every zone on a global change, which is tidier than the hardware and hid a
# real defect: an away-exception room that could never come home. Demo now
# models the hub's actual ranking so that class of bug fails a test instead of
# reaching a cabin.
DEMO_ZONE_OVERRIDES: Set[str] = set()

# Zones we put on a zone-level Eco override when the house went Away. The hub
# will not release these by itself: cancelling the global override cancels only
# the global one, so we have to remember what we did and undo it ourselves.
# Held alongside the persisted exception list rather than instead of it, so a
# zone removed from the list mid-away still gets released.
_away_exception_zones_applied: Set[str] = set(
    config_persistence.load_away_exceptions_applied()
)


def _record_away_exceptions_applied() -> None:
    config_persistence.save_away_exceptions_applied(_away_exception_zones_applied)


async def _clear_away_exceptions(source: str = "api", fallback_mode: str = "home") -> List[str]:
    """
    Release the zone-level Eco overrides that ``_apply_away_exceptions`` created.

    A zone override outranks the global override on the hub, which is exactly why
    the away exception works â€” and exactly why coming home does not undo it.
    ``create_override(NORMAL, GLOBAL)`` cancels the *global* override only, so
    without this the excluded room holds Eco for ever and no amount of pressing
    Home will free it.

    Only zones this app actually overrode are released. A zone that is merely
    *configured* as an exception but was never touched is left alone, so a
    global Comfort still reaches it.

    Returns the zone ids that were released.
    """
    global _away_exception_zones_applied

    zone_ids = set(_away_exception_zones_applied)
    if not zone_ids:
        return []

    with connection_lock:
        current_hub = hub

    released: List[str] = []

    if DEMO_MODE:
        # Dropping the zone override does not mean "follow the schedule" -- it
        # means the global override applies again, whatever that currently is.
        # On the hub that falls out of the ranking for free; here it has to be
        # spelled out, or a global Comfort would land the room on normal.
        settled = 'normal' if fallback_mode in ('home', 'normal') else fallback_mode
        for demo_zone in DEMO_ZONES:
            if str(demo_zone.get('zone_id')) in zone_ids:
                demo_zone['mode'] = settled
                DEMO_ZONE_OVERRIDES.discard(str(demo_zone.get('zone_id')))
                released.append(str(demo_zone.get('zone_id')))
        if released:
            config_persistence.save_demo_zones(DEMO_ZONES)
            add_log_entry(
                "sent",
                f"[DEMO] Away exceptions released: {', '.join(released)}",
                command=f"create_override normal NOW ZONE {','.join(released)}",
                source=source,
            )
        _away_exception_zones_applied = set()
        _record_away_exceptions_applied()
        return released

    if not current_hub:
        return []

    for zone_id in sorted(zone_ids):
        if zone_id not in current_hub.zones:
            # Deleted since. Nothing to release, but stop tracking it.
            _away_exception_zones_applied.discard(zone_id)
            continue
        try:
            await hub_command(current_hub.async_create_override(
                pynobo.nobo.API.OVERRIDE_MODE_NORMAL,
                pynobo.nobo.API.OVERRIDE_TYPE_NOW,
                pynobo.nobo.API.OVERRIDE_TARGET_ZONE,
                zone_id,
            ))
            released.append(zone_id)
            add_log_entry(
                "sent",
                f"create_override(NORMAL, NOW, ZONE, zone_{zone_id}) â€” away exception released",
                command=f"create_override normal NOW ZONE {zone_id}",
                source=source,
            )
        except Exception as exc:
            # Leave it in the applied set so the next global change retries it,
            # rather than stranding the room on Eco.
            logger.error("Could not release away exception on zone %s: %s", zone_id, exc)

    _away_exception_zones_applied -= set(released)
    _record_away_exceptions_applied()
    return released


async def _apply_away_exceptions(source: str = "api") -> List[str]:
    """
    Put every configured exception zone on Eco, right after the house went Away.

    Nobø's Away is a fixed 7 °C that cannot be changed (AWAY_TEMPERATURE). A
    room that must not go that cold has exactly one warmer setting available to
    it — its own Eco temperature — so an exception zone is overridden to Eco
    while the rest of the house holds Away.

    A zone override beats the global override on the hub, and the away schedule
    loop deliberately does not re-assert Away while a window is open, so the Eco
    override stands for the whole away period.

    Returns the zone ids that were actually changed.
    """
    zone_ids = config_persistence.load_away_exceptions()
    if not zone_ids:
        return []

    with connection_lock:
        current_hub = hub

    applied: List[str] = []

    if DEMO_MODE:
        for demo_zone in DEMO_ZONES:
            if str(demo_zone.get('zone_id')) in zone_ids:
                demo_zone['mode'] = 'eco'
                DEMO_ZONE_OVERRIDES.add(str(demo_zone.get('zone_id')))
                applied.append(str(demo_zone.get('zone_id')))
        if applied:
            config_persistence.save_demo_zones(DEMO_ZONES)
            add_log_entry(
                "sent",
                f"[DEMO] Away exceptions kept on Eco: {', '.join(applied)}",
                command=f"create_override now 0 eco {','.join(applied)}",
                source=source,
            )
        _away_exception_zones_applied.update(applied)
        _record_away_exceptions_applied()
        return applied

    if not current_hub:
        return []

    for zone_id in zone_ids:
        if zone_id not in current_hub.zones:
            # A room that has since been deleted. Skip it rather than fail the
            # whole away transition.
            logger.warning("Away exception zone %s no longer exists — skipping", zone_id)
            continue
        try:
            await hub_command(current_hub.async_create_override(
                pynobo.nobo.API.OVERRIDE_MODE_ECO,
                pynobo.nobo.API.OVERRIDE_TYPE_NOW,
                pynobo.nobo.API.OVERRIDE_TARGET_ZONE,
                zone_id,
            ))
            applied.append(zone_id)
            add_log_entry(
                "sent",
                f"create_override(ECO, NOW, ZONE, zone_{zone_id}) — away exception",
                command=f"create_override eco NOW ZONE {zone_id}",
                source=source,
            )
        except Exception as exc:
            # One unreachable zone must not leave the rest of the house un-Away.
            logger.error("Could not apply away exception to zone %s: %s", zone_id, exc)

    _away_exception_zones_applied.update(applied)
    _record_away_exceptions_applied()
    return applied


async def _sync_away_exceptions(mode: str, source: str = "api") -> List[str]:
    """
    Keep the away exceptions in step with the global mode.

    Going Away puts the excluded rooms on a zone-level Eco override; every other
    global mode has to take it back off again, or that override outranks whatever
    was just asked for and the room quietly ignores it.
    """
    if mode == 'away':
        return await _apply_away_exceptions(source=source)
    await _clear_away_exceptions(source=source, fallback_mode=mode)
    return []


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

    # The scheduler changes every zone too, so the same record is needed here or
    # a planned away period would arrive as "somebody changed it".
    note_local_write("*", "mode", 'normal' if mode == 'home' else mode)

    if DEMO_MODE:
        for demo_zone in DEMO_ZONES:
            # As on the hub, a zone-level override survives a global change.
            if str(demo_zone.get('zone_id')) in DEMO_ZONE_OVERRIDES:
                continue
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
        await _sync_away_exceptions(mode, source=source)
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
        await hub_command(current_hub.async_create_override(
            pynobo.nobo.API.OVERRIDE_MODE_NORMAL,
            pynobo.nobo.API.OVERRIDE_TYPE_NOW,
            pynobo.nobo.API.OVERRIDE_TARGET_GLOBAL,
        ))
    else:
        await hub_command(current_hub.async_create_override(
            mode_map[mode],
            pynobo.nobo.API.OVERRIDE_TYPE_NOW,
            pynobo.nobo.API.OVERRIDE_TARGET_GLOBAL,
        ))
    global_mode_source = source
    config_persistence.save_server_state({"global_mode_source": global_mode_source})
    await asyncio.sleep(0.5)
    await _sync_away_exceptions(mode, source=source)


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
                    notifier.notify(
                        "away_period",
                        "The away period has ended",
                        "The planned away period is over. Every room is back on its normal\n"
                        "weekly schedule, so the place will be warming up again.",
                        severity="info",
                        key="away_period_end",
                    )
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
                kept = config_persistence.load_away_exceptions()
                notifier.notify(
                    "away_period",
                    "The away period has started",
                    "The planned away period has begun. Every room has gone to Away, which\n"
                    f"is a fixed {AWAY_TEMPERATURE:.0f}°C anti-frost setting.\n"
                    + (f"\n{len(kept)} room(s) are held on Eco instead, as configured.\n" if kept else ""),
                    severity="info",
                    key="away_period_start",
                )
            except Exception as e:
                logger.error(f"Error applying Away on schedule activation: {e}")

        elif currently_active and last_active:
            # Deliberately does nothing. This used to re-send Away every 30
            # seconds "in case of manual override", which meant a user who
            # came home early and pressed Comfort was silently forced back to
            # Away within half a minute, with no explanation and a command log
            # full of repeated Away entries. The schedule now only acts on the
            # transitions into and out of the away period, so a manual change
            # made during the holiday holds until the period ends.
            pass

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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting week profiles: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===== Schedule API Endpoints =====

# The hub's factory default week profile. It is shared by every zone out of the
# box and users expect it to keep meaning "the default", so it is never edited
# in place.
DEFAULT_WEEK_PROFILE_ID = "1"


async def wait_for_hub_state(predicate, timeout: float = 5.0, interval: float = 0.1):
    """Wait for hub state to catch up after a command.

    Hub commands are fire-and-forget: the reply arrives asynchronously on the
    receive task and updates pynobo's dictionaries. Anything that needs to read
    the result back — most importantly the id the hub assigns to a new object —
    has to wait for it to land.
    """
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(interval)


async def apply_week_profile_to_zone(current_hub, zone_id: str, entries: List[str]) -> str:
    """Write a week profile for one zone and return the profile id used.

    Week profiles are shared objects: several zones can point at the same one,
    and the factory default starts out shared by all of them. Editing in place
    would silently reschedule other rooms, so a profile is only updated when it
    belongs to this zone alone. Otherwise the zone gets its own profile.
    """
    zone = current_hub.zones[zone_id]
    zone_name = decode_hub_name(zone.get('name', f'Zone {zone_id}'))
    profile_id = zone.get('week_profile_id')

    users = [
        zid for zid, z in current_hub.zones.items()
        if z.get('week_profile_id') == profile_id
    ]
    exclusive = profile_id in current_hub.week_profiles and users == [zone_id]

    if exclusive and profile_id != DEFAULT_WEEK_PROFILE_ID:
        name = decode_hub_name(current_hub.week_profiles[profile_id].get('name', zone_name))
        await hub_command(current_hub.async_update_week_profile(profile_id, name, entries))
        # Wait for the edit to land before reporting success. Without this the
        # call returns while pynobo still holds the old entries, so a client
        # that saves and immediately re-reads gets the schedule it just
        # replaced — and believes the save failed.
        #
        # list() on both sides because pynobo's stored value is only ever read
        # through list() elsewhere; comparing a tuple against a list would never
        # match, and this would silently become a five-second sleep.
        await wait_for_hub_state(
            lambda: list(current_hub.week_profiles.get(profile_id, {}).get('profile') or [])
            == list(entries)
        )
        return profile_id

    # Give the zone a profile of its own. The hub assigns the id, so the new
    # profile has to be identified by comparing before and after.
    before = set(current_hub.week_profiles)
    new_name = _unique_week_profile_name(current_hub, f"{zone_name} schedule")
    await hub_command(current_hub.async_add_week_profile(new_name, entries))

    new_ids = await wait_for_hub_state(lambda: set(current_hub.week_profiles) - before)
    if not new_ids:
        raise HTTPException(
            status_code=502,
            detail="The hub did not confirm the new week profile. Please try again.",
        )
    new_id = sorted(new_ids)[0]

    await hub_command(current_hub.async_update_zone(zone_id, week_profile_id=new_id))
    # And wait for the zone to actually carry it. The profile existing is not
    # enough: until the zone points at it, reading the schedule back still
    # returns the factory default this zone was sharing a moment ago.
    await wait_for_hub_state(
        lambda: current_hub.zones.get(zone_id, {}).get('week_profile_id') == new_id
    )
    return new_id


def _unique_week_profile_name(current_hub, preferred: str) -> str:
    """Pick a profile name that is not already taken, within the hub's limit."""
    existing = {
        decode_hub_name(p.get('name', '')) for p in current_hub.week_profiles.values()
    }
    # pynobo enforces 100 bytes for week profile names.
    def clip(value: str) -> str:
        encoded = value.encode('utf-8')
        while len(encoded) > 100:
            value = value[:-1]
            encoded = value.encode('utf-8')
        return value

    candidate = clip(preferred)
    if candidate not in existing:
        return candidate
    for suffix in range(2, 100):
        candidate = clip(f"{preferred} {suffix}")
        if candidate not in existing:
            return candidate
    raise HTTPException(status_code=409, detail="Could not find a free week profile name")


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

        raw = week_profile.get('profile') or []
        try:
            parsed = week_profile_to_schedule(list(raw))
        except ValueError as exc:
            # Show the problem rather than an empty week: a profile the app
            # cannot read is something the user needs to know about.
            logger.warning("Could not read week profile %s: %s", week_profile_id, exc)
            raise HTTPException(
                status_code=502,
                detail=f"The hub returned a week profile this app cannot read: {exc}",
            )

        return {
            "zone_id": zone_id,
            "zone_name": decode_hub_name(zone.get('name', f'Zone {zone_id}')),
            "week_profile_id": week_profile_id,
            "week_profile_name": decode_hub_name(week_profile.get('name', '')),
            "shared_with_zones": [
                # Names, not ids: this is shown to the user as "editing this
                # will also change ...", and an id means nothing to them.
                decode_hub_name(z.get('name', f'Zone {zid}'))
                for zid, z in current_hub.zones.items()
                if z.get('week_profile_id') == week_profile_id and zid != zone_id
            ],
            "schedule": parsed,
            "week_profile": week_profile,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting zone schedule: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/zones/{zone_id}/schedule")
async def update_zone_schedule(zone_id: str, schedule: ScheduleUpdate):
    """Update the weekly schedule for a specific zone"""
    require_capability("edit_schedule")

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

        try:
            entries = schedule_to_week_profile(schedule.schedule)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        profile_id = await apply_week_profile_to_zone(current_hub, zone_id, entries)

        add_log_entry(
            "sent",
            f"Schedule for zone {zone_id} written to week profile {profile_id}",
            command=f"week_profile {profile_id} = {','.join(entries)}",
            source="api",
        )
        await broadcast_zone_update()
        return {
            "status": "success",
            "zone_id": zone_id,
            "week_profile_id": profile_id,
            "message": "Schedule updated",
        }

    except HTTPException:
        raise
    except pynobo.PynoboValidationError as e:
        # The hub rejected the schedule; that is the user's input, not a crash.
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error updating zone schedule: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===== Device Management API Endpoints =====

# Component record layout on the wire (API_Nobo.pdf):
#   <serial> <status> <name> <reverse on/off> <zone id> <active override id>
#   <temperature sensor for zone id>
COMPONENT_SERIAL = 0
COMPONENT_STATUS = 1
COMPONENT_NAME = 2
COMPONENT_REVERSE = 3
COMPONENT_ZONE_ID = 4
COMPONENT_OVERRIDE_ID = 5
COMPONENT_TEMP_SENSOR_ZONE = 6

# The specification fixes these two: status is always 0 and a component never
# carries its own override id.
COMPONENT_STATUS_VALUE = "0"
COMPONENT_OVERRIDE_NONE = "-1"

COMPONENT_NAME_MAX_BYTES = 100

UNASSIGNED_ZONE_ID = "-1"

# How long to wait for the hub's Y03 answer to a pairing request. Pairing is a
# radio operation between the hub and the device, so it is much slower than an
# ordinary command.
PAIRING_TIMEOUT = 30

# A receiver search stops by itself after 30 seconds (API_Nobo.pdf, X00).
SEARCH_DURATION = 30


def require_hub_tap() -> HubProtocolTap:
    """The protocol tap for the live connection, or a clear error."""
    with connection_lock:
        tap = hub_tap
    if tap is None:
        raise HTTPException(status_code=503, detail="Hub not connected")
    return tap


def component_row_for(serial: str) -> List[str]:
    """The component exactly as the hub last reported it.

    Deliberately not read from pynobo's dictionary — see :class:`HubProtocolTap`.
    """
    row = require_hub_tap().component_row(serial)
    if row is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return row


def validate_component_name(name: str) -> str:
    """Check a device name against the hub's limit and encode it for the wire."""
    encoded = encode_hub_name(name)
    size = len(encoded.encode('utf-8'))
    if size > COMPONENT_NAME_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Name is too long for the hub ({size} bytes, maximum "
                   f"{COMPONENT_NAME_MAX_BYTES})",
        )
    return encoded


async def send_component_update(current_hub, row: List[str]) -> None:
    """Send U01 for a component row, normalising the fixed fields."""
    row = list(row)
    row[COMPONENT_STATUS] = COMPONENT_STATUS_VALUE
    row[COMPONENT_OVERRIDE_ID] = COMPONENT_OVERRIDE_NONE
    await hub_command(current_hub.async_send_command(["U01"] + row))


async def wait_for_component(serial: str, matches, timeout: float = 5.0):
    """Wait until the hub confirms a component change, or time out."""
    tap = require_hub_tap()
    return await wait_for_hub_state(
        lambda: matches(tap.component_row(serial)), timeout=timeout
    )


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
            names = zone.get('components_names') or []
            for i, serial in enumerate(zone['components']):
                device_name, supports_comfort, supports_eco = detect_device_type(serial)
                custom_name = names[i].strip() if i < len(names) and names[i] else ''
                devices.append({
                    'serial': serial,
                    'serial_display': zone['components_display'][i] if i < len(zone['components_display']) else format_serial_display(serial),
                    # The friendly name the user gave the device. Renaming used to
                    # appear to work and then vanish on reload, because the list
                    # this endpoint returns never carried the name back.
                    'name': custom_name,
                    'display_name': custom_name or device_name,
                    'device_type': device_name,
                    'zone_id': zone['zone_id'],
                    'zone_name': zone['name'],
                    'supports_comfort': supports_comfort,
                    'supports_eco': supports_eco,
                    'supports_temp_adjust': supports_comfort or supports_eco,
                    'current_mode': zone.get('current_mode', 'normal'),
                })
        
        return {"devices": devices}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting devices: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===== Device discovery (receiver search) =====
#
# Adding a device needs its 12-digit serial number, which is printed on the
# device and is easy to get wrong. A receiver search asks the hub to listen for
# devices in pairing mode nearby and report what it hears, so the user can pick
# from a list instead of typing.


@app.post("/api/devices/search")
async def start_device_search():
    """Ask the hub to listen for nearby devices in pairing mode."""
    require_capability("discover_devices")

    with connection_lock:
        connected = hub_connected
        current_hub = hub

    if not connected or not current_hub:
        raise HTTPException(status_code=503, detail="Hub not connected")

    tap = require_hub_tap()
    await hub_command(current_hub.async_send_command(["X00"]))
    started = await wait_for_hub_state(lambda: tap.search_active)
    if not started:
        raise HTTPException(
            status_code=502, detail="The hub did not start a device search."
        )

    add_log_entry("sent", "Started searching for devices", command="X00", source="api")
    return {
        "status": "searching",
        "seconds": SEARCH_DURATION,
        "message": "Put each device into pairing mode now.",
    }


@app.get("/api/devices/search")
async def get_device_search():
    """What the hub has heard so far."""
    require_capability("discover_devices")

    with connection_lock:
        connected = hub_connected
        current_hub = hub

    if not connected or not current_hub:
        raise HTTPException(status_code=503, detail="Hub not connected")

    tap = require_hub_tap()
    found = []
    for serial in tap.discovered_serials():
        device_type, _, _ = detect_device_type(serial)
        found.append({
            "serial": serial,
            "serial_display": format_serial_display(serial),
            "device_type": device_type,
            # A device the hub already knows will fail to pair again; say so
            # rather than let the user try and get a confusing error.
            "already_registered": tap.component_row(serial) is not None,
        })

    return {"searching": tap.search_active, "devices": found}


@app.delete("/api/devices/search")
async def stop_device_search():
    """Stop an in-progress search."""
    require_capability("discover_devices")

    with connection_lock:
        connected = hub_connected
        current_hub = hub

    if not connected or not current_hub:
        raise HTTPException(status_code=503, detail="Hub not connected")

    await hub_command(current_hub.async_send_command(["X01"]))
    add_log_entry("sent", "Stopped searching for devices", command="X01", source="api")
    return {"status": "stopped"}


class DeviceAdd(BaseModel):
    serial: str
    zone_id: str
    name: Optional[str] = None


@app.post("/api/devices")
async def add_device(device: DeviceAdd):
    """Add a new device to a zone"""
    require_capability("add_device")

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

        if device.zone_id not in current_hub.zones:
            raise HTTPException(status_code=404, detail="Zone not found")

        tap = require_hub_tap()
        existing = tap.component_row(serial)
        if existing is not None:
            zone_name = decode_hub_name(
                current_hub.zones.get(existing[COMPONENT_ZONE_ID], {}).get('name', '')
            )
            where = f" in zone '{zone_name}'" if zone_name else ""
            raise HTTPException(
                status_code=400,
                detail=f"Device with serial {format_serial_display(serial)} "
                       f"is already registered{where}",
            )

        # A device has to be paired with the hub over radio before it can be
        # configured. Pairing only succeeds while the device is in pairing mode,
        # so a failure here is usually the device, not the app.
        await hub_command(current_hub.async_send_command(["X03", serial]))
        paired = await wait_for_hub_state(
            lambda: tap.take_pair_result(serial), timeout=PAIRING_TIMEOUT
        )
        if paired is None:
            raise HTTPException(
                status_code=504,
                detail="The hub did not answer the pairing request. Put the device "
                       "into pairing mode and try again.",
            )
        if paired is False:
            raise HTTPException(
                status_code=502,
                detail="The hub could not pair with this device. Check the serial "
                       "number and that the device is in pairing mode and in range.",
            )

        row = await wait_for_hub_state(lambda: tap.component_row(serial))
        if row is None:
            raise HTTPException(
                status_code=502,
                detail="The device paired but the hub has not reported it yet. "
                       "Reload the page in a moment.",
            )

        row[COMPONENT_ZONE_ID] = device.zone_id
        if device.name:
            row[COMPONENT_NAME] = validate_component_name(device.name.strip())
        await send_component_update(current_hub, row)
        await wait_for_component(
            serial, lambda r: r is not None and r[COMPONENT_ZONE_ID] == device.zone_id
        )

        add_log_entry(
            "sent",
            f"Device {format_serial_display(serial)} paired and added to zone {device.zone_id}",
            command=f"X03 {serial}; U01 {serial} zone_id={device.zone_id}",
            source="api",
        )
        await broadcast_zone_update()
        return {
            "status": "success",
            "serial": serial,
            "serial_display": format_serial_display(serial),
            "device_type": device_name,
            "zone_id": device.zone_id,
            "name": device.name or '',
        }

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
    require_capability("rename_device")

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

        row = component_row_for(clean_serial)
        encoded = validate_component_name(new_name)
        old_name = decode_hub_name(row[COMPONENT_NAME])
        row[COMPONENT_NAME] = encoded

        await send_component_update(current_hub, row)
        confirmed = await wait_for_component(
            clean_serial, lambda r: r is not None and r[COMPONENT_NAME] == encoded
        )
        if not confirmed:
            raise HTTPException(
                status_code=502,
                detail="The hub did not confirm the new name. Please try again.",
            )

        add_log_entry(
            "sent",
            f"Device {format_serial_display(clean_serial)} renamed "
            f"from '{old_name}' to '{new_name}'",
            command=f"U01 {clean_serial} name={new_name}",
            source="api",
        )
        await broadcast_zone_update()
        return {"status": "success", "serial": clean_serial, "name": new_name}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error renaming device: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/devices/{serial}")
async def replace_device(serial: str, replacement: DeviceReplace):
    """Replace a device with a new one"""
    require_capability("replace_device")

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

        tap = require_hub_tap()
        old_row = component_row_for(old_serial)
        if tap.component_row(new_serial) is not None:
            raise HTTPException(
                status_code=400,
                detail=f"Device with serial {format_serial_display(new_serial)} "
                       f"is already registered",
            )

        # A component's serial number identifies it and cannot be changed, so a
        # replacement is genuinely a removal followed by a new pairing. The old
        # device is only unpaired once the new one has answered, so a failed
        # pairing leaves the zone as it was rather than empty.
        await hub_command(current_hub.async_send_command(["X03", new_serial]))
        paired = await wait_for_hub_state(
            lambda: tap.take_pair_result(new_serial), timeout=PAIRING_TIMEOUT
        )
        if paired is None:
            raise HTTPException(
                status_code=504,
                detail="The hub did not answer the pairing request. The old device "
                       "has been left in place. Put the new device into pairing "
                       "mode and try again.",
            )
        if paired is False:
            raise HTTPException(
                status_code=502,
                detail="The hub could not pair with the new device. The old device "
                       "has been left in place.",
            )

        new_row = await wait_for_hub_state(lambda: tap.component_row(new_serial))
        if new_row is None:
            raise HTTPException(
                status_code=502,
                detail="The new device paired but the hub has not reported it yet. "
                       "The old device has been left in place.",
            )

        # Carry the old device's placement and name over to its replacement.
        new_row[COMPONENT_ZONE_ID] = old_row[COMPONENT_ZONE_ID]
        new_row[COMPONENT_NAME] = old_row[COMPONENT_NAME]
        new_row[COMPONENT_TEMP_SENSOR_ZONE] = old_row[COMPONENT_TEMP_SENSOR_ZONE]
        await send_component_update(current_hub, new_row)

        await hub_command(current_hub.async_send_command(["R01"] + old_row))
        await wait_for_hub_state(lambda: tap.component_row(old_serial) is None)

        add_log_entry(
            "sent",
            f"Device {format_serial_display(old_serial)} replaced by "
            f"{format_serial_display(new_serial)}",
            command=f"X03 {new_serial}; R01 {old_serial}",
            source="api",
        )
        await broadcast_zone_update()
        return {
            "status": "success",
            "old_serial": old_serial,
            "new_serial": new_serial,
            "serial_display": format_serial_display(new_serial),
            "device_type": device_name,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error replacing device: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/devices/{serial}")
async def remove_device(serial: str):
    """Remove a device from its zone"""
    require_capability("remove_device")

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

        row = component_row_for(serial_clean)
        await hub_command(current_hub.async_send_command(["R01"] + row))
        gone = await wait_for_hub_state(
            lambda: require_hub_tap().component_row(serial_clean) is None
        )
        if not gone:
            raise HTTPException(
                status_code=502,
                detail="The hub did not confirm the removal. Please try again.",
            )

        add_log_entry(
            "sent",
            f"Device {format_serial_display(serial_clean)} removed from the hub",
            command=f"R01 {serial_clean}",
            source="api",
        )
        await broadcast_zone_update()
        return {"status": "success", "serial": serial_clean}

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
    require_capability("move_device")

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
        if not current_hub:
            raise HTTPException(status_code=503, detail="Hub not connected")

        row = component_row_for(serial_clean)
        if move.new_zone_id not in current_hub.zones:
            raise HTTPException(status_code=404, detail="Target zone not found")

        old_zone_id = row[COMPONENT_ZONE_ID]
        if old_zone_id == move.new_zone_id:
            raise HTTPException(status_code=400, detail="Device is already in the target zone")

        row[COMPONENT_ZONE_ID] = move.new_zone_id
        # A device that reports temperature does so for the zone it lives in, so
        # the sensor assignment has to follow it. Left behind, the old zone would
        # keep reading a thermometer that is now in another room.
        if row[COMPONENT_TEMP_SENSOR_ZONE] == old_zone_id:
            row[COMPONENT_TEMP_SENSOR_ZONE] = move.new_zone_id

        await send_component_update(current_hub, row)
        confirmed = await wait_for_component(
            serial_clean,
            lambda r: r is not None and r[COMPONENT_ZONE_ID] == move.new_zone_id,
        )
        if not confirmed:
            raise HTTPException(
                status_code=502,
                detail="The hub did not confirm the move. Please try again.",
            )

        old_zone_name = decode_hub_name(
            current_hub.zones.get(old_zone_id, {}).get('name', old_zone_id)
        )
        new_zone_name = decode_hub_name(
            current_hub.zones[move.new_zone_id].get('name', move.new_zone_id)
        )
        add_log_entry(
            "sent",
            f"Device {format_serial_display(serial_clean)} moved from "
            f"'{old_zone_name}' to '{new_zone_name}'",
            command=f"U01 {serial_clean} zone_id={move.new_zone_id}",
            source="api",
        )
        await broadcast_zone_update()
        return {
            "status": "success",
            "serial": serial_clean,
            "old_zone_id": old_zone_id,
            "old_zone_name": old_zone_name,
            "new_zone_id": move.new_zone_id,
            "new_zone_name": new_zone_name,
        }

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
_LOGIN_CLASSIC_HTML = """<!DOCTYPE html>
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
  .site { text-align: center; margin: -1rem 0 1.5rem; font-size: .9rem; color: var(--muted); }
</style>
</head>
<body>
<div class="card">
  <h1>🔒 Nobø Control</h1>
  <p class="site"><!--SITE_TAGLINE--></p>
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


# The Cabin sign-in page. It carries the same palette, radii and type as the
# interface behind it, so the first screen of the app is not a different product
# from the second. Self-contained on purpose: it is the one page that must
# render before anything else is known to work, so it pulls in no stylesheet and
# no script it does not own.
_LOGIN_CABIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="theme-color" content="#2F5D50" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#141615" media="(prefers-color-scheme: dark)">
<title>Nobø Control — Sign in</title>
<link rel="icon" type="image/svg+xml" href="/static/ui/cabin/icon.svg">
<link rel="apple-touch-icon" href="/static/ui/cabin/icon-180.png">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --paper: #F6F2EA; --card: #FFFDF9;
    --ink: #211F1C; --ink-soft: #6B655C; --ink-faint: #948C80;
    --rule: #E3DCCF;
    --pine: #2F5D50; --pine-deep: #22453C; --pine-wash: #E7EFEB;
    --danger: #C9453C; --danger-wash: #F8E7E5;
    --radius: 18px; --radius-sm: 12px;
    --shadow: 0 1px 2px rgba(33,31,28,.05), 0 8px 24px rgba(33,31,28,.06);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --paper: #141615; --card: #1D211F;
      --ink: #F0EDE7; --ink-soft: #A8A296; --ink-faint: #7C766B;
      --rule: #2E3432;
      --pine: #6FBBA3; --pine-deep: #8FD3BC; --pine-wash: #1B2A26;
      --danger: #E4695F; --danger-wash: #2B1B19;
      --shadow: none;
    }
  }
  html { -webkit-text-size-adjust: 100%; }
  body {
    background: var(--paper); color: var(--ink);
    font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    min-height: 100svh; display: flex; align-items: center; justify-content: center;
    padding: calc(1.5rem + env(safe-area-inset-top)) 1.25rem calc(1.5rem + env(safe-area-inset-bottom));
    line-height: 1.5;
  }
  .wrap { width: 100%; max-width: 25rem; }

  /* The same strong left rule the app uses on its hero, so the two screens
     read as one product. */
  .brand { border-left: 4px solid var(--pine); padding-left: .9rem; margin-bottom: 1.5rem; }
  .brand h1 { font-size: 1.6rem; font-weight: 650; letter-spacing: -.015em; }
  .brand p { color: var(--ink-soft); font-size: .93rem; margin-top: .15rem; }

  .card {
    background: var(--card); border: 1px solid var(--rule);
    border-radius: var(--radius); box-shadow: var(--shadow); padding: 1.5rem;
  }
  .field { display: block; margin-bottom: 1rem; }
  .field > span {
    display: block; margin-bottom: .4rem;
    font-size: .74rem; font-weight: 700; letter-spacing: .13em;
    text-transform: uppercase; color: var(--ink-faint);
  }
  input {
    width: 100%; padding: .7rem .85rem; min-height: 44px;
    background: var(--paper); color: var(--ink);
    border: 1px solid var(--rule); border-radius: var(--radius-sm);
    font: inherit; font-size: 1rem;
  }
  input:focus-visible { outline: 2px solid var(--pine); outline-offset: 1px; border-color: var(--pine); }
  button {
    width: 100%; margin-top: .35rem; padding: .8rem; min-height: 48px;
    background: var(--pine); color: var(--card);
    font: inherit; font-size: 1rem; font-weight: 650;
    border: 1px solid var(--pine); border-radius: var(--radius-sm); cursor: pointer;
  }
  @media (prefers-color-scheme: dark) { button { color: #0F1614; } }
  button:hover { background: var(--pine-deep); border-color: var(--pine-deep); }
  button:disabled { opacity: .6; cursor: default; }

  .error {
    background: var(--danger-wash); border: 1px solid var(--danger); color: var(--danger);
    border-radius: var(--radius-sm); padding: .65rem .8rem;
    margin-bottom: 1rem; font-size: .88rem; font-weight: 550; display: none;
  }
  .error.show { display: block; }
  .foot { margin-top: 1.1rem; font-size: .82rem; color: var(--ink-faint); text-align: center; }
  .foot a { color: var(--ink-soft); }
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">
    <h1>Nobø Control</h1>
    <p>Sign in to set the heating.</p>
  </div>
  <div class="card">
    <div class="error" id="errorMsg" role="alert"></div>
    <form id="loginForm">
      <label class="field">
        <span>Username</span>
        <input type="text" id="username" name="username" autocomplete="username"
               autocapitalize="none" spellcheck="false" required autofocus>
      </label>
      <label class="field">
        <span>Password</span>
        <input type="password" id="password" name="password" autocomplete="current-password" required>
      </label>
      <button type="submit" id="submitBtn">Sign in</button>
    </form>
  </div>
  <p class="foot"><!--SITE_TAGLINE--></p>
</div>
<script>
const form = document.getElementById('loginForm');
const btn = document.getElementById('submitBtn');
const err = document.getElementById('errorMsg');
form.addEventListener('submit', async e => {
  e.preventDefault();
  err.classList.remove('show');
  btn.disabled = true;
  btn.textContent = 'Signing in…';
  const body = new URLSearchParams({
    username: document.getElementById('username').value,
    password: document.getElementById('password').value,
  });
  try {
    const r = await fetch('/auth/login', { method: 'POST', body,
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' } });
    if (r.ok) {
      window.location.href = '/';
      return;
    }
    const data = await r.json().catch(() => ({}));
    err.textContent = data.detail || 'That username and password did not match.';
    err.classList.add('show');
  } catch {
    err.textContent = 'Could not reach the server. Check the connection and try again.';
    err.classList.add('show');
  }
  btn.disabled = false;
  btn.textContent = 'Sign in';
  document.getElementById('password').select();
});
</script>
</body>
</html>"""

_LOGIN_PAGES = {"cabin": _LOGIN_CABIN_HTML, "classic": _LOGIN_CLASSIC_HTML}


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
    """Serve the sign-in page matching the active interface.

    The name is substituted server-side rather than fetched by script. The
    sign-in page is the first thing anyone sees, and a page that renders "the
    cabin" and then blinks to "Lakeside" looks broken. It also keeps the site
    settings off the list of things readable without a session.
    """
    site = site_settings()

    if site["show_on_login"] and site["is_named"]:
        tagline = f"Heating control for {html.escape(site['name'])}."
    elif ACTIVE_UI == "cabin":
        # The wording Cabin has always shipped with. Left alone when unnamed so
        # an installation that never sets a name looks exactly as it did.
        tagline = f"Heating control for {SITE_INLINE_FALLBACK}."
    else:
        # Classic never had a tagline. Inventing one for it, and a cabin-flavoured
        # one at that, would change a page the user chose specifically to go back to.
        tagline = ""

    page = _LOGIN_PAGES[ACTIVE_UI].replace("<!--SITE_TAGLINE-->", tagline)

    return HTMLResponse(
        content=page,
        headers={"Cache-Control": "no-cache"},
    )


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


class RevalidatingStaticFiles(StaticFiles):
    """Serve static files with `Cache-Control: no-cache`.

    Without an explicit Cache-Control header a browser is free to apply
    heuristic caching, and typically will: it holds the file for a fraction of
    its age with no revalidation. That produced a genuinely confusing failure
    after a deploy - the new index.html was fetched, so a new button appeared,
    while the JavaScript that gave the button its behaviour came from cache, so
    clicking it did nothing at all.

    `no-cache` does not mean "do not store"; it means "revalidate before use".
    StaticFiles already sends an ETag and Last-Modified, so the revalidation is
    a conditional request that almost always comes back 304 with no body. The
    cost is one small round trip per asset; the benefit is that what the user
    is running is always what was deployed.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


app.mount("/static", RevalidatingStaticFiles(directory=STATIC_DIR), name="static")


# ===== Which interface to serve =====
#
# Two interfaces ship in every image: "cabin", the current one, and "classic",
# the original. Which one answers "/" is decided at startup by NOBO_UI.
#
# This is deliberately a runtime switch rather than a branch or a separate
# build. Rolling back a UI by redeploying an older revision would also roll back
# every backend fix made since, and the revision you fall back to is the one
# nobody has run for months. Here a rollback is one line in .env and a restart:
# no rebuild, no network, and the server keeps every fix it has.
#
# Both interfaces are always reachable at /cabin and /classic whatever the
# setting, so they can be compared without changing any configuration.

UI_CHOICES = {
    "cabin":   STATIC_DIR / "ui" / "cabin" / "index.html",
    "classic": STATIC_DIR / "index.html",
}
DEFAULT_UI = "cabin"

_requested_ui = os.environ.get("NOBO_UI", DEFAULT_UI).strip().lower()
if _requested_ui not in UI_CHOICES:
    logger.warning(
        "NOBO_UI=%r is not one of %s; falling back to %r. An unreachable "
        "interface is worse than the wrong one, so this does not stop startup.",
        _requested_ui, ", ".join(sorted(UI_CHOICES)), DEFAULT_UI,
    )
    _requested_ui = DEFAULT_UI
ACTIVE_UI = _requested_ui
logger.info("Serving the %r interface at /. Both are always at /cabin and /classic.", ACTIVE_UI)


def _serve_ui(name: str) -> HTMLResponse:
    path = UI_CHOICES[name]
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("Interface %r is missing its entry point at %s", name, path)
        return HTMLResponse(
            content=f"<h1>The {name} interface is not installed</h1>",
            status_code=404,
        )
    # Served inline rather than as a static file so the choice of interface is
    # made per request and never cached by an intermediary.
    return HTMLResponse(content=content, headers={"Cache-Control": "no-cache"})


@app.get("/")
async def read_root():
    """Serve whichever interface NOBO_UI selects."""
    return _serve_ui(ACTIVE_UI)


@app.get("/cabin")
async def read_cabin():
    """The Cabin interface, reachable whatever NOBO_UI says."""
    return _serve_ui("cabin")


@app.get("/classic")
async def read_classic():
    """The original interface, reachable whatever NOBO_UI says."""
    return _serve_ui("classic")


# ===== Main Entry Point =====
if __name__ == "__main__":
    import uvicorn

    bind = os.getenv("NOBO_BIND", "0.0.0.0")
    port = int(os.getenv("NOBO_PORT", "8000"))

    logger.info("Starting Nobø Web Control Server...")
    logger.info(f"Hub Serial: {NOBO_SERIAL}")
    logger.info(f"Hub IP: {NOBO_IP}")
    if bind == "127.0.0.1":
        # Reachable only through a reverse proxy on this machine. Saying so
        # avoids a confusing "the site is down" when it is in fact deliberate.
        logger.info(f"Listening on 127.0.0.1:{port} — local connections only")
    else:
        logger.info(f"Access the web interface at http://localhost:{port}")
    uvicorn.run(app, host=bind, port=port, log_level="info")
