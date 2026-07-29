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
.venv/bin/python -m pytest -q          # 122 passed
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

1. **Transport** — SPP/RFCOMM was chosen unilaterally. Right default for Android-only
   auto-connect, but confirm before the app's connection layer is written; after that it
   gets expensive to change. (iOS is impossible with SPP — an accepted trade.)
2. **Relay channel count** — everything assumes 4, active-low, two per side.
3. **Battery chemistry** — "lithium AA" is either 1.5 V L91 primaries (4S = 6 V) or 3.7 V
   14500 Li-ion (4S = 14.8 V). Blocks sizing the coil rail and the Pi's regulator.
