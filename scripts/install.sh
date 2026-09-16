#!/usr/bin/env bash
#
# Nobo Web Control — first-time installation.
#
#   sudo bash scripts/install.sh
#
# It asks a handful of questions, writes the configuration for you, and leaves
# a working system running. Nothing has to be edited by hand afterwards.
#
# Re-running it is safe: an existing configuration is kept and only the
# software is updated. Use --reconfigure to answer the questions again.

set -euo pipefail

INSTALL_DIR="/opt/nobo-control"
REPO_URL="https://github.com/aba1975/Nobo_Raspberry_PI_Zigbee.git"
SERVICE_NAME="nobo-control"
TARGET_USER="${SUDO_USER:-}"

RECONFIGURE=0
[ "${1:-}" = "--reconfigure" ] && RECONFIGURE=1

# Answering questions needs a keyboard. Piping this script through bash leaves
# stdin pointing at the download, so read from the terminal explicitly.
INTERACTIVE=0
if [ -r /dev/tty ] && [ -t 1 ]; then
    INTERACTIVE=1
    exec 3</dev/tty
else
    exec 3<&0
fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ask <prompt> <default> -> answer on stdout
ask() {
    local prompt="$1" default="${2:-}" reply=""
    if [ "$INTERACTIVE" = "0" ]; then printf '%s' "$default"; return; fi
    if [ -n "$default" ]; then
        printf '%s [%s]: ' "$prompt" "$default" >&2
    else
        printf '%s: ' "$prompt" >&2
    fi
    read -r reply <&3 || true
    printf '%s' "${reply:-$default}"
}

# ask_yes_no <prompt> <y|n> -> exit 0 for yes
ask_yes_no() {
    local reply
    reply=$(ask "$1 (y/n)" "$2")
    case "$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')" in
        y|yes) return 0 ;;
        *)     return 1 ;;
    esac
}

# set_env KEY VALUE — add or replace, never duplicate
#
# awk rather than `sed -i`: sed's -i flag differs between GNU and BSD, and any
# delimiter chosen for the substitution can appear in a value. Passing the
# value as an awk variable means it is never parsed as part of a pattern, so a
# device path, a password character or a URL all survive intact.
#
# The line is replaced where it stands, keeping it under the comment in
# .env.example that explains it. Commented-out examples start with '#' and are
# deliberately left alone, so the real setting is appended instead.
set_env() {
    local key="$1" value="$2" file="$INSTALL_DIR/.env" tmp
    tmp="$(mktemp)"
    if grep -qE "^${key}=" "$file"; then
        awk -v k="$key" -v v="$value" '
            index($0, k "=") == 1 && !replaced { print k "=" v; replaced = 1; next }
            { print }
        ' "$file" > "$tmp"
    else
        cat "$file" > "$tmp"
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
    fi
    # Copied back rather than moved, so the file keeps its 0600 permissions.
    cat "$tmp" > "$file"
    rm -f "$tmp"
}

say "============================================"
say "  Nobo Web Control — installation"
say "============================================"

[ "$(id -u)" -eq 0 ] || die "Run this with sudo:  sudo bash scripts/install.sh"


# --------------------------------------------------------------------------
step "1/7  Docker"

if command -v docker >/dev/null 2>&1; then
    say "  Already installed."
else
    say "  Installing Docker. This takes a few minutes."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker >/dev/null 2>&1 || true
    systemctl start docker
fi

docker compose version >/dev/null 2>&1 \
    || die "Docker Compose plugin missing. Try: apt-get install docker-compose-plugin"

if [ -n "$TARGET_USER" ] && ! id -nG "$TARGET_USER" | grep -qw docker; then
    usermod -aG docker "$TARGET_USER"
    say "  Added '$TARGET_USER' to the docker group (takes effect at next login)."
fi


# --------------------------------------------------------------------------
step "2/7  Software"

if [ -d "$INSTALL_DIR/.git" ]; then
    say "  Updating the copy in $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only || warn "Could not update — keeping what is there."
