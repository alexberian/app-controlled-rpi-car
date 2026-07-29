"""The link interface: whole lines in, whole lines out, and nothing else.

A transport accepts one client at a time, splits the inbound byte stream into
lines, and hands each line to a :class:`Session`. It does not know what a line
means -- ``protocol.py`` decodes and ``safety.py`` decides -- which is what lets
``tcp.py`` and ``spp.py`` be interchangeable and what lets the whole stack be
exercised over loopback on a laptop (``docs/ARCHITECTURE.md`` section 4).

Two properties of this layer are load-bearing:

* **Disconnect is reported synchronously, before the socket is torn down.**
  ``Session.on_disconnect`` runs inside the ``finally`` that owns the close, so
  ``SafetyGovernor.on_disconnect`` has already de-energized the coils by the
  time the peer is gone (ARCHITECTURE.md 6.2). Never make it a scheduled task:
  a client that vanishes mid-throttle would then leave the car driving for as
  long as the loop takes to get back around.

* **The read buffer is bounded.** ``MAX_LINE_BYTES`` in ``protocol.py`` can only
  be checked once a line exists, so a peer that never sends a newline would
  otherwise buffer without limit. The reader is capped at the same value and an
  over-long line is discarded here, keeping the connection -- a bad frame is not
  a bad link (ARCHITECTURE.md 5.3).
"""

from __future__ import annotations

import abc
import asyncio
import logging
from typing import Protocol

from ..protocol import MAX_LINE_BYTES

__all__ = [
    "Connection",
    "Session",
    "StreamConnection",
    "Transport",
    "TransportError",
    "run_session",
]

log = logging.getLogger(__name__)


class TransportError(Exception):
    """Raised when a link cannot be established or published."""


class Session(Protocol):
    """What the service does with a connection. Implemented above this layer.

    All three callbacks are **synchronous**. That is deliberate: the governor's
    ``command()`` is synchronous, so nothing here needs to await, and a
    synchronous handler cannot reorder frames or let a disconnect overtake the
    last command that arrived before it.
    """

    def on_connect(self, connection: Connection) -> None:
        """A client has connected. Hold the connection to send telemetry on it."""

    def on_line(self, line: bytes) -> None:
        """One complete line, trailing newline included. Never called after
        ``on_disconnect``."""

    def on_disconnect(self, reason: str) -> None:
        """The link is finished. Called exactly once per ``on_connect``, before
        the socket is closed, and on cancellation as well as on EOF."""


class Connection(abc.ABC):
    """One accepted client, as seen from above: a place to put bytes.

    Reading is not exposed. Lines are pushed to the session by the transport's
    own read loop, so there is only ever one reader and no way for a caller to
    stall it.
    """

    @property
    @abc.abstractmethod
    def peer(self) -> str:
        """Who is on the other end, for logs."""

    @property
    @abc.abstractmethod
    def closed(self) -> bool:
        """Whether the link is closed or closing."""

    @abc.abstractmethod
    async def send(self, data: bytes) -> None:
        """Write already-encoded bytes, newline included.

        Never raises on a broken link. The read loop is the authority on
        disconnection, and telemetry failing loudly here would only produce a
        second report of the same event -- while taking down the telemetry task
        that has to keep running for the next client.
        """

    @abc.abstractmethod
    def close(self) -> None:
        """Close the link. Idempotent, synchronous, safe from a finally block."""


class StreamConnection(Connection):
    """A :class:`Connection` over an ``asyncio`` stream pair.

    Shared by ``tcp.py`` and ``spp.py``: an RFCOMM socket becomes a stream pair
    the same way a TCP one does, so all of the framing and bounding below is
    written once.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer: str):
        self._reader = reader
        self._writer = writer
        self._peer = peer

    @property
    def peer(self) -> str:
        return self._peer

    @property
    def closed(self) -> bool:
        return self._writer.is_closing()

    async def send(self, data: bytes) -> None:
        if self._writer.is_closing():
            return
        try:
            self._writer.write(data)
            await self._writer.drain()
        except (OSError, ConnectionError) as exc:
            log.debug("send to %s failed: %s", self._peer, exc)

    def close(self) -> None:
        if not self._writer.is_closing():
            self._writer.close()

    async def pump(self, session: Session) -> str:
        """Read lines until the peer stops. Returns why it stopped.

        Only ``asyncio.CancelledError`` escapes; a socket error is a reason, not
        an exception, because every way a link can end is the same event to the
        layer above.
        """
        while True:
            try:
                line = await self._reader.readuntil(b"\n")
            except asyncio.IncompleteReadError as exc:
                # EOF. A fragment without a newline is not a frame, and
                # guessing at one is exactly how a truncated write turns into a
                # drive command nobody sent.
                if exc.partial:
                    log.debug("discarding %d trailing bytes from %s", len(exc.partial), self._peer)
                return "closed by peer"
            except asyncio.LimitOverrunError as exc:
                # ARCHITECTURE.md 5.3: drop the line, log, keep the connection.
                log.warning(
                    "dropping over-long line from %s (>%d bytes)", self._peer, MAX_LINE_BYTES
                )
                try:
                    await self._discard_line(exc.consumed)
                except asyncio.IncompleteReadError:
                    return "closed by peer"
                continue
            except (OSError, ConnectionError) as exc:
                return f"error: {exc}"
            session.on_line(line)

    async def _discard_line(self, consumed: int) -> None:
        """Drop an over-long line, up to and including its newline.

        ``readuntil`` leaves the buffer untouched when it overruns the limit, so
        the bytes it already scanned have to be consumed explicitly or the very
        next read raises on the same data forever.
        """
        await self._reader.readexactly(consumed)
        while True:
            try:
                await self._reader.readuntil(b"\n")
                return
            except asyncio.LimitOverrunError as exc:
                await self._reader.readexactly(exc.consumed)


async def run_session(connection: StreamConnection, session: Session) -> None:
    """Drive one connection from accept to close.

    The ordering in the ``finally`` is the safety-critical part: the session is
    told about the disconnect *before* the socket is closed, and both happen
    before this returns.
    """
    log.info("client connected: %s", connection.peer)
    session.on_connect(connection)
    reason = "closed"
    try:
        reason = await connection.pump(session)
    except asyncio.CancelledError:
        reason = "shutting down"
        raise
    finally:
        # ARCHITECTURE.md 6.2. Synchronous, and never a scheduled task -- on
        # cancellation there may be no chance to run one.
        session.on_disconnect(reason)
        connection.close()
        log.info("client disconnected: %s (%s)", connection.peer, reason)


class Transport(abc.ABC):
    """A source of connections. One at a time, forever, until cancelled."""

    @abc.abstractmethod
    async def serve(self, session: Session) -> None:
        """Accept connections and run them against ``session`` until cancelled.

        Callers must have completed safe start before calling this: the relays
        are driven off in ``SafetyGovernor.__init__``, which is why the governor
        is constructed before the transport is served (ARCHITECTURE.md 6.5).

        Returns only by cancellation. The current connection is disconnected
        cleanly on the way out.
        """
