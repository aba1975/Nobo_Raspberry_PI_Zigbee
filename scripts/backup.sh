#!/usr/bin/env bash
set -euo pipefail

# Nobo Web Control — Backup script
# Creates a timestamped backup of configuration and data

INSTALL_DIR="/opt/nobo-control"

# When run with sudo, $HOME is /root. Default backups to the real user's home
# instead, so they end up where the README says they will.
if [ -n "${SUDO_USER:-}" ]; then
    DEFAULT_BACKUP_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
else
    DEFAULT_BACKUP_HOME="$HOME"
fi
BACKUP_DIR="${1:-$DEFAULT_BACKUP_HOME/nobo-backups}"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_FILE="$BACKUP_DIR/nobo-backup-$TIMESTAMP.tar.gz"

echo "Nobo Web Control — Backup"

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Collect files to back up
TMPDIR=$(mktemp -d)
mkdir -p "$TMPDIR/backup"

# Back up .env
if [ -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env" "$TMPDIR/backup/"
    echo "  Backed up: .env"
fi

# Back up data volume contents
VOLUME_NAME="nobo_raspberry_pi_nobo-data"
# Try the directory name variant too (depends on how compose was started)
for v in "$VOLUME_NAME" "nobo-control_nobo-data" "opt-nobo-control_nobo-data"; do
    MOUNT=$(docker volume inspect "$v" --format '{{.Mountpoint}}' 2>/dev/null || true)
    if [ -n "$MOUNT" ] && [ -d "$MOUNT" ]; then
        cp -r "$MOUNT" "$TMPDIR/backup/data"
        echo "  Backed up: data volume ($v)"
        break
    fi
done

# If volume not found, try container copy
if [ ! -d "$TMPDIR/backup/data" ]; then
    if docker cp nobo-web-control:/app/data "$TMPDIR/backup/data" 2>/dev/null; then
        echo "  Backed up: data from running container"
    else
        echo "  Warning: Could not back up data volume (container not running?)"
    fi
fi

# Create tarball
tar -czf "$BACKUP_FILE" -C "$TMPDIR" backup
rm -rf "$TMPDIR"

# Make the backup owned by the real user, not root
if [ -n "${SUDO_USER:-}" ]; then
    chown -R "$SUDO_USER" "$BACKUP_DIR" 2>/dev/null || true
fi

echo ""
echo "Backup saved to: $BACKUP_FILE"
echo "Size: $(du -h "$BACKUP_FILE" | cut -f1)"
echo ""
echo "To restore, extract and copy files back:"
echo "  tar -xzf $BACKUP_FILE"
echo "  sudo cp backup/.env $INSTALL_DIR/"
echo "  sudo docker cp backup/data/. nobo-web-control:/app/data/"
