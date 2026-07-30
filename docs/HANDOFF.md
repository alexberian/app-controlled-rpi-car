# Handoff — state of the build

Written 2026-07-26. Last updated 2026-07-29 (the service runs: `telemetry.py`, `session.py`
and `__main__.py` landed on top of the TCP transport; SPP confirmed by the owner — §4 is
settled). Update this file when you finish a chunk; it
is the "where were we" document. `ARCHITECTURE.md` is the design and does not change often
— this one does.

**Read `ARCHITECTURE.md` first.** This file assumes it.

---

## Where things stand

**The service runs.** Over TCP, against the mock backend, a client can drive the car and
watch the relays in telemetry — bring-up step 1 of `ARCHITECTURE.md` section 9 is done.
Everything is tested and lint-clean.

```bash
cd pi
.venv/bin/python -m rpicar                 # or: .venv/bin/rpicar
```

That listens on `0.0.0.0:9999` per `config/car.toml`. Point anything line-oriented at it:
`{"t":"drive","l":1,"r":-1,"seq":1}` per line at 10Hz, and it replies with `state` frames.
Stop it with Ctrl-C or SIGTERM; both leave the relays de-energized.

What has been verified by hand, not just by the suite: forward / spin / reverse with the
dead-time gate visible in `err`, garbage frames ignored without dropping the link, the
watchdog tripping ~500ms after the client goes silent, a second client refused while the
first drives, clean exit on SIGTERM and SIGINT, exit 2 on a bad config, and exit 1 with the
relays still driven off when the port is already bound.

**What is not built: `transport/spp.py`.** Bluetooth is the whole premise and it is the next
thing. `kind = "spp"` in `car.toml` currently fails with `ModuleNotFoundError`.

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
    │   ├── __main__.py     assembles + supervises the stack, signals  DONE
    │   ├── config.py       TOML -> validated frozen dataclasses      DONE
    │   ├── drive.py        SideState/DriveState + H-bridge truth map DONE
    │   ├── safety.py       watchdog / dead-time / dwell governor     DONE
    │   ├── protocol.py     NDJSON codec, SeqTracker                  DONE
    │   ├── session.py      DriveSession: frame -> command            DONE
    │   ├── telemetry.py    TelemetryPublisher, 2Hz + on-change       DONE
    │   ├── gpio/
    │   │   ├── base.py         RelayBank ABC, CoilStates             DONE
    │   │   ├── mock_backend.py records transitions, used by all tests DONE
    │   │   ├── lgpio_backend.py real hardware                        DONE, UNTESTED ON HW
    │   │   └── __init__.py     create_relay_bank() factory           DONE
    │   └── transport/
    │       ├── base.py         Session/Connection/Transport, framing DONE
    │       ├── tcp.py          dev + WiFi listener                   DONE
    │       └── __init__.py     create_transport() factory            DONE
    └── tests/
        ├── conftest.py     FakeClock, FakeConnection, Rig, FailingRelayBank
        ├── test_drive.py       28 tests
        ├── test_safety.py      19 tests — one per ARCHITECTURE.md section 6 invariant
        ├── test_protocol.py    56 tests
        ├── test_transport.py   19 tests — real loopback sockets, not a fake stream
        ├── test_telemetry.py   30 tests — fake clock; 2 async ones cover run()
        └── test_session.py     25 tests — mostly "what is not a heartbeat"
```

`__main__.py` has no unit tests. It is assembly plus a supervisor, and the things worth
asserting about it (safe start before accept, stop before socket teardown, clean signal
exit) are either already covered by `test_safety.py` / `test_transport.py` or need a real
process — see the manual list above, which is the actual coverage.

Verify with:

```bash
cd pi
.venv/bin/python -m pytest -q          # expect: 177 passed
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

If those three are not clean, fix that before doing anything else.

The repo was initialised on 2026-07-29. `main` tracks
`git@github.com:alexberian/app-controlled-rpi-car.git`. `car.toml` is tracked;
`*.local.toml` is ignored for per-machine overrides.

---

## What is left, in order

Do them in this order. Each depends on the one before it.

### 1. `transport/spp.py`

RFCOMM listener plus a D-Bus SDP record. Details and the reasoning are in
`ARCHITECTURE.md` section 4.1. The trap: a raw bound `AF_BLUETOOTH` socket publishes no
SDP record, and Android's `createRfcommSocketToServiceRecord` needs one to find the
channel. Register via `org.bluez.ProfileManager1.RegisterProfile`, not `sdptool`.

Only the listener is new work: subclass `Transport`, accept one RFCOMM socket at a time,
wrap it with `loop.connect_accepted_socket` into a stream pair, and hand it to
`StreamConnection` + `run_session`. All the framing, bounding, and disconnect ordering is
already written and tested in `transport/base.py`. `create_transport` already dispatches to
`SppTransport`; until the module exists, `kind = "spp"` fails with `ModuleNotFoundError`.

