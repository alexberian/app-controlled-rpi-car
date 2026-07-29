# rpi-car — Architecture & Reference

Authoritative design document. Read this in full before writing or modifying code in
this repo. Where this document and the code disagree, the code is a bug unless the
document has been explicitly updated in the same change.

---

## 1. What this is

A Bluetooth-controlled RC car built on a Raspberry Pi Zero W, driven by a custom
Android app. The Pi runs a small Python service that accepts drive commands over a
Bluetooth serial link and actuates **relays** that switch motor power.

Two deliverables:

- `pi/` — the Python control service (this is what we build first)
- `android/` — the Android controller app (built second)

The owner has an EE background. Hardware-level reasoning does not need to be
simplified or hedged, but hardware **safety invariants must still be enforced in
software** — see §6.

---

## 2. Hardware baseline

| Item | Spec | Consequence for software |
|---|---|---|
| Raspberry Pi Zero W | BCM2835, 1GHz single core, 512MB | Single-threaded-ish. Avoid busy loops; asyncio, not thread-per-connection. |
| Bluetooth | BCM43438, BT 4.1 (Classic + BLE), shared antenna/chip with WiFi | Classic SPP available. Heavy WiFi traffic degrades BT latency. |
| GPIO | 3.3V logic, ~16mA/pin, ~50mA total budget | **Cannot drive relay coils directly.** Opto-isolated relay module required. |
| Motor switching | **Relay module** | On/off only. No PWM. No variable speed. Direction via polarity reversal. |
| Motors | Small brushed DC | Inductive + back-EMF. Contact arcing is the wear mechanism. |
| Batteries | Lithium AA | See §2.2 — chemistry matters, and the Pi needs its own rail. |

### 2.1 Relay topology (the one that is safe by construction)

Use **two SPDT/DPDT relays per side** (4 channels total for a 2-side skid-steer car).
Wire each side as a relay H-bridge:

```
        V+                    V+
         |                     |
        NO                    NO
   RLY_A COM ---- MOTOR ---- COM RLY_B
        NC                    NC
         |                     |
        GND                   GND
```

Resulting truth table per side:

| RLY_A | RLY_B | Motor terminals | Result |
|---|---|---|---|
| off | off | GND / GND | **STOP** (shorted = dynamic brake) |
| on  | off | V+  / GND | FORWARD |
| off | on  | GND / V+  | REVERSE |
| on  | on  | V+  / V+  | STOP (brake) |

**Why this topology:** each relay is internally break-before-make, so no combination of
coil states can short V+ to GND. Shoot-through is physically impossible. Do not
substitute a topology where one relay does "enable" and another does "direction" — that
one *can* short if contacts are slow.

Note the de-energized state is STOP. This is the fail-safe state and it is reached
passively — power loss, software crash, and cable pull all coast to brake. Preserve
this property in any hardware change.

### 2.2 Power

- "Lithium AA" is ambiguous and the software should not assume: **L91 primaries** are
  1.5V nominal (4S = 6V), **14500 Li-ion** are 3.7V nominal (4S = 14.8V). Confirm which
  before sizing anything. Relay coils are typically 5V — a 14.8V pack needs a regulator
  for the coil rail.
- **Do not power the Pi from the motor pack rail directly.** Motor inrush and relay coil
  transients cause brownouts, and a brownout mid-write corrupts the SD card. Either give
  the Pi its own pack, or a dedicated 5V buck converter with ≥1000µF bulk capacitance at
  the Pi end.
- Most cheap relay boards tie `JD-VCC` to `VCC` with a jumper, which defeats the
  optoisolation and injects coil noise into the Pi's 5V rail. **Remove that jumper** and
  feed `JD-VCC` from the motor-side regulated rail, sharing only GND.
- Put a snubber across each motor's terminals. Because polarity reverses, a plain flyback
  diode is wrong — use a bidirectional TVS or an RC snubber (e.g. 100nF + 100Ω).

### 2.3 Boot-state hazard

Pi GPIOs come up as **inputs** — floating or weakly pulled — before userspace runs. If
the relay board is active-HIGH, that float can energize relays and the car drives itself
across the room before the service starts.