elif [ -f "$(dirname "$0")/../compose.yml" ] && [ "$(cd "$(dirname "$0")/.." && pwd)" = "$INSTALL_DIR" ]; then
    say "  Using the copy already in $INSTALL_DIR"
else
    say "  Downloading to $INSTALL_DIR"
    # Never prompt. A private repository would otherwise stop here waiting for
    # a username that whoever is following the instructions does not know they
    # need, with no clue as to why.
    if ! GIT_TERMINAL_PROMPT=0 git clone --depth 1 "$REPO_URL" "$INSTALL_DIR" 2>/dev/null; then
        say ""
        die "Could not download the software.

  If the repository is private, GitHub needs a token. Make one at
  https://github.com/settings/tokens with 'repo' (read) access, then:

      sudo git clone https://YOUR_TOKEN@github.com/aba1975/Nobo_Raspberry_PI_Zigbee.git $INSTALL_DIR
      sudo bash $INSTALL_DIR/scripts/install.sh

  Otherwise check this Pi can reach the internet:  ping -c1 github.com"
    fi
fi
cd "$INSTALL_DIR"


# --------------------------------------------------------------------------
step "3/7  Configuration"

FRESH=0
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    FRESH=1
fi

ADMIN_PASSWORD=""

if [ "$FRESH" = "0" ] && [ "$RECONFIGURE" = "0" ]; then
    say "  Keeping the existing configuration in $INSTALL_DIR/.env"
    say "  (run with --reconfigure to answer the questions again)"
else
    if [ "$INTERACTIVE" = "0" ]; then
        warn "No keyboard available — installing in demo mode with no sensors."
    fi

    # --- the hub -----------------------------------------------------------
    say ""
    say "  The Nobo hub."
    say "  Demo mode invents a house so you can look around straight away, and"
    say "  nothing it does can reach a real heater. You can connect the real"
    say "  hub later from Settings, without reinstalling."
    say ""
    if ask_yes_no "  Connect a real Nobo hub now?" "n"; then
        serial=""
        while :; do
            serial=$(ask "    Hub serial number (12 digits, on the sticker)" "")
            case "$serial" in
                [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) break ;;
                *) warn "  That is not 12 digits." ;;
            esac
            [ "$INTERACTIVE" = "0" ] && break
        done
        ip=$(ask "    Hub IP address on your network" "")
        set_env NOBO_DEMO false
        set_env NOBO_SERIAL "$serial"
        set_env NOBO_IP "$ip"
        say "    Real hub configured."
    else
        set_env NOBO_DEMO true
        set_env NOBO_SERIAL 111111111111
        say "    Demo mode."
    fi

    # --- zigbee ------------------------------------------------------------
    say ""
    say "  Door and window sensors (optional)."
    say "  These need a Zigbee USB stick plugged into this Pi. Without one,"
    say "  skip this: nothing extra is installed and the heating is unaffected."
    say ""
    ADAPTERS=()
    if [ -d /dev/serial/by-id ]; then
        while IFS= read -r line; do
            [ -n "$line" ] && ADAPTERS+=("$line")
        done < <(ls /dev/serial/by-id/ 2>/dev/null || true)
    fi

    if [ "${#ADAPTERS[@]}" -eq 0 ]; then
        say "    No USB serial device is plugged in, so sensors stay off."
        say "    Plug the stick in and run this again with --reconfigure."
        set_env COMPOSE_PROFILES ""
    elif ask_yes_no "  Set up Zigbee sensors?" "y"; then
        adapter=""
        if [ "${#ADAPTERS[@]}" -eq 1 ]; then
            adapter="/dev/serial/by-id/${ADAPTERS[0]}"
            say "    Found: ${ADAPTERS[0]}"
        else
            say "    More than one USB serial device is plugged in:"
            i=1
            for item in "${ADAPTERS[@]}"; do
                say "      $i) $item"
                i=$((i + 1))
            done
            choice=$(ask "    Which one is the Zigbee stick?" "1")
            adapter="/dev/serial/by-id/${ADAPTERS[$((choice - 1))]}"
        fi
        set_env NOBO_ZIGBEE_ADAPTER "$adapter"
        set_env COMPOSE_PROFILES zigbee
        say "    Sensors enabled."
    else
        set_env COMPOSE_PROFILES ""
        say "    Skipped."
    fi

    # --- clock -------------------------------------------------------------
    set_env NOBO_TZ "$(cat /etc/timezone 2>/dev/null || echo UTC)"

    # --- the password ------------------------------------------------------
    say ""
    say "  A password for the 'admin' account you will log in with."
    if [ "$INTERACTIVE" = "1" ]; then
        while :; do
            printf '    New password (at least 8 characters): ' >&2
            read -rs ADMIN_PASSWORD <&3 || true; printf '\n' >&2
            printf '    Again: ' >&2
            read -rs confirm <&3 || true; printf '\n' >&2
            if [ "${#ADMIN_PASSWORD}" -lt 8 ]; then
                warn "  Too short."
            elif [ "$ADMIN_PASSWORD" != "$confirm" ]; then
                warn "  They do not match."
            else
                break
            fi
        done
    fi
