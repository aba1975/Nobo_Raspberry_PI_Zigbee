# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Raspberry Pi 4B (ARM64 / Ubuntu Server) deployment of [nobo-web-control](https://github.com/aba1975/nobo-web-control). The application code in `app/` is a direct copy from the source repo — identical Python + frontend.

## Where This Has Got To

Read this before offering to "add tests" or "verify" something. It is the
difference between what has been proven and what has only been reasoned about.

**Proven against real hardware.** The application has been connected to a real
Nobø Hub and read it correctly: zones, devices, week profiles, and non-ASCII
names. A mode change made in the official phone app arrived on the Pi
unprompted a few seconds later, with both connected at once. The connection
held for eighteen minutes with no drops.

**Never run against real hardware.** Everything in Phase 5 of
`docs/TEST_MATRIX.md`: **device discovery, pairing, and editing a week
profile**. Those paths are written from `API_Nobo.pdf` and from pynobo's
handling, and they are exercised only against `tests/fake_hub.py` — which
encodes the same reading of the specification the application does, so it
cannot disagree with it. Expect surprises there, and do not describe those
paths as verified.

**Proven only in demo mode.** Everything else, including the whole interface.

**The honest limit of the fake hub.** It answers each connection faithfully,
but nothing in a functional test counts open sockets or measures how long they
live. Two real bugs were found with `ss -tn` after the fake-hub suite passed —
see rule 4 under "Talking to a Real Hub". Anything about *how many* connections
exist needs real sockets.

`docs/TEST_MATRIX.md` is the checklist for closing that gap. It is ordered so
the read-only checks come first, and every step that changes something says how
to undo it. Test 4.2 — standing next to the heater — is the only one that
proves the hub reached the hardware; everything else proves a message reached
the hub.

## Architecture

- **Backend:** FastAPI (Python 3.12) using `pynobo` for TCP communication with the Nobo Eco Hub
- **Frontend:** Two interfaces sharing one backend (`app/static/`), with live updates over a WebSocket
- **Deployment:** Docker container with `network_mode: host` (required for LAN hub access)
- **Auto-start:** systemd service (`deploy/systemd/nobo-control.service`)
- **Configuration:** `.env` — see `.env.example` for the full list. Hub (`NOBO_SERIAL`, `NOBO_IP`, `NOBO_DEMO`), interface (`NOBO_UI`), binding (`NOBO_BIND`, `NOBO_PORT`) and optional HTTPS (`NOBO_DOMAIN`, `COMPOSE_PROFILES`)
- **Data persistence:** Docker named volumes — `nobo-data` at `/app/data` (accounts, schedules, demo state, `site.json`, `notifications.json`) and `caddy-data` (TLS certificates and, with the internal CA, its private root)

## Key Files

- `app/server.py` — the FastAPI application (~4,300 lines), every API endpoint, and both interfaces' HTML for the sign-in page
- `app/auth.py` — session auth with bcrypt. Five failed attempts lock a username for 60s, which is sized for a LAN and thin for the internet
- `app/away_schedule.py` — the scheduled away window. `away_schedule_loop()` is the only thing that writes to the hub unprompted, and both its paths require `enabled: true`
- `app/config_persistence.py` — atomic JSON persistence: demo zones and schedules, hub config, zone icons, away exceptions, site identity
- `app/notifications.py` / `app/notify_watch.py` — optional email alerts. Read the module docstring before extending: it documents what the hub genuinely cannot report
- `app/static/ui/cabin/` — the production interface. `app/static/index.html` + `app.js` — the classic one, still reachable at `/classic`
- `app/static/ui/shared/core.js` — the API client and all date/temperature formatting, shared by both
- `tls/` — Caddy for optional HTTPS. Two Caddyfiles: the internal CA (default) and Let's Encrypt over DNS-01
- `Dockerfile` — Python 3.12-slim, ARM64. `compose.yml` — the app, plus Caddy behind the `tls` profile

## Build and Run

```bash
docker compose up --build -d         # build and start
docker compose logs -f               # follow logs
docker compose down                  # stop
```

With HTTPS enabled, `COMPOSE_PROFILES=tls` in `.env` makes those same commands
include the proxy. Do not rely on `--profile tls` on the command line: the
systemd unit does not pass it, so a reboot would start the application alone —
and with `NOBO_BIND=127.0.0.1` that leaves nothing answering the network.

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

`tests/test_datetime_format.py` runs the real browser code in `node`, so it
needs node on the path. Without it those tests **skip** rather than fail, which
is easy to miss — see the container command in the README for how to include
it.

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
  `display_name` stands alone ("Lakeside", default `Cabin`) and `inline_name`
  goes mid-sentence ("all of the cabin", default `the cabin`). Write new strings
  to *take* a name rather than to *be* one, or the unnamed default reads as
  "Warm all of Cabin?". `site_settings()` resolves both; never re-derive them.
