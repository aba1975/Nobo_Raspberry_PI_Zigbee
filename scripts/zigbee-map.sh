#!/usr/bin/env bash
set -euo pipefail

# Nobo Web Control — Zigbee network map
#
# Asks Zigbee2MQTT to walk the mesh and report who is talking to whom, then
# prints a summary and leaves the full map on disk.
#
# Usage: bash scripts/zigbee-map.sh            (summary + .dot file)
#        bash scripts/zigbee-map.sh 240        (allow a longer scan)
#
# Why this exists
# ---------------
# Link quality in the interface grades the *last hop*, so a sensor reporting
# through a repeater looks healthy no matter how far it is from the Pi. That
# is the right number for "does this spot need a repeater?" and the wrong one
# for "is there a repeater at all?". This answers the second question: it names
# every router in the network, and shows which parent each end device chose.
#
# A network with a coordinator and nothing but battery sensors has no mesh. It
# has a hub and spokes, and every weak sensor is weak for the same reason.
#
# The scan interrogates each router in turn and is slow — a minute or two is
# normal, and it is heavier than ordinary traffic, so do not run it in a loop.
#
# No root needed, as long as your user is in the docker group.

TIMEOUT="${1:-180}"
BROKER_CONTAINER="nobo-mosquitto"
BASE_TOPIC="zigbee2mqtt"
OUT_DIR="${ZIGBEE_MAP_DIR:-$PWD}"
STAMP=$(date +%Y%m%d-%H%M%S)
DOT_FILE="$OUT_DIR/zigbee-map-$STAMP.dot"

if ! docker info >/dev/null 2>&1; then
    echo "Cannot talk to Docker." >&2
    echo "Either the daemon is not running, or your user is not in the docker group." >&2
    echo "Try:  sudo bash scripts/zigbee-map.sh" >&2
    exit 1
fi

if ! docker inspect "$BROKER_CONTAINER" >/dev/null 2>&1; then
    echo "The broker container '$BROKER_CONTAINER' is not there." >&2
    echo "The Zigbee stack is behind a Compose profile; set COMPOSE_PROFILES=zigbee in .env." >&2
    exit 1
fi

if [ "$(docker inspect -f '{{.State.Running}}' "$BROKER_CONTAINER")" != "true" ]; then
    echo "The broker container '$BROKER_CONTAINER' exists but is not running." >&2
    exit 1
fi

# The request and the answer are two separate messages, and the answer can
# arrive before a subscription made afterwards would have been established.
# Subscribe first, in the background, then publish.
request() {
    local type="$1" outfile="$2"
    local sub_log
    sub_log=$(mktemp)

    docker exec "$BROKER_CONTAINER" \
        mosquitto_sub -h 127.0.0.1 -t "$BASE_TOPIC/bridge/response/networkmap" \
        -C 1 -W "$TIMEOUT" >"$sub_log" 2>/dev/null &
    local sub_pid=$!

    # Give the subscription a moment to land before asking for the scan.
    sleep 1

    docker exec "$BROKER_CONTAINER" \
        mosquitto_pub -h 127.0.0.1 -t "$BASE_TOPIC/bridge/request/networkmap" \
        -m "{\"type\":\"$type\",\"routes\":false}"

    if ! wait "$sub_pid"; then
        rm -f "$sub_log"
        return 1
    fi

    mv "$sub_log" "$outfile"
}

echo "Nobo Web Control — Zigbee network map"
echo "Scanning the mesh. This interrogates every router and takes a minute or two..."

RAW_FILE=$(mktemp)
if ! request raw "$RAW_FILE"; then
    echo "No answer within ${TIMEOUT}s." >&2
    echo "Zigbee2MQTT may still be scanning; try again with a larger timeout:" >&2
    echo "  bash scripts/zigbee-map.sh 300" >&2
    rm -f "$RAW_FILE"
    exit 1
fi

# The summary is the point of the script, so it is derived here rather than
# left to the reader of a .dot file. python3 is already a dependency of the
# install scripts.
python3 - "$RAW_FILE" <<'PY'
import json, sys

with open(sys.argv[1], encoding="utf-8") as handle:
    message = json.load(handle)