fi


# --------------------------------------------------------------------------
step "4/7  Start on boot"

cp deploy/systemd/nobo-control.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null
say "  Installed."


# --------------------------------------------------------------------------
step "5/7  Building"

say "  The first build takes several minutes on a Raspberry Pi. Leave it be."
docker compose build


# --------------------------------------------------------------------------
step "6/7  Starting"

systemctl restart "$SERVICE_NAME"

PORT="$(grep -E '^NOBO_PORT=' .env 2>/dev/null | cut -d= -f2)"
PORT="${PORT:-8000}"

printf '  Waiting for it to answer'
READY=0
for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        READY=1
        break
    fi
    printf '.'
    sleep 2
done
printf '\n'
if [ "$READY" = "1" ]; then
    say "  Running."
else
    warn "Not answering yet. Look at:  sudo journalctl -u $SERVICE_NAME -n 50"
fi


# --------------------------------------------------------------------------
step "7/7  Account"

if [ -n "$ADMIN_PASSWORD" ] && [ "$READY" = "1" ]; then
    # Hashed by the application's own code, inside the container, so it is
    # exactly what the login check expects. Passed in the environment rather
    # than on the command line, where it would appear in the process list.
    if ADMIN_PASSWORD="$ADMIN_PASSWORD" docker compose exec -T \
            -e ADMIN_PASSWORD nobo-web-control python -c '
import os, auth
users = auth.load_users()
entry = users.get("admin", {})
entry["password_hash"] = auth.hash_password(os.environ["ADMIN_PASSWORD"])
entry["role"] = "admin"
users["admin"] = entry
auth.save_users(users)
' >/dev/null 2>&1; then
        say "  Password set for 'admin'."
    else
        warn "Could not set the password. The built-in one is admin / nobohub —"
        warn "log in and change it under Settings straight away."
    fi
elif [ "$FRESH" = "1" ]; then
    warn "Using the built-in login admin / nobohub. Change it under Settings."
else
    say "  Unchanged."
fi
unset ADMIN_PASSWORD


# --------------------------------------------------------------------------
IP_ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
say ""
say "============================================"
say "  Done"
say "============================================"
say ""
say "  Open this on a phone or laptop on the same network:"
say ""
say "      http://${IP_ADDRESS:-<this-pi>}:${PORT}"
say ""
say "  Log in as 'admin'."
say ""
if grep -q '^NOBO_DEMO=true' .env 2>/dev/null; then
    say "  This is demo mode: the rooms are invented and nothing reaches a real"
    say "  heater. Connect your hub under Settings > Hub when you are ready."
    say ""
fi
if grep -q '^COMPOSE_PROFILES=.*zigbee' .env 2>/dev/null; then
    say "  Sensors are on. Add the first one under Settings > Sensors."
    say ""
fi
say "  Later:"
say "    sudo bash $INSTALL_DIR/scripts/update.sh                 get the latest version"
say "    sudo bash $INSTALL_DIR/scripts/backup.sh                 save settings and pairings"
say "    sudo bash $INSTALL_DIR/scripts/install.sh --reconfigure  change these answers"
say ""
