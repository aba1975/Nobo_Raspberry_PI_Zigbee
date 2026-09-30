# Alarm system (optional)

Reads a Verisure alarm — and a Yale Doorman lock that shows up in the Verisure
app — and uses what they say:

| The alarm says | What happens |
| --- | --- |
| **Armed away** | The house goes to global **Away**. Disarming brings it back to Home, unless somebody changed the mode in the meantime. |
| **Armed away or armed at home** | A warning if a door or window is open. |
| **Front door locked from outside** | A warning if a door or window is open. Nothing happens to the heating. |

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
Then it is dropped. It is never written to disk, never logged, and never sent
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
5. If the account has more than one installation, choose which one.

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
So the rules are:

| Locked with | Counts as |
| --- | --- |
| The thumb turn | **Inside** — somebody locked themselves in |
| A code, a tag, the app, or anything the lock reports that we do not recognise | **Outside** |
| Automatic locking | **Inside**, unless you tick *Count the lock locking itself as leaving* |

Auto-lock is left out by default because it happens whether or not anybody has
gone. The Verisure app locks remotely, which counts as outside. That is
deliberate: somebody locking up from the car has left.

## Heating

- Only **armed away** changes the heating, unless you also tick *Away when it is
  armed at home too*. Armed at home usually means somebody is in.
- Away is the same global Away as the button on the front page. The [rooms that
  must not get cold](../README.md#rooms-that-must-not-get-cold) stay on Eco as
  usual.
- **Disarming lifts only an Away the alarm set.** If the house was already on
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
  Armed at home is a normal warning.
- It needs the [door and window sensors](SENSORS.md). Without them the options
  are shown, and say so.
- While the alarm is behind the warning, the ordinary *Something is open and
  the house is empty* alert is held back, so you get one message, not two.

## Checking for updates

Settings reads the alarm once a minute, and slows down when there are errors.
When something changes, the page updates by itself. There is no live feed:
Verisure's API does not offer one to third parties. So Away can start up to a
minute after the alarm is armed.

## The demo alarm

In demo mode the source can be **Demo**: an invented alarm with a front-door
lock, with buttons for arming and locking in Settings. It follows exactly the
same rules, so the whole feature can be tried with no Verisure account. It is
refused on an installation connected to a real hub, where it could put a real
house on Away.

## Files

| File | What |
| --- | --- |
| `app/alarm_provider.py` | The reading every source produces, and the demo alarm |
| `app/alarm_verisure.py` | Verisure: sign-in, the read-only guard, the sign-in file |
| `app/alarm_automation.py` | The rules: what counts as leaving, when to set and lift Away |
| `app/alarm_persistence.py` | Settings, the Away ledger, the demo alarm's state, the sign-in file |
| `app/server.py` | Polling, the heating, the warning, `/api/alarm/*` |
