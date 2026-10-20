# Wall display

An M5Stack PaperColor on the wall by the door shows which doors and windows
are open, so the house can be checked before leaving. The panel is a
4-inch, 400 × 600 Spectra 6 e-paper: six inks (black, white, yellow, red,
blue, green), no backlight, and a picture that stays without power.

It needs the door and window sensors switched on (Settings → Sensors). Nothing
on it can change the heating, and nothing on the Pi depends on it.

## The Pi draws the picture

`GET /api/display/frame.png` is the whole screen, already drawn in the panel's
six inks. The device downloads it and puts it on the panel; it decides nothing
about what is shown.

That split is deliberate:

- **The wording, the layout and the Norwegian letters are tested in Python**,
  in `tests/test_display_frame.py`, against `app/display_render.py`. On the
  device they would be C++, tested by looking at it.
- **A change of layout is an ordinary update** (`scripts/update.sh`), not a
  firmware flash on a device screwed to a wall.
- **The panel is redrawn only when a sensor changes.** The ETag is not a hash
  of the picture but `display_render.sensor_fingerprint`: which doors and
  windows are open and since when, which are left open, which cannot be heard
  from, the alarm's left-open warning, frost, the number of sensors, and
  whether the display's own battery is low, plus `DESIGN_VERSION`, which is
  bumped whenever the drawing changes so an update reaches the wall without
  waiting for a door. The device asks with
  `If-None-Match` and is answered `304 Not Modified` until one of those has
  changed. A new outdoor temperature, an outlook, a room temperature or the
  hub reconnecting is drawn with the next sensor change, never on its own. A
  Spectra 6 refresh takes the best part of twenty seconds and flashes the
  whole panel; it should happen when a door opens, not every quarter of an
  hour.
- **So nothing on the picture may go stale while it waits.** Times carry
  their weekday ("Mon 09:47"), because a bare "09:47" is wrong the next
  morning. The outdoor reading says when it was read ("Outside 4° at 10:05").
  The low-battery note has no percentage. The hub's connection is not on the
  picture at all: doors and windows do not depend on it.

The palette in `display_render.py` is exactly M5GFX's `Panel_ED2208` palette,
and the device draws in `epd_fastest`, which does no dithering, so every
pixel lands on the ink it was drawn in.

### What it says

| Tone | Headline | When |
| --- | --- | --- |
| Red | **Left open** | A contact has been open past its room's delay, or the alarm is on with something open |
| Yellow | **N open** | Something is open, not yet for long |
| Blue | **Check sensors** | Nothing open, but a sensor has gone unheard |
| Green | **All closed** | Every door and window that counts is closed |
| Blue | **No sensors** | Sensors are off, or none is in a room |

Below the headline, one row per thing that needs a look, the longest-left-open
first: the room, the sensor and how long ago it opened (`21:15`, or
`Sat 21:15` when it is a day or more old). Unavailable sensors read "no
signal" and are never counted as closed. A room near freezing gets a red row.
With the alarm on and something open, a red row says so ("Alarm on",
"Locked up", …). The footer carries the outside temperature in whole degrees
and the weather outlook, when there is a weather station.

Two notes can appear at the bottom in red: **No contact with the Nobø hub**,
and **Display battery low: N%** at 20 % or below.

The sensors counted are the ones that count everywhere else: a Verisure
contact set aside behind a Zigbee one on the same door is not shown twice.
`GET /api/display` is the same information as JSON, for anything else that
wants to draw it.

## The display key

A wall display cannot sign in, and should not hold a password that opens the
heating. It holds a **display key** instead, made under **Settings → Wall
Displays** (admins only, shown only with sensors on).

- The key is shown **once**, when it is made. Only its SHA-256 is kept, in
  `data/displays.json` (mode 0600). Lose it and make another.
- It opens `GET /api/display` and `GET /api/display/frame.png` **and nothing
  else** — not the rest of the API, not the WebSocket, not any `POST`.
  `AuthMiddleware` accepts `Authorization: Bearer nd_…` only for those two
  paths, and `tests/test_display_frame.py` proves the rest answer 401.
