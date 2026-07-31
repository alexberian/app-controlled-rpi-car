# Handoff — state of the build

Written 2026-07-26. Last updated 2026-07-30 (**the service is packaged**:
`systemd/rpicar.service` and `scripts/install.sh` landed, and the unit is installed,
enabled and running on the Pi. The restart policy was verified by inducing each exit code.
Nothing above the packaging changed, so `ARCHITECTURE.md` did not need an edit — §7 already
listed both files). Update this file when you finish a chunk; it is the "where were we"
document. `ARCHITECTURE.md` is the design and does not change often — this one does.

**Read `ARCHITECTURE.md` first.** This file assumes it.

---

## Where things stand

**The car is drivable over Bluetooth, and the service is a systemd unit.** Steps 1, 2 and 3
of the bring-up order are done: the whole stack runs on a laptop over TCP, runs on the Pi
over TCP, and runs on the Pi over SPP with a phone-shaped client on the other end.
Everything is tested and lint-clean. What remains is the Android app; the only genuinely
unproven code left is `lgpio_backend.py`.

On the Pi it is a service — installed, enabled at boot, currently running:

```bash
systemctl status rpicar
journalctl -u rpicar -f
```

By hand, from a checkout, which is still how you develop:

```bash
cd pi
.venv/bin/python -m rpicar                            # tcp, per config/car.toml
.venv/bin/python -m rpicar --config config/spp.local.toml   # bluetooth
```

Stop the unit first if you do — it holds port 9999.

`car.toml` still ships `kind = "tcp"` on purpose — the bring-up order says prove TCP first.
A copy with `kind = "spp"` lives on the Pi at `pi/config/spp.local.toml` (`*.local.toml` is
gitignored); that is all switching transports takes, because nothing above the transport
knows which one it got.

Verified by hand on the Pi over the actual radio, not just by the suite: the SDP record is
discoverable from another machine, a bonded client connects in ~3.4s, forward / reverse /
spin all actuate with dead-time gating the reversal, a garbage frame and an over-long line
are both survived without dropping the link, the watchdog stops the car ~500ms after the
client goes silent, a second client is refused while the first drives, an **unbonded** peer
is refused outright (`EACCES`), and SIGTERM exits 0 with the relays de-energized and the
SDP record withdrawn. The same list was checked over TCP on the Pi, plus exit 2 on a bad
config and exit 1 when the port is already bound.

The laptop used for bring-up was deliberately **unpaired again** at the end, so the Pi
currently has no bonds. Run `scripts/setup_bluetooth.sh` to pair the phone.

```
rpi-car/
├── .gitignore              excludes .venv/, caches, egg-info, Android build output
├── README.md               human-facing quickstart
└── pi/
    ├── pyproject.toml      ruff + pytest config; lgpio is an optional extra
    ├── .venv/              dev venv (pytest was not available system-wide)
    ├── config/car.toml     every tunable in the system, heavily commented
    ├── systemd/
    │   └── rpicar.service  unit template; install.sh fills the paths in   DONE
    ├── scripts/
    │   ├── setup_bluetooth.sh  alias, pairable, NoInputNoOutput pairing window DONE
    │   └── install.sh          venv + /etc/rpicar/car.toml + the unit    DONE
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
    │       ├── spp.py          RFCOMM via org.bluez.Profile1         DONE, PROVEN ON HW
    │       └── __init__.py     create_transport() factory            DONE
    └── tests/
        ├── conftest.py     FakeClock, FakeConnection, RecordingSession, Rig,
        │                   FailingRelayBank
        ├── test_drive.py       28 tests
        ├── test_safety.py      19 tests — one per ARCHITECTURE.md section 6 invariant
        ├── test_protocol.py    56 tests
        ├── test_transport.py   19 tests — real loopback sockets, not a fake stream
        ├── test_spp.py         27 tests — fake BlueZ, real socketpair descriptors
        ├── test_telemetry.py   30 tests — fake clock; 2 async ones cover run()
        └── test_session.py     25 tests — mostly "what is not a heartbeat"
```

`dbus-next` is in the **`dev`** extra as well as `spp`. It is pure python and needs no
adapter, and `test_spp.py` imports the module under test, so keeping it out of `dev` would
make the suite size depend on which extras you happened to install.

