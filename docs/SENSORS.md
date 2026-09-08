# Contact sensor provider boundary

Contact sensors are optional and off by default. The first provider is a
persisted simulator for demo mode; it does not require a Zigbee dongle, MQTT
broker, Zigbee2MQTT, or any additional service.

The application consumes normalized contact snapshots rather than MQTT topics
or vendor payloads. A provider supplies:

- an opaque stable provider ID and application sensor ID;
- a display name, door/window type, and assigned Nobø zone ID;
- contact state (`open`, `closed`, or `unknown`);
- availability, battery percentage, change time, and last-seen time;
- async lifecycle and CRUD/pairing operations;
- a callback whenever a snapshot changes.

`sensor_provider.py` defines that contract. `sensor_simulated.py` is the only
implementation in this release. The API, warning aggregation, WebSocket
payloads, UI, persistence policy, and heating automation do not depend on its
storage format.

## Future Zigbee2MQTT provider

A future provider can subscribe to Zigbee2MQTT device and bridge topics,
translate `contact`, `availability`, and `battery` values into the normalized
snapshot, and translate pairing requests into Zigbee2MQTT permit-join
operations. Provider configuration and credentials must remain outside sensor
records and must not be returned by the API.

That integration is not implemented or verified here. In particular, this
release makes no claim about dongle discovery, Zigbee mesh reliability,
Zigbee2MQTT topic variants, retained MQTT messages, or physical sensor battery
reporting.

## Pairing and management

Settings contains only the feature switch, provider status, paired count, and
an **Add sensor** entry point. Pairing collects the physical type (door or
window), a useful name, and the zone assignment. Sensors are then managed in
their zone, beside the state they report, rather than growing one unbounded
list in Settings. The simulated provider completes pairing immediately; a
future Zigbee2MQTT provider will use the same flow while permit-join is active.

Existing schema-v1 simulated records predate the type field and migrate to
`window`, which preserves them without guessing from a user-editable name.
Schema-v1 and schema-v2 zone policies migrate with **Sensor override** off.

## What a rule may do to the heating

Each zone chooses a warning delay and, separately, one thing to do while a
contact of its own is open:

| Action | What it does |
| --- | --- |
| Do nothing | Warn only. The heating is untouched. |
| Set to Away | Hold the fixed 7 °C anti-frost temperature. |
| Set to Eco | Hold the zone's eco temperature. |
| Set to Comfort | Hold the zone's comfort temperature. |
| Return to schedule | Let go of any hold on the room so its schedule decides. |

The first four either hold an override or hold nothing. **Return to schedule**
is the odd one out: it cancels a zone override rather than creating one, so
there is nothing to give back when the contact closes. It is one-shot and
self-limiting — once the hold is gone there is nothing left to release.

### Warmth has an order

`off` is colder than `away`, which is colder than `eco`, which is colder than
`comfort`. A contact rule is a safety net, not a thermostat, so by default it
may only move a room *down* that order:

- an Eco rule leaves a room that is already Away alone;
- a Comfort rule cannot pull an Away or Eco room up;
- **Return to schedule** will not release a manual Away hold onto a Comfort
  week profile, because letting go would warm the room just as surely as
  setting it.

The per-zone **Override colder modes** switch is the only way past this. With
it on, the chosen action applies whatever the room is doing, until a global
mode or the zone itself is set by hand. Switching it back off hands back any
hold that only existed because it was on.

Judging "would this warm the room?" needs two facts about a zone, and the
server sends both: what it is running now, and what it would run with its own
override cancelled — the global override if one is active and the zone follows
it, otherwise the week profile.

### Ownership

An override this automation applies is written down with its exact mode. When
every assigned contact reports closed, exactly that override is cancelled with
a Nobø `NORMAL`; the current global mode or week profile then decides what the
room does. Comfort is never sent to "restore" a room, because there is no
record that Comfort is where it came from.

Anything that stops matching what we applied — a person in the official app, a
global mode, an away exception — ends ownership immediately and without a
command, and suppresses further sensor actions until every contact has closed.
An unknown or unavailable contact is not closed, so it can neither clear a
left-open warning nor release a hold.

When a rule stands down it says why, and the interface repeats it: the room is
already colder, it is held by hand, it has no heater, or the hub is
unreachable. A rule that is deliberately staying out of the way and a rule that
is broken look identical otherwise.

### Simulated hub state

Demo mode has to answer "what would this room fall back to?", which means it
has to model both kinds of override the hub keeps. The active global mode and
the set of zones under their own override live in `data/server_state.json`, so
a restart knows what the simulated hub is holding — exactly as a real hub
remembers its overrides across a power cut.

## Monitoring-only rooms

Sensors attach to the application's existing zone IDs. A zone may contain no
Nobø components: it still receives sensor state and warnings, while heating
actions are unavailable because there is no heater to control. This avoids a
second room model that would drift from the heating UI.
