# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Raspberry Pi 4B (ARM64 / Ubuntu Server) deployment of [nobo-web-control](https://github.com/aba1975/nobo-web-control). The application code in `app/` is a direct copy from the source repo — identical Python + frontend.

## Architecture

- **Backend:** FastAPI (Python 3.12) using `pynobo` for TCP communication with the Nobo Eco Hub
- **Frontend:** Single-page app with WebSocket live updates (`app/static/`)
- **Deployment:** Docker container with `network_mode: host` (required for LAN hub access)
- **Auto-start:** systemd service (`deploy/systemd/nobo-control.service`)
- **Configuration:** `.env` file (NOBO_SERIAL, NOBO_IP, NOBO_DEMO)
- **Data persistence:** Docker named volume mounted at `/app/data` (users.json, away_schedule.json, demo state)

## Key Files

- `app/server.py` — main FastAPI application (~2500 lines), all API endpoints
- `app/auth.py` — session-based auth with bcrypt, brute-force protection
- `app/away_schedule.py` — scheduled away mode persistence
- `app/config_persistence.py` — atomic JSON file persistence for demo zones, schedules, server state
- `Dockerfile` — Python 3.12-slim base, ARM64-compatible
- `compose.yml` — single-service stack with host networking

## Build and Run

```bash
docker compose up --build -d         # build and start
docker compose logs -f               # follow logs
docker compose down                  # stop
```

## Running Tests

Tests run in demo mode (no real hub needed):

```bash
# Inside the container
docker compose exec nobo-web-control pytest /app/../tests/ -v

# Or locally (with Python + deps installed)
cd /path/to/repo
NOBO_DEMO=true python -m pytest tests/ -v
```

## Important Design Notes

- The Nobo Hub allows only ONE TCP connection at a time — the app includes reconnect logic with exponential backoff
- `network_mode: host` is intentional and required — bridge networking breaks hub discovery
- Demo mode activates when `NOBO_DEMO=true` or `NOBO_SERIAL=111111111111`
- API endpoints under `/api/*` and `/ws` are intentionally unauthenticated (for Home Assistant integration)
- The web UI (`/` and `/static/*`) requires session auth via cookie