`__main__.py` has no unit tests. It is assembly plus a supervisor, and the things worth
asserting about it (safe start before accept, stop before socket teardown, clean signal
exit) are either already covered by `test_safety.py` / `test_transport.py` or need a real
process — see the manual list above, which is the actual coverage.

Verify with:

```bash
cd pi
.venv/bin/python -m pytest -q          # expect: 204 passed
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

If those three are not clean, fix that before doing anything else. The suite also passes on
the Pi (12s there against 0.8s on a laptop — it is a 1GHz armv6 single core).

The repo was initialised on 2026-07-29. `main` tracks
`git@github.com:alexberian/app-controlled-rpi-car.git`. `car.toml` is tracked;
`*.local.toml` is ignored for per-machine overrides.

### The Pi

`pi@192.168.0.80`, Raspbian 12 (bookworm), Python 3.11.2, BlueZ 5.66, adapter
`B8:27:EB:C4:54:65`. Key-based ssh works. The checkout lives at `~/rpi-car/pi` with a venv
at `~/rpi-car/pi/.venv` built `--system-site-packages` so the system `lgpio` is visible
without compiling it.

Deploy with rsync, then reinstall in place:

```bash
rsync -a --delete --exclude .venv --exclude __pycache__ --exclude '*.egg-info' \
      --exclude .pytest_cache --exclude '*.local.toml' pi/ pi@192.168.0.80:~/rpi-car/pi/
ssh pi@192.168.0.80 'cd ~/rpi-car/pi && ./scripts/install.sh --now'
```

**`--exclude '*.local.toml'` is not optional.** Those files are gitignored, so they exist
only on the Pi, and `--delete` will happily remove `config/spp.local.toml` — the Bluetooth
config — because your laptop does not have one. An earlier version of this command was
missing it.

`install.sh` reuses the venv, reinstalls the package editable, re-renders the unit and
restarts it; running it repeatedly is fine. It never overwrites `/etc/rpicar/car.toml`
unless you pass `--replace-config`. Plain `pip install -q -e ".[dev,spp,hw]"` still works if
all you want is the package.

Running the service by hand is still the right thing during development
(`sudo systemctl stop rpicar` first — it holds the port). Two things about doing that over
ssh, both of which cost time here: a backgrounded `setsid nohup` process on the Pi did
**not** reliably survive the ssh session ending, so hold the session open instead; and
`pkill -f <pattern>` kills your own ssh session whenever the pattern text also appears
elsewhere in the command line you sent. Put the character class at the end (`rpica[r]`) so
the literal you typed does not match the regex you meant.

---

## What is left, in order

The software side is now one item. The other is hardware, and it does not block it.

### 1. `android/`

Not started, and now unblocked in every direction: the wire protocol (`ARCHITECTURE.md`
section 5) is settled and the Pi answers SPP connections today. Write the connection layer
against the Pi running `kind = "spp"`; `createRfcommSocketToServiceRecord` with UUID
`00001101-0000-1000-8000-00805F9B34FB` will find channel 1 by SDP lookup, which is the case
that has been verified from another machine. `kind = "tcp"` over WiFi remains available and
speaks the identical protocol, which is still the easier thing to iterate the UI against.

The phone has to be **bonded** first (`scripts/setup_bluetooth.sh`) — an unbonded peer never
reaches the service. Auto-connect is therefore a bonded-device lookup by adapter alias
(`car.name`, currently `rpi-car`) plus a retry loop.

### 2. Bring-up step 4 — the `lgpio` backend on real pins

`ARCHITECTURE.md` section 9. `lgpio_backend.py` is the last module that has never executed
against hardware, and steps 1–3 say nothing about it: every relay transition so far was
recorded by the mock backend, not switched. Set `gpio.backend = "lgpio"` in
`/etc/rpicar/car.toml`, wheels off the ground, hand on the battery disconnect. Blocked on
the two open questions below only for the wiring, not for the software.

---

## Things that will bite you

**The doc leads the code.** `ARCHITECTURE.md` says a disagreement between the two is a
code bug unless the doc was updated in the same change. Two invariants in section 6 were
already refined during implementation for exactly this reason, and section 4.1 was rewritten
outright when the Bluetooth transport was built — if you find a fourth, edit the doc too.

### systemd / packaging

**The service reads `/etc/rpicar/car.toml`, not the copy in your checkout.** The unit
passes `--config` explicitly so `systemctl cat rpicar` cannot lie about it, and
`install.sh` deliberately **never** overwrites that file once it exists (`--replace-config`
forces it) — it is hand-edited state, and the whole point of switching `kind` to `spp` is
that the edit survives a redeploy. It does tell you when the two differ. Editing the
checkout copy and wondering why nothing changed is the obvious way to lose twenty minutes.

**No D-Bus policy file is needed, and this is luckier than it looks.** The question of
whether a service account could reach `org.bluez` is now answered: the `pi` user is **not**
in the `bluetooth` group (that group is empty on this Pi), and SPP works anyway, because
Debian's `/etc/dbus-1/system.d/bluetooth.conf` ends with

```xml
<policy context="default">
  <allow send_destination="org.bluez"/>
