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

`docs/HANDOFF.md` is the "where were we" document, and the next agent starts from it
having seen none of your reasoning. **Before you finish a change to code, update it if the
change made anything in it wrong.** Do not defer this to the end of a larger task or to
the next session — a stale handoff has already sent one agent to rebuild a module that
was finished.

Check all of it, not just the checklist:

- **Status and the file tree** — a module that went from missing to DONE, or DONE to
  UNTESTED-ON-HW.
- **The verify block** — the expected test count is written down; if you added tests,
  change the number.
- **"What is left"** — delete what you finished and renumber. If you discovered work that
  is not listed, add it in dependency order.
- **"Things that will bite you"** — the highest-value section. If something cost you more
  than a few minutes, or you made a non-obvious call a reader would otherwise try to
  "fix", write it down with the reasoning.

`docs/ARCHITECTURE.md` is different: it is the design, it leads the code, and a
disagreement between the two is a code bug **unless the doc was updated in the same
change**. So if you deliberately change a designed behaviour — an invariant, the wire
format, the layering, a topology — edit ARCHITECTURE.md in that same change. Two section 6
invariants have already been refined this way. Otherwise leave it alone.

If a change touches neither, say so rather than editing to look diligent.

## Testing

Always develop against the `mock` GPIO backend and the `tcp` transport first; both exist
so the full stack runs on a dev machine. Bring-up order is ARCHITECTURE.md §9 — do not
skip to real hardware.