Mitigations, apply all three:

1. Use an **active-LOW** relay module (nearly all opto-isolated boards are), so
   undriven/high = coil off = STOP.
2. Force the pins to a safe level at boot in `/boot/firmware/config.txt`:
   `gpio=5,6,13,19=op,dh`
3. On service start, drive all four pins to the OFF level **before** the transport is
   allowed to accept a connection.

---

## 3. Drive model

No variable speed. Each side has exactly three states:

```
FORWARD (+1) | STOP (0) | REVERSE (-1)
```

The app's control surface is landscape, split into two vertical touch zones:

```
┌──────────────────────┬──────────────────────┐
│        ▲ FWD         │        ▲ FWD         │
│                      │                      │
│      LEFT SIDE       │      RIGHT SIDE      │
│    (both L wheels)   │    (both R wheels)   │
│                      │                      │
│        ▼ REV         │        ▼ REV         │
└──────────────────────┴──────────────────────┘
```

Dragging up from a zone's rest position → that side FORWARD. Dragging down → REVERSE.
Release → STOP. This is direct tank/skid steering; there is no mixer, no throttle+steer
decomposition. Turning is the operator's job (opposite sides = spin in place).

The app owns the deadzone and hysteresis so the wire only ever carries a settled -1/0/+1.
The Pi still re-validates and rate-limits — never trust the client for relay safety.

**Forward compatibility:** the protocol carries `l` and `r` as numbers, not enums, so
moving to PWM later means widening the range to [-1.0, 1.0] without a protocol version
bump. Code should not assume integer-ness beyond the relay backend, which quantizes at
the last moment.

---

## 4. Transport decision

**Chosen: Bluetooth Classic SPP (RFCOMM), channel 1, UUID `00001101-0000-1000-8000-00805F9B34FB`.**

Rationale:
- Android's `BluetoothDevice.createRfcommSocketToServiceRecord()` gives a plain
  `InputStream`/`OutputStream`. Auto-connect is a bonded-device lookup plus a retry loop —
  roughly 40 lines, versus a GATT server, characteristic table, MTU negotiation, and
  Android's BLE callback maze.
- The link is a byte stream, so it can be driven from a laptop with `rfcomm` + `cat` for
  debugging. That matters a lot during bring-up.
- Latency (~10–20ms) is far below what relay actuation (~5–10ms mechanical) and human
  reaction time need.
- iOS is impossible with SPP. That is an accepted, deliberate trade — the app is
  Android-only.

`transport/base.py` defines the interface so BLE can be added as a peer implementation.
Nothing above the transport layer may import a transport-specific symbol.

The interface is a **`Session` of three synchronous callbacks** — `on_connect`, `on_line`,
`on_disconnect`. Synchronous because the governor's `command()` is, and because a handler
that cannot await cannot let a disconnect overtake the last command that arrived before
it. The transport guarantees `on_disconnect` runs *before* it closes the socket, which is
what makes §6.2 an enforceable property of the link layer rather than an aspiration.

The transport also owns **line splitting and buffer bounding**. `MAX_LINE_BYTES` can only
be checked once a whole line exists, so a peer that never sends a newline is a memory bug
unless the read buffer is capped at the same limit and the over-long line dropped there.

**One client at a time.** A connection attempt while another client is driving is refused
— closed immediately, no session, the existing link untouched. Two clients heartbeating at
10Hz would fight over the relays, and the watchdog cannot distinguish that from a healthy
link. This is a policy of the transport layer, so it holds for TCP and SPP alike.

A `tcp.py` transport ships alongside it. It speaks the identical protocol over WiFi and
exists so the entire stack — protocol, safety, drive, GPIO — can be exercised from a
development machine without touching BlueZ. Bring up TCP first, always.

### 4.1 BlueZ specifics

Python speaks RFCOMM natively:

```python
socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
```

