#!/usr/bin/env bash
#
# Install the drive service on the Pi: venv, config, systemd unit.
#
# Run it from the checkout, as the user the service will run as -- NOT as root.
# The venv lives inside the checkout and the package is installed editable, so
# a root-owned venv in a pi-owned tree is precisely the thing that makes the
# next `git pull && ./scripts/install.sh` fail halfway. The three steps that
# genuinely need privilege (/etc/rpicar, the unit file, systemctl) each go
# through sudo on their own; expect one password prompt.
#
#   ./scripts/install.sh                              # install + enable
#   ./scripts/install.sh --now                        # ... and start it
#   ./scripts/install.sh --config config/spp.local.toml
#   ./scripts/install.sh --uninstall
#
# Enabling but not starting is the default on purpose. `--now` starts a service
# that drives relays, and the moment to decide that is not the moment your
# hands are in the wiring.
#
# This script copies nothing to the Pi. Deploy first if you are working from a
# laptop, then run it over ssh from the checkout on the Pi:
#
#   rsync -a --delete --exclude .venv --exclude __pycache__ \
#         --exclude '*.egg-info' --exclude .pytest_cache \
#         pi/ pi@192.168.0.80:~/rpi-car/pi/

set -euo pipefail

UNIT_NAME="rpicar.service"
UNIT_DEST="/etc/systemd/system/$UNIT_NAME"
CONFIG_DIR="/etc/rpicar"
CONFIG_DEST="$CONFIG_DIR/car.toml"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)" # the pi/ directory
venv="$root/.venv"
template="$root/systemd/rpicar.service"

CONFIG_SRC="$root/config/car.toml"
EXTRAS="spp,hw"
ENABLE=1
START=0
REPLACE_CONFIG=0
UNINSTALL=0

die() {
	echo "install.sh: $*" >&2
	exit 1
}

step() {
	echo
	echo "==> $*"
}

while [[ $# -gt 0 ]]; do
	case "$1" in
	--config)
		CONFIG_SRC="${2:?--config needs a path}"
		shift 2
		;;
	--extras)
		# Pass an empty string for none. `hw` is lgpio, `spp` is dbus-next.
		EXTRAS="${2-}"
		shift 2
		;;
	--replace-config)
		REPLACE_CONFIG=1
		shift
		;;
	--no-enable)
		ENABLE=0
		shift
		;;
	--now)
		START=1
		shift
		;;
	--uninstall)
		UNINSTALL=1
		shift
		;;
	-h | --help)
		sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	*)
		die "unknown argument: $1"
		;;
	esac
done

if [[ $UNINSTALL -eq 1 ]]; then
	step "removing $UNIT_NAME"
	sudo systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
	sudo rm -f "$UNIT_DEST"
	sudo systemctl daemon-reload
	# $CONFIG_DEST and the venv survive: the first is hand-edited state and the
	# second is a working checkout. Neither is this script's to throw away.
	echo "unit removed. $CONFIG_DEST and $venv were left in place."
	exit 0
fi

# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------

[[ $EUID -ne 0 ]] || die "run as the user the service will run as, not as root (sudo is used where it is needed)"
command -v systemctl >/dev/null || die "no systemctl on this machine; there is nothing here to install a unit into"
command -v sudo >/dev/null || die "no sudo; /etc and systemctl need privilege"
[[ -f $root/pyproject.toml ]] || die "expected $root/pyproject.toml -- run this from the checkout"
[[ -f $template ]] || die "missing unit template $template"
[[ -f $CONFIG_SRC ]] || die "no config at $CONFIG_SRC"
# Absolute from here on: it is printed, compared, and handed to sudo install,
# and a relative path would quietly mean something different in each.
CONFIG_SRC="$(cd "$(dirname "$CONFIG_SRC")" && pwd)/$(basename "$CONFIG_SRC")"

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' ||
	die "python3 is $(python3 -V 2>&1); the service needs 3.11+ (tomllib)"

service_user="$(id -un)"

# The unit names both groups in SupplementaryGroups=, and systemd refuses to
# start a unit naming a group that does not exist (216/GROUP) -- a failure mode
# that reads as "the service is broken" rather than "the group is missing".
for group in gpio bluetooth; do
	getent group "$group" >/dev/null ||
		die "no '$group' group on this system, but the unit asks for it in SupplementaryGroups=. Install the package that creates it (raspi-gpio / bluez) or drop it from $template."
done

# Read the two facts that change what the preflight has to check. Parsed with
# tomllib rather than grep, for the same reason setup_bluetooth.sh does.
summary="$(python3 -c '
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    c = tomllib.load(f)
print(c["gpio"]["backend"], c["transport"]["kind"])
' "$CONFIG_SRC")" || die "could not read gpio.backend / transport.kind from $CONFIG_SRC"
read -r backend kind <<<"$summary"

if [[ $backend == lgpio ]] && ! id -nG | grep -qw gpio; then
	# Only affects running it by hand -- the unit grants the group itself.
	echo "note: $service_user is not in the 'gpio' group, so running the service by hand" \
		"cannot open /dev/gpiochip0. The unit can; systemd adds the group itself."