- **Remove** revokes it at once. At most ten displays.
- The card shows when each display was last seen, its battery, its firmware
  and whether it is on USB or battery, which it reports in
  `X-Display-Battery`, `X-Display-Firmware` and `X-Display-Power`. A change
  of battery, firmware or power is written at once; otherwise last-seen is
  written at most every ten minutes, so a display on USB asking twice a
  minute does not wear the SD card.
- **How often it checks on battery** is chosen per display on the card: 2, 5,
  10, 15 (the default), 30 or 60 minutes (`PATCH /api/displays/{id}` with
  `{"battery_minutes": N}`, admins only). Every answer, `200` or `304`,
  carries it in `X-Display-Interval`, and the device keeps it, so a change
  reaches the display at its next check without touching the device.
- **Its lights on USB** are set per display on the same card: Dim (the
  default), Medium, Bright or Off. That is `PATCH /api/displays/{id}` with
  `{"light_level": "dim" | "medium" | "bright"}` and `{"light": false}` or
  `true`, admins only; `light` is a real JSON boolean, and a level that is
  not one of the three changes nothing, not even the rest of the request.
- **Redraw now** on the card has that display draw its picture again though
  no sensor has changed — for a panel left half-drawn or off-colour
  (`POST /api/displays/{id}/redraw`, admins only). It bumps a per-display
  count that is part of that display's ETag (`W/"<fingerprint>.<count>"`), so
  the device needs no new firmware: on USB its held ask notices within a
  second, on battery it is sent at the next wake. The panel still rests three
  minutes between refreshes, so a redraw asked sooner is drawn then. The card
  says whether the picture has been sent since (`redraw_asked_at`,
  `picture_sent_at`). It cannot help a display that has stopped asking: a
  hung or flat device needs its power cycled.

A signed-in browser can open the picture too, which is how the card shows a
preview.

## The device

The firmware is in [`display/papercolor/`](../display/papercolor/README.md):
PlatformIO, M5Unified, and nothing site-specific in it. Wi-Fi, the address,
the key and the Pi's certificate are sent to it over USB by
`display/papercolor/provision.py` and kept in its flash. They are never in
the firmware or this repository.

### On USB power

It asks with `?wait=25`, and the Pi holds the answer open — checking once a
second — until a sensor changes or the 25 seconds are up (the Pi caps it at
30). A change is answered at once, so the panel starts redrawing within a
second or two of a door opening, and then asks again straight away. An
unchanged `304` is followed by the next ask at once, so an idle display costs
two requests a minute. After a failure it tries again in `usb_seconds` (30).
A `304` that comes back without waiting — a Pi too old to hold answers — is
also followed by `usb_seconds`, so it never asks in a tight loop. The buttons
fetch at once and always redraw. An idle display spends nearly all its time
inside a held ask, and before 1.3.2 the buttons were only read between asks,
so a short press was usually lost and a press looked dead. Since 1.3.2 an
interrupt catches it: the lights flash white once at the moment of the press
(unless they are off), and the fetch follows when the held answer returns, so
the redraw begins within about 25 seconds and takes about 15 more.

**Washed-out colours: one driver bug fixed, the cause still open.** On 16
October 2026 the "All closed" band turned moss green, red came out maroon and
blue darker, although every pixel the Pi sent was the panel's own ink; black,
white and yellow were right. M5GFX up to 0.2.33 waits only 20 s for a refresh,
then sends `POWER_OFF` while the panel may still be refreshing; timed on this
board (`refresh_ms` in `status` and on serial, since 1.3.4) a refresh under
that driver took 35 s. M5GFX fixed this on its `develop` branch on 6 October
2026 (the refresh runs to the end by itself, and a controller left busy by a
reset is reset), and `platformio.ini` pins that commit until a release
includes it. With it a refresh takes 15 s, and green looked right after the
first one at 15:45. **But it was moss again after a redraw at 16:56**, with
the same firmware, a full 15 s refresh, the panel no longer busy afterwards,
5.25 V on USB and the Mac awake. So the driver was not the whole cause.
The panel is new and the light had not changed. What the two moss
redraws had in common is that each replaced a **red** band: a window was
opened at 16:55 and closed at 16:56. Every green that looked right came after
white (the `clean` cycle) or after green (a restart). Some red evidently stays
behind, and red in green reads as moss — which also fits the first time, after
an afternoon of opening and closing doors to test the lights. So since 1.3.5
**every picture is drawn over white**: the panel is driven white, then the
picture, two refreshes of 15 s back to back. A picture that cannot be decoded
is found out before the white, so the old one stays. A change therefore takes
30 s to appear rather than 15, and on battery costs two refreshes rather than
one. The chip's temperature (`chip_c`,
31–33 °C) ruled out the board running hot. `busy_after` in `status` is true
if a refresh is ever cut short.