But a raw bound socket publishes **no SDP record**, and Android's
`createRfcommSocketToServiceRecord` does an SDP lookup to find the channel. So the
service must also register an SPP record via D-Bus
`org.bluez.ProfileManager1.RegisterProfile` (UUID 1101, `Role: server`, `Channel: 1`).
Do not reach for the deprecated `sdptool add SP` — it requires bluetoothd's compat mode
and is a dead end on current Pi OS.

Pairing needs a `NoInputNoOutput` agent registered so bonding is Just Works; the adapter
must be `Pairable` and, for first-time setup only, `Discoverable`. Setup lives in
`scripts/setup_bluetooth.sh`.

---

## 5. Wire protocol

Newline-delimited JSON, UTF-8, one object per line. Human-readable on purpose — you can
watch the link with `cat` during bring-up. Full grammar in §5.3.

### 5.1 App → Pi

```json
{"t":"drive","l":1,"r":-1,"seq":42}
```

- `t` — message type
- `l`, `r` — commanded side state, -1 | 0 | 1 (numeric; range widens to [-1.0,1.0] if PWM ever lands)
- `seq` — monotonic uint32, wraps. Used to drop reordered/stale frames and to measure loss.

Sent at a **fixed 10 Hz regardless of whether state changed.** This is the heartbeat; do
not optimize it into change-only updates, because the watchdog in §6 depends on it.

### 5.2 Pi → App

```json
{"t":"state","l":1,"r":-1,"ack":42,"up":12.4,"err":null}
```

Sent at 2 Hz and immediately on any state change or fault. `l`/`r` are the *actual*
applied relay states, which may differ from the command when the safety layer is
intervening (dead-time, dwell limit). The app should render actual, not commanded.

### 5.3 Rules

- Max line length 256 bytes. Longer → drop the line, log, do not disconnect. Enforced
  twice: by the transport's read buffer (§4) and by the codec on the assembled line.
- Malformed JSON or unknown `t` → ignore that line, keep the connection. A single bad
  frame must never take down the link mid-drive.
- Unknown fields are ignored, so the protocol can grow without a version bump.

---

## 6. Safety layer — non-negotiable

These live in `safety.py`, sit between protocol decode and the relay backend, and are
enforced on the Pi. The app is a convenience, not a safety boundary.

1. **Command watchdog.** No valid `drive` frame for 500ms → force STOP on both sides.
   Recovery is automatic once frames resume. This is the single most important behavior
   in the codebase: it is what stops the car when the operator walks out of range.

2. **Disconnect → STOP.** Transport close, error, or EOF forces STOP synchronously
   before the socket is torn down.

3. **Reversal dead-time.** A side may never go FORWARD→REVERSE or REVERSE→FORWARD
   directly. Insert a mandatory STOP of **≥250ms**. Contacts must not close on a motor
   still spinning the other way — that is a contact-welding and arc-erosion event, and
   the current spike browns out the pack.

   The gate keys off **the last direction the motor was actually turning**, not merely the
   previous commanded state. Releasing to STOP and immediately pushing the opposite way is
   the same physical event as a direct reversal — a braked motor is not yet a stationary
   motor — so passing through STOP does not earn an exemption, it only starts the clock.
   `SideGovernor` therefore tracks `_last_moving` and `_moving_ended_at` rather than just
   the current state.

4. **Minimum dwell / relay chatter guard.** A side may not *start moving* within **80ms**
   of its last state change. Relays are rated for ~10⁵ mechanical and ~10⁴ electrical
   operations; a jittery thumb at 10 Hz would burn that budget in an afternoon. Actuate
   only on *change* — a repeated identical command is a heartbeat, not an actuation.

   **Stopping is never gated.** Neither dwell nor dead-time may delay a transition to
   STOP. Both exist to keep the car from starting too eagerly; applying them to a stop
   would mean a released thumb leaves the car driving, which inverts the intent. Contact
   life is still bounded, because every restart after that stop must clear the dwell.

5. **Safe start.** All relay pins driven to OFF before the transport begins accepting
   connections.

6. **Safe stop.** STOP on `SIGTERM`/`SIGINT` and in an `atexit`/`finally` path, so
   `systemctl stop` and crashes both leave the car braked.

