#!/usr/bin/env python3
"""Set up a PaperColor wall display over USB.

Sends the display its Wi-Fi, the address of the picture, its display key and
the certificate the Pi's HTTPS is checked against. They are stored on the
device and nowhere else: not in the firmware, not in this repository.

    python provision.py --port /dev/cu.usbmodem2101 \\
        --ssid "Home" --url https://nobo.example.com/api/display/frame.png \\
        --ca nobo-root.crt

The Wi-Fi password and the display key are asked for, unseen, unless they
are in NOBO_WIFI_PASSWORD and NOBO_DISPLAY_KEY. Neither is ever printed.

    python provision.py --port ... status     # what the display has, minus secrets
    python provision.py --port ... refresh    # fetch and redraw now
    python provision.py --port ... forget     # wipe it, back to "Set me up"

Needs pyserial (pip install pyserial; PlatformIO already has it).
"""

import argparse
import getpass
import json
import os
import sys
import time

try:
    import serial
except ImportError:  # pragma: no cover - a hint, not a code path
    sys.exit("pyserial is needed: pip install pyserial")


def open_port(path: str) -> "serial.Serial":
    port = serial.Serial()
    port.port = path
    port.baudrate = 115200
    port.timeout = 0.2
    # Toggling these resets an ESP32-S3 on its USB serial port.
    port.dtr = False
    port.rts = False
    port.open()
    return port


def ask(port: "serial.Serial", message: dict, wait: float = 45.0) -> dict:
    """Send one command and return the display's JSON answer to it.

    Opening the port usually resets the board, and a command sent while it is
    booting or drawing a screen is lost. So it is sent again every few seconds
    until answered: every command is safe to repeat.
    """
    line_out = (json.dumps(message, separators=(",", ":")) + "\n").encode()
    port.reset_input_buffer()
    deadline = time.monotonic() + wait
    resend_at = 0.0
    buffer = b""
    while time.monotonic() < deadline:
        if time.monotonic() >= resend_at:
            port.write(line_out)
            port.flush()
            resend_at = time.monotonic() + 4.0
        buffer += port.read(4096)
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"{"):
                continue
            try:
                answer = json.loads(line)
            except ValueError:
                continue
            # Fetch reports interleave with answers; an answer has "ok".
            if "ok" in answer:
                return answer
    raise SystemExit("The display did not answer. Is it awake? Press a button and try again.")


def read_ca(path: str) -> str:
    with open(path, encoding="ascii") as fh:
        text = fh.read().strip() + "\n"
    if "-----BEGIN CERTIFICATE-----" not in text:
        raise SystemExit(f"{path} is not a PEM certificate.")
    return text


def secret(env: str, prompt: str) -> str:
    value = os.environ.get(env)
    if value:
        return value
    return getpass.getpass(prompt)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True, help="the display's USB serial port")
    parser.add_argument("--ssid", help="Wi-Fi network name")
    parser.add_argument("--url", help="https://<name>/api/display/frame.png")
    parser.add_argument("--ca", help="the Pi's root certificate, PEM")
    parser.add_argument("--no-password", action="store_true", help="an open Wi-Fi network")
    parser.add_argument("--keep-key", action="store_true", help="keep the display key it already has")
    parser.add_argument("--battery-minutes", type=int, help="minutes between checks on battery (2-240); Settings on the Pi overrides it")
    parser.add_argument("--usb-seconds", type=int, help="seconds to wait after a failed fetch on USB power (15-3600)")
    parser.add_argument("--rotation", type=int, choices=[-1, 0, 1, 2, 3], help="-1 keeps the panel portrait")
    parser.add_argument("--tz", help="POSIX time zone, default Central European")
    parser.add_argument("command", nargs="?", default="config", choices=["config", "status", "refresh", "forget"])
    args = parser.parse_args()

    port = open_port(args.port)
    try:
        if args.command != "config":
            answer = ask(port, {"cmd": args.command})
            print(json.dumps(answer, indent=2))
            return

        message: dict = {"cmd": "config"}
        if args.ssid is not None:
            message["ssid"] = args.ssid
            message["password"] = "" if args.no_password else secret(
                "NOBO_WIFI_PASSWORD", f"Wi-Fi password for {args.ssid}: ")
        if args.url is not None:
            if not args.url.startswith("https://"):
                raise SystemExit("The address must be https://, by name, as the certificate names it.")
            message["url"] = args.url
        if not args.keep_key:
            key = secret("NOBO_DISPLAY_KEY", "Display key (from Settings, Wall Displays): ").strip()
            if not key.startswith("nd_"):
                raise SystemExit("That is not a display key: they start with nd_.")
            message["token"] = key
        if args.ca:
            message["ca"] = read_ca(args.ca)
        for name in ("battery_minutes", "usb_seconds", "rotation", "tz"):
            value = getattr(args, name)
            if value is not None:
                message[name] = value
        answer = ask(port, message)
        print(json.dumps(answer, indent=2))
        if not answer.get("configured"):
            sys.exit("Stored, but the display still lacks something: see above.")
    finally:
        port.close()


if __name__ == "__main__":
    main()