</policy>
```

so any local uid may call `RegisterProfile`. **Upstream BlueZ ships that same stanza as a
`<deny>`.** A bluez upgrade that restores upstream's file — or a distro that never patched
it — silently removes the only thing making this work. That is why the unit names
`SupplementaryGroups=gpio bluetooth` even though the group is not needed today, and why
`install.sh` pings `org.bluez` as the service user before installing anything.

**systemd 252 on Bookworm has no `RestartSteps=` or `RestartMaxDelaySec=`.** There is no
exponential backoff to be had; those arrived in 254. The unit uses a flat `RestartSec=2`
with `StartLimitIntervalSec=300` / `StartLimitBurst=10` on top, so a persistent failure
gives up after ten tries instead of spinning forever. Budget those ten against the Pi's
startup cost, not against `RestartSec` — the interpreter takes ~3s to import on armv6, so a
failing cycle is about 5s, not 2s.

**The restart policy was verified by inducing each exit code, not by reading the man page.**
Bad config → `ExecMainStatus=2`, `NRestarts=0`, unit stays failed (`RestartPreventExitStatus=2`
doing its job). Port already bound → `ExecMainStatus=1`, restarts, and recovers on its own
the moment the port frees. `systemctl stop` → `ExecMainStatus=0`, `Result=success`, no
restart, and `stopped, relays de-energized` in the journal. If you change `Restart=`,
re-run those three; they take two minutes and the failure mode of getting it wrong is a car
that either will not come back or will not stay down.

**`systemd/rpicar.service` is a template and `install.sh` refuses to install a file with a
leftover `@PLACEHOLDER@` in it — comments included.** That check fired on the template's own
header comment the first time, which is how it earned its keep. Do not write a placeholder
literally in the prose; describe it.

**`Restart=on-failure` is safe only because a latched fault does not exit the process.**
Those two facts live in different files and have to be read together — `safety.py` keeps the
governor ticking and reporting `err` rather than raising, precisely so systemd cannot clear
the latch by restarting. Make the governor raise and you have quietly converted "a hardware
fault requires a human" into "a hardware fault requires two seconds".

**`ProtectHome=` and `DevicePolicy=closed` are off in the unit on purpose.** The checkout
and its venv live in the service user's home, and `DevicePolicy=closed` hides
`/dev/gpiochip0` — where the lgpio backend then fails at `open()` with a message that blames
the hardware. `ProtectSystem=full` is on and costs nothing, since the service writes nothing
outside its own tree.

### Bluetooth / BlueZ

**BlueZ owns the RFCOMM listening socket; never bind your own.** `RegisterProfile` with
`Role: server` binds the channel *and* publishes the SDP record *and* delivers connections
via `Profile1.NewConnection`. The trap that cost the most here: if you also bind a raw
`AF_BLUETOOTH` socket on the same channel, the kernel **allows it** rather than returning
`EADDRINUSE`, and then connections are delivered to neither listener — the client just times
out. A registration that looks perfect and an SDP browse that looks perfect are both
consistent with a service nobody can reach. The measured truth table is in ARCHITECTURE.md
section 4.1.

**`sdptool browse local` reports nothing even when the record is live.** It needs
bluetoothd's compat mode. This sent the first round of investigation down a blind alley —
the record was there the whole time. Check from another machine instead:
`sdptool search --bdaddr <addr> SP`.

**The bus must negotiate unix-fd passing.** `MessageBus(..., negotiate_unix_fd=True)`.
Without it the `fd` in `NewConnection` is `None` and nothing else looks wrong.
`test_serve_negotiates_unix_fd_passing` exists only to stop someone deleting it.

**The descriptor from `NewConnection` is yours to close.** `dbus-next` closes no received
fd — there is no `os.close` anywhere in the library. Every exit from the handler must either
hand it to a socket or close it, or the refusal path leaks one fd per rejected connection.

**`bluetoothctl` registers its own `DisplayYesNo` agent at startup, and `agent
NoInputNoOutput` on top of it silently does nothing** — it answers "Agent is already
registered" and leaves the capability alone. Pairing then becomes numeric comparison instead
of Just Works, and the phone asks the car to confirm a passkey it has no way to confirm. The
failure surfaces as `org.bluez.Error.AuthenticationFailed` at the *other* end. `agent off`
first. Also pipe a `sleep` in before the first command: bluetoothctl prints "Waiting to
connect to bluetoothd..." and discards anything sent before that. Both are handled in
`scripts/setup_bluetooth.sh`, with the reasoning inline.

**One-client-at-a-time is enforced twice over SPP, and BlueZ gets there first.** A second
RFCOMM connection to a busy channel is refused by the kernel with `EBUSY` before
`NewConnection` is ever called, so the transport's own refusal path never ran during
hardware bring-up. It is real and unit-tested (`test_a_second_client_is_refused`) — do not
delete it as dead code on the strength of the logs being quiet.

**An unbonded peer is refused by BlueZ with `EACCES` and never reaches the service.** That
is `require_authentication = true` doing its job and it is the only access control on the
link. If bring-up needs to skip pairing, turn it off deliberately in the config; do not
"fix" a connection failure by disabling it and forgetting.

**A dev machine's Python may not be able to speak RFCOMM at all.** `AF_BLUETOOTH` is a
build-time option in CPython. The laptop used here had a Python where
`socket.AF_BLUETOOTH` was missing *and*, once faked with the numeric constant 31,
`connect()` still failed with "bad family" because the interpreter cannot marshal a
`sockaddr_rc`. The Pi's Python is fine. If you need a bring-up client on such a machine,
build the 10-byte `sockaddr_rc` yourself and call libc `connect` through `ctypes` — it is
stable kernel ABI. Do not conclude the Pi is broken.

### dbus-next

**`@method()` replaces your function with a wrapper that calls it and discards the
result.** So `profile.Release()` returns `None`, and `await profile.NewConnection(...)`
awaits nothing at all — the coroutine is created and dropped. The bus never calls that
wrapper; it dispatches through the `_Method` record stashed on it. Tests have to do the
same, which is what `dbus_call()` in `test_spp.py` is for.

**A written-out `-> None` on a `@method()` breaks registration at import time.** With
`from __future__ import annotations` the annotation reaches dbus-next as the *string*
`"None"`, and it rejects that ("service annotations must be a string constant"). Omit the
return annotation entirely — an absent one is read as the empty signature. This is the one
place in the repo that deliberately breaks the type-hint convention, and `spp.py` says so.

**The D-Bus signature annotations need a ruff per-file-ignore, not a `noqa`.** `"o"`, `"h"`
and `"a{sv}"` look like forward references, so ruff wants to unquote them (UP037), cannot
resolve them (F821), and cannot parse `"a{sv}"` (F722). Unquoting any of them breaks
registration. The ignores are in `pyproject.toml` against the one file.

**`filterwarnings = ["error"]` plus Python 3.13 takes out `test_spp.py` collection.**
dbus-next uses `typing.no_type_check_decorator`, deprecated in 3.13, and it fires at import
time. There is a targeted `ignore` in `pyproject.toml`; the Pi's 3.11 never sees it, so this
only bites on a newer dev machine.

### Transport, protocol, safety

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
2026-07-29 (ARCHITECTURE.md §4), and it is now built and proven on hardware. Build the
Android connection layer against it.

## Context on the owner

Degree in EE, comfortable with hardware-level reasoning — do not simplify the electrical
explanations or hedge them. They asked for the Pi software first and the Android app
second, and asked to build it "one by one" rather than in one dump, so check in at module
boundaries rather than delivering five files at once.