- The name reaches `/login`, which is public, so it is escaped with
  `html.escape` and gated behind `show_on_login`. A user may reasonably name the
  system after their street address; that must not be readable to anyone who can
  reach the Pi unless they chose it
- **The clock is 24-hour and the unit is Celsius, everywhere, and neither is a
  setting.** The hub's week profiles are `HHMM` and its handshake is
  `yyyyMMddHHmmss`; `API_Nobo.pdf` p11 says "temperatures are in celsius". Only
  the *date* format is configurable (`site.json` → `locale`), and it is applied
  through `Intl` in `core.js` with `hourCycle: 'h23'` forced — so a locale that
  would normally use AM/PM still renders 24-hour. Never format a date by hand
  with a table of month names; that is what this replaced.

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
4. **Never assign to `hub` without stopping what was there, and never clear it
   from a failure.** Connection attempts are serialised by `hub_connect_lock`,
   skipped entirely when a healthy client is already installed, and any client
   they displace is passed to `stop_hub_client()`. Before that, a configuration
   change and the reconnect loop could each start an attempt in the same
   five-second window; both succeeded, the second won, and the first was left
   holding a socket with its keep-alive still running — so the hub never timed
   it out either. With only two LAN slots, two orphans lock the user out of
   their own heating, and the handshake has no "busy" reject code to explain
   why. Found with `ss -tn`, not by a test; `tests/test_connection_leak.py`
   covers it now.

   The mirror image is just as bad: a *failed* attempt must not clear `hub`,
   `hub_tap` or `hub_connected` if something else has connected since. An
   attempt against an unreachable address sits in a TCP timeout for up to
   thirty seconds, and if a working connection is made beside it, the eventual
   failure used to disconnect a hub it never owned. The generation guard cannot
   catch this — generation only moves when the *configuration* changes, and
   here it has not — so the handler checks `hub is None` as well.

Also worth knowing:

- pynobo has no handling for `Y00`/`Y01`/`Y03`/`Y04`, so device search and
  pairing go entirely through `HubProtocolTap`.- Names travel with U+00A0 instead of spaces. pynobo encodes on write but does
  not decode on read; `decode_hub_name()` / `encode_hub_name()` handle both.
- Week profile states are `0=Comfort, 1=Eco, 2=Away, 4=Off`. `API_Nobo.pdf`
  page 6 says "3: Off" and is wrong — pynobo's `validate_week_profile` accepts
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

## Working on a Running Pi

Things that cost time to rediscover, in the order they usually bite.

**A code change needs a rebuild.** `COPY app/ .` bakes the interface into the
image, so restarting the container serves the old assets. Always
`sudo bash scripts/update.sh`, never just `systemctl restart`.

**Check which branch the Pi is on before trusting an update.** `git pull` on a
Pi left on an old branch updates that branch and reports success. `update.sh`
warns about this now; `git status -sb` confirms it.

**Calling the API by hand:**

| | |
| --- | --- |
| `POST /auth/login` | **form-encoded**, not JSON |
| `POST /api/zones/{id}/temperature` | body is `{"comfort": N, "eco": N}` — *not* `comfort_temp`/`eco_temp`, which returns 400 |
| `POST /api/hub/config` | deliberately signs you out; log in again after a few seconds |
| `GET /api/log` | in-memory, so **empty after every restart**. Not a bug |
| everything else | needs the session cookie; only `/login`, `/auth/login`, `/favicon.ico`, `/api/health` and the two sign-in icons are public |

**Demo mode rounds temperatures to whole degrees**, so a test that sets 23.5
and reads back 24 has not found a bug.

**Static assets are served with `Cache-Control: no-cache`** by
`RevalidatingStaticFiles`. Plain `StaticFiles` sends `ETag` but no
`Cache-Control`, so browsers applied heuristic caching and shipped new HTML with
stale JavaScript — buttons that did nothing. `no-cache` means "revalidate", not
"do not store"; the ETag still makes it a 304.

**The sign-in page's own artwork must stay public.** It is displayed to somebody
who has not logged in, so anything it references has to be in
`PUBLIC_ASSET_PATHS` or the browser gets a 302 to `/login` where it expected an
image — and draws a letter from the hostname instead.

## If You Are Picking This Up Fresh

Likely next task: **`docs/TEST_MATRIX.md` against real hardware**, Phase 5
especially. Ask which Pi is being tested and get its address — there are
usually two, and they serve identical-looking pages, so a test that "passed" on
the wrong one is worse than a test not run. Giving them different names under
Settings makes the header say which is which.

Before starting, confirm rather than assume:

```bash
git rev-parse --abbrev-ref HEAD && git log --oneline -1   # is this Pi current?
curl -s localhost:8000/api/health                         # or https://<name>/api/health
```

The full suite, with node so the browser-code tests actually run rather than
skip, is in the README under Testing.