fi

if [[ $kind == spp ]]; then
	systemctl is-active --quiet bluetooth ||
		echo "warning: bluetooth.service is not active; the service will exit 1 and retry until it is"
	# Proves the service user can reach bluetoothd on the system bus, which is
	# what RegisterProfile needs and the one thing a D-Bus policy could take
	# away. Checked as this user because that is who the unit runs as; the unit
	# additionally puts it in the `bluetooth` group, so this is the weaker test
	# of the two and a pass here is a pass there.
	busctl --system --quiet call org.bluez / org.freedesktop.DBus.Peer Ping >/dev/null 2>&1 ||
		die "cannot reach org.bluez on the system bus as $service_user. Check /etc/dbus-1/system.d/bluetooth.conf -- Debian's copy ends with a default-context allow, upstream BlueZ's denies, and an upgrade can swap them."
fi

# --------------------------------------------------------------------------
# venv + package
# --------------------------------------------------------------------------

step "python environment at $venv"
if [[ ! -x $venv/bin/python ]]; then
	# --system-site-packages so the apt-installed lgpio is importable without
	# being rebuilt in here. There is no armv6 wheel, and compiling it on a
	# 1GHz single core is a long wait for a module already on the machine.
	python3 -m venv --system-site-packages "$venv"
	echo "created"
else
	echo "reusing"
fi

target="$root"
[[ -z $EXTRAS ]] || target="$root[$EXTRAS]"
"$venv/bin/pip" install --quiet --editable "$target"
echo "installed rpicar${EXTRAS:+ [$EXTRAS]}"

# The unit sets RestartPreventExitStatus=2, so a config the service rejects is
# a unit that fails once and stays failed. Much better to hear about it here,
# through the same validator the service uses, than out of `systemctl status`.
step "validating $CONFIG_SRC"
"$venv/bin/python" - "$CONFIG_SRC" <<'PY' || die "fix the config before installing it"
import sys

from rpicar.config import Config, ConfigError

try:
    config = Config.load(sys.argv[1])
except ConfigError as exc:
    print(f"config error: {exc}", file=sys.stderr)
    raise SystemExit(1) from None
print(
    f"ok -- {config.car.name}: {config.transport.kind} transport, "
    f"{config.gpio.backend} gpio backend"
)
PY

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

step "config -> $CONFIG_DEST"
sudo install -d -m 0755 "$CONFIG_DIR"
if [[ -f $CONFIG_DEST && $REPLACE_CONFIG -eq 0 ]]; then
	echo "exists; left untouched (--replace-config to overwrite)"
	cmp -s "$CONFIG_SRC" "$CONFIG_DEST" ||
		echo "note: it DIFFERS from $CONFIG_SRC, and the installed copy is the one the service reads"
else
	sudo install -m 0644 -o root -g root "$CONFIG_SRC" "$CONFIG_DEST"
	echo "installed from $CONFIG_SRC"
fi

# --------------------------------------------------------------------------
# unit
# --------------------------------------------------------------------------

step "unit -> $UNIT_DEST"
rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT

python3 - "$template" "$rendered" "$service_user" "$root" "$venv/bin/rpicar" "$CONFIG_DEST" <<'PY' || die "could not render the unit"
import re
import sys

src, dst, user, workdir, exec_path, config = sys.argv[1:7]

text = open(src, encoding="utf-8").read()
for token, value in (
    ("@USER@", user),
    ("@WORKDIR@", workdir),
    ("@EXEC@", exec_path),
    ("@CONFIG@", config),
):
    text = text.replace(token, value)

# An unsubstituted token is a unit that fails at start with a message about a
# path nobody typed. Catch it while the file is still in /tmp.
left = sorted(set(re.findall(r"@[A-Z_]+@", text)))
if left:
    raise SystemExit(f"unsubstituted tokens in {src}: {', '.join(left)}")

header = f"# Generated by scripts/install.sh from {src}. Edit the template, not this.\n"
open(dst, "w", encoding="utf-8").write(header + text)
PY

sudo install -m 0644 -o root -g root "$rendered" "$UNIT_DEST"
sudo systemctl daemon-reload
echo "installed"

if [[ $ENABLE -eq 1 ]]; then
	sudo systemctl enable "$UNIT_NAME" >/dev/null
	echo "enabled at boot"
fi

if [[ $START -eq 1 ]]; then
	# restart, not start, so re-running this script picks up a changed unit.
	sudo systemctl restart "$UNIT_NAME"
	echo "started"
fi

# --------------------------------------------------------------------------

cat <<EOF

service : $UNIT_NAME ($([[ $ENABLE -eq 1 ]] && echo enabled || echo "not enabled"), $([[ $START -eq 1 ]] && echo running || echo "not started"))
user    : $service_user
command : $venv/bin/rpicar --config $CONFIG_DEST
config  : $CONFIG_DEST ($kind transport, $backend gpio backend)

  sudo systemctl start $UNIT_NAME
  systemctl status $UNIT_NAME
  journalctl -u rpicar -f
  sudo systemctl edit rpicar        # drop-in overrides; survive a reinstall
EOF
