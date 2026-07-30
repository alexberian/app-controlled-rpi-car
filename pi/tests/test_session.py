"""Session tests -- the join between protocol and safety.

The load-bearing property here is what counts as a heartbeat. Only a frame that
``decode_line`` accepted *and* ``SeqTracker`` approved may reach the governor,
because reaching the governor is what resets the watchdog. Both directions of
getting that wrong are dangerous and neither is visible from either neighbour's
own tests: feed the watchdog on garbage and a babbling peer keeps the car alive
forever, trip it on garbage and one stray byte stops the car mid-drive.

The rest is disconnect ordering. ``on_disconnect`` has to leave the car stopped
before it returns, since the transport closes the socket the moment it does.
"""

from __future__ import annotations

import json

import pytest

from conftest import FakeClock, FakeConnection, Rig
from rpicar.config import TelemetryConfig
from rpicar.drive import SideState
from rpicar.session import DriveSession
from rpicar.telemetry import TelemetryPublisher


@pytest.fixture
def pub(rig: Rig, clock: FakeClock) -> TelemetryPublisher:
    return TelemetryPublisher(rig.gov, TelemetryConfig(state_hz=2.0), clock=clock)


@pytest.fixture
def session(rig: Rig, pub: TelemetryPublisher) -> DriveSession:
    return DriveSession(rig.gov, pub)


def drive(left: float, right: float, seq: int | None = None) -> bytes:
    """One encoded ``drive`` frame, as the app would send it."""
    message: dict[str, object] = {"t": "drive", "l": left, "r": right}
    if seq is not None:
        message["seq"] = seq
    return json.dumps(message).encode() + b"\n"


def heartbeat(session: DriveSession, rig: Rig, left: float, right: float, seconds: float) -> None:
    """Hold a command at the 10Hz command rate through the session."""
    period = 0.1
    elapsed = 0.0
    session.on_line(drive(left, right))
    while elapsed < seconds - 1e-9:
        step = min(period, seconds - elapsed)
        rig.advance(step)
        elapsed += step
        session.on_line(drive(left, right))


# --------------------------------------------------------------------------
# a frame becomes a command
# --------------------------------------------------------------------------


def test_a_drive_frame_actuates_the_relays(session: DriveSession, rig: Rig) -> None:
    session.on_line(drive(1.0, 1.0))
    assert rig.applied.left is SideState.FORWARD
    assert rig.applied.right is SideState.FORWARD


def test_a_frame_is_applied_without_waiting_for_the_governor_loop(
    session: DriveSession, rig: Rig
) -> None:
    # `on_line` ticks the governor itself, so the relays move on arrival rather
    # than up to one tick later. Nothing advances the clock here.
    session.on_line(drive(-1.0, 1.0))
    assert (rig.applied.left, rig.applied.right) == (SideState.REVERSE, SideState.FORWARD)


def test_an_unnumbered_frame_still_drives(session: DriveSession, rig: Rig) -> None:
    # A client that does not implement `seq` is degraded, not broken.
    session.on_line(drive(1.0, 1.0))
    assert rig.applied.left is SideState.FORWARD


def test_out_of_range_values_are_clamped_not_rejected(session: DriveSession, rig: Rig) -> None:
    # A miscalibrated client stays drivable.
    session.on_line(drive(5.0, -5.0))
    assert (rig.applied.left, rig.applied.right) == (SideState.FORWARD, SideState.REVERSE)


def test_a_repeated_command_is_a_heartbeat_not_an_actuation(
    session: DriveSession, rig: Rig
) -> None:
    session.on_line(drive(1.0, 1.0, seq=1))
    before = rig.bank.actuations
    for seq in range(2, 12):
        rig.advance(0.1)
        session.on_line(drive(1.0, 1.0, seq=seq))
    # Ten more identical frames, no relay movement -- this is the contact-life
    # guarantee, and the session is where a naive implementation would break it.
    assert rig.bank.actuations == before


# --------------------------------------------------------------------------
# what must not count as a heartbeat
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        b"not json at all\n",
        b"\n",
        b"[]\n",
        b'{"t":"drive","l":"banana","r":1}\n',
        b'{"t":"drive","l":null,"r":1}\n',
        b'{"t":"drive","l":true,"r":1}\n',
        b'{"t":"drive","l":1e999,"r":1}\n',
        b'{"t":"unknown","l":1,"r":1}\n',
        b'{"t":"state","l":1,"r":1}\n',
        b"x" * 300 + b"\n",
    ],
    ids=[
        "malformed",
        "empty",
        "not-an-object",
        "non-numeric",
        "null",
        "boolean",
        "infinity",
        "unknown-type",
        "own-state-echo",
        "over-long",
    ],
)
def test_garbage_does_not_feed_the_watchdog(session: DriveSession, rig: Rig, line: bytes) -> None:
    session.on_line(drive(1.0, 1.0))
    assert rig.applied.left is SideState.FORWARD

    # 400ms of nothing but garbage. If any of it reset the watchdog timer, the
    # car would still be driving 600ms after the last real command.
    for _ in range(4):
        rig.advance(0.1)
        session.on_line(line)
    rig.advance(0.2)

    assert rig.applied.left is SideState.STOP
    assert rig.gov.reason.watchdog


