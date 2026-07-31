# rpi-car

A Bluetooth-controlled RC car: a Raspberry Pi Zero W switching motor **relays**, driven by
a custom Android app.

No variable speed by design. Each side of the car has exactly three states — forward,
stop, reverse — and steering is skid/tank style: drive the sides in opposite directions to
spin in place.

Two deliverables:

| Path | What | Status |
|---|---|---|
| `pi/` | Python control service for the Pi | Drivable over Bluetooth and TCP; runs as a systemd service |
| `android/` | Android controller app | Not started (phase 2) |

## Documentation

- **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** — the authoritative design document:
  hardware topology, wire protocol, safety invariants, and the reasoning behind each.
  Read it before changing code.
- **[`docs/HANDOFF.md`](docs/HANDOFF.md)** — what is built, what is next, and the traps.
- **[`CLAUDE.md`](CLAUDE.md)** — the short version, for coding agents.

## Quickstart (development machine, no hardware)

The whole stack is designed to run on a laptop. `pi/config/car.toml` ships with the `mock`
GPIO backend and the `tcp` transport selected, so nothing touches real pins or BlueZ.

```bash
cd pi
python3 -m venv .venv                  # Python 3.11+
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q          # 204 passed
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

Those three should be clean before you start work. The test suite runs against the mock
GPIO backend with an injected clock, so it is deterministic and takes well under a second.

### Running the service

```bash
.venv/bin/python -m rpicar             # or: .venv/bin/rpicar
```

It listens on `0.0.0.0:9999` and logs every relay transition. Drive it with anything
line-oriented — one JSON object per line, at 10 Hz:

```bash
while :; do echo '{"t":"drive","l":1,"r":-1}'; sleep 0.1; done | nc 127.0.0.1 9999
```

That spins the car in place, and `state` frames come back at 2 Hz. Stop with Ctrl-C; the
relays are de-energized on the way out. `--config PATH` selects a different `car.toml` and
`--log-level DEBUG` logs every frame — at 10 Hz, not something to leave on.

Note the omitted `seq`. It is optional, and an unnumbered frame is always accepted. A
hand-written loop that sends a *fixed* `seq` will drive for 500 ms and then stop, because
every frame after the first is a duplicate — rejected as stale, and a rejected frame is not
a heartbeat. The real app increments it; see ARCHITECTURE.md §5.

Hardware support is an optional extra, deliberately not a hard dependency — `.[hw]` pulls in
`lgpio`, which is Pi-only. `.[spp]` pulls in `dbus-next` for the Bluetooth transport; it is
also in `.[dev]`, because the SPP tests fake BlueZ but still import the module.

### Running it over Bluetooth

On the Pi. Pair the phone once — this is also what sets the adapter alias from `car.name`,
which is the name the app matches a bonded device on:

```bash
./scripts/setup_bluetooth.sh            # 180s pairing window, then discoverable off
```

Then run the service with a config that selects the Bluetooth transport. `car.toml` ships
`kind = "tcp"` deliberately (prove TCP first — see the bring-up order), so keep a local
override; `*.local.toml` is gitignored:

```bash
sed 's/^kind = "tcp"/kind = "spp"/' config/car.toml > config/spp.local.toml
.venv/bin/python -m rpicar --config config/spp.local.toml
```

It publishes an SPP record on RFCOMM channel 1 (UUID `1101`) and takes one client at a
time. Nothing above the transport changes between the two — switching `kind` is the whole
integration. Confirm the record from another machine with
`sdptool search --bdaddr <pi-addr> SP`; `sdptool browse local` on the Pi shows nothing even
when it is working, because it needs bluetoothd's compat mode.

**Pairing is the access control.** `require_authentication = true` means an unbonded device
is refused by BlueZ before the service sees it. Nothing above the transport authenticates
anything, so turn it off only for bring-up, and knowingly.

### Installing it as a service

On the Pi, from the checkout, as the user the service will run as — **not** as root, since
the venv lives in the checkout and `sudo` is used only for the three steps that need it:

```bash
./scripts/install.sh          # venv, /etc/rpicar/car.toml, unit; enable at boot
./scripts/install.sh --now    # ... and start it
./scripts/install.sh --uninstall
```

Enabling without starting is the default: `--now` starts a service that drives relays, and
that is not a decision to make while your hands are in the wiring. Re-running the script is
safe — it reuses the venv, re-renders the unit, and restarts.

```bash
systemctl status rpicar
journalctl -u rpicar -f
sudo systemctl edit rpicar        # drop-in overrides; these survive a reinstall
```

**The service reads `/etc/rpicar/car.toml`**, which `install.sh` seeds from
`config/car.toml` and thereafter leaves alone (`--replace-config` overrides). So pass
`--config config/spp.local.toml` on the first install to get a Bluetooth service, or edit
the installed copy afterwards. Editing the checkout copy changes nothing.

The unit is generated from `systemd/rpicar.service`, which is a template — edit it there and
re-run `install.sh`. Its restart policy leans on the service's exit codes: **2** is a bad
config and is never retried, **1** is a runtime failure (BlueZ not up yet, port already
bound) and is retried ten times over five minutes, **0** follows a signal and is a clean
stop. `systemctl stop` sends SIGTERM, the handler runs, and the relays are de-energized on
the way out.

## How it fits together

```
transport (bytes)  ->  protocol (frames)  ->  safety (gated states)  ->  drive  ->  gpio
```

Dependencies point one direction only. `drive.py` does not know Bluetooth exists;
`safety.py` does not know what JSON is. That is what makes the mock backend and the TCP
transport genuinely useful rather than ceremonial.

The app sends newline-delimited JSON at a fixed 10 Hz — `{"t":"drive","l":1,"r":-1,"seq":42}`
— and the Pi replies at 2 Hz with the state the relays are *actually* in, which can differ
from the command while a safety gate is active.

## Safety model

The safety layer in `pi/src/rpicar/safety.py` is enforced on the Pi. The app is a
convenience, not a safety boundary. See ARCHITECTURE.md §6 for the full reasoning; the
short version:

- **500 ms command watchdog** → STOP. This is what stops the car when the operator walks
  out of range.
- **Disconnect → STOP**, synchronously, before socket teardown.
- **≥250 ms reversal dead-time.** Contacts must not close on a motor still spinning the
  other way.
- **80 ms minimum dwell** between actuations, protecting a finite relay contact budget.
- **Safe start / safe stop.** Pins driven OFF before the transport accepts connections;
  STOP on SIGTERM/SIGINT and in the `finally` path.
- **Stopping is never gated.** No timer may delay a transition to STOP.

De-energized means STOP, in hardware and in software. Power loss, crash, and cable pull
all coast to a braked motor. Every invariant above has a unit test — a change that breaks
one is wrong even if it fixes something else.

## Bring-up order

Do not develop against real hardware. ARCHITECTURE.md §9:

1. ✅ `mock` backend + `tcp` transport, on the dev machine
2. ✅ `mock` + `tcp` on the Pi — proves deployment
3. ✅ `mock` + `spp` on the Pi — proves BlueZ and pairing, with the motors electrically
   incapable of moving
4. `lgpio` backend, wheels off the ground, hand on the battery disconnect
5. Wheels down

Steps 1–3 are done. Step 3 was expected to be where the pain is and it was; doing it against
the mock backend meant the bugs cost a debugging session instead of a wall.

> `lgpio_backend.py` is written against the lgpio API but **has never run on real
> hardware.** Treat step 4 as genuinely unproven, and note that step 3 says nothing about
> it — every relay transition so far has been recorded by the mock backend, not switched.

## Configuration

Every tunable lives in `pi/config/car.toml` — pin map (BCM numbering), active level, all
safety timings, device name, transport selection. A timing or pin number hardcoded in
`src/` is a bug.

## Hardware notes

Two relays per side, wired as a relay H-bridge (4 channels total). Because each relay is
internally break-before-make, no combination of coil states can short V+ to GND —
shoot-through is physically impossible. Do not substitute an "enable + direction"
topology, which *can* short if contacts are slow.

### GPIO pin map

Four outputs, one per relay channel. BCM numbering, matching `[gpio.pins]` in
`pi/config/car.toml` — that file is the source of truth; this diagram follows it.

```
             Raspberry Pi Zero W — 40-pin header, top view
             (pin 1 = square pad, nearest the microSD slot)

                    3V3  ( 1) ( 2)  5V
                  GPIO2  ( 3) ( 4)  5V   ──▶ relay VCC (logic side)
                  GPIO3  ( 5) ( 6)  GND  ──▶ relay GND (logic side)
                  GPIO4  ( 7) ( 8)  GPIO14
                    GND  ( 9) (10)  GPIO15
                 GPIO17  (11) (12)  GPIO18
                 GPIO27  (13) (14)  GND
                 GPIO22  (15) (16)  GPIO23
                    3V3  (17) (18)  GPIO24
                 GPIO10  (19) (20)  GND
                  GPIO9  (21) (22)  GPIO25
                 GPIO11  (23) (24)  GPIO8
                    GND  (25) (26)  GPIO7
                  GPIO0  (27) (28)  GPIO1
    left_a  ◀──   GPIO5  (29) (30)  GND
    left_b  ◀──   GPIO6  (31) (32)  GPIO12
   right_a  ◀──  GPIO13  (33) (34)  GND
   right_b  ◀──  GPIO19  (35) (36)  GPIO16
                 GPIO26  (37) (38)  GPIO20
                    GND  (39) (40)  GPIO21
