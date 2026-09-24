#!/usr/bin/env bash
set -euo pipefail

# Nobo Web Control — turn a spare ZBDongle-P into a Zigbee repeater
#
# Usage: bash scripts/zigbee-flash-router.sh              (pick the adapter)
#        bash scripts/zigbee-flash-router.sh --device /dev/serial/by-id/usb-ITead_...
#        bash scripts/zigbee-flash-router.sh --yes        (skip the confirmation)
#
# Why this exists
# ---------------
# A mains-powered router is the one change that extends a mesh of sleeping
# battery sensors, and a second dongle flashed as a router is a better radio
# than any smart plug — same CC2652P, same amplifier. Once flashed it needs USB
# power and nothing else: no computer, no data, no drivers.
#
# The two ways this goes badly wrong are both guarded here.
#
# 1. Flashing the adapter that is *currently the coordinator* destroys the
#    running network: every sensor is paired to it, and the network key lives
#    on it. The script reads NOBO_ZIGBEE_ADAPTER from .env, resolves it, and
#    refuses. There is no override flag; unplug it instead. A flag would
#    eventually be pasted from a forum by somebody in a hurry.
#
# 2. Flashing the wrong image can lock the bootloader permanently, after which
#    the stick cannot be rescued in software. The image is therefore not a
#    parameter: it is fetched from Koenkk's release for this exact adapter and
#    checked against the digest GitHub publishes for it.
#
# It does not need Zigbee2MQTT stopped. The coordinator is off limits anyway,
# and a brand new dongle is not a device anything has opened.

REPO="Koenkk/Z-Stack-firmware"
ASSET_GLOB="CC1352P2_CC2652P_launchpad_router_"
BSL_REPO="https://github.com/JelmerT/cc2538-bsl.git"
# Pinned rather than tracking the branch tip: this writes to flash, and an
# unreviewed change arriving silently is not a risk worth taking for a
# convenience. Override with CC2538_BSL_REF to move it deliberately.
BSL_REF="${CC2538_BSL_REF:-28cce9749bfb73c50363ded01bd8001b1639d9fb}"

DEVICE=""
ASSUME_YES=0
while [ $# -gt 0 ]; do
    case "$1" in
        --device) DEVICE="${2:-}"; shift 2 ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        -h|--help) sed -n '3,12p' "$0"; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }

ROOT=$(cd "$(dirname "$0")/.." && pwd)

# -- which adapter is doing the real job ------------------------------------

# `readlink -f` is GNU-only and `mapfile` needs bash 4, neither of which is
# safe to assume — the same portability trap that `sed -i` sprang in
# install.sh. python3 is a hard requirement here anyway.
resolve() {
    [ -n "${1:-}" ] || return 0
    python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null || true
}

COORDINATOR=""
if [ -f "$ROOT/.env" ]; then
    COORDINATOR=$(awk -F= '/^NOBO_ZIGBEE_ADAPTER=/{sub(/^[^=]*=/,""); print; exit}' "$ROOT/.env")
fi
COORDINATOR_REAL=$(resolve "$COORDINATOR")

# -- choose the one to flash ------------------------------------------------

SERIAL_DIR="${NOBO_SERIAL_BY_ID:-/dev/serial/by-id}"