if message.get("status") != "ok":
    print(f"Zigbee2MQTT refused the scan: {message.get('error', 'no reason given')}")
    raise SystemExit(1)

value = message["data"]["value"]
nodes = value.get("nodes", [])
links = value.get("links", [])

by_addr = {n["ieeeAddr"]: n for n in nodes}
routers = [n for n in nodes if n.get("type") == "Router"]
enddevices = [n for n in nodes if n.get("type") == "EndDevice"]
coordinator = next((n for n in nodes if n.get("type") == "Coordinator"), None)


def label(node):
    return node.get("friendlyName") or node.get("ieeeAddr", "?")


print()
print(f"  Coordinator : {label(coordinator) if coordinator else 'not reported'}")
print(f"  Routers     : {len(routers)}")
print(f"  End devices : {len(enddevices)}")
print()

if not routers:
    if enddevices:
        print("  No routers. Every sensor is talking straight to the dongle, so the")
        print("  mesh cannot extend past the dongle's own radius. One mains-powered")
        print("  plug or relay between the dongle and the weak end of the building")
        print("  is the single change that will help most.")
    else:
        print("  No routers, and nothing paired yet. Pair a mains-powered plug or")
        print("  relay first: it joins as a router, and every sensor paired")
        print("  afterwards can then choose it as a parent.")
    print()
else:
    print("  Routers in the mesh:")
    for node in sorted(routers, key=label):
        print(f"    {label(node)}")
    print()

# Which parent each end device actually chose, and how the link was scored.
# A device parented on the coordinator is not using the mesh at all.
parents = {}
for link in links:
    source = link.get("source", {}).get("ieeeAddr")
    target = link.get("target", {}).get("ieeeAddr")
    if source is None or target is None:
        continue
    node = by_addr.get(source)
    if node is None or node.get("type") != "EndDevice":
        continue
    quality = link.get("linkquality")
    best = parents.get(source)
    if best is None or (quality is not None and quality > best[1]):
        parents[source] = (target, quality if quality is not None else -1)

if enddevices:
    print("  End devices, and what they report through:")
    unheard = 0
    for node in sorted(enddevices, key=label):
        chosen = parents.get(node["ieeeAddr"])
        if chosen is None:
            unheard += 1
            print(f"    {label(node):<28} not heard during the scan")
            continue
        parent = by_addr.get(chosen[0])
        via = label(parent) if parent else chosen[0]
        kind = parent.get("type") if parent else "?"
        quality = chosen[1]
        # A link can be reported with no score at all. Calling that "fair"
        # would invent a measurement, which is the one thing this must not do.
        if quality < 0:
            verdict, shown = "quality not reported", ""
        else:
            verdict = "weak" if quality < 50 else "fair" if quality < 100 else "good"
            shown = f"LQI {quality} "
        direct = " (direct to coordinator)" if kind == "Coordinator" else ""
        print(f"    {label(node):<28} via {via}  {shown}{verdict}{direct}")
    print()
    if unheard:
        # Expected, not a fault. A scan reads each router's neighbour table,
        # and a sleeping contact sensor is often absent from every one of them
        # until it next wakes and speaks.
        print(f"  {unheard} of {len(enddevices)} were in no neighbour table at scan time.")
        print("  Sleeping sensors frequently are not; it is not a fault, and it is")
        print("  not evidence of a weak link either way.")
        print()
else:
    print("  No end devices are paired, so there is nothing to route yet.")
    print()
PY

# Keep the graphviz form too: it is the one that can be drawn, and a picture of
# the mesh is worth more than a list when deciding where a repeater goes.
DOT_RAW=$(mktemp)
if request graphviz "$DOT_RAW"; then
    python3 - "$DOT_RAW" "$DOT_FILE" <<'PY'
import json, sys

with open(sys.argv[1], encoding="utf-8") as handle:
    message = json.load(handle)

if message.get("status") == "ok":
    with open(sys.argv[2], "w", encoding="utf-8") as out:
        out.write(message["data"]["value"])
    print(f"  Full map written to {sys.argv[2]}")
    print("  Draw it with:  dot -Tpng <file> -o map.png")
    print("  or paste it into https://dreampuf.github.io/GraphvizOnline/")
PY
fi

rm -f "$RAW_FILE" "$DOT_RAW"
