"""
display_render.py — the wall display's picture, in the e-paper panel's six colours.

The M5Stack PaperColor has a Spectra 6 panel: 400×600 pixels that can each be
black, white, yellow, red, blue or green, and nothing in between. The device
draws whatever PNG it is given and maps every pixel to the nearest of those
six (M5GFX's ``epd_fastest`` mode, which does not dither). So this module
draws with exactly the six colours and with anti-aliasing off: what is tested
here is what appears on the wall.

Two rules shape the layout.

*The panel is redrawn only when a sensor changes.* A Spectra 6 refresh takes
the best part of twenty seconds and flashes, so the ETag is not a hash of the
picture but of ``sensor_fingerprint`` — the doors, windows and warnings the
display exists to show. A new outdoor temperature or a hub reconnecting is
drawn at the next sensor change, not on its own. Everything on the picture is
therefore written to stay true while it waits: times are absolute and carry
their weekday ("Mon 09:47", never "12 min ago" or a bare "09:47" that is wrong
the next morning), and the outdoor reading says when it was read. Saying that
the picture is current is the device's job: when it cannot reach the Pi it
replaces the picture with one saying so, because an e-paper panel keeps its
last image for ever, even with a flat battery, and an old "All closed" must
never pass for a current one.

*Doors and windows first.* The header is what can be read from across the
room: red when something has been left open (or the alarm is on with
something open), yellow when something is open, blue when a sensor cannot be
heard from, green when every one is closed.
"""

import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

WIDTH = 400
HEIGHT = 600

# The ideal colours M5GFX maps to (Panel_ED2208.inl, epd_palette). Drawing in
# exactly these means the device's nearest-colour lookup is an identity.
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
YELLOW = (255, 243, 56)
RED = (191, 0, 0)
BLUE = (100, 64, 255)
GREEN = (67, 138, 28)
PALETTE = (BLACK, WHITE, YELLOW, RED, BLUE, GREEN)

TONES = {
    "green": (GREEN, WHITE),
    "yellow": (YELLOW, BLACK),
    "red": (RED, WHITE),
    "blue": (BLUE, WHITE),
}

OUTLOOK_WORDS = {
    "storm": "Storm possible",
    "falling_fast": "Rain and wind likely",
    "falling": "Weather turning",
    "steady": "No big change",
    "rising": "Improving",
    "rising_fast": "Clearing, gusty",
}

ALARM_TITLES = {
    "armed_away": "Alarm on",
    "armed_home": "Alarm on at home",
    "locked_outside": "Locked up",
    "locked_inside": "Locked in",
}

LOW_DISPLAY_BATTERY = 20

FONT_DIRS = (
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/dejavu"),
    Path("/opt/homebrew/share/fonts/dejavu"),
)


@dataclass
class Row:
    colour: str
    title: str
    detail: str
    tag: str = ""


@dataclass
class Frame:
    tone: str
    site: str
    headline: str
    subline: str
    rows: List[Row] = field(default_factory=list)
    calm: Optional[str] = None
    footer: str = ""
    notes: List[Tuple[str, str]] = field(default_factory=list)