**The moss came back anyway.** On 17 October 2026 at 13:21 a window closed and
the display drew white then green, on its own power away from the Mac, and the
owner saw moss again. White first is therefore not a cure either, and the
true cause is still unknown. The owner asked for a darker green. Since then
the Pi draws the "All closed" band as green with **one black pixel in every
2×2 block** (`SHADED_TONES` in `app/display_render.py`). The panel has a
single green ink and nothing between its six inks, so mixing in black dots is
the only way to make it darker. At reading distance the dots blend into a
deeper green. This is a server change; the firmware is untouched. `DESIGN_VERSION`
went to 2 with it, so the darker band reached the display by itself.

**Not a change in M5GFX's colour handling.** The firmware asked for
`M5GFX@^0.2.17`, so builds before 16 October took 0.2.32 and the builds of
16 October took 0.2.33 (13 October). Between those two releases nothing in the
ED2208 driver changed: 0.2.33 touched SPI transfers from PSRAM, which would
scatter wrong pixels rather than tint a whole band, and USB power on other
boards. The driver's colour table last changed in May 2026. The driver also
never sets the panel's temperature (no `0x41`/`0xE5` in its init), so the panel
picks its waveform from its own thermometer. The same firmware and the same
picture gave green at 17:22 and moss at 16:56 on 16 October, so the cause
varies from one refresh to the next. A firmware or library bug would give the
same result for the same input. The most likely remaining causes are the
panel's temperature reading and its power supply during a refresh. Neither
has been measured.

The panel is also never refreshed twice within **three minutes** (since
1.3.3), which E Ink asks of a Spectra 6 panel. A picture that arrives sooner is
still fetched, and the lights follow it at once; it is kept and drawn when the
three minutes are up, and the next ask is held no longer than that. A door
opened and closed within a minute is therefore drawn closed up to three
minutes after it was drawn open. A person is the exception (since 1.3.9): a
button press, and "Redraw now" in Settings, are drawn at once, rest or no
rest. The Pi marks the picture a redraw asked for with `X-Display-Redraw: now`.
Before 1.3.9 both were kept like any other picture, so on 20 October 2026 a
press right after the front door closed seemed to do nothing, and the display
looked stuck on yellow until it was unplugged. On battery, wakes are at least two minutes
apart, so nothing is kept back there. A serial `clean` drives the whole panel
black, then white, then shows the six inks side by side with their names, and
draws the picture three minutes later, to judge the colours by eye.
A serial `cycle` (since 1.3.6, `--loops` 1–10, default 5) drives the whole
panel black, white, red, yellow, blue and green in turn, that many times, to
work loose ink particles that have stuck. It keeps the three-minute rest
between colours, so five loops take an hour and a half. It runs alongside the
display's usual work: the lights follow the Pi, and a picture that arrives
meanwhile is kept until the end. A button press or `--loops 0` stops it, and
the picture is drawn once the panel has rested.

**Conditioning did not help.** On 17 October 2026, five loops ran from 19:52
to 21:30 on USB from the Mac, every refresh a full 15 s. The last colour, the
whole panel green, was moss to the owner's eye, with the chip at 32 °C. That is
the bare green ink, with no picture and no red before it, so the moss is
neither left-over red nor anything in the picture. (1.3.6 also left that green
on screen instead of drawing the picture; 1.3.7 fixes it.) What remains
unmeasured is temperature and supply: on USB the board is awake and warm
beside the panel all the time. On battery it sleeps cool between wakes.