Nothing above the transport needs to change: `__main__.py` builds whatever
`create_transport` returns, so switching `kind` in `car.toml` is the whole integration.

### 2. `scripts/` + `systemd/rpicar.service`

`setup_bluetooth.sh` (NoInputNoOutput agent, pairable, adapter alias from `car.name`),
`install.sh` (venv + unit install), and the unit itself with a restart policy.

For the unit: the service exits **2** for a bad config, **1** for a runtime failure, **0**
on a signal. Restarting will never fix a 2 — `RestartPreventExitStatus=2` — and the unit
wants `RESTART=on-failure` with a backoff for the 1s, most likely of which is BlueZ not
being up yet. Also set `KillSignal=SIGTERM` (the default) and leave `KillMode` alone; the
signal handler needs to run, and `SIGKILL` would skip every one of the three stop paths
except de-energization by power removal.

### 3. `android/`

Not started. The wire protocol (`ARCHITECTURE.md` section 5) and the transport (SPP,
confirmed) are both settled, so the connection layer can be written against a Pi running
`kind = "spp"` — or, before that exists, against `kind = "tcp"` over WiFi, which speaks the
identical protocol. Developing the app against TCP first is worth it for the same reason it
was worth it on the Pi side.

---

## Things that will bite you

**The doc leads the code.** `ARCHITECTURE.md` says a disagreement between the two is a
code bug unless the doc was updated in the same change. Two invariants in section 6 were
already refined during implementation for exactly this reason — if you find a third, edit
the doc too.

**Do not use `Server.serve_forever()` in a transport.** This cost an hour. Its cancellation
path calls `await Server.wait_closed()`, and since Python 3.12.1 that waits for the
connection handler tasks to finish — but the handler is exactly what the transport's own
`finally` has not cancelled yet, because it never gets to run. Shutdown deadlocks with the
client still connected and the disconnect never reported. `tcp.py` therefore idles on
`asyncio.Event().wait()` and owns the teardown itself: close the listener, cancel the
handler, wait for it. `start_server` is already accepting by the time it returns, so
nothing is lost.

**`readuntil` leaves the buffer untouched when it overruns the limit.** Catching
`LimitOverrunError` and continuing gets you the same exception forever on the same bytes.
The consumed count has to be read off the exception and drained explicitly — that is what
`StreamConnection._discard_line` is doing, and both overrun cases (no separator yet,
separator found beyond the limit) go through it.

**`Session`'s three callbacks are synchronous, and that is the design.** `on_disconnect`
runs inside the `finally` that owns the socket close, which is the whole mechanism behind
ARCHITECTURE.md 6.2 — the coils are de-energized before the peer is gone. Make any of them
`async`, or schedule the stop as a task, and the guarantee silently becomes "eventually".
It is also why cancellation is safe: there may be no chance to run a scheduled task.

**After a reset (RST), the connection is already closed when `on_disconnect` fires.** Only
the graceful path can promise otherwise, so do not assert on socket state there — assert
that the stop happened. `test_a_reset_connection_is_a_disconnect_not_a_crash` covers it.

**`TcpConfig` forbids port 0**, so a test cannot let the listener pick its own port. The
harness binds an ephemeral port, closes it, and hands the number over; `serve()` also binds
inside its own task, so a test client has to retry the connect rather than assume the
listener exists. Both are in `tests/test_transport.py`'s harness — reuse it.

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

**Telemetry's change signature deliberately excludes `ack` and `up`.** It is
`(applied.left, applied.right, err)` and nothing else. Both excluded fields move on every
heartbeat, so adding either turns 2Hz telemetry into 10Hz telemetry — the whole link
saturated with frames the app already knows about. If a change frame seems to be missing,
the fix is almost certainly in `GateReason`, not in the signature.
`test_a_new_ack_alone_does_not_publish_a_frame` guards this.

**Telemetry polls the governor; the governor does not call back into telemetry.** That
keeps the dependency pointing `telemetry -> safety` like everything else, and it costs
nothing because the governor is a ticking machine — nothing it does can outrun its own
tick, so `min(tick_interval, interval_s)` sees every change. Inverting this into a callback
would put a telemetry reference inside the safety layer for no latency gain.

**A periodic frame can be up to one poll interval late, and that is fine.** Due-ness is
checked once per poll, so the effective rate is ~1.92Hz rather than exactly 2Hz. Do not add
deadline-carry arithmetic to `_is_due` to "fix" the drift; it is a status feed. What this
does mean is that **a test asserting an exact frame count over a fixed window is
phase-dependent and will fail on float accumulation** — this already cost one debugging
round. Assert the interval between consecutive frames instead, which is what
`test_periodic_frames_land_one_period_apart` does.