def _since(value: Optional[str], now: datetime) -> str:
    if not value:
        return ""
    try:
        when = datetime.fromisoformat(value).astimezone()
    except ValueError:
        return ""
    # The weekday even for today: the picture may stay up past midnight.
    if (now.date() - when.date()).days < 7:
        return when.strftime("%a %H:%M")
    return f"{when.day} {when.strftime('%b %H:%M')}"


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def build_frame(
    payload: Dict[str, Any], *, display_battery: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Frame:
    """What the picture says, decided apart from how it is drawn."""
    now = now or datetime.now().astimezone()
    site = str(payload.get("site") or "")
    notes: List[Tuple[str, str]] = []
    # No percentage: the note is redrawn when it starts, not as it falls.
    if _battery_low(display_battery):
        notes.append(("red", "Display battery low, charge it"))
    footer = _footer(payload, now)

    if not payload.get("sensors_enabled"):
        return Frame(
            tone="blue", site=site, headline="No sensors",
            subline="Door and window sensors are turned off",
            calm="Turn them on under Settings → Sensors.",
            footer=footer, notes=notes,
        )

    contacts = sorted(
        payload.get("open_contacts") or [],
        key=lambda item: (not item.get("left_open"), item.get("since") or ""),
    )
    left = [item for item in contacts if item.get("left_open")]
    unavailable = payload.get("unavailable_sensors") or []
    alarm = payload.get("alarm") or {}
    alarm_open = bool(alarm.get("left_open"))

    rows: List[Row] = []
    if alarm_open:
        rows.append(Row(
            "red", ALARM_TITLES.get(alarm.get("reason") or "", "Alarm on"),
            "With a door or window open",
        ))
    for item in contacts:
        word = "left open" if item.get("left_open") else "open"
        rows.append(Row(
            "red" if item.get("left_open") else "yellow",
            str(item.get("zone") or ""),
            f"{item.get('sensor')} · {word}",
            _since(item.get("since"), now),
        ))
    for item in unavailable:
        rows.append(Row("blue", str(item.get("zone") or ""), f"{item.get('sensor')} · no signal"))
    frost = [room["name"] for room in payload.get("rooms") or [] if "frost" in (room.get("warnings") or [])]
    for name in frost:
        rows.append(Row("red", name, "Near freezing"))

    count = int(payload.get("contact_count") or 0)
    calm = None
    if left or alarm_open:
        tone = "red"
        headline = "Left open" if left or not contacts else "Open"
        subline = _plural(len(contacts), "door or window open", "doors and windows open")
    elif contacts:
        tone = "yellow"
        headline = f"{len(contacts)} open"
        subline = _plural(len(contacts), "door or window open", "doors and windows open")
    elif unavailable:
        tone = "blue"
        headline = "Check sensors"
        subline = _plural(len(unavailable), "sensor not heard from", "sensors not heard from")
    elif count == 0:
        tone = "blue"
        headline = "No sensors"
        subline = "No door or window sensor is assigned to a room"
        calm = "Add them under Settings → Sensors."
    else:
        tone = "green"
        headline = "All closed"
        subline = _plural(count, "door or window", "doors and windows")
        calm = "Every door and window is closed."
    return Frame(
        tone=tone, site=site, headline=headline, subline=subline,
        rows=rows, calm=calm if not rows else None, footer=footer, notes=notes,
    )


# The colours the display's own lights may blink, when it is on USB power.
LIGHTS = ("green", "red", "blue", "off")


def status_light(payload: Dict[str, Any]) -> str:
    """What the display's lights say, in one word: the headline, from afar.

    Red when anything is open, whether or not it has been left open yet;
    green only when every door and window is closed *and* heard from, because
    a green light is what lets somebody walk out without looking; blue, as on
    the panel, when a sensor cannot be heard from; off when there is nothing
    to watch. Built from the same fields as ``sensor_fingerprint``, so a
    change of light is always a change of ETag and a waiting display hears it
    at once.
    """
    if not payload.get("sensors_enabled"):
        return "off"
    alarm = payload.get("alarm") or {}
    if payload.get("open_contacts") or alarm.get("left_open"):
        return "red"
    if payload.get("unavailable_sensors"):
        return "blue"
    if not int(payload.get("contact_count") or 0):
        return "off"
    return "green"


def _battery_low(display_battery: Optional[int]) -> bool:
    return display_battery is not None and display_battery <= LOW_DISPLAY_BATTERY


def sensor_fingerprint(payload: Dict[str, Any], *, display_battery: Optional[int] = None) -> str:
    """A hash of what the display is for: doors, windows and their warnings.

    It is the picture's ETag, so the panel is redrawn when — and only when —
    this changes. Weather, room temperatures and the hub's connection are
    deliberately left out. The display's own low battery is in, as a flag.
    """
    alarm = payload.get("alarm") or {}
    state = {
        "enabled": bool(payload.get("sensors_enabled")),
        "count": int(payload.get("contact_count") or 0),
        "open": sorted(
            [str(item.get("zone")), str(item.get("sensor")), str(item.get("kind")),
             str(item.get("since")), bool(item.get("left_open"))]
            for item in payload.get("open_contacts") or []
        ),
        "unavailable": sorted(
            [str(item.get("zone")), str(item.get("sensor"))]
            for item in payload.get("unavailable_sensors") or []
        ),
        "alarm": [bool(alarm.get("left_open")), alarm.get("reason")],
        "frost": sorted(
            str(room.get("name")) for room in payload.get("rooms") or []
            if "frost" in (room.get("warnings") or [])
        ),
        "battery_low": _battery_low(display_battery),
    }
    text = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _footer(payload: Dict[str, Any], now: datetime) -> str:
    parts: List[str] = []
    outdoor = payload.get("outdoor") or {}
    if outdoor.get("temperature") is not None:
        # Stamped, because it is redrawn only with a sensor change.
        parts.append(f"Outside {round(float(outdoor['temperature']))}° at {now.strftime('%H:%M')}")
    outlook = payload.get("weather_outlook") or {}
    words = OUTLOOK_WORDS.get(outlook.get("tendency") or "")
    if words:
        parts.append(words)
    return "  ·  ".join(parts)


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

_font_cache: Dict[Tuple[bool, int], Any] = {}


def _font(size: int, bold: bool = False):
    key = (bold, size)
    if key in _font_cache:
        return _font_cache[key]
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    font = None
    for folder in FONT_DIRS:
        path = folder / name
        if path.exists():
            font = ImageFont.truetype(str(path), size)
            break
    if font is None:
        # Pillow's own font. Fine for tests on a machine without DejaVu; the
        # image installs fonts-dejavu-core, which is what the wall shows.
        font = ImageFont.load_default(size)
    _font_cache[key] = font
    return font


def _fit(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> List[str]:
    lines: List[str] = []
    line = ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and draw.textlength(trial, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    return lines


ROW_TOP = 168
ROW_HEIGHT = 64
FOOTER_TOP = 520
MARGIN = 18


def draw_frame(frame: Frame) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(image)
    # Anti-aliasing would put greys at every edge, which the panel cannot
    # show and the device would round to whichever colour is nearest.
    draw.fontmode = "1"
    inner = WIDTH - 2 * MARGIN

    band, ink = TONES[frame.tone]
    draw.rectangle((0, 0, WIDTH, 148), fill=band)
    draw.text((MARGIN, 14), _fit(draw, frame.site, _font(20), inner), font=_font(20), fill=ink)
    size = 54
    while size > 34 and draw.textlength(frame.headline, font=_font(size, True)) > inner:
        size -= 2
    headline_font = _font(size, bold=True)
    draw.text(
        (MARGIN, 42 + (54 - size) // 2),
        _fit(draw, frame.headline, headline_font, inner), font=headline_font, fill=ink,
    )
    draw.text((MARGIN, 112), _fit(draw, frame.subline, _font(20), inner), font=_font(20), fill=ink)

    notes_height = 30 * len(frame.notes)
    bottom = FOOTER_TOP - notes_height
    if frame.rows:
        room = max(1, (bottom - ROW_TOP) // ROW_HEIGHT)
        shown = frame.rows if len(frame.rows) <= room else frame.rows[: room - 1]
        y = ROW_TOP
        for row in shown:
            colour = {"red": RED, "yellow": YELLOW, "blue": BLUE}.get(row.colour, BLACK)
            draw.rectangle((MARGIN, y + 2, MARGIN + 12, y + ROW_HEIGHT - 10), fill=colour)
            text_x = MARGIN + 26
            width = WIDTH - MARGIN - text_x
            title_width = width
            if row.tag:
                tag_width = draw.textlength(row.tag, font=_font(20, True))
                draw.text(
                    (WIDTH - MARGIN - tag_width, y + 5), row.tag, font=_font(20, True),
                    fill=RED if row.colour == "red" else BLACK,
                )
                title_width = int(width - tag_width - 12)
            draw.text(
                (text_x, y), _fit(draw, row.title, _font(26, True), title_width),
                font=_font(26, True), fill=BLACK,
            )
            detail_ink = RED if row.colour == "red" else BLACK
            draw.text((text_x, y + 32), _fit(draw, row.detail, _font(19), width), font=_font(19), fill=detail_ink)
            y += ROW_HEIGHT
        hidden = len(frame.rows) - len(shown)
        if hidden:
            draw.text((MARGIN + 26, y + 6), f"+ {hidden} more", font=_font(22, True), fill=BLACK)
    elif frame.calm:
        font = _font(30, bold=True)
        y = ROW_TOP + 40
        for line in _wrap(draw, frame.calm, font, inner):
            draw.text((MARGIN, y), line, font=font, fill=BLACK)
            y += 40

    y = bottom
    for colour, text in frame.notes:
        ink_colour = RED if colour == "red" else BLUE
        draw.text((MARGIN, y), _fit(draw, text, _font(20, True), inner), font=_font(20, True), fill=ink_colour)
        y += 30

    draw.rectangle((MARGIN, FOOTER_TOP + 8, WIDTH - MARGIN, FOOTER_TOP + 10), fill=BLACK)
    if frame.footer:
        draw.text((MARGIN, FOOTER_TOP + 26), _fit(draw, frame.footer, _font(22), inner), font=_font(22), fill=BLACK)
    return image


def _palette_image() -> Image.Image:
    flat: List[int] = []
    for colour in PALETTE:
        flat.extend(colour)
    flat.extend([0] * (768 - len(flat)))
    holder = Image.new("P", (1, 1))
    holder.putpalette(flat)
    return holder


def render_png(
    payload: Dict[str, Any], *, display_battery: Optional[int] = None,
    now: Optional[datetime] = None,
) -> bytes:
    """The picture as an indexed PNG: a few kilobytes, six colours, nothing else."""
    image = draw_frame(build_frame(payload, display_battery=display_battery, now=now))
    indexed = image.quantize(palette=_palette_image(), dither=Image.Dither.NONE)
    out = io.BytesIO()
    indexed.save(out, format="PNG", optimize=True)
    return out.getvalue()
