# Handoff — state of the build

Written 2026-07-26. Last updated 2026-07-29. Update this file when you finish a chunk; it
is the "where were we" document. `ARCHITECTURE.md` is the design and does not change often
— this one does.

**Read `ARCHITECTURE.md` first.** This file assumes it.

---

## Where things stand

The Pi service's safety-critical core and its wire codec are **complete, tested, and
lint-clean**. Nothing talks to a network or a Bluetooth stack yet — `protocol.py` is handed
lines by a caller that does not exist.

```
rpi-car/
├── .gitignore              excludes .venv/, caches, egg-info, Android build output
├── README.md               human-facing quickstart
└── pi/
    ├── pyproject.toml      ruff + pytest config; lgpio and dbus-next are optional extras
    ├── .venv/              dev venv (pytest was not available system-wide)
    ├── config/car.toml     every tunable in the system, heavily commented
    ├── src/rpicar/
    │   ├── __init__.py
    │   ├── config.py       TOML -> validated frozen dataclasses      DONE
    │   ├── drive.py        SideState/DriveState + H-bridge truth map DONE
    │   ├── safety.py       watchdog / dead-time / dwell governor     DONE
    │   ├── protocol.py     NDJSON codec, SeqTracker                  DONE
    │   └── gpio/
    │       ├── base.py         RelayBank ABC, CoilStates             DONE
    │       ├── mock_backend.py records transitions, used by all tests DONE
    │       ├── lgpio_backend.py real hardware                        DONE, UNTESTED ON HW
    │       └── __init__.py     create_relay_bank() factory           DONE
    └── tests/
        ├── conftest.py     FakeClock, Rig, FailingRelayBank
        ├── test_drive.py       28 tests
        ├── test_safety.py      19 tests — one per ARCHITECTURE.md section 6 invariant
        └── test_protocol.py    56 tests
```

Verify with:

```bash
cd pi
.venv/bin/python -m pytest -q          # expect: 103 passed
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

If those three are not clean, fix that before doing anything else.

The repo was initialised on 2026-07-29 (branch `main`, one commit, no remote). `car.toml`
is tracked; `*.local.toml` is ignored for per-machine overrides.

---

## What is left, in order

Do them in this order. Each depends on the one before it.

### 1. `transport/base.py` + `transport/tcp.py`

Async line-oriented byte stream. The interface needs a disconnect notification, because
`SafetyGovernor.on_disconnect()` must run **synchronously before socket teardown**
(`ARCHITECTURE.md` 6.2). Do not make that a fire-and-forget task.

The transport owns line splitting and hands whole lines to `decode_line`. Note that
`MAX_LINE_BYTES` is checked *after* a line exists, so the transport must bound its own read
buffer as well — a peer that never sends a newline would otherwise buffer without limit.
`asyncio.StreamReader.readuntil` takes a `limit` and raises `LimitOverrunError`; catch it,
discard to the next newline, and keep the connection.

Call `SeqTracker.reset()` on disconnect. The next client starts its own numbering and a
retained position would reject everything it sends until it happened to pass us.

TCP exists so the whole stack runs on a laptop. Build and prove it before touching BlueZ.

### 2. `telemetry.py` + `__main__.py`

Telemetry: 2 Hz periodic state frames, plus an immediate frame on any change or fault.
Send `gov.applied` (what the relays are doing), **not** `gov.target` — the app renders
actual state, and they legitimately diverge while a gate is active. `encode_state` already
takes `applied`, `ack`, `uptime_s`, and `err`; fill `ack` from `SeqTracker.last` and `err`
from `GateReason.as_wire()`, which exists and is still called by nothing.

`__main__.py` assembles config → `create_relay_bank` → `DriveController` →
`SafetyGovernor` → protocol → transport, and installs SIGTERM/SIGINT handlers.
`SafetyGovernor.run()` already stops the car in its `finally`, so the handler mainly needs
to cancel the task and let it unwind.

`pyproject.toml` already declares the `rpicar` console script pointing at
`rpicar.__main__:main`. Until this lands, an editable install succeeds and the `rpicar`
command fails at runtime.

### 3. `transport/spp.py`

RFCOMM listener plus a D-Bus SDP record. Details and the reasoning are in
`ARCHITECTURE.md` section 4.1. The trap: a raw bound `AF_BLUETOOTH` socket publishes no
SDP record, and Android's `createRfcommSocketToServiceRecord` needs one to find the
channel. Register via `org.bluez.ProfileManager1.RegisterProfile`, not `sdptool`.

### 4. `scripts/` + `systemd/rpicar.service`

`setup_bluetooth.sh` (NoInputNoOutput agent, pairable, adapter alias from `car.name`),
`install.sh` (venv + unit install), and the unit itself with a restart policy.

Then `android/`, which has not been started.

---

## Things that will bite you

**The doc leads the code.** `ARCHITECTURE.md` says a disagreement between the two is a
code bug unless the doc was updated in the same change. Two invariants in section 6 were
already refined during implementation for exactly this reason — if you find a third, edit
the doc too.

**`safety.py` is a synchronous state machine (`tick`) wrapped in a trivial async loop
(`run`).** Keep it that way. All time comes from an injected clock, which is why the tests
are deterministic instead of sleeping. Do not introduce `time.monotonic()` calls inside
the governor; take the clock from `self._clock`.

**Ticking is not an optimisation.** A dead-time hold has to expire on schedule even if the
next heartbeat is late, and the watchdog has to fire precisely when heartbeats *stop* —
which is exactly when no command is arriving to trigger an evaluation. Do not rewrite this
as "evaluate on command arrival".

**A `None` from `decode_line` must not feed the watchdog.** It means "nothing happened", so
it can neither reset the watchdog timer nor trip it. Only a decoded `DriveCommand` that
`SeqTracker.accept()` also approves counts as a heartbeat. Getting this wrong in either
direction is bad: reset on garbage and a babbling peer keeps the car alive forever; trip on
garbage and one stray byte stops the car mid-drive.

**`decode_line` clamps `l`/`r` rather than rejecting them,** but rejects `NaN`, booleans,
and non-numbers outright. The clamp keeps a miscalibrated client drivable; the rejections
keep an obviously broken frame from quietly reading as a direction. Quantisation to a relay
state stays in `drive.py` (`SideState.from_command`, deadzone 0.5) — do not move it into
the codec, because keeping the wire numeric is what lets PWM land later as a range change
instead of a type change.

**`Rig.hold()` in `conftest.py` sends a frame at both ends of its window** so that time
since last command is zero on return. Without the trailing frame every watchdog assertion
downstream silently shifts by one heartbeat period. This already caused one false failure.

**`all_off()` force-writes all four pins** rather than diffing against the cached state.
After a failed `apply()` the cache is stale for precisely the pin that is still energized.
Do not "optimise" the force flag away.

**A latched fault does not self-clear.** `emergency_stop()` is permanent for the process
lifetime — a hardware fault should require a human, not a reconnect.

**`lgpio_backend.py` has never run on real hardware.** It is written against the lgpio API
but nothing has verified it. Treat step 4 of the bring-up order (`ARCHITECTURE.md` section
9) as genuinely unproven, and keep the wheels off the ground.

---

## Open questions for the owner

Do not guess these; they change hardware sizing and the Android connection layer. They are
also listed in `ARCHITECTURE.md` section 10.

1. **Transport was chosen without sign-off.** SPP/RFCOMM is the right default for an
   Android-only auto-connecting app, but it was decided unilaterally when the owner
   skipped the question. Confirm before writing the app's connection layer — after that
   it is expensive to change.
2. **Relay module channel count.** Everything assumes **4 channels, active-low**, two per
   side. `config/car.toml` and the truth table both depend on it. Asked, not yet answered.
3. **Battery chemistry.** "Lithium AA" is either 1.5V L91 primaries (4S = 6V) or 3.7V
   14500 Li-ion (4S = 14.8V). Blocks sizing the relay coil rail and the Pi's regulator.
4. **No battery telemetry in v1.** The `state` frame has room; the Pi has no ADC.

## Context on the owner

Degree in EE, comfortable with hardware-level reasoning — do not simplify the electrical
explanations or hedge them. They asked for the Pi software first and the Android app
second, and asked to build it "one by one" rather than in one dump, so check in at module
boundaries rather than delivering five files at once.