def test_garbage_does_not_trip_the_watchdog(session: DriveSession, rig: Rig) -> None:
    # The other direction: one stray byte in a healthy stream must not stop the
    # car. The frames either side of it are what keep it alive.
    session.on_line(drive(1.0, 1.0))
    for _ in range(4):
        rig.advance(0.05)
        session.on_line(b"garbage\n")
        session.on_line(drive(1.0, 1.0))
    assert rig.applied.left is SideState.FORWARD
    assert not rig.gov.reason.watchdog


def test_a_stale_frame_does_not_feed_the_watchdog(session: DriveSession, rig: Rig) -> None:
    session.on_line(drive(1.0, 1.0, seq=100))
    # Replays of an older frame. A stale frame is a stale throttle position, so
    # it is rejected -- and a rejected frame is not a heartbeat either.
    for _ in range(4):
        rig.advance(0.1)
        session.on_line(drive(1.0, 1.0, seq=50))
    rig.advance(0.2)
    assert rig.applied.left is SideState.STOP
    assert session.seq.rejected == 4


def test_a_stale_stop_cannot_overtake_a_newer_command(session: DriveSession, rig: Rig) -> None:
    session.on_line(drive(0.0, 0.0, seq=10))
    heartbeat(session, rig, 1.0, 1.0, 0.3)
    session.on_line(drive(1.0, 1.0, seq=20))
    assert rig.applied.left is SideState.FORWARD
    # An old frame arriving late must not be applied, whatever it says.
    session.on_line(drive(0.0, 0.0, seq=11))
    assert rig.applied.left is SideState.FORWARD


def test_lost_frames_are_counted(session: DriveSession) -> None:
    session.on_line(drive(1.0, 1.0, seq=1))
    session.on_line(drive(1.0, 1.0, seq=5))
    # The gap is the only visibility we get into link quality.
    assert session.seq.lost == 3


# --------------------------------------------------------------------------
# connect and disconnect
# --------------------------------------------------------------------------


def test_connecting_publishes_an_immediate_state_frame(
    session: DriveSession, pub: TelemetryPublisher
) -> None:
    assert pub.tick() is None
    session.on_connect(FakeConnection())
    frame = pub.tick()
    assert frame is not None
    assert json.loads(frame)["t"] == "state"


def test_disconnecting_stops_the_car(session: DriveSession, rig: Rig) -> None:
    session.on_connect(FakeConnection())
    heartbeat(session, rig, 1.0, 1.0, 0.2)
    assert rig.applied.left is SideState.FORWARD

    # Synchronously, before returning -- the transport closes the socket the
    # moment this call comes back (ARCHITECTURE.md 6.2).
    session.on_disconnect("closed by peer")
    assert rig.applied.left is SideState.STOP
    assert rig.applied.right is SideState.STOP


def test_disconnecting_stops_publishing(
    session: DriveSession, rig: Rig, pub: TelemetryPublisher
) -> None:
    session.on_connect(FakeConnection())
    session.on_disconnect("closed by peer")
    assert not pub.connected
    rig.advance(1.0)
    assert pub.tick() is None


def test_the_next_client_may_start_its_own_numbering(session: DriveSession, rig: Rig) -> None:
    session.on_connect(FakeConnection())
    session.on_line(drive(1.0, 1.0, seq=9000))
    session.on_disconnect("closed by peer")

    # Past the dwell that the forced stop just started -- otherwise this would
    # assert the gate rather than the sequence reset.
    rig.advance(0.1)

    # A fresh app process starts its counter at 1. Holding the old position
    # would reject everything it sends until it happened to pass 9000.
    session.on_connect(FakeConnection())
    session.on_line(drive(1.0, 1.0, seq=1))
    assert session.seq.rejected == 0
    assert rig.applied.left is SideState.FORWARD


def test_a_reconnect_cannot_slam_the_opposite_direction(session: DriveSession, rig: Rig) -> None:
    session.on_connect(FakeConnection())
    heartbeat(session, rig, 1.0, 1.0, 0.2)
    session.on_disconnect("closed by peer")

    # The forced stop recorded which way the motor was turning, so dead-time
    # still applies across the reconnect -- a braked motor is not a stationary
    # one, and a dropped link is exactly when this gets exercised.
    session.on_connect(FakeConnection())
    session.on_line(drive(-1.0, -1.0, seq=1))
    assert rig.applied.left is SideState.STOP
    assert rig.gov.reason.dead_time or rig.gov.reason.dwell


def test_a_disconnect_without_a_connect_is_survivable(session: DriveSession, rig: Rig) -> None:
    # Cancellation during accept can produce this ordering; it must stop the car
    # rather than raise.
    session.on_disconnect("shutting down")
    assert rig.applied.left is SideState.STOP