**Not the temperature either.** The same evening the display spent half an
hour on battery, then went back on USB. At 22:25 a window closed and it drew
white then green, with the display, in the owner's words, "very cold": moss
again. Moss has now been seen warm (chip 32 °C) and cold, on USB from the wall
and from the Mac, over white, after conditioning, and as the bare ink. Nor is it
time since the display started: the good green at 17:22 on 16 October came
8 minutes after a start, and the moss at 22:25 about 6. One condition remains
untried, a redraw on battery power: unplug the display, wait a minute for it to
sleep, and press a button, which starts it afresh on battery and draws white then
the picture. If that is moss too, what is left is the panel itself or the drive
settings M5GFX sends it. M5GFX's settings have not changed since April 2026, and
some of them differ from the panel maker's reference code: booster `0x06` and
PLL `0x30`. The `clean` chart of the six inks is the photograph to send
M5Stack.

**Moss comes with soft text.** On 18 October 2026 the owner noticed that when
the green is moss, the black text on white is less crisp too, and when the
green is right the text is sharp. So the whole picture is under-driven, not
green alone — as if the refresh did not run its full course. The serial
timings so far did not show that (15 s, the panel no longer busy, for good and
moss alike), but they were spot checks. The M5GFX commit pinned here runs the
refresh entirely inside the panel's controller (the `AUTO` command), and the
controller chooses its waveform, and so how long and how hard it drives the
ink, from its own thermometer; nothing in M5GFX sets the temperature. Since
1.3.8 the display therefore reports every refresh to the Pi, sent with its
next ask in `X-Display-Refresh: n=<count>;ms=<duration>;busy=<0|1>;chip=<°C>`.
It is kept in RTC memory, so a display on battery reports it on the next
wake. The Pi keeps the last 20 per display, with the time it heard each one,
in `refreshes` (`GET /api/displays`), and the card in Settings shows the last.
The next time the green is moss, compare the duration of that redraw with a
good one: a shorter one confirms the waveform, and points at forcing the
panel's temperature.

On USB it never sleeps, so nothing would otherwise start it afresh. Firmware
1.3.0 ran for five days on the demo Pi and then, from 10 October 2026, failed
every ask: it opened the connection and closed it 25 ms later without sending
a byte of TLS (seen with `tcpdump` on the Pi), and told the panel "No answer".
That is the TLS client failing to set up before it starts, which is what an
exhausted or fragmented heap looks like; the certificate chain still verified
against the root it holds. Since 1.3.1 it takes a **fresh start** — a deep
sleep of one second, which leaves RAM and Wi-Fi clean while the panel, the
clock, the ETag and the last success survive in the panel and RTC memory —
after three failed asks in a row (at most once an hour, so a Pi that is
simply down is not met with a restart every minute and a half), and once a
day after an answer. A failure now names the TLS layer's own reason rather
than "connection refused", and `status` over serial reports `uptime_s`,
`free_heap`, `largest_block` and `last_fresh_start`.

### Its lights, on USB power

The PaperColor has two RGB LEDs. On USB they show the colour the Pi sends with
every answer, `200` or `304`, in `X-Display-Light`
(`display_render.status_light`). Green is held steady: all is well, and a
steady light is calmer to live with. Yellow, red and blue blink slowly — a 0.7 s glow
every 3 s — because they ask for something to be done. A change of colour shows
within a tenth of a second of the answer, not at the end of a blink.

How bright is the Pi's too, in `X-Display-Light-Level`, a WS2812 drive level
of 255 (`LIGHT_LEVELS` in `display_tokens.py`): dim 6, medium 18, bright 48.
The LEDs are far brighter than the scale suggests — 48, the only level before
firmware 1.3.0, was too bright in a dark hall — so it is set on the Pi, where
it can be tuned without reflashing. A Pi too old to send the header leaves the
firmware at 18.

