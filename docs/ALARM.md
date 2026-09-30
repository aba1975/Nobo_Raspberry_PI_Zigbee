# Alarm system (optional)

Reads a Verisure alarm — and a Yale Doorman lock that shows up in the Verisure
app — and uses what they say:

| The alarm says | What happens |
| --- | --- |
| **Armed away** | The house goes to global **Away**, with no return time. Disarming brings it back to Home, unless somebody changed the mode in the meantime. If you had already chosen Away yourself, it is left exactly as you set it. |
| **Armed away or armed at home** | A warning if a door or window is open. |
| **Front door locked from outside** | A warning if a door or window is open. Optionally, Eco or Away until it is unlocked. |
| **Front door locked from inside** | Optionally a warning, and optionally Eco until it is unlocked. Both off by default. |

The alarm's own door and window sensors, and the temperature its smoke
detectors measure, can also be added to rooms as sensors — see
[The alarm's own sensors](#the-alarms-own-sensors).

It is **off by default**, and while it is off nothing about an alarm appears
anywhere. Turn it on under **Settings → Alarm System**. No extra hardware is
needed: the Pi talks to Verisure's servers, just as the Verisure app does.

**It only ever reads.** Nothing in this application can arm, disarm, lock or
unlock anything. This is enforced in the code, not just left unused — see
[Read-only, enforced](#read-only-enforced).

## What has and has not been tested

- The rules — when Away is applied, when it is lifted, when a warning is raised
  — are covered by tests and by the demo alarm on the demo Pi.
- The Verisure side is tested against a stand-in for Verisure's servers, built
  from the library's own code. **It has not yet been run against a real
  Verisure account.** Treat the first week as commissioning: arm, disarm and
  lock the door, and check that Settings shows what the Verisure app shows.

## Verisure has no public API

This goes through [`vsure`](https://github.com/persandstrom/python-verisure),
the same library Home Assistant's Verisure integration uses. It speaks the
private cloud API that the Verisure app itself uses. That makes it
**unofficial**: Verisure can change it without notice, and has done before.

When that happens the integration fails safe:

- the heating is left exactly as it is;
- no warning is raised or cleared on a reading it could not get;
- Settings, and the System status line, say that Verisure cannot be read;
- optionally, an email (**Alerts → The alarm cannot be read**).

A reading more than 15 minutes old is treated as *unknown* — the same rule
as an offline door sensor. "I cannot tell you" is never taken to mean
"disarmed".

## How your Verisure account is protected

**Your password is never stored.** When you sign in, the password is held in
memory, for at most five minutes, until you enter the code Verisure sends you.
Straight after the code is accepted it is sent to Verisure once more, with the
trust the code has just earned. That is how Home Assistant signs in, and the
session it gives is the one kept; the session the code step returns on its
own was refused at its first renewal on a real account. Then it is dropped. It is never written to disk, never logged, and never sent
back to the browser.

What is kept is the **sign-in Verisure hands back** — the same thing your phone
keeps after you sign in to the Verisure app. It cannot be turned back into
your password. If you think it has been copied, sign out here or change your
Verisure password, and it stops working.

That sign-in is stored like this:

- in `data/verisure/session.json`, inside the Docker volume. The folder is
  readable only by its owner (0700), and so is the file (0600).
- **not in backups.** `scripts/backup.sh` leaves the folder out on purpose. After
  restoring a backup you sign in again. A backup is a file that gets copied to
  places, and this one would be a key to your alarm account.
- **deleted when you sign out**, when you turn the integration off, and when
  you switch the source to the demo alarm. Signing out also ends the sign-in
  at Verisure's end, not only here.
- **deleted if it is damaged**, rather than set aside the way other damaged
  files are. A stray copy of it is exactly what must not exist.

The library keeps its own copy of the sign-in as a Python *pickle*, a file
format that can run code when it is read. This application never reads one.
The library's file goes to a scratch location inside the same private folder
and is deleted after every call. The saved sign-in is plain JSON, written by
this application.

**The password only crosses an encrypted connection.** The sign-in and code
endpoints refuse plain `http://` from anywhere but the Pi itself. The Settings
page greys out **Sign in to Verisure** and says why. Set up
[HTTPS](../README.md#https-on-your-own-network) first. If you cannot, sign in
from a browser on the Pi.

**Only administrators** can see or change any of this. The Settings section,
and every `/api/alarm/*` endpoint, return nothing to anyone else.

**Nothing Verisure's servers say is shown or logged.** Their error messages can
include account details. So only the kind of error is logged, and you see one
of this application's own sentences. The library's logger is turned down to
errors only, because at its normal level it logs your email address and, when
signing out, a URL containing the sign-in token. Your email address is shown in
Settings masked, as `a***@example.no`.

**Attempts are limited.** Five sign-in attempts in ten minutes, then a wait.
That is well inside anything Verisure would treat as abuse — a locked Verisure
account is a much worse outcome than a pause here. If Verisure itself asks for
fewer requests, polling backs off: 5, 15, 30 and then 60 minutes.

## Signing in

1. Open the app over **https://**.
2. **Settings → Alarm System → On**, and Source **Verisure** if you are asked.
3. **Sign in to Verisure** — the email and password you use in the Verisure app.
4. Verisure sends a code by text message or email. Type it in.
5. If the account has more than one installation, choose which one. Signing
   in again later keeps that choice.

Settings then shows the alarm's state and the lock, and the System status line
has an **Alarm** row.

**You may have to do this again one day.** The password is not stored, so when
Verisure ends the sign-in, this application cannot sign in again by itself.
Verisure does not say how long a sign-in lasts. When it ends:

- the section heading turns red and says so;
- the alarm is not read, and the heating is left alone;
- an email is sent if **The alarm cannot be read** is on under Alerts.

Storing the password would avoid this. We chose not to: an alarm password on a
heating controller is a worse risk than signing in again now and then.

## What counts as "locked from outside"

The Yale Doorman reports **how** it was locked, not where the person was.
Each way of locking is read as inside or outside:

| Locked with | Counts as, unless you change it |
| --- | --- |
| The thumb turn | **Inside** — somebody locked themselves in |
| Automatic locking | **Inside** — it happens whether or not anybody has gone |
| A code, the ✱ button, a tag, a key, the app | **Outside** |
| Anything the lock reports that we do not recognise | **Outside** |

**You can change any of these** under Settings → Alarm System → *Which way of locking counts as
outside*. The table there lists every method, plus any other name the lock has
actually reported, and marks the one it used last. Verisure does not document
which name the Doorman reports for the ✱ button on the outside, so lock the
door that way once, see what Settings says was used, and set it to Outside if
it is not already.

An unknown method counts as outside because being wrong that way costs one
warning; being wrong the other way is a window left open all week. The Verisure
app locks remotely, which counts as outside. That is deliberate: somebody
locking up from the car has left.

## Heating

- **Armed away** puts the house on Away. Armed at home changes nothing, unless
  you also tick *Away when it is armed at home too*. Armed at home usually
  means somebody is in.
- **The lock** can change the heating too, and each side is chosen separately:
  - *Locked from outside*: Nothing (the default), Eco, or Away.
  - *Locked from inside*: Nothing (the default) or Eco — say, Eco for the night
    when the door is locked at bedtime. Away is not offered for inside: the
    people who locked it are in the house.
  - Unlocking lifts only the mode the lock set.
- **The alarm outranks the lock.** While the alarm is holding Away, locking
  the door does not turn it into Eco, and unlocking does not lift it.
- **The alarm's Away has no return time.** It holds until the alarm is
  disarmed; no away period with an end date is created. If Away was already on
  when the alarm was armed — set by hand, or by a planned away period — the
  alarm does not take it over: it keeps its end time, if it had one, and
  disarming leaves it on.
- A change to the lock's heating setting applies from the next time the door
  is locked. It does not reach back to a door that was locked before the
  change.
- Away is the same global Away as the button on the front page. The [rooms that
  must not get cold](../README.md#rooms-that-must-not-get-cold) stay on Eco as
  usual.
- **Disarming lifts only a mode the alarm set.** If the house was already on
  Away when the alarm was armed, it stays on Away. If somebody changed the mode
  while the alarm was armed, their change wins and disarming leaves it alone.
- The decision is recorded in `data/alarm_state.json`, so a restart or a power
  cut neither sets Away twice nor forgets to lift it.
- Turning the integration off, or unticking the option, **leaves the house as it
  is**. An Away the alarm had set is handed over as if you had set it yourself.
  Warmth coming back because a setting was changed would be a surprise.
- With no hub connected nothing is sent, and the decision is made again when
  the hub returns.

## Warnings

The warning is one alert for the whole house, naming every open door and window
and the rooms they are in. It also says which sensors are offline and so cannot
be checked. It shows at the top of the front page and, if you turn it on, comes
as an email (**Alerts → Something is open when the alarm goes on**).

- It waits **five minutes**, the same as for Away, so shutting a window on the
  way out does not trigger it.
- Armed away, or locked from outside, is **urgent**: quiet hours do not hold it.
  Armed at home, and locked from inside, are normal warnings.
- *Locked from inside* only warns if you tick it. It is off by default,
  because the door is locked from inside every evening.
- It needs door and window sensors: [Zigbee sensors](SENSORS.md), the
  [alarm's own](#the-alarms-own-sensors), or both. Without them the options
  are shown, and say so.
- While the alarm is behind the warning, the ordinary *Something is open and
  the house is empty* alert is held back, so you get one message, not two.

## The alarm's own sensors

With the alarm and [the sensors](SENSORS.md) both switched on, **Add sensor**
on a room asks where the sensor comes from: a new Zigbee sensor, or one the
alarm already has. Choosing the alarm shows a list of what it reports:

- **Door and window sensors.** They work like a Zigbee contact: open and closed
  show in the room, feed the left-open warning, the alarm warning and a room's
  heating rule. A room made just for an outbuilding — a woodshed, say —
  becomes a [monitoring-only room](SENSORS.md#monitoring-only-rooms) that warns
  when its door is left open.
- **Temperatures.** Verisure smoke detectors (and some sirens) measure the
  temperature, and a room can use it as its thermometer — the same
  **Actual** reading, the same too-cold and near-freezing warnings.

They are administered like Zigbee sensors — renamed, moved between rooms,
given heating rules, removed. Removing one here only removes it from this
application; nothing changes in the alarm.

### Zigbee comes first

A Verisure sensor can be added in one of two ways:

- **As its own sensor**, in a room of its own choosing. Use this where there is
  no Zigbee sensor, like the woodshed.
- **As a backup** for a particular Zigbee sensor on the same door. It then sits
  in that sensor's room and is **not counted** while the Zigbee sensor is
  reporting. If the Zigbee sensor goes offline, or reports nothing it can be
  believed about, the Verisure sensor **stands in** for it until it is back.
  The door is always counted once.

Temperatures follow the same rule without being asked: a Verisure temperature
is set aside while the room has a Zigbee thermometer with a recent reading,
and used when it has not.

The room shows which sensor is being counted, and why the other is not.

### What is different about them

- **They are read, not heard.** The alarm is read once a minute, so an opened
  door can take a minute to show here. A Zigbee sensor shows it in about a
  second.
- **No battery or signal.** Verisure does not report either for its sensors
  through this route, and the alarm looks after its own batteries. So the
  *battery low* and *not heard from* alerts are Zigbee sensors only; the Alerts
  page says which alerts an alarm sensor can raise.
- **Offline means the alarm cannot be read.** When the alarm's reading is more
  than 15 minutes old, or it has stopped listing the device, the sensor is
  offline — never closed.
- **Smoke detectors read warm.** They hang on the ceiling, where the air is
  warmest, and report perhaps once an hour. They are a fair second opinion; a
  Zigbee thermometer at sitting height is better, which is why it comes first.

## Checking for updates

Settings reads the alarm once a minute, and slows down when there are errors.
When something changes, the page updates by itself. There is no live feed:
Verisure's API does not offer one to third parties. So Away can start up to a
minute after the alarm is armed.

## Read-only, enforced

Every request to Verisure passes through `check_read_only` in
`app/alarm_verisure.py` before it leaves the Pi. It checks the operation name
against a short list and refuses anything that is not a query. The list is:
installations, arm state, locks, door and window sensors, and
climate readings. A test fails if the module so much as names one of the
library's commands for arming, disarming or locking.

## The demo alarm

In demo mode the source can be **Demo**: an invented alarm with a front-door
lock, four door sensors (front door, patio door, tech room and woodshed) and
two smoke detectors, with buttons for arming and locking in Settings. The lock
can be locked with any method the real one reports, to try the inside/outside
table, and each of its sensors can be opened, closed or given a temperature
from the sensor's own settings. It follows exactly the
same rules, so the whole feature can be tried with no Verisure account. It is
refused on an installation connected to a real hub, where it could put a real
house on Away.

## Files

| File | What |
| --- | --- |
| `app/alarm_provider.py` | The reading every source produces, and the demo alarm |
| `app/alarm_verisure.py` | Verisure: sign-in, the read-only guard, the sign-in file |
| `app/alarm_automation.py` | The rules: what counts as leaving, when to set and lift Away |
| `app/alarm_persistence.py` | Settings, the ledger of the mode it holds, the demo alarm's state, the sign-in file |
| `app/sensor_verisure.py` | The alarm's sensors as room sensors: which were chosen, their readings, and Zigbee first |
| `app/server.py` | Polling, the heating, the warning, `/api/alarm/*` |
