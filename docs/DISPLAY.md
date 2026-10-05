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
  whether the display's own battery is low. The device asks with
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
- **Its lights on USB** can be turned off per display on the same card
  (`PATCH /api/displays/{id}` with `{"light": false}`, admins only; a real
  JSON boolean, nothing else). They are on by default.

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
fetch at once and always redraw; during a held ask a press is noticed when
the answer comes.

### Its lights, on USB power

The PaperColor has two RGB LEDs. On USB they blink slowly — a 0.7 s glow every
3 s, at a quarter of full brightness — in the colour the Pi sends with every
answer, `200` or `304`, in `X-Display-Light`
(`display_render.status_light`):

| Light | When |
| --- | --- |
| green | every door and window is closed, and every sensor is heard from |
| red | any door or window is open, or the alarm is on with one open — left open or not yet |
| blue | nothing is open, but a sensor cannot be heard from; or this display has not reached the Pi for 30 minutes (when it draws **Not up to date**) |
| off | sensors turned off, no door or window sensor assigned, the light turned off in Settings, or a Pi too old to send the header |

The colour is the Pi's, as the picture is: the firmware never guesses one.
Green is only ever shown when it is known, because green is what lets
somebody walk out without looking. Every input to the light is also in the
ETag fingerprint, so a held ask ends the moment the light should change. A
Settings change does not move the ETag and arrives with the next answer,
within 25 seconds.

**On battery the lights are never on.** The board sleeps between asks, and the
LEDs hold their colour without the processor, so they are put out before
every sleep. A display unplugged in the middle of a held ask blinks until
that ask ends, at most about 45 seconds, then goes dark.

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
The next success redraws the real picture.

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
neither has the production Pi. The lights (firmware 1.2.0) are tested on the
Pi side only; that the LEDs show the right colour is for someone standing in
front of them.
