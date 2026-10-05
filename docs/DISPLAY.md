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
- **The ETag is a hash of the picture**, and the picture carries no clock, so
  the device asks with `If-None-Match` and is answered `304 Not Modified`
  until something it shows has actually changed. A Spectra 6 refresh takes the
  best part of twenty seconds and flashes the whole panel; it should happen
  when a door opens, not every quarter of an hour.

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
- The card shows when each display was last seen, its battery and its
  firmware, which it reports in `X-Display-Battery` and `X-Display-Firmware`.
  That is written to disk at most every ten minutes, so a display on USB
  polling every minute does not wear the SD card.

A signed-in browser can open the picture too, which is how the card shows a
preview.

## The device

The firmware is in [`display/papercolor/`](../display/papercolor/README.md):
PlatformIO, M5Unified, and nothing site-specific in it. Wi-Fi, the address,
the key and the Pi's certificate are sent to it over USB by
`display/papercolor/provision.py` and kept in its flash. They are never in
the firmware or this repository.

### On USB power

It fetches every minute (`usb_seconds`) and redraws only on a change. The
buttons fetch at once and always redraw.

### On battery

It wakes every 15 minutes (`battery_minutes`), joins Wi-Fi (remembering the
access point and channel, which makes it quicker), asks for the picture, and
goes back to deep sleep. A `304` costs a few seconds of radio and no refresh.
Any of the three buttons wakes it and forces a redraw. Unplugging USB moves it
to battery behaviour by itself.

This is a picture of the house **up to 15 minutes old**. It is not an alarm
and cannot be one: a door opened a minute after it woke is shown at the next
wake, or when a button is pressed. For a check on the way out, press a button.

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

On the demo Pi with the device on USB: provisioning, the first draw, a
redraw when a simulated door opens, `304` on unchanged polls, and the stale
screen when the Pi is unreachable. Battery behaviour, battery life and the
button wake have **not** been measured on a wall.