Every one of these has a unit test against the mock GPIO backend. A change that makes a
safety test fail is wrong even if it makes something else work.

---

## 7. File structure

```
rpi-car/
├── CLAUDE.md                       # Short pointer + hard rules for agents
├── README.md                       # Human-facing quickstart
├── docs/
│   └── ARCHITECTURE.md             # This file
│
├── pi/
│   ├── pyproject.toml              # deps, ruff/pytest config
│   ├── config/
│   │   └── car.toml                # pin map, timings, device name — all tunables
│   ├── systemd/
│   │   └── rpicar.service
│   ├── scripts/
│   │   ├── setup_bluetooth.sh      # BlueZ agent, pairable, SPP record
│   │   └── install.sh              # venv + systemd install on the Pi
│   ├── src/rpicar/
│   │   ├── __main__.py             # entrypoint: wire everything, signal handlers
│   │   ├── config.py               # load + validate car.toml -> frozen dataclass
│   │   ├── protocol.py             # encode/decode NDJSON, seq handling
│   │   ├── safety.py               # watchdog, dead-time, dwell limiter  [§6]
│   │   ├── drive.py                # DriveController: (l,r) -> relay bank states
│   │   ├── telemetry.py            # periodic state frames
│   │   ├── gpio/
│   │   │   ├── base.py             # RelayBank ABC
│   │   │   ├── lgpio_backend.py    # real hardware (lgpio, Pi OS Bookworm)
│   │   │   └── mock_backend.py     # records transitions; dev + tests
│   │   └── transport/
│   │       ├── base.py             # Transport ABC: async byte-line stream
│   │       ├── tcp.py              # dev transport over WiFi
│   │       └── spp.py              # RFCOMM + D-Bus SDP registration
│   └── tests/
│       ├── test_protocol.py
│       ├── test_safety.py          # the important one
│       └── test_drive.py
│
└── android/                        # phase 2
```

### 7.1 Layering

```
transport (bytes)  ->  protocol (frames)  ->  safety (gated states)  ->  drive  ->  gpio
```

Dependencies point one direction only. `drive.py` must not know that Bluetooth exists;
`safety.py` must not know what JSON is. This is what makes the mock backend and the TCP
transport useful rather than ceremonial.

---

## 8. Conventions

- **Python 3.11+**, `asyncio` throughout. No threads except where a C library forces it.
- **GPIO library: `lgpio`.** `RPi.GPIO` is unmaintained and broken on Bookworm's
  gpiochip interface; `pigpio` needs a daemon and its hardware-PWM advantage is
  irrelevant to relays. `gpiozero` on top of lgpio is acceptable if it earns its keep.
- **Every tunable lives in `car.toml`** — pin numbers (BCM numbering), active level,
  all timings from §6, device name, transport selection. No magic numbers in code.
- Type hints on all public functions. `ruff` for lint and format.
- Log at INFO for state transitions and connection events, DEBUG for per-frame. Never log
  per-frame at INFO — it is 10 Hz and it will fill the SD card.

---

## 9. Development workflow

Do not develop against real hardware first. The order is:

1. `MOCK` backend + `tcp` transport, on the dev machine. Run the tests.
2. `MOCK` backend + `tcp` transport, on the Pi. Confirms deployment and the service unit.
3. `MOCK` backend + `spp` transport, on the Pi. Confirms BlueZ, pairing, and the app's
   auto-connect — with the motors electrically incapable of moving.
4. `lgpio` backend, wheels off the ground, hand on the battery disconnect.
5. Wheels down.

Step 3 is where most of the pain will be, and doing it with a mock backend means a bug
there costs a debugging session instead of a wall.

---

## 10. Open items

- **Transport was chosen without owner sign-off** (§4). SPP is the right default for
  Android-only auto-connect, but it is the one architectural call made unilaterally.
  Revisit before the Android app's connection layer is written.
- Battery chemistry unconfirmed (§2.2) — blocks regulator and coil-rail sizing.
- Relay channel count unconfirmed. Design assumes 4 (two per side).
- No battery telemetry in v1. The `state` frame has room for it; ADC hardware would be
  needed since the Pi has none.