```

| `car.toml` key | BCM | Header pin | Relay ch. | Energized ⇒ |
|---|---|---|---|---|
| `left_a`  | 5  | 29 | IN1 | left side FORWARD |
| `left_b`  | 6  | 31 | IN2 | left side REVERSE |
| `right_a` | 13 | 33 | IN3 | right side FORWARD |
| `right_b` | 19 | 35 | IN4 | right side REVERSE |

The four signals sit together on pins 29–35, with GND on 30/34 for a short return path.
Nothing else on the header is used — no I²C, SPI, or UART — so pins 1–28 stay free.

The `IN1`–`IN4` column is convention, not a constraint: the software only knows the four
BCM numbers. Any channel order works as long as the physical wiring matches `car.toml`.

**Levels are inverted.** `active_low = true`: the Pi drives a pin **LOW** to energize that
channel's coil. HIGH, floating, and unpowered all mean coil off. Read §2.3 of
ARCHITECTURE.md before changing this — it is what makes the boot-float state safe.

Per-side truth table (ARCHITECTURE.md §2.1). Both coils on is also a brake, but the
software never commands it:

| `_a` | `_b` | Motor terminals | Side state |
|---|---|---|---|
| off | off | GND / GND | **STOP** (shorted = dynamic brake) |
| on  | off | V+ / GND  | FORWARD (`+1`) |
| off | on  | GND / V+  | REVERSE (`-1`) |

### Power domains

Two rails that share only ground. This is the whole point of the opto-isolated board:

```
  LOGIC SIDE                          │  MOTOR SIDE
  (Pi's own 5V supply)                │  (lithium pack)
                                      │
  Pi 5V   (pin 2 or 4)  ──▶ VCC       │  pack V+       ──▶ relay COM/NO/NC
  Pi GND  (pin 6)       ──▶ GND       │  pack 5V reg.  ──▶ JD-VCC
  BCM 5   (pin 29)      ──▶ IN1       │  pack GND      ──▶ board GND
  BCM 6   (pin 31)      ──▶ IN2       │
  BCM 13  (pin 33)      ──▶ IN3       │  contacts      ──▶ motors
  BCM 19  (pin 35)      ──▶ IN4       │
                                      │
        opto LEDs ───────── isolation barrier ───────── coils + contacts
                                      │
              the two sides share GND and nothing else
```

Snubber each motor across its terminals with a bidirectional TVS or an RC network
(100 nF + 100 Ω) — **not** a plain flyback diode. Polarity reverses on this topology, so a
single diode is wrong in one of the two directions.

Three things that will bite you, in full in ARCHITECTURE.md §2:

- Pi GPIOs boot as floating **inputs**. Use an active-LOW relay board and pin the levels in
  `/boot/firmware/config.txt` (`gpio=5,6,13,19=op,dh`), or the car drives itself across the
  room before the service starts.
- **Do not power the Pi from the motor rail.** Brownout mid-write corrupts the SD card.
- Remove the relay board's `JD-VCC`/`VCC` jumper — leaving it defeats the optoisolation it
  was bought for.

## Open questions

Blocking, and listed in ARCHITECTURE.md §10:

1. **Relay channel count** — everything assumes 4, active-low, two per side.
2. **Battery chemistry** — "lithium AA" is either 1.5 V L91 primaries (4S = 6 V) or 3.7 V
   14500 Li-ion (4S = 14.8 V). Blocks sizing the coil rail and the Pi's regulator.

Settled: the transport is **SPP/RFCOMM**, confirmed 2026-07-29 and now built and proven on
hardware. (iOS is impossible with SPP — an accepted trade; the app is Android-only.)
