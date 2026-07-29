# rpi-car

Bluetooth-controlled Raspberry Pi Zero W car + custom Android app.

**Read `docs/ARCHITECTURE.md` before writing or changing code here.** It is the
authoritative design document — hardware topology, wire protocol, safety invariants,
file layout, and the reasoning behind each. This file is only the short version.

**Then read `docs/HANDOFF.md`** for what is built, what is next, and the traps. Update
it when you finish a chunk.

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

## Testing

Always develop against the `mock` GPIO backend and the `tcp` transport first; both exist
so the full stack runs on a dev machine. Bring-up order is ARCHITECTURE.md §9 — do not
skip to real hardware.
