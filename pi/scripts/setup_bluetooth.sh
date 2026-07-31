#!/usr/bin/env bash
#
# One-time Bluetooth setup for the car, and the pairing window for a new phone.
#
# The service does not do any of this itself. `transport/spp.py` publishes the
# SPP record and takes connections; who is *allowed* to connect is decided by
# the bond, and creating a bond needs a pairing agent and a discoverable
# adapter. Both are setup concerns, and neither should be true while the car is
# just driving around -- see docs/ARCHITECTURE.md section 4.1.
#
# Run it once per phone:
#
#   ./scripts/setup_bluetooth.sh              # 180s pairing window
#   ./scripts/setup_bluetooth.sh --window 60
#   ./scripts/setup_bluetooth.sh --no-pairing # alias + pairable only, no window
#
# The bond survives reboots, so once a phone is paired the car never needs to be
# discoverable again.

set -euo pipefail

WINDOW=180
OPEN_WINDOW=1

while [[ $# -gt 0 ]]; do
	case "$1" in
	--window)
		WINDOW="${2:?--window needs a number of seconds}"
		shift 2
		;;
	--no-pairing)
		OPEN_WINDOW=0
		shift
		;;
	-h | --help)
		sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	*)
		echo "unknown argument: $1" >&2
		exit 2
		;;
	esac
done

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
config="${RPICAR_CONFIG:-$here/../config/car.toml}"

if [[ ! -f $config ]]; then
	echo "no config at $config (set RPICAR_CONFIG to override)" >&2
	exit 2
fi

# car.toml is the single source of truth for the adapter alias -- it is the name
# the app matches a bonded device on. Parsed with tomllib rather than sed so a
# quoted-string edge case cannot silently rename the car.
name="$(python3 -c '
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    print(tomllib.load(f)["car"]["name"])
' "$config")"

channel="$(python3 -c '
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    print(tomllib.load(f)["transport"]["spp"]["channel"])
' "$config")"

if ! systemctl is-active --quiet bluetooth; then
	echo "bluetooth.service is not running; start it first" >&2
	exit 1
fi

echo "adapter alias    : $name"
echo "rfcomm channel   : $channel (published by the service, not by this script)"

# `bluetoothctl` holds its agent only for as long as it is running, which is
# exactly what we want: the agent exists for the pairing window and then goes
# away with it. Feeding it on stdin with a sleep in the middle is what keeps it
# alive that long -- a here-doc without the sleep would register the agent and
# immediately drop it again.
#
# Two non-obvious things about driving it this way, both of which produce a
# *silent* wrong capability rather than an error:
#
#   * It prints "Waiting to connect to bluetoothd..." and only then starts
#     reading commands. Anything piped in before that is answered with "Failed
#     to register agent object", so the pipe opens with a sleep.
#
#   * It registers an agent of its own at startup, with the default
#     (DisplayYesNo) capability. `agent NoInputNoOutput` on top of that replies
#     "Agent is already registered" and changes nothing -- the capability stays
#     DisplayYesNo, pairing becomes numeric comparison instead of Just Works,
#     and the phone asks the car to confirm a passkey it has no way to confirm.
#     `agent off` first is what makes the capability ours.
if [[ $OPEN_WINDOW -eq 1 ]]; then
	echo "pairing window   : ${WINDOW}s -- put the phone into pairing mode now"
	{
		sleep 2
		echo "power on"
		sleep 1
		# NoInputNoOutput makes bonding Just Works. The car has no keypad and no
		# display, so any agent that can be asked to confirm a passkey would
		# simply block pairing forever.
		echo "agent off"
		sleep 1
		echo "agent NoInputNoOutput"
		sleep 1
		echo "default-agent"
		sleep 1
		echo "system-alias $name"
		echo "pairable on"
		echo "discoverable on"
		sleep "$WINDOW"
		# Closed again on the way out. A car that stays discoverable is a car
		# any passer-by can enumerate; the bond is what matters from here on.
		echo "discoverable off"
		echo "quit"
	} | bluetoothctl
	echo "pairing window closed; discoverable off"
else
	{
		sleep 2
		echo "power on"
		sleep 1
		echo "system-alias $name"
		echo "pairable on"
		echo "discoverable off"
		echo "quit"
	} | bluetoothctl
fi

echo
echo "paired devices:"
bluetoothctl devices Paired || true

echo
echo "current adapter state:"
bluetoothctl show | sed -n '/Powered/p;/Discoverable:/p;/Pairable/p;/Alias/p'
