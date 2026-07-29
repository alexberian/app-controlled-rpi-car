# Handoff — state of the build

Written 2026-07-26. Update this file when you finish a chunk; it is the "where were we"
document. `ARCHITECTURE.md` is the design and does not change often — this one does.

**Read `ARCHITECTURE.md` first.** This file assumes it.

---

## Where things stand

The safety-critical core of the Pi service is **complete, tested, and lint-clean**. Nothing
talks to a network or a Bluetooth stack yet.

```
pi/
├── pyproject.toml          ruff + pytest config; lgpio and dbus-next are optional extras
├── .venv/                  dev venv (pytest was not available system-wide)
├── config/car.toml         every tunable in the system, heavily commented
├── src/rpicar/
│   ├── __init__.py
│   ├── config.py           TOML -> validated frozen dataclasses      DONE
│   ├── drive.py            SideState/DriveState + H-bridge truth map DONE
│   ├── safety.py           watchdog / dead-time / dwell governor     DONE
│   └── gpio/
│       ├── base.py         RelayBank ABC, CoilStates                 DONE
│       ├── mock_backend.py records transitions, used by all tests    DONE
│       ├── lgpio_backend.py real hardware                            DONE, UNTESTED ON HW
│       └── __init__.py     create_relay_bank() factory               DONE
└── tests/
    ├── conftest.py         FakeClock, Rig, FailingRelayBank
    ├── test_drive.py
    └── test_safety.py      one test per ARCHITECTURE.md section 6 invariant
```

Verify with:

```bash
cd pi
.venv/bin/python -m pytest -q          # expect: 47 passed
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

If those three are not clean, fix that before doing anything else.

---

## What is left, in order

Do them in this order. Each depends on the one before it.

### 1. `protocol.py` — NDJSON frame codec

Spec is `ARCHITECTURE.md` section 5. Decode `{"t":"drive","l":..,"r":..,"seq":..}`, encode
`{"t":"state",...}`. Requirements that are easy to miss:

- 256-byte line cap; over-long lines are **dropped and logged, not fatal**.
- Malformed JSON or unknown `t` → ignore the line, keep the connection. A single bad frame
  must never drop the link mid-drive.
- Unknown fields ignored, so the protocol can grow without a version bump.
- `l`/`r` decode as **floats**, not ints. Quantisation to a relay state already happens in
  `drive.py` (`SideState.from_command`, deadzone 0.5). Do not quantise in the codec —
  keeping the wire numeric is what lets PWM land later as a range change instead of a type
  change.
- `seq` is a wrapping uint32, used to drop reordered frames and measure loss.

### 2. `transport/base.py` + `transport/tcp.py`

Async line-oriented byte stream. The interface needs a disconnect notification, because
`SafetyGovernor.on_disconnect()` must run **synchronously before socket teardown**
(`ARCHITECTURE.md` 6.2). Do not make that a fire-and-forget task.

TCP exists so the whole stack runs on a laptop. Build and prove it before touching BlueZ.

### 3. `telemetry.py` + `__main__.py`

Telemetry: 2 Hz periodic state frames, plus an immediate frame on any change or fault.
Send `gov.applied` (what the relays are doing), **not** `gov.target` — the app renders
actual state, and they legitimately diverge while a gate is active.
`GateReason.as_wire()` already exists to fill the `err` field; nothing calls it yet.

`__main__.py` assembles config → `create_relay_bank` → `DriveController` →
`SafetyGovernor` → protocol → transport, and installs SIGTERM/SIGINT handlers.
`SafetyGovernor.run()` already stops the car in its `finally`, so the handler mainly needs
to cancel the task and let it unwind.

### 4. `transport/spp.py`

RFCOMM listener plus a D-Bus SDP record. Details and the reasoning are in
`ARCHITECTURE.md` section 4.1. The trap: a raw bound `AF_BLUETOOTH` socket publishes no
SDP record, and Android's `createRfcommSocketToServiceRecord` needs one to find the
channel. Register via `org.bluez.ProfileManager1.RegisterProfile`, not `sdptool`.

### 5. `scripts/` + `systemd/rpicar.service`

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

**Not a git repo**, and there is no `.gitignore`. `.venv/`, `__pycache__/`, `.ruff_cache/`
and `*.egg-info/` are all sitting untracked in `pi/`. If you `git init`, exclude them.

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
