"""Transport tests -- ``docs/ARCHITECTURE.md`` sections 4 and 5.3.

These run against real loopback sockets rather than a fake stream pair. The
whole point of the TCP transport is that it exercises the same code paths the
Bluetooth one will (``asyncio`` streams, EOF, resets, partial writes), and a
mock reader would quietly not do that -- the buffer-bounding behaviour in
particular only exists because a real peer can withhold a newline forever.

The theme mirrors the codec tests: a bad *frame* is not a bad *link*, and every
way a link can end must produce exactly one disconnect, reported before the
socket is torn down.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

import pytest

from conftest import Rig
from rpicar.config import TcpConfig, TelemetryConfig
from rpicar.drive import SideState
from rpicar.protocol import MAX_LINE_BYTES, decode_line
from rpicar.session import DriveSession
from rpicar.telemetry import TelemetryPublisher
from rpicar.transport import Connection, TcpTransport, TransportError

# Every await in here is on loopback against a service in the same event loop,
# so anything that has not happened within a second is a hang, not slowness.
TIMEOUT_S = 1.0


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


@dataclass
class RecordingSession:
    """A session that only remembers what it was told."""

    lines: list[bytes] = field(default_factory=list)
    connects: list[Connection] = field(default_factory=list)
    disconnects: list[str] = field(default_factory=list)
    # Whether the connection was still open at the moment we were told it had
    # ended. ARCHITECTURE.md 6.2 requires that it is -- the stop has to happen
    # before the teardown, not after it.
    open_at_disconnect: list[bool] = field(default_factory=list)

    def on_connect(self, connection: Connection) -> None:
        self.connects.append(connection)

    def on_line(self, line: bytes) -> None:
        self.lines.append(line)

    def on_disconnect(self, reason: str) -> None:
        self.disconnects.append(reason)
        self.open_at_disconnect.append(not self.connects[-1].closed)


def drive_session(rig: Rig) -> DriveSession:
    """The shipped session, wired to the test rig.

    Deliberately the real ``rpicar.session.DriveSession`` rather than a stand-in:
    these three tests are the only place the whole stack is exercised over an
    actual socket, so a copy of the logic here would prove nothing about the code
    that runs on the car -- and would drift.
    """
    return DriveSession(rig.gov, TelemetryPublisher(rig.gov, TelemetryConfig(state_hz=2.0)))


@dataclass
class Harness:
    transport: TcpTransport
    session: RecordingSession | DriveSession
    port: int
    task: asyncio.Task[None]


def _free_port() -> int:
    """An ephemeral port, released before the transport claims it.

    ``TcpConfig`` forbids port 0, so the listener cannot pick its own.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@asynccontextmanager
