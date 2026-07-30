"""``state`` frames back to the app -- ``docs/ARCHITECTURE.md`` section 5.2.

Periodic at ``telemetry.state_hz``, plus an immediate frame whenever the applied
state or the gate reason changes. The immediate frame is the point of this
module: at 2Hz alone, a watchdog stop could sit unreported for half a second,
which is exactly the moment the operator most needs the app to agree with the
car.

What goes on the wire is ``governor.applied`` -- what the relays are *actually*
doing -- and never ``governor.target``. The two diverge legitimately whenever a
gate is active, and an app that rendered the target would show the car driving
through its own dead-time hold.

Like ``safety.py`` this is a synchronous state machine (``tick``) wrapped in a
trivial async loop (``run``), with all time from an injected clock. Telemetry
timing is then testable without sleeping, and for the same reason: the interval
that matters is measured in hundreds of milliseconds, so asserting on real
elapsed time would only buy flakiness.

Changes are detected by polling the governor rather than by having the governor
call back into here. That keeps the dependency pointing the same way as
everything else (``telemetry -> safety``), and it matches how the governor
already works -- it is a ticking machine, so nothing it does can outrun its own
tick.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from .config import TelemetryConfig
from .protocol import encode_state
from .safety import SafetyGovernor

if TYPE_CHECKING:
    # Type-only: telemetry needs somewhere to put bytes, not a transport. The
    # import stays out of the runtime graph so this module cannot start
    # depending on a link backend by accident.
    from .transport.base import Connection

__all__ = ["TelemetryPublisher"]

log = logging.getLogger(__name__)

# What has to differ for a frame to count as "something changed". Deliberately
# excludes `ack` and `up`: both move on every heartbeat, and including either
# would silently promote telemetry to the 10Hz command rate.
_Signature = tuple[int, int, str | None]


class TelemetryPublisher:
    """Publishes ``state`` frames for whichever client is currently connected.

    Owns no socket. The session hands it a :class:`Connection` on connect and
    takes it away on disconnect, so a client that drops mid-drive simply stops
    being sent to -- there is no teardown to get wrong here, and the stop itself
    is the governor's job (ARCHITECTURE.md 6.2).
    """

    def __init__(
        self,
        governor: SafetyGovernor,
        config: TelemetryConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gov = governor
        self._config = config
        self._clock = clock
        self._started_at = clock()
        self._connection: Connection | None = None
        self._last_signature: _Signature | None = None
        self._last_sent_at: float | None = None
        # The governor is what produces the changes we are watching for, so
        # polling faster than its tick cannot see anything new. Taking the min
        # keeps a hypothetical high state_hz honest as well.
        self._poll_s = min(governor.tick_interval, config.interval_s)

    @property
    def poll_interval(self) -> float:
        return self._poll_s

    @property
    def connected(self) -> bool:
        return self._connection is not None

    # -- session hooks ------------------------------------------------------

    def attach(self, connection: Connection) -> None:
        """Start publishing to a newly connected client.

        Clearing both bits of history is what makes the first frame immediate:
        the app gets the current state on connect instead of waiting up to a
        full period to find out what the car is doing.
        """
        self._connection = connection
        self._last_signature = None
        self._last_sent_at = None

    def detach(self) -> None:
        """Stop publishing. Idempotent."""
        self._connection = None

    # -- the state machine --------------------------------------------------

    def tick(self) -> bytes | None:
        """The frame to send right now, or ``None`` if none is due.

        Returns the bytes rather than sending them so that the decision is
        synchronous and testable in isolation; ``run`` does the I/O.
        """
        if self._connection is None:
            # No client. Returning None rather than a frame matters: marking a
            # frame as sent when nothing received it would delay the immediate
            # frame the next client is owed.
            return None

        now = self._clock()
        applied = self._gov.applied
        err = self._gov.reason.as_wire()
        signature: _Signature = (int(applied.left), int(applied.right), err)

        if signature == self._last_signature and not self._is_due(now):
            return None

        self._last_signature = signature
        self._last_sent_at = now
        # `ack` is the governor's view of the last accepted command rather than
        # the SeqTracker's: the two agree for every numbered frame, and taking
        # it from the governor keeps this module to a single dependency.
        return encode_state(
            applied,
            ack=self._gov.seq,
            uptime_s=now - self._started_at,
            err=err,
        )

    def _is_due(self, now: float) -> bool:
        if self._last_sent_at is None:
            return True
        return now - self._last_sent_at >= self._config.interval_s

    async def run(self) -> None:
        """Publish until cancelled."""
        log.info(
            "telemetry running: %.1fHz periodic, polling every %.0fms",
            self._config.state_hz,
            self._poll_s * 1000,
        )
        while True:
            frame = self.tick()
            connection = self._connection
            if frame is not None and connection is not None:
                # Connection.send never raises on a broken link -- the read
                # loop is the authority on disconnection, and a second report
                # here would only take down the task the next client needs.
                await connection.send(frame)
                log.debug("state -> %s: %s", connection.peer, frame.rstrip().decode())
            await asyncio.sleep(self._poll_s)
