# PaperColor wall display firmware

Firmware for an M5Stack PaperColor (ESP32-S3, 4" Spectra 6 e-paper) that
shows the picture the Pi draws at `/api/display/frame.png`. What it shows,
and why it is drawn on the Pi, is in [docs/DISPLAY.md](../../docs/DISPLAY.md).

## Build and flash

Needs [PlatformIO](https://platformio.org/) (`pip install platformio`). The
first build downloads the ESP32 toolchain, which takes a while.

```bash
cd display/papercolor
pio run                                         # build
pio run -t upload --upload-port /dev/cu.usbmodem2101   # flash over USB-C
```

The port is `/dev/cu.usbmodem…` on a Mac and usually `/dev/ttyACM0` on
Linux. If the upload cannot connect, put the board in download mode as
M5Stack's documentation for the PaperColor describes, and try again.

## Set it up

1. On the Pi, under **Settings → Wall Displays**, add a display and copy the
   key. It is shown once.
2. Fetch the Pi's root certificate (internal CA):

   ```bash
   ssh <pi> 'cd /opt/nobo-control && sudo docker compose exec -T caddy \
     cat /data/caddy/pki/authorities/local/root.crt' > nobo-root.crt
   ```

3. Send everything to the display:

   ```bash
   python provision.py --port /dev/cu.usbmodem2101 \
       --ssid "Home Wi-Fi" \
       --url https://nobo.example.com/api/display/frame.png \
       --ca nobo-root.crt
   ```

   It asks for the Wi-Fi password and the display key without echoing them,
   or reads them from `NOBO_WIFI_PASSWORD` and `NOBO_DISPLAY_KEY`. Neither is
   printed or written anywhere but the display's flash. Delete
   `nobo-root.crt` afterwards if you like; it is public, but not needed.

The display answers with its status and fetches at once.

Other commands:

```bash
python provision.py --port … status            # its settings, never its secrets
python provision.py --port … refresh           # fetch and redraw now
python provision.py --port … forget            # wipe everything, back to "Set me up"
python provision.py --port … --keep-key --battery-minutes 30   # change one thing
```

| Option | Default | |
| --- | --- | --- |
| `--battery-minutes` | 15 | Minutes between wakes on battery (2–240), until the Pi says otherwise: Settings → Wall Displays sets it per display, and the Pi's value wins from the next check |
| `--usb-seconds` | 30 | On USB the display holds an ask open at the Pi and redraws as soon as a sensor changes; this is only how long it waits after a failure (15–3600) |
| `--rotation` | -1 | -1 keeps the panel portrait; 0–3 force a rotation |
| `--tz` | `CET-1CEST,M3.5.0,M10.5.0/3` | POSIX time zone for "last updated" |

On USB its two LEDs blink slowly green when every door and window is closed,
red when any is open and blue when it cannot tell; on battery they are always
off. Settings → Wall Displays can turn them off per display. See
[`docs/DISPLAY.md`](../../docs/DISPLAY.md#its-lights-on-usb-power).

Opening the serial port resets the board; `provision.py` repeats its command
until the display answers, which can take ten seconds or so while it boots
and draws. On battery it listens for 1.5 seconds only after a button wake or
a cold boot, so plug it into USB.

## Moving it to another house

Make a key on that Pi, then run `provision.py` again with the new Wi-Fi,
address, key and certificate. Remove the old key on the old Pi.

## Restoring the factory firmware

If a copy of the original flash was taken first
(`esptool --port … read-flash 0 0x1000000 factory-flash.bin`), it goes back
with:

```bash
esptool --port /dev/cu.usbmodem2101 write-flash 0 factory-flash.bin
```
