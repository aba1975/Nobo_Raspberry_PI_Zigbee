#!/usr/bin/env bash
set -euo pipefail

# Nobo Web Control — Update script
# Pulls latest code, rebuilds, and restarts the service

INSTALL_DIR="/opt/nobo-control"
SERVICE_NAME="nobo-control"

echo "Updating Nobo Web Control..."

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root (use sudo)."
    exit 1
fi

if [ ! -d "$INSTALL_DIR/.git" ]; then
    echo "ERROR: $INSTALL_DIR is not a git repository."
    echo "  Run scripts/install.sh first."
    exit 1
fi

cd "$INSTALL_DIR"

echo "[1/5] Pulling latest code..."

# Which branch is this checkout actually on? A Pi left on an old feature branch
# pulls that branch, reports success, and stays on old code — the update looks
# like it worked and nothing changed. Worth saying out loud rather than leaving
# somebody to wonder why a fix they can see on GitHub is not on the machine.
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
DEFAULT_BRANCH=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||' || true)
DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"

if [ "$BRANCH" != "$DEFAULT_BRANCH" ]; then
    echo ""
    echo "  NOTE: this checkout is on '$BRANCH', not '$DEFAULT_BRANCH'."
    echo "  Pulling will update '$BRANCH' only. To move to the released code:"
    echo ""
    echo "      cd $INSTALL_DIR"
    echo "      sudo git fetch origin"
    echo "      sudo git checkout -B $DEFAULT_BRANCH origin/$DEFAULT_BRANCH"
    echo "      sudo git branch --set-upstream-to=origin/$DEFAULT_BRANCH $DEFAULT_BRANCH"
    echo "      sudo bash scripts/update.sh"
    echo ""
    echo "  Your .env and data volume are untouched by that."
    echo ""
fi

git pull
echo "  Now at: $(git log --oneline -1)"

echo "[2/5] Rebuilding Docker image..."
docker compose build

echo "[3/5] Checking data directory ownership..."
# The container used to run as root and now runs as uid 1001. An existing
# data volume is still owned by root, and the application could not write
# users.json any more, so nobody could log in after upgrading.
VOLUME_NAME=$(docker inspect nobo-web-control \
    --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Name}}{{end}}{{end}}' \
    2>/dev/null || true)
if [ -z "$VOLUME_NAME" ]; then
    MATCHES=$(docker volume ls -q 2>/dev/null | grep -E '(^|_)nobo-data$' || true)
    if [ "$(echo "$MATCHES" | grep -c .)" = "1" ]; then
        VOLUME_NAME="$MATCHES"
    fi
fi
if [ -n "$VOLUME_NAME" ]; then
    MOUNT=$(docker volume inspect "$VOLUME_NAME" --format '{{.Mountpoint}}' 2>/dev/null || true)
    if [ -n "$MOUNT" ] && [ -d "$MOUNT" ]; then
        chown -R 1001:1001 "$MOUNT"
        echo "  Data volume $VOLUME_NAME is writable by the application user."
    fi
fi

echo "[4/5] Restarting service..."
systemctl restart "$SERVICE_NAME"

echo "[5/5] Checking service status..."
sleep 3
systemctl status "$SERVICE_NAME" --no-pager

echo ""

# Print the address that actually works. With the TLS proxy in front, the
# application is bound to loopback and port 8000 answers nothing, so the old
# unconditional http://<ip>:8000 sent people to a dead address every time.
NOBO_DOMAIN=$(grep -E '^NOBO_DOMAIN=' "$INSTALL_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
NOBO_BIND=$(grep -E '^NOBO_BIND=' "$INSTALL_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
NOBO_PORT=$(grep -E '^NOBO_PORT=' "$INSTALL_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)

if [ "$NOBO_BIND" = "127.0.0.1" ] && [ -n "$NOBO_DOMAIN" ]; then
    echo "Update complete. Web interface: https://$NOBO_DOMAIN"
else
    echo "Update complete. Web interface: http://$(hostname -I | awk '{print $1}'):${NOBO_PORT:-8000}"
fi