**`TelemetryPublisher.tick()` returns `None` when no client is attached,** rather than a
frame that gets dropped by the caller. Marking a frame as sent when nothing received it
would eat the immediate on-connect frame that the *next* client is owed, leaving a freshly
connected app blank for up to a full period.

**`ack` comes from `governor.seq`, not `SeqTracker.last`.** The two agree for every
numbered frame — the session only calls `gov.command` for frames the tracker accepted — and
taking it from the governor keeps `telemetry.py` to a single dependency. An earlier draft of
this file said to use the tracker; the governor already exposes `seq` for exactly this, so
that instruction was the redundant one.

**`Connection` is imported into `telemetry.py` under `TYPE_CHECKING` only.** Telemetry
needs somewhere to put bytes, not a transport, and keeping the import out of the runtime
graph is what stops this module from quietly acquiring a dependency on a link backend.
`session.py` does the same.

**`DriveSession.on_line` calls `gov.tick()` itself, and that is an addition to the
governor's periodic tick, never a replacement.** It exists so a command actuates on arrival
instead of up to 20ms later. Deleting it costs latency; deleting the *periodic* tick breaks
the watchdog and dead-time entirely (see the two entries above on ticking). Extra ticks are
safe because `SideGovernor.evaluate` cannot return a change a gate would have refused.

**A latched fault does not exit the process.** `emergency_stop` leaves the governor ticking
and returning early, so the car sits stopped and telemetry keeps reporting the fault in
`err`. This is deliberate: exiting would let systemd restart the service, and a restart
clears the latch — which is exactly what "a hardware fault should require a human" forbids.
Do not "fix" this by raising from the governor.

**`_supervise` cancels every task and gathers before inspecting results.** The gather is
what runs `SafetyGovernor.run`'s `finally`, which is what stops the car. Anything that
returns or raises before that gather completes skips the stop. This is also why the
supervisor does not use `asyncio.TaskGroup`: the group's own cancellation semantics make the
ordering between "sibling died" and "our finally ran" much harder to see, and this is not a
place to be clever.

**Three separate things de-energize the relays on the way out, on purpose**
(`SafetyGovernor.run`'s `finally`, `DriveController.close` in `_serve`'s `finally`, and an
`atexit` hook on `bank.all_off`). They are redundant by design, not by accident. The
`atexit` hook is safe after `close()` because `RelayBank.all_off` swallows write failures
internally — verify that still holds before touching either.

**Telemetry can miss an `err` transient shorter than one poll; it cannot miss an applied
state.** The governor and the publisher are independent 20ms tasks, so a gate reason that
lasts a single tick (the one-tick `dead_time` that precedes a `dwell`, for instance) may
never be sampled. That is fine — `err` is advisory and telemetry samples state rather than
logging events. The *applied* state is never missed, because dwell guarantees every relay
state persists ≥80ms, which is four polls. Do not add an event queue to close the `err` gap.

**Hand-testing with a fixed `seq` looks like a broken watchdog.** A shell loop sending the
same `{"t":"drive","l":1,"r":-1,"seq":1}` forever drives for 500ms and then stops: every
frame after the first is a duplicate, `SeqTracker` rejects it as stale, and a rejected frame
is not a heartbeat. This is correct, and it caught out the first version of the README
example. Either omit `seq` — unnumbered frames are always accepted — or increment it.

**Do not develop against `0.0.0.0:9999` if something else on the machine wants it.**
`car.toml` ships that way for the Pi. For local work, copy it, set
`host = "127.0.0.1"` and a high port, and pass `--config`; `*.local.toml` is gitignored for
exactly this. `RPICAR_CONFIG` also works.

---

## Open questions for the owner

Do not guess these; they change hardware sizing and the Android connection layer. They are
also listed in `ARCHITECTURE.md` section 10.

1. **Relay module channel count.** Everything assumes **4 channels, active-low**, two per
   side. `config/car.toml` and the truth table both depend on it. Asked, not yet answered.
2. **Battery chemistry.** "Lithium AA" is either 1.5V L91 primaries (4S = 6V) or 3.7V
   14500 Li-ion (4S = 14.8V). Blocks sizing the relay coil rail and the Pi's regulator.
3. **No battery telemetry in v1.** The `state` frame has room; the Pi has no ADC.

**Settled — do not reopen:** the transport. SPP/RFCOMM was confirmed by the owner on
2026-07-29 (ARCHITECTURE.md §4). Build `transport/spp.py` and the Android connection layer
against it.

## Context on the owner

Degree in EE, comfortable with hardware-level reasoning — do not simplify the electrical
explanations or hedge them. They asked for the Pi software first and the Android app
second, and asked to build it "one by one" rather than in one dump, so check in at module
boundaries rather than delivering five files at once.
