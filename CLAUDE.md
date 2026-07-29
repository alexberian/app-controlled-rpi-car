# rpi-car

Bluetooth-controlled Raspberry Pi Zero W car + custom Android app.

**Read `docs/ARCHITECTURE.md` before writing or changing code here.** It is the
authoritative design document — hardware topology, wire protocol, safety invariants,
file layout, and the reasoning behind each. This file is only the short version.

**Then read `docs/HANDOFF.md`** for what is built, what is next, and the traps.

## Hard rules

- **Relays, not H-bridges.** On/off only. Never PWM a relay output. No variable speed.
- **Three states per side:** `-1` reverse, `0` stop, `+1` forward. Skid steer, no mixer.
- **The safety layer in `safety.py` is non-negotiable** (ARCHITECTURE.md §6): 500ms
  command watchdog, STOP on disconnect, ≥250ms dead-time on direction reversal, 80ms
  minimum dwell between relay actuations, safe start, safe stop. Each has a test. A
  change that breaks a safety test is wrong, whatever else it fixes.
- **De-energized == STOP.** Preserve this in hardware and software. Never let a code path
  leave relays energized on error, exit, or signal.
- **Layering is one-directional:** `transport → protocol → safety → drive → gpio`.
  `drive.py` must not know Bluetooth exists; `safety.py` must not know JSON exists.
- **Tunables live in `pi/config/car.toml`**, never inline. Pins are BCM numbering.
- Python 3.11+, asyncio, `lgpio` for GPIO (not RPi.GPIO — broken on Bookworm).
- **Leave the docs true.** See below — this is a rule, not housekeeping.

## Keeping the docs true

Three docs, three jobs. Updating them is part of finishing a change, not cleanup after it.

| Doc | Job | Edit it when |
|---|---|---|
| `docs/ARCHITECTURE.md` | the design; leads the code | you deliberately change designed behaviour |
| `docs/HANDOFF.md` | "where were we": present state + traps | a change made anything in it wrong |
| `README.md` | orientation for a human | setup, status, or hardware facts changed |

### HANDOFF.md

The next agent starts here having seen none of your reasoning. **Update it before you
finish the change** — not at the end of a larger task, not next session. A stale handoff
has already sent one agent to rebuild a module that was done.

Check all of it, not just the checklist:

- **Header date** — bump "Last updated" and name what landed.
- **Status and the file tree** — a module that went from missing to DONE, or DONE to
  UNTESTED-ON-HW.
- **The verify block** — the expected test count is written down; if you added tests,
  change the number.
- **"What is left"** — delete what you finished and renumber. Add work you discovered, in
  dependency order.
- **"Things that will bite you"** — the highest-value section. Anything that cost you more
  than a few minutes, or a non-obvious call a reader would otherwise try to "fix", goes
  here *with the reasoning*. Add to it; don't rewrite entries that are still true.
- **"Open questions"** — if the owner answered one, delete it here and in ARCHITECTURE §10.

Keep it a snapshot, not a changelog: current state and live traps, no history of who did
what. Absolute dates (`2026-07-29`), never "last week".

### ARCHITECTURE.md

It leads the code — a disagreement between the two is a **code bug** unless the doc was
updated in the same change. So edit it only when you deliberately change a designed
behaviour (a §6 invariant, the wire format, the layering, a topology), and do it in that
same change. Two §6 invariants have already been refined this way. Otherwise leave it alone.

### README.md

Orientation, not a second handoff. It carries the test count, the status table, the pin map,
and the hardware warnings — all of which go stale. Keep it consistent with `car.toml` and
with HANDOFF's verify block; link to ARCHITECTURE.md rather than restating it.

### Always

Say which docs you touched and why — or that the change needed none. Never edit a doc to
look diligent.

## Testing

Always develop against the `mock` GPIO backend and the `tcp` transport first; both exist
so the full stack runs on a dev machine. Bring-up order is ARCHITECTURE.md §9 — do not
skip to real hardware.
