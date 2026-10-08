#!/usr/bin/env bash
# End-to-end test: two DirectShare instances in two network namespaces joined by a veth
# pair (a virtual Ethernet cable). Needs $SUDO for the namespaces only; the instances run
# as the calling user, just like the real app.
set -euo pipefail
SUDO="${SUDO:-sudo}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ME="$(id -un)"
WORK="$(mktemp -d)"
trap 'cleanup' EXIT

cleanup() {
    for d in "$WORK"/*/mnt/*; do [ -d "$d" ] && fusermount3 -uz "$d" 2>/dev/null || true; done
    $SUDO ip netns del dsA 2>/dev/null || true
    $SUDO ip netns del dsB 2>/dev/null || true
    rm -rf "$WORK"
}

$SUDO ip netns del dsA 2>/dev/null || true
$SUDO ip netns del dsB 2>/dev/null || true
$SUDO ip netns add dsA
$SUDO ip netns add dsB
$SUDO ip link add dsa0 type veth peer name dsb0
$SUDO ip link set dsa0 netns dsA
$SUDO ip link set dsb0 netns dsB
for ns in dsA dsB; do $SUDO ip -n $ns link set lo up; done
$SUDO ip -n dsA link set dsa0 up
$SUDO ip -n dsB link set dsb0 up
sleep 3  # IPv6 duplicate address detection

mkdir -p "$WORK/A" "$WORK/B" "$WORK/markers"

run() {  # run <netns> <iface> <home> args...
    local ns=$1 ifc=$2 home=$3; shift 3
    # nsenter only switches the network namespace, so the FUSE mounts stay visible.
    $SUDO nsenter --net=/run/netns/$ns $SUDO -u "$ME" env DIRECTSHARE_HOME="$home" DIRECTSHARE_IFACES="$ifc" \
        python3 "$ROOT/tests/headless_peer.py" "$@"
}

status=0
echo "== accept flow =="
run dsB dsb0 "$WORK/B" --role responder --marker-dir "$WORK/markers" & RESP=$!
sleep 1
run dsA dsa0 "$WORK/A" --role initiator --marker-dir "$WORK/markers" || status=1
wait $RESP || status=1

echo "== decline flow =="
run dsB dsb0 "$WORK/B" --role responder --decline --marker-dir "$WORK/markers" & RESP=$!
sleep 1
run dsA dsa0 "$WORK/A" --role initiator --decline --marker-dir "$WORK/markers" || status=1
wait $RESP || status=1

echo "== cable pulled =="
run dsB dsb0 "$WORK/B" --role responder --marker-dir "$WORK/markers" & RESP=$!
sleep 1
run dsA dsa0 "$WORK/A" --role initiator --hold --marker-dir "$WORK/markers" & INIT=$!
sleep 15
$SUDO ip -n dsA link set dsa0 down
wait $INIT || status=1
wait $RESP || status=1

[ $status -eq 0 ] && echo "E2E: PASS" || echo "E2E: FAIL"
exit $status
