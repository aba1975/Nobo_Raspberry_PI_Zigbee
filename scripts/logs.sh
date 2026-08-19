#!/usr/bin/env bash
set -euo pipefail

# Show logs from the Nobo Web Control container
# Usage: bash scripts/logs.sh         (last 50 lines, then follow)
#        bash scripts/logs.sh 100     (last 100 lines, then follow)
#
# No root needed, as long as your user is in the docker group.

LINES="${1:-50}"

cd /opt/nobo-control
docker compose logs --tail "$LINES" -f
