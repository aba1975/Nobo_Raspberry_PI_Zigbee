# Zigbee sensor support

Off unless asked for. See `docs/SENSORS.md` for the design and
`README.md` for the user-facing description.

    # .env
    COMPOSE_PROFILES=zigbee
    NOBO_ZIGBEE_ADAPTER=/dev/serial/by-id/usb-ITead_Sonoff_Zigbee_3.0_USB_Dongle_Plus_<serial>-if00-port0

Find the adapter path with `ls -l /dev/serial/by-id/`. Use that path and never
`/dev/ttyUSB0`: USB numbering is not stable across reboots, and pointing a
coordinator at the wrong adapter is not a failure that announces itself.

Zigbee2MQTT keeps its network key and device database in the
`zigbee2mqtt-data` volume. **Losing it means re-pairing every sensor by hand.**

## Moving a dongle to another Pi

The sensors stay paired to the *coordinator*, not to the Pi, but the network
key lives in that volume. Moving the dongle without it means every sensor must
be paired again.

Sensor names, door/window types and room assignments are keyed by IEEE address
and live in the application's own `data/zigbee_sensor_metadata.json`. Carry
that file across and re-paired sensors come back to the rooms and names they
already had, instead of arriving anonymous.