ADAPTERS=()
for item in "$SERIAL_DIR"/*; do
    [ -e "$item" ] || continue
    ADAPTERS+=("$item")
done

if [ -z "$DEVICE" ]; then
    [ "${#ADAPTERS[@]}" -gt 0 ] || die \
"No USB serial adapters found under $SERIAL_DIR. Plug the new dongle in."
    CANDIDATES=()
    for item in "${ADAPTERS[@]}"; do
        real=$(resolve "$item")
        if [ -n "$COORDINATOR_REAL" ] && [ "$real" = "$COORDINATOR_REAL" ]; then
            continue
        fi
        CANDIDATES+=("$item")
    done
    if [ "${#CANDIDATES[@]}" -eq 0 ]; then
        say "The only adapter present is the one this system is using as its coordinator:"
        say "  $COORDINATOR"
        say ""
        say "Flashing that would destroy the running Zigbee network — every sensor is"
        say "paired to it. Plug the new dongle in as well and run this again."
        exit 1
    fi
    if [ "${#CANDIDATES[@]}" -gt 1 ]; then
        say "More than one adapter could be flashed. Choose with --device:"
        for item in "${CANDIDATES[@]}"; do say "  $item"; done
        exit 1
    fi
    DEVICE="${CANDIDATES[0]}"
fi

[ -e "$DEVICE" ] || die "No such device: $DEVICE"

DEVICE_REAL=$(resolve "$DEVICE")
if [ -n "$COORDINATOR_REAL" ] && [ "$DEVICE_REAL" = "$COORDINATOR_REAL" ]; then
    die "Refusing: $DEVICE is this system's coordinator.

Every sensor is paired to it and the network key lives on it; flashing it as a
router would leave you re-pairing every device by hand. Unplug it and flash the
spare on its own, or pass --device for the other adapter."
fi

# The model is only legible in the by-id name, so a caller who passed a plain
# /dev/ttyUSB path is looked back up rather than refused for saying the same
# thing a different way.
IDENTITY="$DEVICE"
for item in "${ADAPTERS[@]}"; do
    if [ "$(resolve "$item")" = "$DEVICE_REAL" ]; then
        IDENTITY="$item"
        break
    fi
done

case "$IDENTITY" in
    *ITead*|*Sonoff*|*sonoff*) ;;
    *)
        say "Warning: $IDENTITY does not look like a Sonoff ZBDongle-P."
        say "This script only knows that adapter, and the wrong image can lock a"
        say "bootloader permanently. Stopping."
        exit 1
        ;;
esac

# -- confirm, before doing any work -----------------------------------------
#
# Asked here rather than after the downloads: what is being confirmed is *which
# stick*, which is known now, and nobody should sit through a download to find
# out they are about to be asked something they would answer "no" to.

say ""
say "About to flash:"
say "  adapter   : $DEVICE"
say "  firmware  : the current Zigbee router build for this adapter"
say "  result    : a Zigbee *repeater*. It stops being able to act as a"
say "              coordinator until it is flashed back."
say ""
if [ "$ASSUME_YES" -ne 1 ]; then
    # Without a terminal there is nobody to ask, and `read` hitting EOF under
    # `set -e` used to end the script with no message at all — which looks
    # exactly like a flash that failed silently.
    if [ ! -t 0 ]; then
        say "Nothing was written: there is no terminal to confirm at."
        say "Re-run with --yes if you are certain of the adapter."
        exit 1
    fi
    printf 'Type "flash" to continue: '
    read -r reply || reply=""
    [ "$reply" = "flash" ] || { say "Nothing was written."; exit 1; }
fi

# -- the firmware -----------------------------------------------------------

for tool in python3 git curl unzip; do
    command -v "$tool" >/dev/null 2>&1 || die "Needs $tool, which is not installed."
done

# Checked before anything is downloaded: finding out about a missing package
# after a clone and a firmware download is a worse way to learn it.
python3 -c 'import serial' 2>/dev/null || die \
"Needs the pyserial package:  sudo apt install python3-serial"
python3 -c 'import intelhex' 2>/dev/null || die \
"Needs the intelhex package:  sudo apt install python3-intelhex"

WORK=$(mktemp -d)
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

say "Finding the current router firmware for this adapter..."
python3 - "$REPO" "$ASSET_GLOB" "$WORK/asset.txt" <<'PY'
import json, sys, urllib.request

repo, glob, out = sys.argv[1], sys.argv[2], sys.argv[3]
with urllib.request.urlopen(
    f"https://api.github.com/repos/{repo}/releases?per_page=30", timeout=30
) as response:
    releases = json.load(response)

for release in releases:
    if "router" not in release["tag_name"]:
        continue
    for asset in release["assets"]:
        if asset["name"].startswith(glob):
            digest = asset.get("digest") or ""
            with open(out, "w", encoding="utf-8") as handle:
                handle.write(f"{asset['name']}\n{asset['browser_download_url']}\n{digest}\n")
            print(f"  {release['tag_name']}  ->  {asset['name']}")
            raise SystemExit(0)

raise SystemExit("Could not find a router firmware asset for this adapter.")
PY

NAME=$(sed -n 1p "$WORK/asset.txt")
URL=$(sed -n 2p "$WORK/asset.txt")
DIGEST=$(sed -n 3p "$WORK/asset.txt")

curl -fsSL "$URL" -o "$WORK/$NAME" || die "Could not download $URL"

# GitHub publishes a digest for each asset. Checking it is the difference
# between "the right firmware" and "whatever arrived over the wire".
if [ -n "$DIGEST" ]; then
    EXPECTED="${DIGEST#sha256:}"
    ACTUAL=$(python3 -c "
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())
" "$WORK/$NAME")
    [ "$EXPECTED" = "$ACTUAL" ] || die "Checksum mismatch on $NAME. Not flashing."
    say "  checksum ok"
else
    say "  warning: GitHub published no digest for this asset; cannot verify it"
fi

unzip -q -o "$WORK/$NAME" -d "$WORK/fw"
HEX=$(find "$WORK/fw" -name '*.hex' | head -1)
[ -n "$HEX" ] || die "No .hex inside $NAME"

# -- the flasher ------------------------------------------------------------

say "Fetching the flashing tool..."
# Fetched by commit rather than cloned by branch, so the code that writes to
# flash is the code that was reviewed. --branch cannot take a commit, hence
# the long form. No silent fallback to the branch tip: that would quietly
# undo the pinning at the one moment it matters.
git init -q "$WORK/bsl"
git -C "$WORK/bsl" remote add origin "$BSL_REPO"
git -C "$WORK/bsl" fetch -q --depth 1 origin "$BSL_REF" || die \
"Could not fetch cc2538-bsl at $BSL_REF.

If that commit has gone, set CC2538_BSL_REF to one you have checked yourself:
  CC2538_BSL_REF=main bash scripts/zigbee-flash-router.sh"
git -C "$WORK/bsl" checkout -q FETCH_HEAD

# --bootloader-sonoff-usb toggles this adapter into its bootloader over the
# serial line, so its enclosure never has to be opened for the boot button.
python3 "$WORK/bsl/cc2538-bsl.py" \
    --bootloader-sonoff-usb -e -w -v -p "$DEVICE" "$HEX"

say ""
say "Done. Unplug it and power it from any USB supply, anywhere in the house —"
say "it needs power only, not a computer."
say ""
say "Then open the app, start pairing, and it should join by itself. Pair the"
say "sensors that need it *afterwards*, so they can choose it as their parent."
say ""
say "It starts at 9 dBm. Once it has joined, raise it to 20 — see"
say "\"A spare dongle makes the strongest repeater\" in docs/SENSORS.md."
