"""What a connection means to the car: the join between protocol and safety.

This is the ``Session`` the transport layer calls into (``transport/base.py``).
It is the only place where a decoded frame becomes a command, and the only place
that knows both that lines are JSON and that a car is attached -- which is
exactly why it lives here and not in either neighbour. ``protocol.py`` must not
know a governor exists, and ``safety.py`` must not know what JSON is.

All three callbacks are **synchronous**, and that is load-bearing rather than
incidental. ``on_disconnect`` runs inside the ``finally`` that owns the socket
close, so the coils are de-energized before the peer is gone
(``docs/ARCHITECTURE.md`` 6.2). Make any of them ``async``, or schedule the stop
as a task, and that guarantee silently degrades to "eventually" -- on
cancellation there may be no chance to run a scheduled task at all.

The ordering in ``on_line`` is the other load-bearing part. ``decode_line``
returning ``None`` means "nothing happened", and a rejected sequence number
means "this already happened": neither may reach the governor, because neither
is a heartbeat. Getting that wrong in either direction is bad -- feed the
watchdog on garbage and a babbling peer keeps the car alive forever, trip it on
garbage and one stray byte stops the car mid-drive.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .protocol import SeqTracker, decode_line
from .safety import SafetyGovernor
from .telemetry import TelemetryPublisher

if TYPE_CHECKING:
    from .transport.base import Connection

__all__ = ["DriveSession"]

log = logging.getLogger(__name__)


class DriveSession:
    """One client's worth of driving: decode, order, gate, actuate, publish.

    A single instance is reused for every connection the transport accepts --
    the per-connection state is just the sequence position and the telemetry
    sink, and both are reset on disconnect.
    """

    def __init__(self, governor: SafetyGovernor, telemetry: TelemetryPublisher) -> None:
        self._gov = governor
        self._telemetry = telemetry
        self._seq = SeqTracker()

    @property
    def seq(self) -> SeqTracker:
        """Exposed for logging link quality; not part of the ``Session`` protocol."""
        return self._seq

    def on_connect(self, connection: Connection) -> None:
        # The governor is already stopped -- by safe start on the first
        # connection, and by the previous `on_disconnect` on every one after --
        # so there is nothing to reset here. Attaching telemetry publishes an
        # immediate frame, so the app renders the real state on connect instead
        # of assuming a stop.
        self._telemetry.attach(connection)

    def on_line(self, line: bytes) -> None:
        command = decode_line(line)
        # A `None` here means "nothing happened" and must not touch the watchdog
        # in either direction; a rejected seq means the frame is stale, and a
        # stale frame is a stale throttle position.
        if command is None or not self._seq.accept(command.seq):
            return
        self._gov.command(command.left, command.right, command.seq)
        # Act on it now rather than waiting up to one tick. This is *in addition
        # to* the governor's periodic tick, never a replacement for it: a
        # dead-time hold has to expire on schedule even when the next heartbeat
        # is late, and the watchdog has to fire precisely when frames stop --
        # which is when no command is arriving to trigger an evaluation. Extra
        # ticks are free and cannot actuate anything a gate would have refused.
        self._gov.tick()

    def on_disconnect(self, reason: str) -> None:
        # Stop first. Everything after this point is bookkeeping, and the stop
        # has to be done before the socket teardown that is waiting on us.
        self._gov.on_disconnect(reason)
        self._telemetry.detach()
        # The next client starts its own numbering; holding this one's position
        # would reject everything it sends until it happened to pass us.
        self._seq.reset()
        if self._seq.lost or self._seq.rejected:
            # Cumulative for the process, not this connection: `reset()`
            # deliberately keeps the counters, since they are the only visibility
            # we get into link quality and a per-session count of zero would
            # hide a link that drops frames on every reconnect.
            log.info(
                "link quality since start: %d frames lost, %d rejected",
                self._seq.lost,
                self._seq.rejected,
            )
