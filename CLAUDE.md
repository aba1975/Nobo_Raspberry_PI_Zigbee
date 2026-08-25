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

`pytest.ini` puts `app/` on the import path, so the plain command works from the
repository root with no environment variables:

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

Most tests run in demo mode, so they never touch a real Nobø Hub. The real-hub
code paths are covered separately, against `tests/fake_hub.py`:

- `tests/fake_hub.py` — a TCP server speaking the real hub protocol (handshake,
  initial dump, zone/component/week-profile commands, search and pairing,
  errors). The genuine `pynobo` client connects to it.
- `tests/test_fake_hub.py` — proves the fake is faithful. If it fails, every
  other real-hub test is meaningless.
- `tests/test_real_hub_endpoints.py` — drives the whole FastAPI app against the
  fake hub over HTTP.

**Any change to a protocol assumption belongs in `fake_hub.py` first.**

The code has now been run against real hardware (hub `102 000 147 017`), and it
worked: seven zones, eleven devices and seven week profiles read correctly, and
a mode change made in the official app arrived unprompted a few seconds later.
That exercise also found a connection leak the fake hub could not have caught —
see rule 4 below — so treat the fake as necessary but not sufficient. Anything
about *how many* connections exist, or how long they live, needs real sockets or
`ss` to verify. `docs/TEST_MATRIX.md` is the checklist for that.

## Important Design Notes

- The hub accepts **two** LAN connections at once, plus up to ten via the Internet
  (`API_Nobo.pdf` §5.8). The Pi holds one permanently, so the official app can stay
  connected too and the hub pushes every change to both. The reconnect logic with
  exponential backoff exists for dropped connections and the hub's ~18-hourly
  reboot, **not** because only one client is allowed
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
- The installation's name (`data/site.json`) is Pi-only — the hub has no such
  field. It resolves to **two** forms, and they are not interchangeable:
  `display_name` stands alone ("Mostugu", default `Cabin`) and `inline_name`
  goes mid-sentence ("all of the cabin", default `the cabin`). Write new strings
  to *take* a name rather than to *be* one, or the unnamed default reads as
  "Warm all of Cabin?". `site_settings()` resolves both; never re-derive them.
- The name reaches `/login`, which is public, so it is escaped with
  `html.escape` and gated behind `show_on_login`. A user may reasonably name the
  system after their street address; that must not be readable to anyone who can
  reach the Pi unless they chose it

## Talking to a Real Hub

Four rules, each learned from a bug:

1. **Never call pynobo's synchronous wrappers from a request handler.** They
   create their task on whichever event loop is running, which inside FastAPI is
   the web server's loop, not the loop that owns the hub socket. Use the
   `async_*` variants wrapped in `hub_command(...)`, which runs them on
   `hub_loop`, the dedicated loop that owns the connection.
2. **`nobo.stop()` is a coroutine.** There is no synchronous version. Use
   `stop_hub_client()`; calling `stop()` bare leaks the connection.
3. **Never build a component update from `hub.components`.** pynobo overwrites
   `zone_id` with `tempsensor_for_zone_id` when a component is only a
   temperature sensor, so echoing its dict back would move the device into that
   zone. Use `HubProtocolTap.component_row()`, which keeps the raw wire rows.
4. **Never assign to `hub` without stopping what was there.** Connection
   attempts are serialised by `hub_connect_lock`, skipped entirely when a
   healthy client is already installed, and any client they displace is passed
   to `stop_hub_client()`. Before that, a configuration change and the reconnect
   loop could each start an attempt in the same five-second window; both
   succeeded, the second won, and the first was left holding a socket with its
   keep-alive still running — so the hub never timed it out either. With only
   two LAN slots, two orphans lock the user out of their own heating, and the
   handshake has no "busy" reject code to explain why. Found with `ss -tn`, not
   by a test; `tests/test_connection_leak.py` covers it now.

Also worth knowing:

- pynobo has no handling for `Y00`/`Y01`/`Y03`/`Y04`, so device search and
  pairing go entirely through `HubProtocolTap`.
- Names travel with U+00A0 instead of spaces. pynobo encodes on write but does
  not decode on read; `decode_hub_name()` / `encode_hub_name()` handle both.
- Week profile states are `0=Comfort, 1=Eco, 2=Away, 4=Off`. `API_Nobo.pdf`
  page 6 says "3: Off" and is wrong � pynobo's `validate_week_profile` accepts
  only `0124`, and it is pynobo that talks to the hub.
- Ids for new zones and week profiles are assigned by the hub, not the client.
  Send a placeholder and find the real one by diffing state before and after.
- Week profiles are shared between zones and every zone starts on profile `1`.
  `apply_week_profile_to_zone()` only edits in place when the profile belongs to
  that zone alone and is not `1`; otherwise it creates a per-zone copy.
- **`current_temperature` is `null` for most devices, and that is correct.**
  Receivers such as the R80 RDC 700 and NTB-2R have no thermometer, so the hub
  reports no reading and the UI must not present the absence as a fault. Models
  that do measure (for example thermostats reporting via `Y02`) populate it
  normally. Do not "fix" a null by substituting the setpoint — that would show a
  number the hardware never measured.
