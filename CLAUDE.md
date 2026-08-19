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

Tests run in demo mode (no real hub needed), so they never touch a real Nobø
Hub. `pytest.ini` puts `app/` on the import path, so the plain command works
from the repository root with no environment variables:

```bash
# On the Pi or any machine with Python 3.12 + dev dependencies
cd /opt/nobo-control
pip install -r requirements-dev.txt
python -m pytest -q          # add -v for per-test output
```

If you would rather not install anything on the host, run the suite inside a
throwaway container using the image the app already builds:

```bash
cd /opt/nobo-control
docker run --rm -v "$PWD":/src -w /src \
  nobo-control-nobo-web-control:latest \
  sh -c 'pip install -q -r requirements-dev.txt && python -m pytest -q'
```

`tests/` is deliberately excluded from the production image by `.dockerignore`,
so `docker compose exec ... pytest` will not work — the tests are not in the
running container. Mount the repository as above instead.

## Important Design Notes

- The Nobo Hub allows only ONE TCP connection at a time — the app includes reconnect logic with exponential backoff
- `network_mode: host` is intentional and required — bridge networking breaks hub discovery
- Demo mode activates when `NOBO_DEMO=true` or `NOBO_SERIAL=111111111111`
- Everything is behind session auth. Only `/login`, `/auth/login`, `/favicon.ico`
  and `/api/health` are public; `/api/health` is public so the container
  healthcheck works and it does not disclose the hub serial. Setting
  `NOBO_ALLOW_ANON_API=true` re-opens `/api/*` and `/ws` for headless
  integrations such as Home Assistant, at the cost of letting anyone on the
  network control the heating
- `BaseHTTPMiddleware` never sees WebSocket handshakes, so `/ws` repeats the
  session check inside the endpoint
- Any successful write under `/api/` is broadcast to all WebSocket clients by
  `ZoneBroadcastMiddleware`, so individual handlers do not call
  `broadcast_zone_update()` themselves
- Paths are resolved from the module location, never the working directory, so
  the app and the tests behave the same wherever they are started from
