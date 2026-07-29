"""TCP transport -- the one you develop against.

It speaks the identical protocol over WiFi, so the whole stack (protocol,
safety, drive, GPIO) can be exercised from a laptop with ``nc`` and no BlueZ in
sight. Bring this up first, always: ARCHITECTURE.md section 9 puts a mock
backend over TCP at step 1 precisely because a bug found there costs a debugging
session rather than a wall.

It is not a deployment transport. There is no authentication of any kind, so the
listener belongs on a network you control -- on the car it is either bound to
loopback or replaced by ``spp.py``, where pairing is the access control.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial

from ..config import TcpConfig
from ..protocol import MAX_LINE_BYTES
from .base import Session, StreamConnection, Transport, TransportError, run_session

__all__ = ["TcpTransport"]

log = logging.getLogger(__name__)


def _peer_name(writer: asyncio.StreamWriter) -> str:
    peer = writer.get_extra_info("peername")
    if isinstance(peer, tuple) and len(peer) >= 2:
        return f"{peer[0]}:{peer[1]}"
    return str(peer)


class TcpTransport(Transport):
    """Line-oriented TCP listener, one client at a time."""

    def __init__(self, config: TcpConfig) -> None:
        self._config = config
        self._active: StreamConnection | None = None
        self._active_task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        return self._active is not None

    async def serve(self, session: Session) -> None:
        try:
            server = await asyncio.start_server(
                partial(self._handle, session=session),
                self._config.host,
                self._config.port,
                # Bounds what a peer that never sends a newline can make us
                # buffer. See the note in base.py -- MAX_LINE_BYTES cannot be
                # enforced by the codec until a whole line exists.
                limit=MAX_LINE_BYTES,
            )
        except OSError as exc:
            raise TransportError(
                f"cannot listen on {self._config.host}:{self._config.port}: {exc}"
            ) from exc

        log.info("tcp transport listening on %s:%d", self._config.host, self._config.port)
        try:
            # `start_server` is already accepting, so this is only the idle
            # wait. Deliberately not `server.serve_forever()`: its cancellation
            # path awaits `Server.wait_closed()`, which since 3.12.1 waits for
            # the connection handlers -- and the handler is exactly what the
            # `finally` below has not cancelled yet. That deadlocks shutdown.
            await asyncio.Event().wait()
        finally:
            # Closing the listening socket does not touch an established
            # connection, so the live one is cancelled explicitly -- otherwise
            # shutdown would leave a client connected to a service that has
            # stopped reading it. The governor's own `finally` still stops the
            # car regardless; this is what makes the disconnect *reported*.
            server.close()
            task = self._active_task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.wait({task})

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        session: Session,
    ) -> None:
        connection = StreamConnection(reader, writer, _peer_name(writer))

        # One driver at a time. Two clients sending drive frames would fight
        # over the relays at 10Hz each, and the watchdog could not tell the
        # difference between that and a healthy link.
        if self._active is not None:
            log.warning("refusing %s: %s is already driving", connection.peer, self._active.peer)
            connection.close()
            return

        self._active = connection
        self._active_task = asyncio.current_task()
        try:
            await run_session(connection, session)
        finally:
            self._active = None
            self._active_task = None
