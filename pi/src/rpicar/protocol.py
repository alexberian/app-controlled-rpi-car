"""NDJSON wire codec -- ``docs/ARCHITECTURE.md`` section 5.

One UTF-8 JSON object per line, in both directions. Human-readable on purpose:
during bring-up the link can be watched with ``cat``.

This layer converts bytes to intent and back. It does not decide what the car
does with a command -- quantisation to a relay state is ``drive.py``'s job and
gating is ``safety.py``'s, so nothing here knows about deadzones, dwell, or
GPIO. It also does not read from a socket; it is handed one already-split line,
which is what lets it be tested without a transport.

The governing principle for decoding is that **a bad frame is not a bad link**.
A truncated write, a stray keepalive, or a field from a newer app version all
resolve to "ignore this line and keep driving". The watchdog in ``safety.py``
is what handles a peer that has genuinely stopped making sense: if the garbage
persists, no valid command arrives, and the car stops on its own 500ms later.
Dropping the connection here would only make that slower and less predictable.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass

from .drive import DriveState

__all__ = [
    "MAX_LINE_BYTES",
    "SEQ_MODULUS",
    "DriveCommand",
    "SeqTracker",
    "decode_line",
    "encode_state",
]

log = logging.getLogger(__name__)

# ARCHITECTURE.md 5.3. A drive frame is ~40 bytes; this is generous headroom
# that still bounds what a peer can make us buffer per line.
MAX_LINE_BYTES = 256

# `seq` is a uint32 and wraps. At the 10Hz command rate that is one wrap every
# ~13.6 years, but the arithmetic below costs nothing and a client that resets
# its counter on reconnect would otherwise stall the ordering check forever.
SEQ_MODULUS = 1 << 32
_SEQ_HALF = SEQ_MODULUS // 2

# Wire values are numeric so that PWM can land as a range widening rather than
# a type change (ARCHITECTURE.md section 3). Out-of-range values are clamped
# rather than rejected: the app is a convenience, not a safety boundary, and a
# clamp keeps a miscalibrated client drivable instead of silently dead.
_COMMAND_MIN = -1.0
_COMMAND_MAX = 1.0


@dataclass(frozen=True, slots=True)
class DriveCommand:
    """A decoded ``drive`` frame.

    ``left``/``right`` are floats in [-1.0, 1.0], not relay states. ``seq`` is
    ``None`` when the peer omitted it or sent one that was not a valid uint32 --
    a frame is still perfectly drivable without it, it just cannot be ordered.
    """

    left: float
    right: float
    seq: int | None = None


def _coerce_command(value: object) -> float | None:
    """A wire ``l``/``r`` value as a clamped float, or ``None`` if unusable."""
    # bool is a subclass of int; `true` reading as FORWARD would be a silent
    # and surprising interpretation of an obviously malformed frame.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    # NaN would quantise to STOP, which is safe, but it is still a broken frame
    # and treating it as one keeps the watchdog the thing that reports the
    # problem rather than a car that mysteriously refuses to move.
    if not math.isfinite(number):
        return None
    return max(_COMMAND_MIN, min(_COMMAND_MAX, number))


def _coerce_seq(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 <= value < SEQ_MODULUS:
        return None
    return value


def decode_line(raw: bytes) -> DriveCommand | None:
    """Decode one line from the peer.

    Returns the command, or ``None`` for any line that is not a usable ``drive``
    frame. Never raises: every rejection path here is an ordinary, logged event.
    Callers should treat ``None`` as "nothing happened" and specifically must
    not let it reset or trip the watchdog.

    The line may include its trailing newline; it does not have to.
    """
    if len(raw) > MAX_LINE_BYTES:
        # Logged above DEBUG because at 10Hz this cannot be routine noise --
        # it means the peer is framing wrongly or something is corrupting the
        # stream, and neither shows up anywhere else.
        log.warning("dropping over-long line (%d bytes, max %d)", len(raw), MAX_LINE_BYTES)
        return None

    line = raw.strip()
    if not line:
        return None

    try:
        message = json.loads(line)
    except (UnicodeDecodeError, ValueError):
        log.debug("dropping malformed JSON line: %r", raw[:64])
        return None

    if not isinstance(message, dict):
        log.debug("dropping non-object frame: %r", raw[:64])
        return None

    kind = message.get("t")
    if kind != "drive":
        # Not an error. Unknown types are how the protocol grows, and the Pi
        # sees its own `state` frames echoed back on a loopback test.
        log.debug("ignoring frame of type %r", kind)
        return None

    left = _coerce_command(message.get("l"))
    right = _coerce_command(message.get("r"))
    if left is None or right is None:
        log.debug("dropping drive frame with bad l/r: %r", raw[:64])
        return None

    # Unknown fields are ignored on purpose (ARCHITECTURE.md 5.3) -- that is
    # what lets the app add fields without a version bump.
    return DriveCommand(left=left, right=right, seq=_coerce_seq(message.get("seq")))


class SeqTracker:
    """Orders frames by their wrapping uint32 ``seq`` and counts what was lost.

    UDP-style reasoning over a stream transport looks paranoid, but the point is
    not packet reordering -- RFCOMM will not do that. It is that a stale frame
    delivered late is a stale *throttle position*, and applying one after a
    newer stop is exactly the failure this protocol should make impossible.
    The loss counter is also the only visibility we get into link quality.
    """

    __slots__ = ("_last", "lost", "rejected")

    def __init__(self) -> None:
        self._last: int | None = None
        # Frames the peer sent that never arrived, inferred from gaps.
        self.lost = 0
        # Duplicates and out-of-order arrivals we refused.
        self.rejected = 0

    @property
    def last(self) -> int | None:
        """Highest sequence number accepted so far, or ``None`` before the first."""
        return self._last

    def accept(self, seq: int | None) -> bool:
        """Whether a frame with this ``seq`` should be acted on.

        An unnumbered frame is always accepted -- it carries no ordering claim,
        so there is nothing to check, and refusing it would break a client that
        simply does not implement ``seq``.
        """
        if seq is None:
            return True

        if self._last is None:
            self._last = seq
            return True

        # Distance forward from the last accepted frame, modulo the wrap. A
        # small value is a newer frame (possibly with a gap); anything in the
        # top half of the space is a frame from behind us, expressed as a very
        # long way forward.
        advance = (seq - self._last) % SEQ_MODULUS
        if advance == 0 or advance >= _SEQ_HALF:
            self.rejected += 1
            log.debug("rejecting out-of-order seq %d (last %d)", seq, self._last)
            return False

        self.lost += advance - 1
        self._last = seq
        return True

    def reset(self) -> None:
        """Forget the sequence position, keeping the counters.

        Called on disconnect: the next client starts its own numbering, and
        holding the old position would reject everything it sends until it
        happened to pass us.
        """
        self._last = None


def encode_state(
    applied: DriveState,
    *,
    ack: int | None = None,
    uptime_s: float = 0.0,
    err: str | None = None,
) -> bytes:
    """Encode a ``state`` frame, newline included.

    ``applied`` is what the relays are *actually* doing, not what was commanded
    -- they diverge whenever a safety gate is active, and the app renders the
    actual state (ARCHITECTURE.md 5.2).
    """
    message = {
        "t": "state",
        # SideState is an IntEnum; int() keeps -1/0/1 on the wire instead of
        # serialising an enum repr.
        "l": int(applied.left),
        "r": int(applied.right),
        "ack": ack,
        # One decimal is plenty for a human-readable uptime and keeps the frame
        # from growing a 17-digit float.
        "up": round(uptime_s, 1),
        "err": err,
    }
    return json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
