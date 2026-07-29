"""Codec tests -- ``docs/ARCHITECTURE.md`` section 5.

The theme of the decode tests is that garbage in produces ``None``, never an
exception and never a disconnect. Each rejection case below is a real thing a
half-written or newer client will eventually put on the wire.
"""

from __future__ import annotations

import json

import pytest

from rpicar.drive import DriveState, SideState
from rpicar.protocol import (
    MAX_LINE_BYTES,
    SEQ_MODULUS,
    DriveCommand,
    SeqTracker,
    decode_line,
    encode_state,
)


def frame(**fields: object) -> bytes:
    """A drive frame as the app would send it, plus overrides."""
    message = {"t": "drive", "l": 0, "r": 0, "seq": 1}
    message.update(fields)
    return json.dumps(message).encode("utf-8") + b"\n"


# --------------------------------------------------------------------------
# decoding: the happy path
# --------------------------------------------------------------------------


def test_decodes_a_drive_frame() -> None:
    assert decode_line(frame(l=1, r=-1, seq=42)) == DriveCommand(1.0, -1.0, 42)


def test_trailing_newline_is_optional() -> None:
    assert decode_line(b'{"t":"drive","l":1,"r":1}') == DriveCommand(1.0, 1.0, None)


@pytest.mark.parametrize("line_end", [b"\n", b"\r\n", b""])
def test_line_endings_are_tolerated(line_end: bytes) -> None:
    assert decode_line(b'{"t":"drive","l":0,"r":0}' + line_end) is not None


def test_values_decode_as_floats_not_ints() -> None:
    """The wire stays numeric so PWM widens a range instead of changing a type."""
    command = decode_line(frame(l=0.4, r=-0.7))
    assert command is not None
    assert isinstance(command.left, float)
    assert (command.left, command.right) == (0.4, -0.7)


def test_intermediate_values_are_not_quantised_here() -> None:
    """Quantisation is drive.py's job, at the last moment before the relays."""
    command = decode_line(frame(l=0.6, r=0.2))
    assert command is not None
    assert command.left == 0.6
    assert SideState.from_command(command.left) is SideState.FORWARD
    assert SideState.from_command(command.right) is SideState.STOP


def test_unknown_fields_are_ignored() -> None:
    """So the app can grow fields without a protocol version bump."""
    assert decode_line(frame(l=1, r=1, mode="turbo", battery=7.4)) == DriveCommand(1.0, 1.0, 1)


@pytest.mark.parametrize(
    ("sent", "expected"),
    [(5, 1.0), (-5, -1.0), (1.5, 1.0), (-1.5, -1.0)],
)
def test_out_of_range_values_are_clamped(sent: float, expected: float) -> None:
    command = decode_line(frame(l=sent, r=sent))
    assert command is not None
    assert command.left == expected


# --------------------------------------------------------------------------
# decoding: rejection. None every time, exceptions never
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\n",
        b"   \n",
        b"not json at all\n",
        b'{"t":"drive","l":1,"r":\n',  # truncated write
        b"[1,2,3]\n",  # valid JSON, wrong shape
        b'"drive"\n',
        b"null\n",
        b'{"t":"ping"}\n',  # unknown type
        b"{}\n",  # no type at all
        b'{"t":"drive","r":1}\n',  # missing l
        b'{"t":"drive","l":1}\n',  # missing r
        b'{"t":"drive","l":"1","r":1}\n',  # string, not number
        b'{"t":"drive","l":null,"r":1}\n',
        b'{"t":"drive","l":true,"r":1}\n',  # bool is an int subclass; must not read as 1
        b'{"t":"drive","l":NaN,"r":1}\n',
        b'{"t":"drive","l":Infinity,"r":1}\n',
        b"\xff\xfe binary garbage\n",
    ],
)
def test_bad_lines_are_dropped_not_raised(raw: bytes) -> None:
    assert decode_line(raw) is None


def test_state_frames_from_ourselves_are_ignored() -> None:
    """A loopback test echoes these back; they must not read as commands."""
    assert decode_line(encode_state(DriveState.STOPPED)) is None


def test_over_long_line_is_dropped() -> None:
    padded = frame(l=1, r=1, pad="x" * MAX_LINE_BYTES)
    assert len(padded) > MAX_LINE_BYTES
    assert decode_line(padded) is None


def test_line_at_the_cap_is_accepted() -> None:
    """The limit is inclusive -- an off-by-one here silently drops valid frames."""
    base = len(frame(l=1, r=1, pad=""))
    padded = frame(l=1, r=1, pad="x" * (MAX_LINE_BYTES - base))
    assert len(padded) == MAX_LINE_BYTES
    assert decode_line(padded) == DriveCommand(1.0, 1.0, 1)


