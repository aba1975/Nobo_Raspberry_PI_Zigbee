#!/usr/bin/env bash
set -euo pipefail

# Stop the Nobo Web Control service

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root (use sudo)."
    exit 1
fi

echo "Stopping Nobo Web Control..."
systemctl stop nobo-control
echo "Service stopped."