async def serving(session: RecordingSession | DriveSession) -> AsyncIterator[Harness]:
    """A transport serving ``session`` on loopback, cancelled on exit."""
    port = _free_port()
    transport = TcpTransport(TcpConfig(host="127.0.0.1", port=port))
    task = asyncio.create_task(transport.serve(session))
    try:
        yield Harness(transport=transport, session=session, port=port, task=task)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def connect(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect, retrying while the listener comes up.

    ``serve()`` binds inside its own task, so the listener may not exist yet the
    first time round.
    """
    async with asyncio.timeout(TIMEOUT_S):
        while True:
            try:
                return await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await asyncio.sleep(0.01)


@asynccontextmanager
async def client(port: int) -> AsyncIterator[tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
    """A connected client, closed on exit."""
    reader, writer = await connect(port)
    try:
        yield reader, writer
    finally:
        with suppress(OSError):
            writer.close()
            await writer.wait_closed()


async def until(predicate: Callable[[], bool]) -> None:
    """Wait for a condition the service reaches on its own, or fail the test."""
    async with asyncio.timeout(TIMEOUT_S):
        while not predicate():
            await asyncio.sleep(0.002)


def frame(**fields: object) -> bytes:
    message = {"t": "drive", "l": 0, "r": 0, "seq": 1}
    message.update(fields)
    return json.dumps(message).encode("utf-8") + b"\n"


# --------------------------------------------------------------------------
# framing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delivers_a_whole_line() -> None:
    session = RecordingSession()
    async with serving(session) as harness, client(harness.port) as (_, writer):
        writer.write(b'{"t":"drive","l":1,"r":1,"seq":1}\n')
        await writer.drain()
        await until(lambda: len(session.lines) == 1)

    # The newline is kept: `decode_line` strips it, and dropping it here would
    # make a line indistinguishable from a fragment.
    assert session.lines == [b'{"t":"drive","l":1,"r":1,"seq":1}\n']


@pytest.mark.asyncio
async def test_splits_several_lines_from_one_write() -> None:
    session = RecordingSession()
    async with serving(session) as harness, client(harness.port) as (_, writer):
        writer.write(frame(seq=1) + frame(seq=2) + frame(seq=3))
        await writer.drain()
        await until(lambda: len(session.lines) == 3)

    assert [decode_line(line).seq for line in session.lines] == [1, 2, 3]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_holds_a_partial_line_until_its_newline() -> None:
    session = RecordingSession()
    async with serving(session) as harness, client(harness.port) as (_, writer):
        writer.write(b'{"t":"drive","l":1,')
        await writer.drain()
        # Nothing to assert against directly, so give the read loop room to get
        # it wrong: if the fragment were delivered, it would be delivered now.
        await asyncio.sleep(0.05)
        assert session.lines == []

        writer.write(b'"r":1,"seq":7}\n')
        await writer.drain()
        await until(lambda: len(session.lines) == 1)

    assert decode_line(session.lines[0]) is not None


@pytest.mark.asyncio
async def test_a_line_ending_at_eof_is_not_a_frame() -> None:
    """A truncated final write must not be guessed at -- see ``pump``."""
    session = RecordingSession()
    async with serving(session) as harness:
        async with client(harness.port) as (_, writer):
            writer.write(b'{"t":"drive","l":1,"r":1')
            await writer.drain()
        await until(lambda: session.disconnects != [])

    assert session.lines == []


# --------------------------------------------------------------------------
# bounding the buffer (ARCHITECTURE.md 5.3)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_peer_that_never_sends_a_newline_is_bounded() -> None:
    session = RecordingSession()
    async with serving(session) as harness, client(harness.port) as (_, writer):
        writer.write(b"x" * (MAX_LINE_BYTES * 10))
        await writer.drain()
        writer.write(b"\n")
        await writer.drain()

        # Dropped, not disconnected: the link survives and the next frame is
        # delivered normally.
        writer.write(frame(l=1, r=1, seq=2))
        await writer.drain()
        await until(lambda: len(session.lines) == 1)

        assert decode_line(session.lines[0]) is not None
        assert session.disconnects == []


@pytest.mark.asyncio
async def test_an_over_long_line_arriving_whole_is_dropped() -> None:
    """The other overrun case: the newline exists, but too far away."""
    session = RecordingSession()
    async with serving(session) as harness, client(harness.port) as (_, writer):
        writer.write(b"y" * (MAX_LINE_BYTES + 50) + b"\n" + frame(seq=3))
        await writer.drain()
        await until(lambda: len(session.lines) == 1)

        assert decode_line(session.lines[0]).seq == 3  # type: ignore[union-attr]
        assert session.disconnects == []


@pytest.mark.asyncio
async def test_several_over_long_lines_in_a_row_are_each_dropped() -> None:
    session = RecordingSession()
    async with serving(session) as harness, client(harness.port) as (_, writer):
        for _ in range(3):
            writer.write(b"z" * (MAX_LINE_BYTES * 4) + b"\n")
            await writer.drain()
        writer.write(frame(seq=9))
        await writer.drain()
        await until(lambda: len(session.lines) == 1)

    assert decode_line(session.lines[0]).seq == 9  # type: ignore[union-attr]


# --------------------------------------------------------------------------
# disconnect
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eof_reports_one_disconnect() -> None:
    session = RecordingSession()
    async with serving(session) as harness:
        async with client(harness.port) as (_, writer):
            writer.write(frame())
            await writer.drain()
            await until(lambda: len(session.lines) == 1)
        await until(lambda: session.disconnects != [])
        # Give a duplicate every chance to show up.
        await asyncio.sleep(0.05)

    assert len(session.connects) == 1
    assert len(session.disconnects) == 1


@pytest.mark.asyncio
async def test_disconnect_is_reported_before_the_socket_is_closed() -> None:
    """ARCHITECTURE.md 6.2 -- the stop happens first, not eventually."""
    session = RecordingSession()
    async with serving(session) as harness:
        async with client(harness.port) as (_, writer):
            writer.write(frame())
            await writer.drain()
            await until(lambda: len(session.lines) == 1)
        await until(lambda: session.disconnects != [])

    assert session.open_at_disconnect == [True]


@pytest.mark.asyncio
async def test_a_reset_connection_is_a_disconnect_not_a_crash() -> None:
    """A reset arrives as an error, not an EOF, and the socket is already gone
    by the time we hear about it -- which is why the stop cannot wait for a
    clean teardown to trigger it."""
    session = RecordingSession()
    async with serving(session) as harness:
        _, writer = await connect(harness.port)
        await until(lambda: len(session.connects) == 1)
        # SO_LINGER with a zero timeout sends RST instead of FIN, which is what
        # a phone leaving Bluetooth range looks like from this end.
        writer.get_extra_info("socket").setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
        await until(lambda: session.disconnects != [])
        assert len(session.disconnects) == 1

        # And the listener is still up afterwards.
        async with client(harness.port) as (_, second):
            second.write(frame())
            await second.drain()
            await until(lambda: len(session.lines) == 1)
            assert len(session.connects) == 2


@pytest.mark.asyncio
async def test_cancelling_serve_reports_the_disconnect() -> None:
    """``systemctl stop`` must not leave the session thinking it is connected."""
    session = RecordingSession()
    async with serving(session) as harness, client(harness.port) as (_, writer):
        writer.write(frame())
        await writer.drain()
        await until(lambda: len(session.lines) == 1)
        harness.task.cancel()
        with suppress(asyncio.CancelledError):
            await harness.task

        assert session.disconnects == ["shutting down"]
        assert session.open_at_disconnect == [True]


# --------------------------------------------------------------------------
# one driver at a time
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_client_is_refused_while_one_is_driving() -> None:
    session = RecordingSession()
    async with serving(session) as harness, client(harness.port) as (_, first):
        first.write(frame(seq=1))
        await first.drain()
        await until(lambda: len(session.lines) == 1)

        async with client(harness.port) as (intruder_reader, intruder):
            # Refused connections get EOF, not a session.
            assert await asyncio.wait_for(intruder_reader.read(), TIMEOUT_S) == b""
            intruder.write(frame(l=1, r=1, seq=2))
            with suppress(OSError):
                await intruder.drain()
            await asyncio.sleep(0.05)

        # The intruder never reached the session, and the driver is untouched.
        assert len(session.connects) == 1
        assert len(session.lines) == 1
        assert session.disconnects == []

        first.write(frame(seq=3))
        await first.drain()
        await until(lambda: len(session.lines) == 2)


@pytest.mark.asyncio
async def test_a_new_client_may_connect_once_the_first_has_gone() -> None:
    session = RecordingSession()
    async with serving(session) as harness:
        async with client(harness.port) as (_, first):
            first.write(frame(seq=1))
            await first.drain()
            await until(lambda: len(session.lines) == 1)
        await until(lambda: session.disconnects != [])

        async with client(harness.port) as (_, second):
            second.write(frame(seq=2))
            await second.drain()
            await until(lambda: len(session.lines) == 2)

    assert len(session.connects) == 2


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_reaches_the_client() -> None:
    session = RecordingSession()
    async with serving(session) as harness, client(harness.port) as (reader, _):
        await until(lambda: len(session.connects) == 1)
        await session.connects[0].send(b'{"t":"state"}\n')
        line = await asyncio.wait_for(reader.readline(), TIMEOUT_S)

    assert line == b'{"t":"state"}\n'


@pytest.mark.asyncio
async def test_send_after_the_client_has_gone_is_silent() -> None:
    """Telemetry races the disconnect at 2Hz; it must not take the task down."""
    session = RecordingSession()
    async with serving(session) as harness:
        async with client(harness.port):
            await until(lambda: len(session.connects) == 1)
        await until(lambda: session.disconnects != [])

        await session.connects[0].send(b'{"t":"state"}\n')

    assert session.connects[0].closed


# --------------------------------------------------------------------------
# startup failure
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_busy_port_fails_loudly() -> None:
    """Better to refuse to start than to run with no way in."""
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = int(occupied.getsockname()[1])
        transport = TcpTransport(TcpConfig(host="127.0.0.1", port=port))
        with pytest.raises(TransportError, match="cannot listen"):
            await transport.serve(RecordingSession())


# --------------------------------------------------------------------------
# the whole stack over a real socket
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_drive_frame_over_a_socket_moves_the_relays(rig: Rig) -> None:
    session = drive_session(rig)
    async with serving(session) as harness, client(harness.port) as (_, writer):
        writer.write(frame(l=1, r=-1, seq=1))
        await writer.drain()
        await until(lambda: rig.applied.is_moving)

        assert rig.left is SideState.FORWARD
        assert rig.right is SideState.REVERSE
        assert rig.bank.states.left_a
        assert rig.bank.states.right_b


@pytest.mark.asyncio
async def test_garbage_never_reaches_the_governor(rig: Rig) -> None:
    session = drive_session(rig)
    async with serving(session) as harness, client(harness.port) as (_, writer):
        writer.write(b"not json\n")
        writer.write(b'{"t":"hello"}\n')
        writer.write(b'{"t":"drive","l":"fast","r":1,"seq":2}\n')
        writer.write(b"q" * (MAX_LINE_BYTES * 3) + b"\n")
        await writer.drain()
        # Then something valid, so there is a moment we know the earlier lines
        # have all been through the session.
        writer.write(frame(l=1, r=1, seq=5))
        await writer.drain()
        await until(lambda: rig.applied.is_moving)

        # Only the numbered frame that decoded got as far as the ordering
        # check, so the three bad ones never touched the watchdog either.
        assert session.seq.last == 5


@pytest.mark.asyncio
async def test_the_car_stops_when_the_client_disappears(rig: Rig) -> None:
    """The disconnect path end to end: socket gone, coils de-energized."""
    session = drive_session(rig)
    async with serving(session) as harness:
        async with client(harness.port) as (_, writer):
            writer.write(frame(l=1, r=1, seq=1))
            await writer.drain()
            await until(lambda: rig.applied.is_moving)
        await until(lambda: not rig.applied.is_moving)

    assert not rig.bank.states.any_energized
    # The next client starts its own numbering.
    assert session.seq.last is None
