# Nobo Web Control for Raspberry Pi

A containerized deployment of [nobo-web-control](https://github.com/aba1975/nobo-web-control) for Raspberry Pi 4B running Ubuntu Server. Provides local web-based control of your Nobo Energy Hub heating system.

It began as a port of a Windows project and has since grown well past it. It has
been commissioned against real hardware — a live Nobø Eco Hub running 7 zones and
11 heaters — and the behaviour described here is what that hub actually does, not
what the protocol documentation implies. Where the two disagree, this README says
so.

## Try it first, without a hub

You do not need a Nobø hub to see what this is. With no configuration at all the
application starts in **demo mode**, against a simulated house of 8 zones and 11
heaters:

```bash
git clone https://github.com/aba1975/Nobo_Raspberry_PI.git
cd Nobo_Raspberry_PI
docker compose up -d          # no .env needed — the default serial means demo
```

The first run builds the image, which takes a couple of minutes on a Pi and less
on a laptop. Then open `http://localhost:8000` and log in with `admin` /
`nobohub`.

Everything works: zones, both interfaces, schedules, away periods, the lot.
Nothing reaches a real heater. The demo house is deliberately mixed — some
heaters can be adjusted remotely and some only by a dial on the wall — so the
awkward cases are visible rather than hidden.

Two things to know:

- **Run this on Linux** (a Pi, a laptop, a VM). Both containers use
  `network_mode: host`, which Docker Desktop on macOS and Windows does not
  implement the same way, so `localhost:8000` will not answer there.
- **Demo mode is on because the serial defaults to `111111111111`.** Give it a
  real serial later and it connects to a real hub. There is no separate "demo
  build".

`docker compose down -v` removes it again, volumes and all.

When you are ready for the real thing, start at [Prerequisites](#prerequisites).

## Contents

- [What This Project Does](#what-this-project-does) · [Features](#features)
- **Installing:** [Prerequisites](#prerequisites) · [1 Prepare the Pi](#step-1-prepare-the-raspberry-pi) · [2 SSH](#step-2-enable-and-use-ssh) · [3 Docker](#step-3-install-docker-and-docker-compose) · [4 Clone](#step-4-clone-the-repository) · [5 Configure](#step-5-configure-environment-variables) · [6 Start](#step-6-start-the-system) · [7 Start on reboot](#step-7-make-it-start-on-reboot) · [8 Verify](#step-8-verify-it-is-working)
- **Living with it:** [User accounts](#managing-user-accounts) · [Naming your system](#naming-your-system) · [Dates, times and temperature](#dates-times-and-temperature) · [Hub settings](#changing-hub-settings-from-the-web-interface) · [Alongside the official app](#using-this-alongside-the-official-app) · [Choosing the interface](#choosing-the-interface)
- **Running it:** [HTTPS](#https-on-your-own-network) · [Updating](#updating-the-software) · [Backups](#backing-up-configuration-and-data) · [Ports](#ports) · [Timezone](#timezone) · [Security notes](#security-notes)
- **When something is wrong:** [Troubleshooting](#troubleshooting) · [API](#api) · [Testing](#testing) · [Project structure](#project-structure) · [Reference documents](#reference-documents)

## What This Project Does

- Controls your Nobo heating system through a web interface on your local network
- Shows real-time temperatures and zone status via WebSocket
- Supports all Nobo device types (NTB-2R, R80 RDC 700, and 20+ others)
- Provides comfort, eco, and away modes per zone or globally
- Includes weekly schedule editing and scheduled away mode
- Runs 24/7 on your Raspberry Pi as an always-on home server
- Works entirely on your local network — no cloud required

> **You can keep using the official Nobø app.** The hub accepts **two** LAN
> connections at once (plus up to ten over the Internet), and pushes every
> change to all of them, so the Pi and the app stay in sync. See
> [Using this alongside the official app](#using-this-alongside-the-official-app).

## Features

Everything below is reached from the web interface at `http://<pi-ip>:8000`.

### Heating control

| Feature | What it does |
| --- | --- |
| **Zone overview** | Every zone with its current temperature, comfort and eco set points, and the mode it is in right now. Updates by itself — you never need to refresh. |
| **Per-zone override** | Put a single zone into Comfort, Eco or Away, or return it to Normal so it follows its weekly schedule again. |
| **Global mode** | Put the whole house into Comfort, Eco, Away or Home in one click. Zones that are set to follow it are released from any mode they were holding, so the instruction actually reaches them — see [Zones that follow the whole house, and zones that do not](#zones-that-follow-the-whole-house-and-zones-that-do-not). |
| **Temperature set points** | Set the comfort and eco temperature per zone, between 7 °C and 30 °C. Both are adjustable from the zone screen whatever mode the zone is in, so changing the eco temperature never means switching the zone to Eco and remembering to switch it back. The eco temperature must be lower than the comfort temperature, and values are whole degrees because that is all the hub stores — the +/− buttons step by 1 °C. |
| **Weekly schedule** | A named schedule of which mode applies at which time on each day (see [Weekly schedule rules](#weekly-schedule-rules)). Schedules are shared objects: several zones can follow one, and Settings lists them with the zones that use each. |
| **Scheduled away** | Set a holiday period. The house goes to Away when it starts and back to Home when it ends. |
| **Rooms that must not get cold** | Nobø's Away is a fixed 7 °C anti-frost temperature and cannot be raised. Nominate the zones that should hold their **Eco** temperature instead — a bathroom with pipes in the wall, a workshop — and they stay on Eco whenever the rest of the house goes Away, whether you pressed Away or an away period started on its own. See [Rooms that must not get cold](#rooms-that-must-not-get-cold). |
| **Changed outside this app** | A Nobø thermostat with a dial rewrites the hub's set point when somebody turns it, and the hub keeps no record of the old value. The app remembers what it set, flags a zone whose temperature no longer matches, and offers to restore it or accept the new value. A global mode change restores the intended set points first. See [When somebody turns a dial](#when-somebody-turns-a-dial). |
| **Follow Home and Away** | Per zone, and stored on the hub itself, so the Nobø app shows the same setting. On, the whole-house buttons apply to the zone. Off, the zone is independent and keeps whatever it is on. A zone that will not follow the house is marked on the front page, because a room quietly holding Eco while the house says Home is how pipes freeze. See [Zones that follow the whole house, and zones that do not](#zones-that-follow-the-whole-house-and-zones-that-do-not). |
| **Zones** | Add, rename, re-icon and delete zones. |
| **Devices** | Add, rename, move, replace and remove devices. With a real hub, the hub can also search for a device in pairing mode so you do not have to read its serial number off the back. |
| **Command log** | A running list of what was sent to the hub and what came back, which is the first place to look when something behaves unexpectedly. |
| **Alerts by email** | Optional, and off by default. Can tell you when the hub goes offline and when settings are changed from another app. It cannot see a cold room or a heater without power — see [Alerts](#alerts) for what the hardware does and does not report. |

Some devices — plain on/off receivers such as the R80 RSC 700 — have no
adjustable set point. Their temperature is set on the device itself, and the
interface says so rather than pretending the change worked.

### Accounts

| Feature | What it does |
| --- | --- |
| **Login** | The whole interface and the entire API require a login. Nothing is readable without one. |
| **Change password** | Under the 👤 icon. |
| **Rename your account** | Under the 👤 icon. |
| **Manage users** | Admins can add, remove and change the role of other users. |
| **Lockout** | Repeated failed logins from the same address are temporarily blocked. |

### Administration

| Feature | What it does |
| --- | --- |
| **Name your system** | Call it what you call the place — "The Lodge", "Main Street 12" — and the whole app follows, sign-in page included. See [Naming Your System](#naming-your-system). |
| **Hub settings in the browser** | Switch between demo mode and your real hub, and set the hub serial and IP, without editing files or using SSH. See [Changing Hub Settings From the Web Interface](#changing-hub-settings-from-the-web-interface). |
| **Demo mode** | A full simulated house with eight zones, so you can try everything before a hub is connected. |
| **Automatic start** | Starts on boot and restarts by itself if it stops. |
| **Backup and restore** | A script that captures your settings and data. |

### What works with a real hub, and what does not

| Feature | Demo mode | Real hub |
| --- | --- | --- |
| View zones, temperatures and modes | ✅ | ✅ |
| Per-zone and global overrides | ✅ | ✅ |
| Change comfort / eco temperatures | ✅ | ✅ |
| Rename a zone | ✅ | ✅ |
| Follow / ignore the whole-house modes per zone | ✅ | ✅ |
| Scheduled away | ✅ | ✅ |
| View weekly schedules | ✅ | ✅ |
| Edit weekly schedules | ✅ | ✅ |
| Add or delete a zone | ✅ | ✅ |
| Add, remove, move, rename or replace a device | ✅ | ✅ |
| **Discover and pair a new device** | ❌ | ⚠️ hub only, model dependent |
| **Measured room temperature** | Only the SW4 room | Only if you own an SW4 |

**Everything on that list has now been run against a real hub**, on a house of
7 zones and 11 heaters. The exceptions are the two marked above.

**Discovery only finds devices that support it.** Autosearch hears devices in
pairing mode, and not every model has one — Nobø's manual states that the
R80 RDC 700 and R80 RXC 700 "must be registered manually". For those, typing the
12-digit code from the label is the documented method and needs nothing done at
the heater. Demo mode has no radio at all, so discovery is the one thing it
cannot offer.

**Room temperature is the exception worth knowing about.** Of the 25 device
models this software knows, only the **SW4** control panel has a thermometer.
The NTB-2R, the R80 RDC 700 and every other receiver control the temperature
without reporting it, so a blank room temperature is correct behaviour and not
a fault. It is not the setpoint, and the app will not substitute one. This
matters beyond cosmetics: it decides which [alerts](#alerts) can work.
**Confirmed on a real hub during commissioning:** every component reported
`tempsensor_for_zone_id = None` and the hub's temperature table was empty.

You do not have to remember this table. The application asks the server what it
can do (`GET /api/capabilities`) and greys out anything the current mode cannot
honour, with the reason as the tooltip. Nothing you can click will fail with a
"not implemented" error.

#### These used to be marked "does not work with a real hub"

That was true, but the stated reason was wrong, so it is worth correcting.

The hub was never the obstacle. The Nobø Eco Hub protocol has commands for all
of it: zones (`A00`, `U00`, `R00`), components (`A01`, `U01`, `R01`), week
profiles (`A02`, `U02`, `R02`) and pairing (`X00`, `X01`, `X03`). The gap was in
this application. It grew up around the built-in demo data, and the real-hub
half of those endpoints was never written — the code checked that the zone
existed and then stopped.

Two genuine bugs were found and fixed while filling the gap in, and both
affected the features that *did* claim to work with a real hub:

- **Hub commands were sent on the wrong event loop.** pynobo's older
  synchronous helpers create their task on whichever loop happens to be
  running. Called from inside a web request that is the *web server's* loop, not
  the loop that owns the hub's socket, and an asyncio stream may only be used
  from its own loop. Writes therefore failed silently or corrupted the
  connection. The hub client now owns a dedicated loop and every command is
  routed to it.
- **The hub connection was never closed on a mode switch.** `stop()` is a
  coroutine, and it was being called without being awaited, so it did nothing.
  Each switch between demo mode and a real hub leaked a connection.
- **Two connection attempts could race, and the loser was abandoned.** Changing
  the hub configuration starts a connection attempt; the reconnect loop, which
  wakes every five seconds, could start a second one before the first finished.
  Both succeeded, the second became the live one, and the first was left holding
  an open socket with its keep-alive still running — so the hub never timed it
  out either. Since the hub allows only two connections on the LAN, a couple of
  those would lock you out of your own heating with nothing to explain why.
  Found by counting sockets on a real hub, not by a test.

If real-hub control has been unreliable for you in the past, those are the
likely reason.

#### Now verified against real hardware

Earlier versions of this file said, correctly at the time, that none of the
real-hub code had ever run against a real Nobø Eco Hub. That is no longer true.

The application has been connected to a live hub and read it correctly: seven
zones, eleven devices across two models, and seven week profiles, with names
containing `æ`, `ø` and `å` decoded properly. A mode change made from the
official Nobø phone app appeared on the Pi a few seconds later without anyone
refreshing anything, which is the hub pushing to two clients at once exactly as
the specification describes. The connection held for eighteen minutes with no
drops, and the Pi wrote nothing to the hub throughout — every entry in the
command log was inbound.

That exercise found the connection race described above, which the fake hub
could not have caught: the fake answers each connection faithfully, but nothing
in a functional test counts how many sockets are open.

So the fake remains necessary and is still the place to start
(`tests/fake_hub.py`, driven by `tests/test_real_hub_endpoints.py`). It catches
this application's own mistakes — name encoding, week profile sharing, component
fields, error handling. What it cannot catch is a hub that behaves differently
from the specification, because the fake encodes the same reading of the
specification that the app does, or anything about how many connections exist
and how long they live.

Two areas are still unproven on real hardware, because both change the hub and
the verification above was deliberately read-only: **discovery and pairing of a
new device**, and **editing a week profile**. The reply shapes for those are
inferred from the protocol document and from pynobo's handling. If you exercise
them, the command log shows exactly what was sent and what came back.

`docs/TEST_MATRIX.md` is a checklist for working through the rest, in an order
that starts read-only and keeps every step reversible.

#### Things worth knowing about a real hub

- **Most heaters have no thermometer, so no room temperature is shown.**
  Receivers such as the R80 RDC 700 and the NTB-2R switch power to a heater and
  report nothing back, so the hub has no reading to give and the room shows a
  setpoint but no measured temperature. This is the hardware, not a fault, and
  not something an update can change. Models that do measure — thermostats that
  report a temperature to the hub — display it normally, and a house can mix
  both. The application deliberately shows nothing rather than showing the
  setpoint, because a number you cannot trust is worse than an honest blank.
- **Weekly schedules are shared objects.** A hub week profile can be used by
  several zones, and every zone starts out on the same factory profile. Editing
  it in place would silently reschedule the whole house, so the first time you
  save a schedule for a zone, that zone is given its own copy and the other
  zones keep what they had. The schedule editor tells you when this is about to
  happen. The factory profile is never overwritten or deleted.
- **A device's serial number cannot be changed.** "Replace" therefore pairs the
  new device first and only removes the old one once that has succeeded, so a
  failed replacement leaves the zone as it was.
- **A zone must be empty before it can be deleted.** The hub would otherwise
  leave its devices unassigned, which is harder to recover from than an error
  message, so the request is refused with a clear explanation instead.
- **Zone icons are stored on the Pi, not on the hub.** The hub has no icon
  field. They live in `data/zone_icons.json` and are included in a backup.
- **Names are stored with non-breaking spaces.** That is how the protocol
  encodes a space. The application converts in both directions, so what you
  type is what you see; if you read the hub with another tool you will see
  `\xa0` where you expected a space.

### Weekly schedule rules

A schedule is a **shared object on the hub**, not a property of one zone. Several
zones can follow the same one, which is the point: two bedrooms that want the
same week should share a schedule rather than have two that happen to match.
**Settings → Schedules** lists every schedule with the zones that use it, and
lets you add, edit, rename and delete them.

Because they are shared, editing matters in two different places:

- **From a zone's week** you are adjusting *that zone*. If the schedule is
  shared, the app asks whether to change just this zone — which gives it a copy
  of its own — or every zone using it. It also offers to save the edit as a new
  named schedule and point the zone at it.
- **From Settings** you picked a named schedule out of a list of schedules, so
  the edit changes the schedule, and every zone following it. The interface
  names those zones before saving.

The hub's own built-in schedules cannot be changed. It accepts the command and
silently ignores it, so the app refuses up front rather than reporting a success
that did not happen.

When you save a schedule, the whole week is sent at once and it must describe
every minute of every day:

- All seven days must be present.
- Each day's blocks must run from `00:00` to `24:00` with no gaps and no overlaps.
- Blocks must be in order, and each must be at least one minute long.
- Each block's mode is `comfort`, `eco`, `away` or `off`.
- Times are in the Raspberry Pi's own timezone, not UTC (see [Timezone](#timezone)).
- The hub stores times to the quarter hour; the editor snaps to it and says so.

**`off` is accepted but no longer offered.** It is valid in a week profile — the
hub stores it, and a schedule made in the official Nobø app may contain one — so
the app reads, writes and displays it. It is not offered as a choice, because in
a building left empty it means no frost protection at all, and Away's fixed 7 °C
already covers "as cold as is safe". A block that is already `off` still shows
as such and can be changed. There is no "off" override either, so
`POST /api/zones/{id}/override/off` is rejected.

A partial update is rejected rather than merged, so that a saved schedule is
never half old and half new. The editor in the web interface builds a valid
week for you; these rules matter if you call the API yourself.

### When somebody turns a dial

A Nobø thermostat with a dial does **not** create an override when somebody
turns it. It rewrites the zone's comfort or eco temperature on the hub outright,
permanently, and the hub keeps no record of what the value used to be. Confirmed
on real hardware: turning an NTB-2R moved `comfort` from 17.0 to 21.0 with no
override created, and Comfort, Eco and Schedule afterwards all left it at 21.0.

There is no override to cancel and nothing to read back, so this app remembers
instead. Every temperature it sets is written down as the intended value, and a
zone whose temperature no longer matches is flagged with two ways out:

- **Restore** — put back the temperature set here.
- **Keep** — accept the new value as the intended one.

Choosing a **global mode** — Comfort, Eco, Away, or letting the schedule run —
restores the intended set points first, because that is the owner saying "use my
settings". Otherwise a temperature dialled in on a wall silently becomes the one
the whole house uses.

Two things this deliberately does not do:

- **It does not claim where the change came from.** The official Nobø app
  rewrites the same field in the same way, so a dial and a phone are
  indistinguishable from here. The wording says only that it was not changed
  from this system.
- **It does not store a warning.** The difference is recomputed from the
  intended value and the hub's current value whenever anything asks, so a flag
  cannot be stranded by a missed message — and a dial turned while the Pi was
  switched off is still visible when it comes back.

The intended set points live in `data/intended_setpoints.json` and are included
in a backup.

### Zones that follow the whole house, and zones that do not

Pressing **Home**, **Comfort**, **Eco** or **Away** on the front page sends the
hub one *global* override. On the hub, a zone's own override always outranks
that global one. Both facts are useful — the away exception below depends on
them — but together they used to produce a genuinely dangerous outcome:

> A zone was put on Eco by hand. Later, Home was pressed. The global override was
> cancelled, the interface showed the house on Home, and that zone stayed on Eco.
> Nothing ever released it. In a cabin, a room silently holding Eco through a
> winter is how pipes freeze.

The hub already has the setting that resolves this. Each zone carries an
`override_allowed` flag — field 6 of its record, and the same checkbox the Nobø
app shows on a zone — which decides whether the global override reaches it. This
app calls it **Follow Home and Away** and puts it on the zone screen, under the
mode buttons.

- **On** (the hub's factory setting, and what a zone gets if the flag is
  missing). The whole-house buttons apply. Anything the zone was holding is
  released first, so the mode you asked for actually takes effect. This is the
  safe default: a zone that follows the house cannot be forgotten.
- **Off.** The zone is deliberately independent — a workshop, a cellar, a room
  with its own routine. It keeps whatever it is on and ignores the whole-house
  buttons entirely. Its weekly schedule still runs.

Because Off is a standing risk rather than a passing state, a zone with it turned
off is marked **Ignores Home/Away** in amber on the front page, and the zone
screen says what it is holding and that only that screen can change it. A zone
that is merely set by hand but still follows the house is marked **Set by hand**
in a quieter style, since the next whole-house mode will release it.

The flag lives on the hub, not here, so turning it off in this app turns it off
in the Nobø app too, and a change made there shows up here.

The scheduler behaves exactly like the buttons. An away period that starts while
nobody is in the cabin releases the same zones a pressed Away would, which is
precisely when an overlooked zone would otherwise go unnoticed for a fortnight.

### Rooms that must not get cold

**Away is 7 °C, and that is fixed.** It is an anti-frost setting decided by the
Nobø hub. It is not a set point, it is not exposed by the protocol, and no app —
this one, Nobø's own, or any other — can raise it. If a room needs to stay
warmer than 7 °C while you are away, Away is the wrong tool: **Eco** is the only
mode with a temperature you control that is still below Comfort.

So the app lets you nominate the rooms that should be held on Eco instead of
Away. Under **Settings → Rooms that must not get cold**, tick the zones
concerned. From then on:

- pressing **Away**, or an **away period** starting, sets every zone to Away as
  before, and then immediately puts the ticked zones back on **Eco**;
- the ticked zones stay on Eco for the whole trip;
- coming **Home** returns every zone, ticked or not, to its weekly schedule.

This works because a per-zone override outranks the global override on the hub.

Two things worth knowing:

- The list is applied **on the Raspberry Pi, not in your browser.** An away
  period starts in a background loop, which is usually running with nobody
  logged in — so the exception has to live on the server to be worth anything.
- It only affects **global** Away. Setting one room to Away by hand is treated
  as a deliberate choice about that room and is left alone.

The list is stored in `data/away_exceptions.json` and is included in a backup.
If a zone in the list is deleted, it is ignored rather than causing an error,
and `GET /api/global-mode/away-exceptions` reports it under `unknown_zone_ids`.

### Alerts

Optional email alerts. **Everything here is off by default, including the
feature itself**, and that is a deliberate judgement rather than caution: the
Nobø hub reports so little about individual heaters that the honest set of
alerts is small, and none of it is urgent enough to mail somebody unasked. It is
kept because it costs nothing to keep and somebody may want it.

Read [What it can and cannot know](#what-it-can-and-cannot-know) **before**
relying on this for anything. In particular, there is no frost alarm, and there
cannot be one.

Turn it on under **Settings → Telling you when something is wrong**. The
settings stay hidden until you press the switch, and nothing is sent until you
press **Save alerts**. Use **Send a test email** first — if the mail server
refuses, you get its actual words back, because "authentication failed" and "no
such host" need different fixes.

| Event | What it means |
| --- | --- |
| **Hub goes offline** | The Pi has lost contact with the hub — hub power, or the network. Noticed within about half a minute. **The only fault this hardware genuinely reports.** |
| **Hub comes back** | Sent after an offline alert, so you know it fixed itself. |
| **Something changed from another app** | A zone changed and it was not this system that did it. |
| **An away period starts or ends** | Confirms a planned trip actually took effect. |

Each condition speaks **once** when it starts and once when it clears, never
repeatedly while it persists. **Quiet hours** holds back routine news overnight
but never something urgent.

Several more alerts were built and then removed, which is worth recording so
nobody rebuilds them:

| Removed | Why |
| --- | --- |
| A room is too cold | Needs a room temperature. Nothing reports one. |
| A room cannot get warm | Same. |
| A thermostat stops reporting | Same. |
| A heater has been running for X hours | The hub is never told whether an element draws power. |
| A regular "still here" email | Removed as not worth reading. |
| A room is left switched off | Removed as not worth reading. |
| A weekly schedule event starts | Removed as not worth reading. |

The first four could never fire on this hardware; the last three could, but an
alert nobody wants to read is not harmless — it teaches people to ignore the
ones that matter.

#### What it can and cannot know

The limits here are the hub's, and no amount of software removes them. They are
worth stating plainly, because the alternative is a feature that looks like
protection and is not.

**Is there a keep-alive?** Yes — but only in one place.

| Link | Keep-alive | So we can tell if… |
| --- | --- | --- |
| Pi ↔ hub (network) | **Yes, both ways.** The Pi sends `HANDSHAKE` every 14 s and the hub echoes it. Nothing back within 28 s and the link is declared dead. The hub also UDP-broadcasts every 2 s. | …the **hub** loses power or the network drops. **Within about 30 seconds.** |
| Hub ↔ heater (radio) | **No liveness reporting of any kind.** | …nothing. A **heater** switched off at the wall or without power is **invisible**. |

The second row deserves precision, because it is the difference between "no
heartbeat" and "no way to see a heartbeat", and only one of those is provable
from the specification.

The hub is **not** deaf. `X00` starts a *receiver search*, and the hub answers
with a `Y04` for every component it hears. So the radio hardware can receive,
and devices do transmit — during pairing.

What does not exist is any way for that to reach this application during normal
operation:

- The component struct's `Status` field is *"not yet implemented — always set to
  0"*, so the one field that could carry health never does.
- `Active override Id` on a component is likewise *"not yet implemented — always
  set to -1"*, and component-level overrides are *"not yet supported"*.
- There is **no command to ask** whether a component is alive, and **no
  unsolicited message** that reports one has gone away. The words "not
  responding", "offline", "unreachable" and "battery" do not appear anywhere in
  the specification. The only component messages the hub pushes are database
  events — added, updated, removed — plus `Y02` temperature and `Y04` search
  results.

So whether a paired NTB-2R quietly beacons on the radio is undocumented and
cannot be determined from here. It also does not matter: **there is no channel
through which the hub could tell the Pi**, so a heater that has lost power
cannot be reported, and no amount of work on this end changes that.

**Nothing measures room temperature.** Of the 25 device models this software
knows, only the SW4 control panel has a thermometer, and it is no longer sold.
The NTB-2R, the R80 RDC 700 and every other receiver control temperature without
ever reporting it. A blank room temperature is therefore correct, not a fault.

Three alerts — a cold-room alarm, a silent-thermostat alarm and a
cannot-get-warm alarm — were built and then **removed** for this reason. They
were not left switched off or hidden behind a setting, because an alert that
cannot fire is worse than none at all: the owner believes the cabin is watched
when nothing is watching it.

**"Warn me if a heater runs constantly for X hours"** cannot be done either, and
for the same reason: the hub is never told whether an element is drawing power.
The thermostat that cycles the heater is inside the device, and it reports
nothing.

**Who changed a temperature** is answered *by elimination*. The Nobø override
struct is `<Id> <Mode> <Type> <End> <Start> <Target> <TargetID>` — no field for
the source. The server records every write it makes, and anything arriving
without a matching record came from somewhere else. The alert says "it was not
changed from here" and stops; it cannot tell the official Nobø app from another
browser, and does not pretend to.

#### What is left, and why it is worth having

Given all of the above, what remains is modest and it is better to say so.

**The hub going offline** is the one genuine fault this hardware reports, and it
does so within about half a minute. If the hub loses power or the network drops
while you are away, you will know.

**Changes made from another app** are worth knowing about if more than one
person has access, and an **away period starting or ending** confirms a planned
trip actually took effect.

That is the honest list. It will not tell you a pipe is freezing.

**And there is no good workaround on this hardware.** A smart plug with a local
API would give the "running constantly" and "lost power" signals — but only for
a heater you can put a plug in front of. A floor-heating thermostat wired into
the wall by an electrician has no plug to intercept, so that route is closed
too. Anything better would mean an independent sensor on its own network, which
is a different product rather than a change to this one.

#### Settings

`data/notifications.json`, included in a backup. The mail password is stored in
it and the file is written `0600`. **It is never returned by the API**: reading
the settings gives you `password_set: true` instead, so it cannot leak through a
screenshot, a proxy log or a support bundle. Leave the password box empty when
saving to keep the one already stored.

When alerts are off, the settings are hidden — there is nothing to configure
until you want them. Turning the switch on reveals the form; nothing is sent
until you press **Save alerts**, because the server refuses to enable a
configuration that could not deliver.

All three notification endpoints are **admin only** — the settings decide where
alerts about your building are sent, and who can silence them.

A mail server that is down, slow or misconfigured can never affect the heating:
sending happens on a worker thread with a timeout, and every failure is logged
and swallowed rather than raised.

## Prerequisites

### Hardware

- Raspberry Pi 4B (4 GB or 8 GB RAM recommended)
- microSD card (16 GB or larger, Class 10 or better)
- Power supply (official USB-C 5V/3A recommended)
- Ethernet cable (recommended) or Wi-Fi connection
- Nobo Energy Hub on the same local network

### Software

- Ubuntu Server 24.04 LTS (ARM64) — installed on the Raspberry Pi
- Docker and Docker Compose (installed by the setup script, or manually)

### Information You Need

- **Hub serial number:** 12-digit number on the back of your Nobo Eco Hub (e.g., `123456789012`)
- **Hub IP address:** Found in your router's device list (e.g., `192.168.1.100`)

## Step 1: Prepare the Raspberry Pi

### Install Ubuntu Server on the microSD Card

1. Download the [Raspberry Pi Imager](https://www.raspberrypi.com/software/) on your computer
2. Insert your microSD card
3. Open Raspberry Pi Imager:
   - **Choose Device:** Raspberry Pi 4
   - **Choose OS:** Other general-purpose OS → Ubuntu → Ubuntu Server 24.04 LTS (64-bit)
   - **Choose Storage:** Your microSD card
4. Click the gear icon (settings) before writing:
   - Set a hostname (e.g., `nobo-pi`)
   - Enable SSH (use password authentication for now)
   - Set a username and password (e.g., `nobo` / choose a strong password)
   - Configure Wi-Fi if not using Ethernet
   - Set your locale and timezone
5. Click **Write** and wait for it to finish
6. Insert the microSD card into your Raspberry Pi and power it on
7. Wait 2-3 minutes for first boot to complete

## Step 2: Enable and Use SSH

SSH should already be enabled if you configured it in the Raspberry Pi Imager (Step 1). If not:

### Enable SSH (if needed)

Connect a keyboard and monitor to your Pi, log in, then:

```bash
sudo systemctl enable ssh
sudo systemctl start ssh
```

### Verify SSH Is Running

```bash
sudo systemctl status ssh
```

You should see `Active: active (running)`.

### Connect from Another Computer

First you need your Pi's IP address. Pick whichever method is easiest:

**Method 1 — try the hostname (no monitor needed).** If you set the hostname to `nobo-pi` in Step 1, try this from your computer:

```bash
ssh nobo@nobo-pi.local
```

If that works, you can skip the rest of this section.

**Method 2 — check your router.** Open your router's admin page in a browser and look at the list of connected devices. Find the one named after your hostname (e.g., `nobo-pi`) and note its IP address (e.g., `192.168.1.50`).

**Method 3 — read it on the Pi.** If you have a keyboard and monitor connected, log in and run:

```bash
hostname -I
```

The first number shown is your Pi's IP address.

Once you have the IP, connect from your computer (replace `192.168.1.50` with your Pi's actual IP):

**Linux / macOS Terminal:**
```bash
ssh nobo@192.168.1.50
```

**Windows (PowerShell or Command Prompt):**
```bash
ssh nobo@192.168.1.50
```

**Windows (PuTTY):** Enter the IP address, port 22, click Open, and log in.

Accept the fingerprint prompt the first time (`yes`), then enter the password you set in Step 1.

> **Tip:** Give your Pi a fixed (static) IP address in your router settings. Otherwise the IP may change after a reboot and your bookmark to the web interface will stop working.

### Optional: Use SSH Keys Instead of Passwords

On your computer, generate a key pair (if you don't have one):

```bash
ssh-keygen -t ed25519
```

Copy the public key to your Pi:

```bash
ssh-copy-id nobo@192.168.1.50
```

Now you can connect without typing a password.

### Optional: Harden SSH

Edit the SSH config on the Pi:

```bash
sudo nano /etc/ssh/sshd_config
```

Recommended changes (after setting up SSH keys):

```
PasswordAuthentication no
PermitRootLogin no
```

Apply changes:

```bash
sudo systemctl restart ssh
```

## Step 3: Install Docker and Docker Compose

> **Shortcut:** The install script in Step 6 (Option B) does all of Step 3 and Step 4 for you. If you plan to use it, you can skip straight to [Step 6](#step-6-start-the-system). The manual steps below are here so you know what the script does.

SSH into your Raspberry Pi, then run:

```bash
curl -fsSL https://get.docker.com | sudo sh
```

This takes a few minutes. Add your user to the docker group (so you don't need `sudo` for docker commands):

```bash
sudo usermod -aG docker $USER
```

**You must log out and back in for the group change to take effect:**

```bash
exit
```

Then SSH in again and verify Docker works:

```bash
docker --version
docker compose version
```

Both commands should print version information. If you instead see `permission denied while trying to connect to the Docker daemon socket`, you did not log out and back in — run `exit` and reconnect.

## Step 4: Clone the Repository

```bash
sudo git clone https://github.com/aba1975/Nobo_Raspberry_PI.git /opt/nobo-control
sudo chown -R $USER:$USER /opt/nobo-control
cd /opt/nobo-control
```

## Step 5: Configure Environment Variables

```bash
cp .env.example .env
nano .env
```

Edit the file with your hub details:

```
NOBO_SERIAL=123456789012
NOBO_IP=192.168.1.100
NOBO_DEMO=false
```

- `NOBO_SERIAL`: The 12-digit serial number from the back of your Nobo Eco Hub
- `NOBO_IP`: The IP address of your hub on your local network
- `NOBO_DEMO`: Set to `true` to test without a real hub (uses simulated data)
- `NOBO_ALLOW_ANON_API`: Leave this alone. It is explained under [Security Notes](#security-notes).
- `NOBO_UI`: Which interface you get at the usual address — `cabin` (default) or `classic`.
  See [Choosing the Interface](#choosing-the-interface).

**Two things that catch people out:**

1. `NOBO_DEMO` accepts `true`, `1` and `yes` (any capitalisation) as "on".
   Anything else, including an empty value, means off.
2. **The serial `111111111111` switches demo mode on by itself**, whatever
   `NOBO_DEMO` says. That value is also the built-in default, so if you skip
   creating `.env` entirely the system starts in demo mode and shows a
   simulated house rather than reporting an error. If you are seeing eight
   zones called "Large Bathroom", "Kitchen" and so on, this is why.

If the defaults are used because no `.env` exists, the hub IP defaults to
`10.0.0.100`. That is only a placeholder; it is not where your hub lives.

Save and exit (`Ctrl+O`, `Enter`, `Ctrl+X` in nano).

> **Tip:** To find your hub's IP address, check your router's admin page for connected devices. Look for a device named "Nobo Hub" or similar.

### Not ready to connect a real hub yet?

You can run the whole system in **demo mode** with simulated zones and temperatures. This is a good way to check the installation worked before touching your real heating system. Use these settings:

```
NOBO_SERIAL=123456789012
NOBO_IP=192.168.1.100
NOBO_DEMO=true
```

In demo mode the serial number and IP are ignored, so the placeholder values above are fine.

### Switching between demo mode and your real hub

You can switch at any time after installation. There are two ways to do it:

- **From the web interface (recommended)** — no SSH, no restart. See [Changing hub settings from the web interface](#changing-hub-settings-from-the-web-interface).
- **By editing `.env`** — the original method, described below.

#### Method 2: Editing the `.env` file

**1. Open the configuration file:**

```bash
sudo nano /opt/nobo-control/.env
```

**2. Change the values.**

To use **demo mode** (simulated zones, no hub needed):

```
NOBO_SERIAL=123456789012
NOBO_IP=192.168.1.100
NOBO_DEMO=true
```

To use your **real hub**:

```
NOBO_SERIAL=<your 12-digit serial>
NOBO_IP=<your hub's IP address>
NOBO_DEMO=false
```

**3. Save and exit** (`Ctrl+O`, `Enter`, `Ctrl+X`).

**4. Apply the change** — the setting is only read when the container starts, so you must restart:

```bash
sudo systemctl restart nobo-control
```

**5. Confirm which mode you are in:**

```bash
curl http://localhost:8000/api/status
```

Look at the `demo_mode` value in the response:

```json
{"connected":true,"demo_mode":true,"hub_serial":"123456789012",...}
```

- `"demo_mode":true` — running on simulated data
- `"demo_mode":false` and `"connected":true` — talking to your real hub
- `"connected":false` — check the serial number and IP, that the hub is powered on, and that no more than one *other* device is on the hub over the LAN (the hub allows two)

> **Note:** After switching modes, do a hard refresh in your browser (`Ctrl+Shift+R`, or `Cmd+Shift+R` on macOS) so it does not show cached data from the previous mode.

## Step 6: Start the System

### Option B: Using the Install Script (recommended)

The install script does everything for you: installs Docker, adds you to the docker group, clones or updates the code in `/opt/nobo-control`, creates `.env` if it is missing, installs the systemd service, and builds the Docker image.

```bash
sudo bash /opt/nobo-control/scripts/install.sh
```

The first run takes roughly 5-10 minutes, most of it building the Docker image.

> The script **enables** the service, meaning it will come up on every boot, but
> it deliberately does not start it there and then — you have not had a chance
> to put your hub details in `.env` yet. Nothing is running until you do this:

```bash
sudo systemctl start nobo-control
```

> **Note:** If the script installed Docker for the first time, log out (`exit`) and SSH back in before running `docker` commands yourself. Otherwise you will get `permission denied` on the Docker socket.

### Option A: Quick Start (manual)

Only use this if you skipped the install script. It starts the container but does **not** set it to start automatically on boot — you still need Step 7.

```bash
cd /opt/nobo-control
docker compose up --build -d
```

## Step 7: Make It Start on Reboot

If you used the install script (Option B above), the service is already enabled. Otherwise:

```bash
sudo cp /opt/nobo-control/deploy/systemd/nobo-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable nobo-control
sudo systemctl start nobo-control
```

Verify the service is running:

```bash
sudo systemctl status nobo-control
```

You should see `Active: active (running)`.

These commands explained:
- `systemctl enable` — tells the system to start this service automatically on every boot
- `systemctl start` — starts it right now
- `systemctl status` — shows whether it is running

#### Why there are two "restart automatically" settings

`compose.yml` says `restart: always` and the systemd unit says
`Restart=always`. That looks like a conflict but is not, and you do not need to
change either:

- **Docker** brings the container back if the application inside it crashes.
- **systemd** brings the whole thing back if Docker itself, or the machine,
  went away.

Stopping the service runs `docker compose down`, which removes the container
altogether, and Docker never resurrects a container that no longer exists. So
`sudo systemctl stop nobo-control` really does stop it and it stays stopped.

The unit also runs `docker compose pull` before starting. Since the image is
built on the Pi rather than downloaded, this normally logs "Skipped - No image
to be pulled" and moves on. It is prefixed with `-` so a failure (for example
with no internet connection) cannot stop the service from starting.

### Confirm it really survives a reboot

It is worth testing this once, so you are not surprised after a power cut:

```bash
sudo reboot
```

Your SSH session will disconnect. Wait about 1-2 minutes, then SSH back in and check:

```bash
systemctl is-active nobo-control
docker ps
```

You should see `active`, and a `nobo-web-control` container with status `Up ... (healthy)`. The container normally becomes healthy within about 30 seconds of boot.

## Step 8: Verify It Is Working

### Check the service status

```bash
sudo systemctl status nobo-control
```

### Check the container is healthy

```bash
docker ps
```

Look for the `nobo-web-control` container with status `healthy`. It can take up to 30 seconds after starting before it changes from `health: starting` to `healthy`.

### Check the API responds

Run this on the Pi itself:

```bash
curl http://localhost:8000/api/health
```

You should get a response like:

```json
{"status":"ok","connected":true,"demo_mode":true,"timestamp":"..."}
```

`connected: true` means the app is talking to your hub (or to the simulated hub if `demo_mode` is `true`).

### Open the web interface

From any device on your local network, open a browser and go to:

```
http://<YOUR_PI_IP>:8000
```

Replace `<YOUR_PI_IP>` with your Raspberry Pi's IP address (e.g., `http://192.168.1.50:8000`).

### Default login credentials

| Username | Password |
|----------|----------|
| `admin`  | `nobohub` |

**Change the default password immediately** after first login — see [Managing User Accounts](#managing-user-accounts) below.

### Check logs

```bash
bash /opt/nobo-control/scripts/logs.sh
```

No `sudo` is needed here, as long as your user is in the `docker` group (the
install script puts you there). You can also ask for a different number of
past lines — the default is 50:

```bash
bash /opt/nobo-control/scripts/logs.sh 200
```

Or directly:

```bash
cd /opt/nobo-control && docker compose logs --tail 50 -f
```

Press `Ctrl+C` to stop following logs.

## Managing User Accounts

Everything to do with accounts lives behind the **user icon (👤) in the top-right corner** of the web interface. Click it to open the **User Settings** panel. Close it with the ✕ button, the `Esc` key, or by clicking outside the panel.

The panel has these sections:

### 🔑 Change Password

Available to every user, for their own account. Enter your current password, then the new one twice. Passwords must be at least 8 characters.

**Do this first**, to replace the default `admin` / `nobohub` credentials.

### ✏️ Rename Account

Change your own username. You stay logged in afterwards.

### 🛠️ Manage Users (admins only)

This section is only shown to users with the `admin` role. It lists every account and lets you:

- **Add a user** — enter a username and password (minimum 8 characters) and pick a role:
  - **`user`** — can view and control heating.
  - **`admin`** — can additionally manage accounts and change the hub connection settings.
- **Delete a user** — click the 🗑️ button next to them. You cannot delete your own account, so there is always at least one admin left.

### 🚪 Logout

Ends your session and returns you to the login page.

> **Note:** User accounts are stored in the Docker data volume (`data/users.json`), with passwords hashed using bcrypt. They survive restarts, software updates and reboots, and are included in `scripts/backup.sh`.

## Naming Your System

Out of the box the app calls itself **Cabin**. That is the name of the
*interface*, not of anybody's house, so you can change it to whatever you call
the place: a nickname like `Lakeside`, a street address like `Main Street 12`,
or something plain like `The flat`.

### How to do it

1. Log in as an **admin** user.
2. Open **Settings** (the ⚙ icon).
3. The first card is **What this place is called**. Type the name and press
   **Save name**.

The change takes effect immediately, everywhere:

| Where | Before | After |
| --- | --- | --- |
| Header | Cabin | Lakeside |
| Sign-in page | Heating control for the cabin. | Heating control for Lakeside. |
| Trip card | Cabin | Lakeside |
| Whole-house modes | All of the cabin | All of Lakeside |
| Confirmations | Warm all of the cabin? | Warm all of Lakeside? |
| Browser tab | Cabin - Nobø Control | Lakeside - Nobø Control |
| Home-screen icon | Cabin | Lakeside |

Leave the field empty to go back to **Cabin**.

### A note on the sign-in page

The sign-in page is served to **anyone who can reach the Pi**, before any
password is asked for. A nickname there is harmless. A street address is your
address, given to whoever is on the network — and to the whole internet if you
have forwarded a port to this machine.

So there is a checkbox, **Show it on the sign-in page**, next to the name. It is
on by default, because a name nobody chose to hide is a name they wanted shown.
Turn it off and the sign-in page goes back to the generic wording while the rest
of the app still uses your name.

This is worth thinking about for about five seconds and then forgetting: if you
are naming it `Lakeside`, leave it on.

### Notes

- **Only an admin can change it.** It changes what every user of the
  installation sees, including on a page shown before sign-in, so it is not left
  open to an ordinary account.
- **The hub does not know about it.** The name lives on the Pi, in
  `data/site.json`, and is included in `scripts/backup.sh`. Nothing is sent to
  the hub, and the official Nobø app is unaffected.
- **It does not rename the interface.** The Cabin interface is still called
  Cabin and is still at `/cabin`; the rollback instructions are unchanged.
- **Names are limited to 40 characters** and to a single line, because the name
  goes in page titles and headings. Anything longer is trimmed rather than
  refused.
- Non-ASCII is fine — `æ`, `ø` and `å` are stored and displayed as typed.

## Dates, Times and Temperature

Nobø is sold mainly in Norway and the rest of the Nordics, so the defaults lean
that way — but the format is yours to choose.

### Date format

**Settings → Date format.** It decides how dates are written and in what
language the days of the week appear:

| Setting | A date reads |
| --- | --- |
| Norsk (bokmål) | `søn. 30. aug., 18:00` |
| Svenska | `sön 30 aug. 18:00` |
| Suomi | `su 30.8. klo 18.00` |
| English (UK) | `Sun 30 Aug, 18:00` |
| Follow each browser | whatever each device is set to |

It is stored **on the system, not per browser**, so the tablet in the hall and
a phone in the kitchen show the same thing. Any
[BCP 47](https://en.wikipedia.org/wiki/IETF_language_tag) tag is accepted, not
just the ones in the list — the list is there to save you looking one up.

This changes the *format*, not the language of the interface. Buttons and
explanations are in English throughout.

### The clock is always 24-hour

Not a setting, and deliberately so. The hub's own weekly schedules are `HHMM`
strings and its handshake timestamp is `yyyyMMddHHmmss`, so 24-hour is the
protocol's format rather than a preference. Offering a 12-hour display would
invent an ambiguity the system does not have — and misreading `7:30` on a
heating schedule is the kind of mistake that costs a frozen pipe.

Times you type are 24-hour too. `<input type="time">` sends `HH:MM` whatever
your browser draws around it, so a phone showing an AM/PM picker still stores
the right value.

### Temperatures are always Celsius

Also not a setting. `API_Nobo.pdf` states it plainly, in the section describing
the zone structure:

> Temperatures are in celsius. — page 11

and again for each setpoint: *"Comfort temperature. Degrees Celsius. Expected
to be an integer in the range 7 to 30."*

There is no other unit to ask the hub for. A Fahrenheit option would mean
either sending a number the hub reads as Celsius, or converting on the way in
and out — one more thing to get wrong, for hardware that cannot use it. Nobø
heaters are not sold with Fahrenheit dials.

## Changing Hub Settings From the Web Interface

Once the system is running you no longer need SSH to switch between demo mode and your real Nobø Hub. You can do it from the browser.

### How to do it

1. Log in to the web interface at `http://<your-pi-ip>:8000` as an **admin** user.
2. Click the **cogwheel (⚙) / Devices** icon in the top bar.
3. At the top of the page you will see the **Hub Connection** card, showing which mode you are currently in.
4. Choose one of the two options:
   - **Demo mode** — simulated zones and temperatures, no hub required. Useful for testing.
   - **Connect to a real Nobø Hub** — then fill in:
     - **Hub serial number** — the 12 digits from the sticker on the bottom of the hub. Spaces are allowed, so `210 000 016 247` and `210000016247` both work.
     - **Hub IP address** — the hub's address on your network, e.g. `192.168.1.42`.
5. Click **Save Hub Settings**.
6. A confirmation box appears summarising exactly what will change. Click **Apply Change** to go ahead, or **Cancel** to go back.
7. **You are signed out and returned to the login page.** Log in again — the app reloads with the new settings.

The change is applied **immediately** on the server. **You do not need to restart the container or reboot the Pi.**

### Why does it log me out?

Switching between demo mode and a real hub replaces every zone, device and schedule in the system at once. Rather than leave half-updated pages open in your browser, the app deliberately ends your session so the next login starts from a clean, consistent state. This is expected behaviour, not an error.

### Good to know

- **Admin only.** Regular (non-admin) users cannot change the hub settings, and the card is not shown to them.
- **The setting is remembered.** It is written to `data/hub_config.json` inside the Docker data volume, so it survives container restarts, `docker compose down/up`, software updates, and a full Pi reboot. After a reboot the systemd service starts automatically in whichever mode you last selected.
- **It overrides `.env`.** If you have set the hub from the web interface, that value wins over `NOBO_DEMO`, `NOBO_SERIAL` and `NOBO_IP` in `.env`. The `.env` values are only used until the first time you save settings from the web interface. The Hub Connection card tells you which one is currently in effect (`environment` or `web interface`).
- **If the hub cannot be reached**, the settings are still saved and you get a warning message before being signed out. The app keeps retrying in the background, so once the hub becomes reachable it will connect on its own. Check that:
  - the serial number and IP address are correct,
  - the hub is powered on and on the same network,
  - no more than one *other* device is already connected to the hub over the LAN.
    The hub allows two LAN connections at once, so the Pi plus one phone is fine;
    a second phone on the same network is one too many. Phones connected over the
    Internet rather than the LAN do not count towards that. See
    [Using this alongside the official app](#using-this-alongside-the-official-app).
- **Switching back to demo mode** is always safe and always works, even if the real hub is unreachable. This is a good way to confirm the web interface itself is healthy.

### Reading the status indicator

The coloured dot in the top-right corner tells you the real state of the system:

| Indicator | Meaning |
|-----------|---------|
| `Connecting...` | The browser is still opening its connection to the Pi. |
| `Disconnected` / `Connection Error` | The browser cannot reach the Pi at all. Check that the Pi is powered on and the service is running. |
| `🟡 Demo Mode` | Working normally on simulated data. No hub is being contacted. |
| `⚠️ Hub Unreachable` | The Pi is fine, but it cannot talk to your Nobø Hub. Zones will not load. Check the serial number and IP address under **Devices**. |
| `Connected` | Connected to your real hub and receiving live data. |

`⚠️ Hub Unreachable` clears by itself, without a page refresh, as soon as the hub becomes reachable again.

### Reverting to the `.env` file

If you want the `.env` file to control the mode again, delete the saved override and restart:

```bash
cd /opt/nobo-control
docker compose exec nobo-web-control rm -f /app/data/hub_config.json
sudo systemctl restart nobo-control
```

## Using This Alongside the Official App

You do not have to choose. The Pi and the official Nobø Energy Control app can
be connected to the hub at the same time, and there is nothing to switch.

This is stated in the official protocol specification
([Nobø Hub API v1.1](#reference-documents)):

> **Two devices can be connected directly via LAN to one Hub at the same time.**
> (In addition, up to 10 devices can be simultaneously connected via the
> Internet.) — §5.8, page 4

and, on keeping them consistent:

> **If several clients are connected simultaneously, the Hub always pushes any
> changes to all connected clients, to keep them synchronized.** The Hub always
> pushes any changes to all connected clients, also if the change is not
> initiated by a connected client. — §3, page 3

So a change you make in the app appears on the Pi within seconds, and vice
versa, without either side polling. The same mechanism reports things neither of
them did: an override expiring, or someone pressing a physical Nobø Switch.

This has been confirmed on real hardware, not just read off the page: with the
Pi connected, setting the house to Home from the phone app appeared on the Pi
about six seconds later, on its own. Both were connected the whole time.

### The budget

| Route | Simultaneous connections |
|-------|--------------------------|
| Local network (LAN, TCP 27779) | **2** |
| Via the Internet (the app's remote mode) | **10** |

The Pi holds one LAN connection permanently — it is an always-on server, that is
the point of it. That leaves **one** LAN slot.

In practice:

- **Pi + one phone at home** — fine, this is the normal case.
- **Pi + several phones away from home** — fine. Remote phones use the Internet
  route and do not consume LAN slots.
- **Pi + two phones at home on Wi-Fi** — one too many. The third connection is
  simply not accepted. Close the app on one phone, or turn its Wi-Fi off so it
  connects remotely instead.

The app releases its slot when you close it, so this is rarely noticeable. Note
that the spec's handshake reject codes (page 5) cover only version, serial,
argument and timestamp errors — there is **no** "hub busy" code, so a hub at its
limit does not explain itself. If a device silently fails to connect at home
while everything else looks right, suspect the LAN slot count.

### If you want the Pi to let go entirely

There is no need for this in normal use, but if you want the hub completely to
yourself — during setup or fault-finding — either stop the service:

```bash
sudo systemctl stop nobo-control       # start it again with: sudo systemctl start nobo-control
```

or switch the Pi to demo mode from **Settings → Hub Connection**, which drops
the hub connection while leaving the web interface running. Demo mode is
reversible and never touches your real heating.

### What the app can do that this cannot

Adding and removing *receivers* (the units in the heaters) is done with the
official app, following Nobø's own [user manual](#reference-documents). This web
interface can register a device by serial number and organise zones, but the app
remains the tool for the initial pairing of the system. Keeping both connected
means you never have to disconnect one to use the other.

## HTTPS on Your Own Network

By default the web interface answers plain HTTP on port 8000. That is fine on a
home network, but your password crosses the network in clear text and the
browser warns you about it every visit.

This is optional and off unless you ask for it. It gives you
`https://nobo.example.com` with a padlock and no warnings.

> **This is not remote access.** It encrypts traffic on your own network.
> Nothing here opens a port to the internet, and you should think hard before
> publishing a heating system that is defended by one password. To reach it
> from outside, use a VPN — see [Reaching It From Outside](#reaching-it-from-outside).

### First, two things you need either way

- **A hostname.** Pick one, for example `nobo.home.arpa` or a name in a domain
  you already own.
- **A local DNS record** pointing that name at the Pi's LAN address, so it
  resolves inside the house. Most routers can do this; on UniFi it is
  Settings → Networks → your network → DNS.

Then choose how the certificate is issued.

### Option A: Caddy's own certificate authority (default)

Caddy runs a small private CA on the Pi, issues itself a certificate and renews
it for ever. **Nothing to register, no DNS records, no third party, and no
expiry date to forget.** It works with any hostname, including invented ones
like `nobo.home.arpa`.

The price: browsers do not know that CA, so you install its root certificate
once on each device. After that, no warnings.

Set two things in `.env`:

```bash
NOBO_DOMAIN=nobo.home.arpa
NOBO_BIND=127.0.0.1
COMPOSE_PROFILES=tls
```

`NOBO_BIND=127.0.0.1` makes the application answer only the proxy, so the
unencrypted port stops answering the network altogether.

`COMPOSE_PROFILES=tls` is the one that is easy to skip and should not be. It
makes the proxy part of the normal stack, so `docker compose up`,
`scripts/update.sh` and the **systemd service** all start Caddy too. Without it
the service starts the application alone — and with `NOBO_BIND=127.0.0.1` that
leaves nothing answering the network after a reboot.

Start it:

```bash
cd /opt/nobo-control
sudo docker compose up -d
```

Then export the root certificate:

```bash
sudo docker compose exec caddy \
  cat /data/caddy/pki/authorities/local/root.crt > nobo-root.crt
```

Copy `nobo-root.crt` to each device and install it:

| Device | How |
| --- | --- |
| **Windows** | Double-click → Install Certificate → Local Machine → place in **Trusted Root Certification Authorities** |
| **macOS** | Double-click → Keychain Access → find it → Get Info → Trust → **Always Trust** |
| **iPhone / iPad** | AirDrop or email it to yourself → Settings → Profile Downloaded → Install. **Then** Settings → General → About → Certificate Trust Settings → switch it on. That second step is easy to miss and nothing works without it |
| **Android** | Settings → Security → Encryption & credentials → Install a certificate → **CA certificate** |
| **Linux** | `sudo cp nobo-root.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates` |

Firefox keeps its own trust store on every platform, so add it there too if you
use Firefox: Settings → Privacy & Security → Certificates → View Certificates →
Authorities → Import.

> **Keep the `caddy-data` volume.** The CA lives in it. Delete it and Caddy
> generates a brand new root, and every device you installed the old one on
> starts warning again. It is included in `scripts/backup.sh`.

The certificate the browser sees lasts about twelve hours and is renewed
automatically — that is normal for a local CA. The root you install is the one
that lasts ten years.

### Option B: a Let's Encrypt certificate

Use this if you would rather not install anything on your devices — for example
if guests use the interface, or there are many devices.

You need **a real domain you own** (a certificate cannot be issued for
`nobo.home.arpa` or an IP address) and a way to write a DNS TXT record
automatically. Certificates last 90 days and the challenge token changes every
time, so this has to be automated or you will be editing DNS by hand six times
a year.

The challenge used is **DNS-01**, which proves you own the name by writing a DNS
record rather than by answering a connection — so this still needs no port open
to the internet.

**If your DNS host has an API** (Cloudflare, Route53, deSEC, DigitalOcean and
many more), use it directly. In `.env`:

```bash
NOBO_CADDYFILE=./tls/Caddyfile.acme
CADDY_BINARY_SOURCE=caddy-plugins
CADDY_DNS_PLUGIN=github.com/caddy-dns/cloudflare
NOBO_TLS_EMAIL=you@example.com
```

then follow that plugin's README for its credentials and edit the `tls` block in
`tls/Caddyfile.acme` to match.

**If your DNS host has no API** (one.com and many others), use
[acme-dns](https://github.com/joohoi/acme-dns). Register once:

```bash
curl -sX POST https://auth.acme-dns.io/register
```

Add a **CNAME** at your DNS host pointing the challenge name at the `fulldomain`
from the reply:

```
_acme-challenge.nobo.example.com.   CNAME   <fulldomain-from-the-reply>.
```

The record Let's Encrypt looks up is still a TXT — the CNAME simply points that
lookup somewhere that can be written automatically. It is static and never
changes again.

Then in `.env`:

```bash
NOBO_CADDYFILE=./tls/Caddyfile.acme
CADDY_BINARY_SOURCE=caddy-plugins
NOBO_BIND=127.0.0.1
NOBO_DOMAIN=nobo.example.com
NOBO_TLS_EMAIL=you@example.com
COMPOSE_PROFILES=tls
ACMEDNS_USERNAME=...
ACMEDNS_PASSWORD=...
ACMEDNS_SUBDOMAIN=...      # the "subdomain" field, not your hostname
```

```bash
sudo docker compose up -d --build
```

The first build compiles Caddy with the DNS plugin and takes several minutes on
a Pi. Watch it with `sudo docker compose logs -f caddy`;
`certificate obtained successfully` means it worked.

### Check it worked

```bash
# who issued the certificate, and for what
echo | openssl s_client -connect nobo.example.com:443 -servername nobo.example.com 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates

# the plain-HTTP port should now be refused from another machine
curl -sS --max-time 5 http://<pi-lan-ip>:8000/api/health
```

That last command **failing** is the point: with `NOBO_BIND=127.0.0.1` the
application no longer answers the network directly, only through Caddy.

### If it goes wrong

SSH still works and the application is still running, so you are never locked
out of the Pi. To go back to plain HTTP:

```bash
# stop the proxy and remove it from the normal stack
sudo docker compose down
# in .env: set NOBO_BIND=0.0.0.0 and comment out COMPOSE_PROFILES
sudo docker compose up -d
```

| Symptom | Cause |
| --- | --- |
| Nothing answers at all after a reboot | `COMPOSE_PROFILES=tls` is missing from `.env`, so the service started the application alone while `NOBO_BIND=127.0.0.1` kept it off the network |
| Warning on every device | Expected with Option A until the root certificate is installed — and on iOS, until it is switched on in Certificate Trust Settings as well |
| Warnings came back after a rebuild | The `caddy-data` volume was deleted, so the CA was regenerated. Export and install the new root |
| `502 Bad Gateway` | The application is not on the port Caddy expects. `NOBO_PORT` must match on both sides |
| Certificate fine, page does not load | `NOBO_BIND` was changed but the application container was not restarted |
| `no such module dns.providers.*` | Option B without `CADDY_BINARY_SOURCE=caddy-plugins`, or the image was not rebuilt — add `--build` |
| Stuck retrying the challenge | Option B: the CNAME is missing or wrong. Check with `dig _acme-challenge.nobo.example.com CNAME` |

### Reaching It From Outside

HTTPS on the LAN does not make the heating reachable from elsewhere, and that is
deliberate. The safer way in is a VPN back into your own network: nothing is
published, and the same certificate keeps working because as far as the Pi is
concerned you are on the LAN.

- **UniFi Teleport** (Dream Router and similar) — easiest by a distance. One
  click, and family scan a QR code in the WiFiman app.
- **WireGuard** — built into most routers now, clients for every platform.
- **Tailscale** — no router configuration at all, runs on the Pi itself.

If you publish it anyway, at minimum: change the default password, put the Pi on
an isolated VLAN that can still reach the hub on TCP 27779, and understand that
you are exposing your heating to the whole internet.

## Updating the Software

To update to the latest version:

```bash
cd /opt/nobo-control
sudo bash scripts/update.sh
```

This pulls the latest code, rebuilds the Docker image, and restarts the service. Your configuration (`.env`) and data (user accounts, schedules) are preserved.

Manual update steps if preferred:

```bash
cd /opt/nobo-control
git pull
docker compose build
sudo systemctl restart nobo-control
```

### If the Pi is on an old branch

`git pull` updates whatever branch the checkout is on. A Pi left on a feature
branch will pull *that* branch, report success, and stay on old code — the
update looks like it worked and nothing changed. `scripts/update.sh` prints a
warning when it notices, but it is worth checking directly:

```bash
cd /opt/nobo-control
git rev-parse --abbrev-ref HEAD     # which branch am I on?
git log --oneline -1                # and at which commit?
```

If that is not `main`, move it across:

```bash
cd /opt/nobo-control
sudo git fetch origin
sudo git checkout -B main origin/main
sudo git branch --set-upstream-to=origin/main main
sudo bash scripts/update.sh
```

Nothing you care about lives in the working tree, so this is safe: `.env` is
gitignored and your accounts, schedules, system name and certificates are in
Docker volumes. Only the code changes.

Check it took:

```bash
git status -sb                      # should say "## main...origin/main"
curl -s localhost:8000/api/health   # or https://<your-name>/api/health
```

## Troubleshooting

### Web page loads but no zones appear ("connecting" forever)

If the interface loads and you can log in, but the zone tiles never appear, first check whether the backend is actually fine:

```bash
curl http://localhost:8000/api/status
curl http://localhost:8000/api/zones
```

If those return data (in demo mode you should see 8 example zones), the backend is healthy and the problem is in the browser:

1. **Hard refresh the page** — `Ctrl+Shift+R` (`Cmd+Shift+R` on macOS). An old cached copy of `app.js` is the most common cause.
2. **Open the browser console** — press `F12` and look at the Console tab. A red `SyntaxError` or similar means the page script failed to load, which stops the zone list and the live updates from ever starting.
3. **Check for a newer version** — `sudo bash /opt/nobo-control/scripts/update.sh`.

If `/api/zones` returns an empty list while `NOBO_DEMO=false`, the app is connected to a real hub that has no zones configured — set up your zones in the official Nobo app first.

### "⚠️ Hub Unreachable" and "Cannot reach the Nobø hub"

The Pi and the web interface are working, but the app cannot talk to your Nobø Eco Hub, so there are no zones to show. Check, in order:

1. **The IP address is correct.** Hubs often get a new address from the router after a power cut. Find the current one in your router's device list, then update it under **Devices → Hub Connection**. Consider giving the hub a static/reserved IP in your router so this cannot happen again.
2. **The serial number is correct** — all 12 digits from the sticker on the hub.
3. **Not too many things are connected over the LAN.** The hub allows **two** local
   connections at once, and the Pi holds one of them. The official app can stay
   open on one phone; a *second* phone on the same Wi-Fi is one too many. The hub
   gives no "busy" message when this happens, it simply does not accept the
   connection. Phones using the app away from home go via the Internet and do not
   count. See [Using this alongside the official app](#using-this-alongside-the-official-app).
4. **The hub is on the same network** as the Pi and is powered on.

You can confirm what the server thinks is going on with:

```bash
curl http://localhost:8000/api/hub/config
```

To rule out a network problem entirely, switch to demo mode from **Devices → Hub Connection**. If the 8 example zones appear, the Pi and the app are healthy and the issue is purely with reaching the hub.

You do **not** need to restart anything after correcting the address — the app retries in the background and the indicator changes to `Connected` on its own.

### Clicking the user icon (👤) does nothing

The User Settings panel should open when you click the person icon in the top-right corner. If nothing happens:

1. **Hard refresh the page** — `Ctrl+Shift+R` (`Cmd+Shift+R` on macOS). An old cached copy of `auth.js` is the most likely cause.
2. **Make sure you are on the current version** — this was a genuine bug in earlier builds, where the script opened the panel with one CSS class while the stylesheet styled another, so the panel was technically open but invisible. Update with:

   ```bash
   sudo bash /opt/nobo-control/scripts/update.sh
   ```

3. **Check the browser console** — press `F12` and look at the Console tab for errors from `auth.js`.

Note that the **🛠️ Manage Users** section only appears for accounts with the `admin` role. If you can open the panel and change your own password but see no user management, you are signed in as a regular user.

### Switched back to demo mode, but no zones load

Fixed in the current version. Older builds had a race: connecting to a real hub
happens in the background with a timeout, and if you switched back to demo mode
before that attempt gave up, the failed attempt marked the system disconnected
*after* demo mode had started. Demo mode was then stuck reporting
`Hub not connected` and no zones appeared until the service was restarted.

If you see this, update and restart:

```bash
sudo bash /opt/nobo-control/scripts/update.sh
```

As an immediate workaround on an old build, `sudo systemctl restart
nobo-control` clears it.

### The "Search for device" button is greyed out

Device discovery asks the hub to listen on its radio for devices in pairing
mode, so it needs a real hub. In demo mode there is nothing to listen with, and
the button says so. Type the 12-digit serial number in by hand instead.

This is the only feature that is restricted by mode. Everything else — adding
and deleting zones, editing weekly schedules, and adding, moving, renaming,
replacing or removing devices — now works in both modes. If you are on an older
build where more of the interface is greyed out, update (see
[Updating the Software](#updating-the-software)).

### A device change was refused with "the hub did not confirm..."

The command reached the hub but the hub did not answer, or answered with an
error. Nothing was changed, so it is safe to try again.

Common causes, in order of likelihood:

- The device is not in pairing mode, or stopped being in pairing mode while the
  request was running. Pairing waits up to 30 seconds.
- The device is out of range of the hub.
- The device is already registered on the hub. Adding it again is refused; use
  **Replace** instead, or remove it first.

`GET /api/log`, or the command log in the interface, shows exactly what was
sent and what came back.

### Deleting a zone says it still contains devices

That is deliberate. The hub would let you delete the zone and leave its devices
unassigned, which is harder to unpick than an error message. Move or remove the
devices first, then delete the zone.

### I changed one zone's schedule and expected other zones to change too

Schedules on a hub are **shared objects**, and several zones can follow the same
one. When you save a schedule that is shared, the app asks which you meant:

- **Just this zone** — it gets a copy of its own, and the others keep the
  schedule as it was.
- **Every zone using it** — the schedule itself changes, so every zone following
  it changes with it.

If you wanted several zones on one schedule, that is the second option, or use
**Use a different schedule** on each zone to point them all at the same one.
**Settings → Schedules** shows every schedule with the zones that use it.

Editing from **Settings** always means the second thing: you picked a named
schedule out of a list of schedules, so the edit changes the schedule and the
app names the affected zones before saving.

One thing you cannot do: change the hub's own built-in schedules. The hub accepts
the command and ignores it, so the app disables those rather than reporting a
success that did not happen. Make a new schedule instead.

### A zone ignored Home, Comfort, Eco or Away

Look at the zone on the front page. If it is marked **Ignores Home/Away**, then
its **Follow Home and Away** setting is off, and that is exactly what off means:
the zone keeps whatever it is on and the whole-house buttons do not touch it.
Open the zone and turn the setting back on, or set the zone's mode there
directly.

This setting lives on the hub, so it may have been turned off in the Nobø app
rather than here. It is shown on the zone screen either way.

If the zone is *not* marked, and still did not follow the house, that is a fault
worth reporting — check `GET /api/log` for the `create_override` that should have
released it. See [Zones that follow the whole house, and zones that do
not](#zones-that-follow-the-whole-house-and-zones-that-do-not).

### "permission denied while trying to connect to the Docker daemon socket"

Your user is not in the `docker` group yet, or you have not logged out since being added. Fix it:

```bash
sudo usermod -aG docker $USER
exit
```

Then SSH back in and try again. Group membership only applies to new login sessions.

### Service won't start

```bash
# Check service logs
sudo journalctl -u nobo-control -n 50 --no-pager

# Check Docker logs
cd /opt/nobo-control && docker compose logs --tail 50
```

### Can't connect to the Nobo Hub

- Verify the serial number and IP in `.env` are correct
- Check that the Pi and Hub are on the same network/subnet
- Make sure no more than one other device is connected to the hub over the LAN — it allows two, and the Pi uses one
- Try restarting the Nobo Hub (power cycle)
- Check the logs for connection error messages

### Web interface not loading

- Check the container is running: `docker ps`
- Check nothing else is using port 8000: `sudo ss -tlnp | grep 8000`
- Try accessing from the Pi itself: `curl http://localhost:8000/api/health`
- Check firewall: `sudo ufw status` (if active, run `sudo ufw allow 8000`)

### Docker build fails

- Ensure you have internet access: `ping -c 1 google.com`
- Check disk space: `df -h` (Docker images need ~500 MB)
- Try rebuilding without cache: `docker compose build --no-cache`

### Container keeps restarting

```bash
docker logs nobo-web-control --tail 100
```

Common causes:
- Invalid `NOBO_SERIAL` or `NOBO_IP` in `.env`
- Hub not reachable on the network
- Port 8000 already in use

### Pi runs out of memory

If using a 2 GB Pi and experiencing issues:
```bash
# Check memory usage
free -h

# The app typically uses ~100-150 MB
docker stats --no-stream
```

### Permission denied errors

```bash
# Fix ownership of the project directory
sudo chown -R $USER:$USER /opt/nobo-control
```

## Backing Up Configuration and Data

### Create a backup

```bash
sudo bash /opt/nobo-control/scripts/backup.sh
```

Backups are saved to the `nobo-backups` folder in your home directory (e.g., `/home/nobo/nobo-backups/`). You can specify a different directory:

```bash
sudo bash /opt/nobo-control/scripts/backup.sh /path/to/backup/dir
```

### What is backed up

- `.env` — your hub configuration (serial, IP)
- `data/` volume — user accounts, away schedules, demo zone state, zone icons
  (`zone_icons.json`), the system name (`site.json`), the intended set points
  (`intended_setpoints.json`, see [When somebody turns a dial](#when-somebody-turns-a-dial))
  and server state
- `caddy-data` volume — TLS certificates and, if you use Caddy's own CA, its
  private root. Only present when HTTPS is switched on. Worth having: that root
  is the one you installed on every phone and laptop, and losing it means
  installing a new one on all of them.

Zone icons are worth calling out: the hub has no icon field, so they exist only
on the Pi. The same goes for the system name. Everything else about a real hub's
zones and devices lives on the hub itself and is not part of this backup.

### Restore from backup

```bash
tar -xzf ~/nobo-backups/nobo-backup-YYYYMMDD-HHMMSS.tar.gz
sudo cp backup/.env /opt/nobo-control/
sudo docker cp backup/data/. nobo-web-control:/app/data/
# docker cp writes the files as root, but the application runs as a normal
# user. Without this it cannot save changes and logins start failing.
sudo docker exec -u root nobo-web-control chown -R nobo:nobo /app/data
sudo systemctl restart nobo-control
```

The restart is not optional: the running application keeps its own copy of this
data in memory and would overwrite what you just restored.

## Differences from the Windows Version

| Feature | Windows Version | Raspberry Pi Version |
|---------|----------------|---------------------|
| Runtime | Python installed directly | Docker container |
| Auto-start | Windows Service (NSSM) or Task Scheduler | systemd + Docker |
| Configuration | Environment variables or edit `server.py` | `.env` file |
| Data storage | `data/` folder on disk | Docker named volume |
| Updates | Manual download | `git pull` + rebuild |
| Network | Runs directly on host network | Docker host networking mode |

The application code is identical. All features — zones, schedules, device management, WebSocket updates, authentication, away scheduling — work the same way.

## Project Structure

```
Nobo_Raspberry_PI/
├── app/                        # Application code (from nobo-web-control)
│   ├── server.py               # FastAPI backend
│   ├── auth.py                 # Authentication module
│   ├── away_schedule.py        # Away schedule persistence
│   ├── config_persistence.py   # Config/state persistence
│   └── static/                 # Web UI (HTML, CSS, JS, images)
├── tests/                      # Test suite (pytest) — see Testing below
├── deploy/
│   └── systemd/
│       └── nobo-control.service  # systemd unit file
├── scripts/
│   ├── install.sh              # First-time setup
│   ├── update.sh               # Update to latest version
│   ├── backup.sh               # Backup config/data
│   ├── start.sh                # Start the service
│   ├── stop.sh                 # Stop the service
│   └── logs.sh                 # View logs
├── Dockerfile                  # Container build instructions
├── compose.yml                 # Docker Compose configuration
├── pytest.ini                  # Test configuration (lets pytest find app/ and tests/)
├── .dockerignore               # What stays out of the image (tests, .git, .env)
├── .env.example                # Configuration template
├── requirements.txt            # Python runtime dependencies
├── requirements-dev.txt        # Development/testing dependencies
├── CLAUDE.md                   # Notes for AI coding assistants working on this repo
└── README.md                   # This file
```

## Choosing the Interface

There are two interfaces, and both are installed in every build.

| Address | Interface | |
|---------|-----------|--|
| `/` | whichever `NOBO_UI` selects | what you normally open |
| `/cabin` | Cabin | always reachable |
| `/classic` | the original | always reachable |

**Cabin** is the current interface. It is arranged around the away period, because a cabin stands
empty most of the year: it leads with "I'm leaving" and "I'm back" rather than raw hub modes, shows
what a zone is doing right now with both its set points adjustable underneath, flags heaters whose
temperature can only be turned by hand, manages named schedules that several zones can share, lets
you add a zone and register a heater by serial number, keeps an activity log under
**Settings → Diagnostics**, and installs on an iPhone home screen.

It says **zone**, not "room". A zone is what shares a schedule and a comfort/eco pair, and in a
house where several rooms share one — both first-floor bedrooms on one schedule, both second-floor
bedrooms on another — calling it a room hides the fact that the grouping is the point.

**Classic** is the original interface, unchanged.

### Switching, and switching back

Edit `.env` on the Pi:

```bash
cd /opt/nobo-control
nano .env          # NOBO_UI=classic   (or: cabin)
sudo systemctl restart nobo-control
```

That takes a few seconds. It needs no rebuild and no internet, which matters if the Pi is in a
cabin on a phone hotspot.

Rolling back this way was a deliberate choice over keeping the old interface on an old branch or
in a separate repository. Both interfaces sit on top of the same server code, so redeploying an
older revision to change the interface would also undo every hub, scheduling and security fix made
since — and the revision you would be falling back to is the one nobody has run for months. A
setting keeps the two decisions separate: you change the interface without changing anything else.

You do not need to change the setting just to look at the other one. `/cabin` and `/classic` both
work whatever `NOBO_UI` says, so you can open them side by side.

If `NOBO_UI` is set to something that is not recognised, the server logs a warning and starts with
Cabin. A typo should not leave you with no way to turn the heating on.

The last revision before Cabin became the default is tagged `classic-ui-final`, if you ever want
the code exactly as it was.

## Design Exploration

Cabin began as one of four interactive design concepts, built against the real API so they could be
compared on a real device. Concepts A, B and C are kept at `/static/concepts/` as the record of
that exploration; they still work, but they are no longer being developed. Concept D was adopted
and became Cabin.

The write-up — analysis of the original UI, the usability problems found, the concepts, the
comparison and the reasoning behind the promotion — is in
[`docs/UI_REDESIGN.md`](docs/UI_REDESIGN.md).

## Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 8000 | TCP/HTTP | Web interface and API |

The container runs with `network_mode: host`, meaning it shares the Pi's network stack directly. This is required so the application can discover and communicate with the Nobo Eco Hub on your LAN.

## API

The server provides a REST API for integration with other systems (for example
Home Assistant).

**Everything requires a login.** Send the `session_id` cookie you get back from
`POST /auth/login`. The only exception is `GET /api/health`, which is left open
so monitoring tools can check the service is alive without credentials.

```bash
# Log in and keep the session cookie in a file
curl -c cookies.txt -X POST http://<pi-ip>:8000/auth/login \
     -d 'username=admin&password=yourpassword'

# Then use it
curl -b cookies.txt http://<pi-ip>:8000/api/zones
```

Note that `/auth/login` takes form fields, not JSON.

### Reading

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Health check. The only endpoint that does not need a login. |
| `GET /api/status` | Connection status, demo mode, away schedule and the timezone in use |
| `GET /api/capabilities` | Which features work in the current mode (see [What works with a real hub](#what-works-with-a-real-hub-and-what-does-not)) |
| `GET /api/zones` | All zones with current status |
| `GET /api/zones/{zone_id}/schedule` | One zone's weekly schedule, the schedule's name, and which other zones share it |
| `GET /api/week_profiles` | Every schedule on the hub: its decoded week, the zones using it, and whether it can be edited or deleted |
| `GET /api/hub` | What the hub says about itself: name, firmware, hardware version, production date, protocol version, serial |
| `GET /api/devices` | All devices, with their friendly names and zone assignment |
| `GET /api/devices/search` | What a running device search has heard so far (real hub only) |
| `GET /api/hub/config` | Current hub connection settings |
| `GET /api/site` | What this installation is called and how it writes dates, with both display forms resolved |
| `GET /manifest.webmanifest` | The installed-app manifest, carrying the chosen name |
| `GET /api/log` | Recent commands sent to and received from the hub |
| `GET /api/global-mode/away-schedule` | The current holiday period, if any |
| `GET /api/global-mode/away-exceptions` | Zones held on Eco during Away, plus the fixed away temperature |
| `GET /api/notifications` | Alert settings. Admin only. The mail password is replaced by `password_set` |
| `WS /ws` | Live updates. Pushes the current zones on connect, then again whenever anything changes. |

### Controlling

| Endpoint | Purpose |
| --- | --- |
| `POST /api/zones/{zone_id}/override/{mode}` | Set one zone to `comfort`, `eco`, `away` or `normal` |
| `POST /api/global/override/{mode}` | Set every zone at once. Here `home` is accepted as another word for `normal`; on the per-zone endpoint above it is not. Zone-level overrides are released for zones whose `follows_global_mode` is true, and the released ids come back as `zone_overrides_released`. |
| `POST /api/zones/{zone_id}/temperature` | Set `comfort` and/or `eco` for a zone. Supplying one fills the other in from the hub, so the pair is always validated together. |
| `POST /api/zones/{zone_id}/restore-setpoints` | Put a zone back to the temperatures set here (see [When somebody turns a dial](#when-somebody-turns-a-dial)). `400` if it already matches. |
| `POST /api/zones/{zone_id}/accept-setpoints` | Accept the zone's current temperatures as the intended ones |
| `PUT /api/zones/{zone_id}` | Rename a zone, change its icon, or set `follow_global_mode` (see [Zones that follow the whole house](#zones-that-follow-the-whole-house-and-zones-that-do-not)). Name and flag are written as one hub command, because they share one record. |
| `PUT /api/global-mode/away-schedule` | Set the holiday period |
| `DELETE /api/global-mode/away-schedule` | Clear the holiday period |
| `PUT /api/global-mode/away-exceptions` | Replace the list of zones held on Eco during Away. Body `{"zone_ids": ["1","4"]}`. Applied immediately if the house is already away. |
| `PUT /api/notifications` | Change alert settings. Admin only. Partial: omitted fields keep their value, and an omitted password keeps the stored one. Refuses to enable alerts that could not be delivered. |
| `POST /api/log/clear` | Discard the command log. Changes no setting and no zone — it only throws away the record. |
| `POST /api/notifications/test` | Send a test email, optionally using settings in the body that have not been saved yet. Admin only. Returns the mail server's own error on failure. |
| `POST /api/zones/{zone_id}/schedule` | Replace a zone's whole week. Optional `apply_to`: `"zone"` (default) copies a shared schedule so only this zone changes; `"profile"` changes the schedule itself and therefore every zone using it. See [Weekly schedule rules](#weekly-schedule-rules). |

### Schedules

A schedule is shared: several zones can follow one. See
[Weekly schedule rules](#weekly-schedule-rules).

| Endpoint | Purpose |
| --- | --- |
| `POST /api/week_profiles` | Create a schedule. Body `{"name": "...", "schedule": {...}}`. The hub assigns the id, so use the one in the reply. |
| `PATCH /api/week_profiles/{profile_id}` | Change a schedule's `name`, its `schedule`, or both. Changes every zone following it. `409` if the hub refuses, which it does silently for its own built-in schedules. |
| `DELETE /api/week_profiles/{profile_id}` | Delete a schedule. `409` while a zone still uses it, or if it is one of the hub's own. |
| `POST /api/zones/{zone_id}/week-profile` | Point a zone at an existing schedule (`{"profile_id": "23"}`) |

### Zones and devices

These work in both demo mode and against a real hub, except where noted.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/zones` | Create a zone. The hub assigns the id, so use the one in the reply. |
| `DELETE /api/zones/{zone_id}` | Delete a zone. Refused with `409` while it still contains devices. |
| `POST /api/devices` | Add a device to a zone by serial number. Registers it in the hub's database, which needs no pairing mode — that is how Nobø documents adding a mains-powered receiver. A battery unit (Nobø Switch, TCU 700) that the hub will not take this way falls back to a pairing request, and does need to be in pairing mode. |
| `PATCH /api/devices/{serial}/name` | Rename a device |
| `POST /api/devices/{serial}/move` | Move a device to another zone (`{"new_zone_id": "2"}`) |
| `PUT /api/devices/{serial}` | Replace a device with a new one (`{"new_serial": "..."}`). Pairs the new one first, then removes the old. |
| `DELETE /api/devices/{serial}` | Remove a device |
| `POST /api/devices/search` | Ask the hub to listen for devices in pairing mode (real hub only) |
| `DELETE /api/devices/search` | Stop the search (real hub only) |

Adding a device is usually a database operation and takes effect at once. A
battery unit that needs the radio can take up to 30 seconds, because it waits
for the hub to confirm the pairing. `502` means the hub answered but refused or
said nothing useful; `504` means it never answered at all. In both cases nothing
was changed.

**Autosearch does not work with every model.** Nobø's manual is explicit that
the R80 RDC 700 and R80 RXC 700 "must be registered manually" and lists them
outside the models that support it. Typing the 12-digit code in is the documented
route for those, and needs nothing done at the heater.

### Administration

| Endpoint | Purpose |
| --- | --- |
| `POST /auth/login` / `POST /auth/logout` | Start and end a session |
| `GET /auth/me` | Who you are logged in as |
| `POST /auth/change-password`, `POST /auth/rename` | Your own account |
| `GET/POST /auth/admin/users`, `PATCH/DELETE /auth/admin/users/{username}` | Manage users (admins only) |
| `POST /api/hub/config` | Switch between demo mode and a real hub (admins only; ends your session on success) |
| `PUT /api/site` | Rename the installation, or set its date format (admins only). Body `{"name": "Lakeside", "show_on_login": true, "locale": "nb-NO"}` — any field may be omitted to leave it unchanged. |

### If a request fails

The reason is always in the `detail` field, and the status code tells you what
kind of problem it is:

| Code | Meaning |
| --- | --- |
| `400` | The request itself was wrong — for example an eco temperature above the comfort temperature |
| `401` | Not logged in, or the session expired |
| `403` | Logged in, but this needs an admin |
| `404` | No such zone or device |
| `409` | The request conflicts with the current state — for example deleting a zone that still contains devices |
| `501` | The feature is not available in the current mode. In practice this is only device discovery in demo mode. |
| `502` | The hub answered, but not in a way the request could be completed with |
| `503` | The hub is not reachable right now |
| `504` | The hub did not answer in time. Nothing was changed. |

### Letting other systems in without a login

If you want Home Assistant or a script to read and control the heating without
handling a password, set `NOBO_ALLOW_ANON_API=true` in `.env` and restart. That
opens every `/api/...` address and the live-update connection to anyone who can
reach the Pi, so only do it on a network where you trust every device. Logging
in still works as normal, and the admin-only endpoints stay admin-only.

## Testing

The tests run without a hub, without a network and without a Raspberry Pi.

```bash
cd /opt/nobo-control          # or wherever you cloned it
pip install -r requirements-dev.txt
python -m pytest
```

Run one file, or one test, while working on something:

```bash
python -m pytest tests/test_capabilities.py
python -m pytest tests/test_temperature_validation.py -k rounding
```

Run them from the repository root. `pytest.ini` there tells pytest where the
application code and the tests live, so the command above works regardless of
which subdirectory you cloned into.

### How the real-hub code is tested without a hub

Most tests run in demo mode. That leaves the real-hub half of every endpoint
untested, which is how this project ended up shipping features that quietly did
not work against a hub.

`tests/fake_hub.py` closes that gap. It is a real TCP server that speaks the
Nobø Eco Hub protocol — handshake, the initial data dump, zone, component and
week profile commands, receiver search and pairing, and error responses. The
genuine `pynobo` client connects to it, so what is being tested is this
application's own behaviour.

| File | What it covers |
| --- | --- |
| `tests/test_fake_hub.py` | That the fake is faithful enough for the real client. If these fail, nothing below means anything. |
| `tests/test_real_hub_endpoints.py` | The full HTTP API driven against the fake hub: schedules, zones, devices, discovery and pairing. |
| `tests/test_connection_leak.py` | That overlapping connection attempts leave exactly one open client. The hub allows two connections, so a leak here locks you out. |
| `tests/test_mode_switch_recovery.py` | That switching between demo mode and a real hub cannot get stuck "not connected". |
| `tests/test_datetime_format.py` | That the clock stays 24-hour and dates follow the chosen locale. **Needs `node`** — it runs the real browser code rather than a Python reimplementation of it, and skips if node is missing. |

This cannot catch a hub that behaves differently from its specification, because
the fake encodes the same reading of the specification the application does, and
a functional test cannot see how many sockets are open. Both limits are real:
the connection leak above was found on live hardware with `ss -tn`, after the
fake-hub suite had passed. See
[Now verified against real hardware](#now-verified-against-real-hardware), and
`docs/TEST_MATRIX.md` for checks that need a hub, a phone or a browser.

One message in the output is expected and harmless: a
`PynoboConnectionError: Failed to connect to Nobø Ecohub at 192.0.2.10`
traceback. A test deliberately points the application at an address that cannot
answer, to check it survives an unreachable hub.

To run them the same way the application does, in the container image:

```bash
cd /opt/nobo-control
docker compose build
docker run --rm --user root -v "$PWD":/src -w /src \
  nobo-control-nobo-web-control:latest \
  sh -c 'apt-get update -qq && apt-get install -y -qq --no-install-recommends nodejs
         pip install -q -r requirements-dev.txt && python -m pytest'
```

`--user root` is needed because the image runs as an unprivileged user that
does not own your checkout.

`nodejs` is installed because `tests/test_datetime_format.py` runs the real
browser code rather than a Python imitation of it — testing a reimplementation
would prove nothing about what the browser actually does. It adds about
half a minute on a Pi. Leave it out and those tests **skip**, which is worth
knowing: a skipped test looks like a passing one in the summary line.

The application image itself does not contain node; this installs it into the
throwaway test container only.

### Testing a real installation

The suite above cannot tell you whether the hub behaves as documented, whether
the radio actually switches a heater on, or whether the Pi survives a power cut.
[`docs/TEST_MATRIX.md`](docs/TEST_MATRIX.md) is a checklist for those: seven
phases, ordered so the read-only checks come first and every step that changes
something says how to undo it.

If you have twenty minutes, the short version at the end of that document covers
the failure modes that actually bite.

## Timezone

Week schedules and away periods are wall-clock times: "07:00" means seven in
the morning where the Pi is, not in UTC. Containers default to UTC, so the
container is given the Pi's own clock settings (`/etc/localtime` and
`/etc/timezone` are mounted read-only in `compose.yml`).

Check which timezone is actually in use:

```bash
curl -b cookies.txt http://localhost:8000/api/status
```

Look at the `timezone` field. It is also written to the log at startup. If it
says `UTC` when it should not, fix the Pi's own timezone and restart:

```bash
sudo timedatectl set-timezone Europe/Oslo
sudo systemctl restart nobo-control
```

Do not set a `TZ` variable in `.env`. An empty `TZ` is treated as UTC and
overrides the mounted files, which is exactly the bug this avoids.

## Security Notes

- **HTTPS is available but off by default.** Out of the box the login password
  and session cookie travel across your network in plain text, and anyone able
  to watch that traffic can read them. On a home network behind a router that
  is a normal trade-off; on a shared or public network it is not. See
  [HTTPS on Your Own Network](#https-on-your-own-network) to turn it on — it
  needs no port open to the internet.
- **Do not forward port 8000 from your router.** If you need access from
  outside the house, use a VPN back into your network, or put a reverse proxy
  such as Caddy or nginx in front of it to terminate TLS and reach the
  application over `http://localhost:8000`. Exposing it directly puts your
  heating, and a plain-text password, on the public internet.
- **The login lockout is sized for a LAN, not the internet.** Five failed
  attempts lock an account for a minute, which is ample against someone
  guessing on your own network and thin against a determined attacker with a
  list. It is another reason not to publish this.
- **Change the default `admin` / `nobohub` password immediately.** It is
  published here and in every copy of this repository.
- Every API address and the live-update connection require a login, unless you
  deliberately turn that off with `NOBO_ALLOW_ANON_API` (see above).
- Repeated failed logins from the same address are temporarily locked out.
- Passwords are stored as bcrypt hashes, never in plain text.
- The application runs as an unprivileged user inside the container, so a flaw
  reachable from a web request does not get root on the Pi. The container does
  share the Pi's network (`network_mode: host`), which is required to reach the
  hub, so it is not isolated from your LAN.
- **Your hub's serial number is a credential, not a model number.** The Nobø
  protocol authenticates with it, so anyone who has it *and* can reach your
  network can drive your heating. Keep it out of screenshots, issue reports and
  public forks — `.env` is gitignored for this reason. The examples in this
  README use `123456789012`, which is not a real hub.
- The data volume holds your user accounts. Treat a backup of it like a
  password file.

## Reference documents

The code and comments cite two of Nobø's own documents by name. They are **not**
included in this repository — they are Glen Dimplex Nordic's copyright, not ours
to redistribute — so here is where to get them.

### `API_Nobo.pdf` — Nobø Hub API v1.1

*"Nobø Hub – API version 1.1 … Integration for advanced users."* The local TCP
protocol on port 27779: the command and response codes (`G00`, `H01`, `A03`,
`U00` …), the record layouts, and the override and week-profile structures.
Everything this project does to a hub comes from here.

**Glen Dimplex no longer publishes it.** The old `glendimplex.no` and
`glendimplex.se` sites now redirect to `nobo.no` / `nobo.se`, and every
`/media/…` path returns 404. The document is absent from the new sites entirely
— it is not in their content library, and their own search returns nothing for
it. Every third-party integration that cites it, including
[pynobo](https://github.com/echoromeo/pynobo) and the openHAB binding, now links
to a dead URL.

The only working copies are Internet Archive captures of the manufacturer's own
server:

- [Nobø Hub API v1.1 (Internet Archive, capture of `glendimplex.se`, 2024-12-29)](https://web.archive.org/web/20241229052955id_/https://www.glendimplex.se/media/15650/nobo-hub-api-v-1-1-integration-for-advanced-users.pdf)
- [the same document captured from `glendimplex.no`, 2024-06-26](https://web.archive.org/web/20240626104610id_/https://www.glendimplex.no/media/15650/nobo-hub-api-v-1-1-integration-for-advanced-users.pdf)

If both ever fail, the practical substitute is the `nobo.API` class in
[pynobo](https://github.com/echoromeo/pynobo), which encodes the same v1.1
command table in Python.

### `Manual_Nobo.pdf` — Nobø Eco Hub user manual

Still published, and the place to look for pairing, registering receivers, and
what each device model can do. Document 802124-I.

- [Nobø Hub product page](https://en.nobo.no/product/nobo-hub) — "Manuals and
  user guides" ([Norwegian](https://www.nobo.no/produkt/nobo-hub))
- [The manual online](https://help.nobo.no/en/user-manual/), which is easier to
  search than the PDF

This is the source for a claim made in several places here — that some receivers
cannot be found by an automatic search. The manual states it plainly:

> **R80 RDC 700 og R80 RXC 700: Require manual registration.**

Which is why [`POST /api/devices`](#controlling) registers a device by serial
number first and only falls back to a radio search.

### A note on trademarks

Nobø, Eco Hub, Orion 700 and the device model names belong to Glen Dimplex
Nordic. This project is not affiliated with, supported by, or endorsed by them.
Neither document carries a redistribution licence, which is exactly why they are
linked here rather than committed.

## License

[MIT](LICENSE). Use it, change it, redistribute it; keep the copyright notice,
and it comes with no warranty. Heating a building is your responsibility, not
this software's.

The libraries it builds on keep their own licences — notably
[pynobo](https://github.com/echoromeo/pynobo), which does the actual talking to
the hub, and [FastAPI](https://fastapi.tiangolo.com/). Nobø, Eco Hub and the
device names are trademarks of Glen Dimplex Nordic; this project is not
affiliated with or endorsed by them.

## Acknowledgments

- Application code from [nobo-web-control](https://github.com/aba1975/nobo-web-control)
- Built with [FastAPI](https://fastapi.tiangolo.com/) and [pynobo](https://github.com/echoromeo/pynobo)
- Containerized for Raspberry Pi with Docker
