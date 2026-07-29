# rpi-car

A Bluetooth-controlled RC car: a Raspberry Pi Zero W switching motor **relays**, driven by
a custom Android app.

No variable speed by design. Each side of the car has exactly three states — forward,
stop, reverse — and steering is skid/tank style: drive the sides in opposite directions to
spin in place.

Two deliverables:

| Path | What | Status |
|---|---|---|
| `pi/` | Python control service for the Pi | Core done, link layer in progress |
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
.venv/bin/python -m pytest -q          # 103 passed
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

Those three should be clean before you start work. The test suite runs against the mock
GPIO backend with an injected clock, so it is deterministic and takes well under a second.

Hardware and Bluetooth support are optional extras, deliberately not hard dependencies —
`.[hw]` pulls in `lgpio` (Pi only), `.[spp]` pulls in `dbus-next`.

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

1. `mock` backend + `tcp` transport, on the dev machine
2. `mock` + `tcp` on the Pi — proves deployment and the service unit
3. `mock` + `spp` on the Pi — proves BlueZ and pairing, with the motors electrically
   incapable of moving
4. `lgpio` backend, wheels off the ground, hand on the battery disconnect
5. Wheels down

Step 3 is where the pain is, and a mock backend makes a bug there cost a debugging session
instead of a wall.

> `lgpio_backend.py` is written against the lgpio API but **has never run on real
> hardware.** Treat step 4 as genuinely unproven.

## Configuration

Every tunable lives in `pi/config/car.toml` — pin map (BCM numbering), active level, all
safety timings, device name, transport selection. A timing or pin number hardcoded in
`src/` is a bug.

## Hardware notes

Two relays per side, wired as a relay H-bridge (4 channels total). Because each relay is
internally break-before-make, no combination of coil states can short V+ to GND —
shoot-through is physically impossible. Do not substitute an "enable + direction"
topology, which *can* short if contacts are slow.

Three things that will bite you, in full in ARCHITECTURE.md §2:

- Pi GPIOs boot as floating **inputs**. Use an active-LOW relay board and pin the levels in
  `/boot/firmware/config.txt` (`gpio=5,6,13,19=op,dh`), or the car drives itself across the
  room before the service starts.
- **Do not power the Pi from the motor rail.** Brownout mid-write corrupts the SD card.
- Remove the relay board's `JD-VCC`/`VCC` jumper — leaving it defeats the optoisolation it
  was bought for.

## Open questions

Blocking, and listed in ARCHITECTURE.md §10:

1. **Transport** — SPP/RFCOMM was chosen unilaterally. Right default for Android-only
   auto-connect, but confirm before the app's connection layer is written; after that it
   gets expensive to change. (iOS is impossible with SPP — an accepted trade.)
2. **Relay channel count** — everything assumes 4, active-low, two per side.
3. **Battery chemistry** — "lithium AA" is either 1.5 V L91 primaries (4S = 6 V) or 3.7 V
   14500 Li-ion (4S = 14.8 V). Blocks sizing the coil rail and the Pi's regulator.
