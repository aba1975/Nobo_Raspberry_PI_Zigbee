# Contact sensor provider boundary

Contact sensors are optional and off by default. The first provider is a
persisted simulator for demo mode; it does not require a Zigbee dongle, MQTT
broker, Zigbee2MQTT, or any additional service.

The application consumes normalized contact snapshots rather than MQTT topics
or vendor payloads. A provider supplies:

- an opaque stable provider ID and application sensor ID;
- a display name and assigned Nobø zone ID;
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

## Heating ownership

The optional Eco action never restores Comfort. It only takes an otherwise
unowned room that is demanding Comfort, marks the resulting zone Eco override
as automation-owned, and releases that exact ownership with a Nobø `NORMAL`
zone override after every assigned contact explicitly closes. Normal then lets
the current global mode or weekly schedule decide what happens.

Any manual or external takeover ends ownership for the current open cycle. An
unknown or unavailable contact is not closed and therefore cannot release an
existing left-open warning or automation-owned override.

## Monitoring-only rooms

Sensors attach to the application's existing zone IDs. A zone may contain no
Nobø components: it still receives sensor state and warnings, while the Eco
action is unavailable because there is no heater to control. This avoids a
second room model that would drift from the heating UI.