| Light | When |
| --- | --- |
| green | every door and window is closed, and every sensor is heard from |
| yellow | a door or window is open, but not yet left open (the panel's yellow band) |
| red | a door or window has been left open, or the alarm is on with one open |
| blue | nothing is open, but a sensor cannot be heard from; or this display has not reached the Pi for 30 minutes (when it draws **Not up to date**) |
| off | sensors turned off, no door or window sensor assigned, the light turned off in Settings, or a Pi too old to send the header |

Yellow came with firmware 1.4.0 (20 October 2026), so the lights match the
band. Equal red and green looks lime on a WS2812, so the firmware mixes
yellow as red with three fifths as much green. An older firmware would read
`yellow` as a word it does not know and go dark with a door open, so the Pi
sends it `red` instead (`light_for_firmware`), as it always did.

The colour is the Pi's, as the picture is: the firmware never guesses one.
Green is only ever shown when it is known, because green is what lets
somebody walk out without looking. Every input to the light is also in the
ETag fingerprint, so a held ask ends the moment the light should change. A
Settings change does not move the ETag and arrives with the next answer,
within 25 seconds.

The firmware drives the two WS2812s on GPIO 21 itself, through the Arduino
core's RMT API. `M5.Led` cannot be used: on the Arduino core this project
builds with (2.x, ESP-IDF 4.4) M5Unified's LED bus is a stub that accepts
colours and sends nothing, which is how firmware 1.2.0's first build had
dark LEDs while reporting the right colour. The serial command
`{"cmd":"light","color":"red","level":6}` sets both by hand, for checking by
eye, until the Pi's next answer.

**On battery the lights are never on.** The board sleeps between asks, and the
LEDs hold their colour without the processor, so they are put out before
every sleep. A display unplugged in the middle of a held ask stays lit until
that ask ends, at most about 45 seconds, then goes dark.

Before 1.3.8 that was true only of a display that had been on USB: on battery
the firmware never started the LED driver, so "put out" sent nothing. On
18 October 2026, after a restart on battery, the first of the two LEDs was
lit and the second dark — a WS2812 can latch a stray colour as its power comes
on, and the first in the chain is the one that sees the glitch. Nothing ever
cleared it, so it glowed through every sleep. Since 1.3.8 the LEDs are always
sent "off" before a sleep, whether or not they were in use, the send cannot
be cut into by the light task mid-colour, and the data line is held low while
the board sleeps.

### When it stops

On 18 October 2026 at 13:15 the display, on USB, fetched a picture, stopped
in the middle of drawing it — the panel was left yellowish — and asked for
nothing more for seven hours. Its button did nothing and it did not answer on
the network, so it had hung rather than slept, and stayed so until its power
was cycled. Since 1.3.8 a **watchdog** restarts the board if its loop has not
come round for five minutes, longer than any one turn can take (an ask held
30 s, TLS, and two refreshes that M5GFX abandons after 40 s each). RTC memory
survives that restart, so after a watchdog, crash or brownout restart the
ETag is cleared and the picture is drawn again rather than met with a `304`
over a half-drawn panel. `status` reports `reset_reason`. A redraw asked from
Settings (**Redraw now**) cannot reach a display that has stopped asking; the
watchdog is what brings it back.

### On battery

It wakes every `battery_minutes` (from the Pi, default 15), joins Wi-Fi
(remembering the access point and channel, which makes it quicker), asks for
the picture, and goes back to deep sleep. A `304` costs a few seconds of radio
and no refresh. On a timer wake it goes straight to the ask; only a cold boot
or a button listens 1.5 s for `provision.py` first. Any of the three buttons
wakes it and forces a redraw. Unplugging USB moves it to battery behaviour by
itself.

This is a picture of the house **up to `battery_minutes` old**. It is not an
alarm and cannot be one: a door opened a minute after it woke is shown at the
next wake, or when a button is pressed. For a check on the way out, press a
button.

Battery life, estimated rather than measured (1250 mAh, a wake with no change
about 5–8 s, a redraw 20–35 s): roughly 5–7 weeks at 15 minutes, 2–3 weeks at
5 minutes, about a week at 2 minutes. Nearly all of it is the Wi-Fi join, so
it scales with the interval, not with how often a door opens.

### When it cannot reach the Pi

A single failed wake leaves the last picture: Wi-Fi blinks. Once there has
been no successful fetch for **30 minutes**, or none at all since it was
powered on, it draws **Not up to date**, with when it last succeeded and why
it failed. A screen that says "All closed" must never quietly be hours old.
The next success redraws the real picture. A button press that fails again
redraws the notice with **Tried again** and the time (since 1.3.2), so the
press is seen to have done something; before that the notice was drawn once
and a press while the Pi was away changed nothing on the screen.

On 16 October 2026 the demo Pi, on Wi-Fi, heard nothing from the LAN from
13:16 to 14:21 while it kept running — its health checks passed and it could
not reach Verisure — and the display rightly said Not up to date. The Pi's
Wi-Fi was in power save; see "When something is wrong" in
[INSTALL.md](INSTALL.md) for turning it off. On a cable this does not arise.

At **5 %** battery it draws a "charge me" screen once and checks again hourly,
so it does not die showing a stale "All closed".

### HTTPS

The device checks the Pi's certificate. With Caddy's internal CA it needs that
CA's root, which the Pi already has:

```bash
sudo docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt
```

The address must be the **name** the certificate is for
(`https://nobo.example.com/api/display/frame.png`), not an IP address. With
Let's Encrypt, give it the ISRG Root X1 certificate instead.

The clock on the device is set from the HTTP `Date` header, which is all it
needs to say when it last succeeded.

## What is verified

On the demo Pi, 5 October 2026, with the device on USB and demo Wi-Fi:
provisioning over serial; the first fetch over HTTPS against Caddy's internal
CA; the key recorded as seen, with battery and firmware; `304` on unchanged
polls; a redraw when a simulated door opened and again when it closed; and a
failed fetch with nothing succeeded since power-on taking the "Not up to
date" path. That the panel then looked right was not confirmed by anyone
standing in front of it.

Opening the USB serial port resets the board, and a reset clears what it
remembers, so the first fetch after one always redraws.

Battery behaviour, battery life and the button wake have **not** been run, and
neither has the production Pi. The lights (firmware 1.2.0) were checked by eye
on the demo Pi, 5 October 2026: green with everything closed, red with a
window open, blue with a sensor made unavailable, and dark once USB was
unplugged. Blue for a display that cannot reach the Pi has not been watched.
Firmware 1.3.0 (steady green, set brightness) was flashed the same evening and
reported green, level 18, lights running, over serial.

1.3.0 then ran on USB from 5 to 10 October 2026 before failing as described
under "On USB power". That the 1.3.1 fresh start cures it is reasoned from the
symptom, not proven: the heap was not measured before it failed.
1.3.1 was flashed on 11 October 2026; just after boot it reported 264 KB of
internal heap free with a largest block of 254 KB, and the Pi logged its
`200` and held `304`s again. The fresh start itself has not yet been seen to
fire.

1.3.2 was flashed on 16 October 2026. Over serial: a `refresh` (the path a
button takes) with the address pointed at a closed port failed with "TLS:
ERROR - Generic error" and took the Tried again path; with the address
restored it fetched and redrew; and for three minutes afterwards it sat on
held `304`s, so the new interrupt does not fire by itself. A real press — the
white flash and the redraw — has **not** yet been watched.

1.3.4, built against M5GFX `develop`, was flashed on 16 October 2026. Its
first refresh took 15.0 s against 35.3 s for 1.3.3 on the same board ten
minutes earlier, with the panel no longer busy afterwards, and the owner
confirmed by eye that the "All closed" band was green again. At 17:01 it was
moss again (see "On USB power"); 1.3.5 (white before every picture) was flashed at
17:14. A window opened at 17:18:30 drew white then red, and the owner saw a
good red; closed at 17:19:20, it drew white then green at 17:22:31, after the
three-minute rest (the lights turned green at once). The owner confirmed by eye
that this green looked right: the red-to-green change that had come out moss
twice that afternoon came out green over white.
On 17 October at 13:21 a green drawn over white was moss again, on the
display's own power. The band has been drawn darkened with black since (see
"On USB power").