@pytest.mark.parametrize("seq", [-1, SEQ_MODULUS, 1.5, "42", True, None])
def test_bad_seq_keeps_the_command_and_drops_the_number(seq: object) -> None:
    """A frame is still drivable unordered; throwing it away would be worse."""
    command = decode_line(frame(l=1, r=1, seq=seq))
    assert command == DriveCommand(1.0, 1.0, None)


# --------------------------------------------------------------------------
# sequence tracking
# --------------------------------------------------------------------------


def test_accepts_an_increasing_sequence() -> None:
    tracker = SeqTracker()
    assert all(tracker.accept(seq) for seq in range(10))
    assert (tracker.lost, tracker.rejected) == (0, 0)


def test_first_seq_is_always_accepted() -> None:
    tracker = SeqTracker()
    assert tracker.accept(9999) is True
    assert tracker.last == 9999


def test_unnumbered_frames_are_always_accepted() -> None:
    tracker = SeqTracker()
    tracker.accept(100)
    assert tracker.accept(None) is True
    assert tracker.last == 100


def test_duplicate_is_rejected() -> None:
    tracker = SeqTracker()
    tracker.accept(5)
    assert tracker.accept(5) is False
    assert tracker.rejected == 1


def test_stale_frame_is_rejected() -> None:
    tracker = SeqTracker()
    tracker.accept(10)
    assert tracker.accept(4) is False
    assert tracker.last == 10


def test_stale_frame_does_not_advance_the_position() -> None:
    """A rejected frame must not become the new baseline, or the real next
    frame after it would look like a huge jump."""
    tracker = SeqTracker()
    tracker.accept(10)
    tracker.accept(4)
    assert tracker.accept(11) is True
    assert tracker.lost == 0


def test_gap_counts_as_loss() -> None:
    tracker = SeqTracker()
    tracker.accept(1)
    assert tracker.accept(5) is True
    assert tracker.lost == 3


def test_sequence_wraps() -> None:
    tracker = SeqTracker()
    tracker.accept(SEQ_MODULUS - 2)
    assert tracker.accept(SEQ_MODULUS - 1) is True
    assert tracker.accept(0) is True
    assert tracker.accept(1) is True
    assert (tracker.lost, tracker.rejected) == (0, 0)


def test_stale_frame_across_the_wrap_is_rejected() -> None:
    tracker = SeqTracker()
    tracker.accept(SEQ_MODULUS - 1)
    tracker.accept(2)
    assert tracker.accept(SEQ_MODULUS - 3) is False
    assert tracker.last == 2


def test_reset_lets_a_new_client_start_over() -> None:
    """Without this a reconnecting app that restarts at 0 would be locked out."""
    tracker = SeqTracker()
    tracker.accept(50_000)
    tracker.reset()
    assert tracker.accept(0) is True
    assert tracker.lost == 0


def test_reset_keeps_the_counters() -> None:
    tracker = SeqTracker()
    tracker.accept(1)
    tracker.accept(4)
    tracker.reset()
    assert tracker.lost == 2


# --------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------


def test_encodes_a_state_frame() -> None:
    raw = encode_state(
        DriveState(SideState.FORWARD, SideState.REVERSE),
        ack=42,
        uptime_s=12.4,
        err=None,
    )
    assert raw.endswith(b"\n")
    assert json.loads(raw) == {"t": "state", "l": 1, "r": -1, "ack": 42, "up": 12.4, "err": None}


def test_encodes_side_states_as_plain_ints() -> None:
    raw = encode_state(DriveState(SideState.REVERSE, SideState.STOP))
    assert b'"l":-1' in raw and b'"r":0' in raw


def test_uptime_is_rounded() -> None:
    raw = encode_state(DriveState.STOPPED, uptime_s=1 / 3)
    assert json.loads(raw)["up"] == 0.3


def test_error_field_carries_a_gate_reason() -> None:
    raw = encode_state(DriveState.STOPPED, err="watchdog")
    assert json.loads(raw)["err"] == "watchdog"


def test_state_frame_fits_the_line_cap() -> None:
    raw = encode_state(
        DriveState(SideState.FORWARD, SideState.REVERSE),
        ack=SEQ_MODULUS - 1,
        uptime_s=999999.9,
        err="dead_time",
    )
    assert len(raw) <= MAX_LINE_BYTES


def test_state_frame_is_one_line() -> None:
    """The peer splits on newlines; an embedded one would desync the stream."""
    raw = encode_state(DriveState.STOPPED, err="a\nb")
    assert raw.count(b"\n") == 1
