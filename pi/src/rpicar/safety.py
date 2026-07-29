"""The gate between what the app asked for and what the relays are allowed to do.

Everything in ``docs/ARCHITECTURE.md`` section 6 is implemented here. The app is
a convenience, not a safety boundary -- a malicious, buggy, or simply
disconnected client must not be able to weld a contact or drive the car away.

The design is a synchronous state machine (``tick``) wrapped in a trivial async
loop (``run``). All time comes from an injected clock, so tests drive
transitions deterministically instead of sleeping and hoping.

Ticking rather than acting only on command arrival matters: a dead-time hold has
to expire on schedule even if the next heartbeat is late, and the watchdog has
to fire precisely when heartbeats stop -- which is exactly when there is no
command arriving to trigger the evaluation.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from .config import SafetyConfig
from .drive import DriveController, DriveState, SideState
from .gpio import GpioError

__all__ = ["GateReason", "SafetyGovernor", "SideGovernor"]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GateReason:
    """Why the applied state differs from the commanded one. Sent as telemetry."""

    watchdog: bool = False
    dead_time: bool = False
    dwell: bool = False
    fault: str | None = None

    @property
    def active(self) -> bool:
        return self.watchdog or self.dead_time or self.dwell or self.fault is not None

    def as_wire(self) -> str | None:
        """Compact reason string for the ``err`` field, or None when clear."""
        if self.fault is not None:
            return self.fault
        for flag, name in (
            (self.watchdog, "watchdog"),
            (self.dead_time, "dead_time"),
            (self.dwell, "dwell"),
        ):
            if flag:
                return name
        return None


class SideGovernor:
    """Per-side gating. One instance for the left wheels, one for the right.

    Tracks not just the current state but the last direction the motor was
    *actually turning*, because that is what dead-time protects against. An
    operator who releases reverse and immediately pushes forward is asking for
    the same contact-welding event as a direct reversal -- the motor is still
    spinning either way, and a braked motor takes time to stop.
    """

    def __init__(self, config: SafetyConfig, name: str) -> None:
        self._config = config
        self._name = name
        self._applied = SideState.STOP
        # -inf means "no relevant history", so the very first command is not
        # made to wait out a dwell that never happened.
        self._changed_at = -math.inf
        self._last_moving = SideState.STOP
        self._moving_ended_at = -math.inf

    @property
    def applied(self) -> SideState:
        return self._applied

    def evaluate(self, target: SideState, now: float) -> tuple[SideState, GateReason]:
        """Decide what this side may actually be set to. Pure -- no mutation."""
        if target is self._applied:
            return self._applied, GateReason()

        # Stopping is never gated. Dwell and dead-time exist to stop the car
        # *starting* too eagerly; delaying a stop would mean a released thumb
        # leaves the car driving, which is the opposite of safe.
        if target is SideState.STOP:
            return SideState.STOP, GateReason()

        # Direct reversal: force a stop first. The motor has to be braked before
        # the opposite contacts close. The dead-time itself is enforced on the
        # next evaluation, once this stop has been recorded.
        if self._applied.is_moving:
            return SideState.STOP, GateReason(dead_time=True)

        # Currently stopped and asked to move.
        if now - self._changed_at < self._config.min_dwell_s:
            return self._applied, GateReason(dwell=True)

        if (
            target.opposes(self._last_moving)
            and now - self._moving_ended_at < self._config.reversal_dead_time_s
        ):
            return self._applied, GateReason(dead_time=True)

        return target, GateReason()

    def commit(self, state: SideState, now: float) -> None:
        """Record a state that has been successfully applied to the hardware."""
        previous = self._applied
        if state is previous:
            return
        if previous.is_moving and state is SideState.STOP:
            # Remember which way it was turning, and when it was told to stop.
            # A stopped motor is not a stationary motor.
            self._last_moving = previous
            self._moving_ended_at = now
        self._applied = state
        self._changed_at = now

    def force_stop(self, now: float) -> None:
        """Collapse to STOP, bypassing every gate.

        Still records the motion history, so that reconnecting and immediately
        slamming the opposite direction is dead-time gated like any other
        reversal.
        """
        self.commit(SideState.STOP, now)


class SafetyGovernor:
    """Owns the drive controller. Nothing else may command it."""

    def __init__(
        self,
        drive: DriveController,
        config: SafetyConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._drive = drive
        self._config = config
        self._clock = clock
        self._left = SideGovernor(config, "left")
        self._right = SideGovernor(config, "right")

        self._target = DriveState.STOPPED
        # None means "no command has ever arrived", which the watchdog treats
        # the same as a stale one: stay stopped.
        self._last_command_at: float | None = None
        self._watchdog_tripped = True
        self._reason = GateReason(watchdog=True)
        self._fault: str | None = None
        self._seq: int | None = None

        # Granularity for releasing dead-time holds. Fast enough that the hold
        # is accurate to a few percent, slow enough to be free on a Zero W.
        self._tick_s = max(0.005, min(0.02, config.min_dwell_s / 4 or 0.02))

        # Safe start (ARCHITECTURE.md 6.5): the relays are de-energized before
        # any transport is allowed to accept a connection.
        self._drive.stop()

    # -- state for telemetry ------------------------------------------------

    @property
    def applied(self) -> DriveState:
        """What the relays are actually doing."""
        return self._drive.state

    @property
    def target(self) -> DriveState:
        """What the app last asked for."""
        return self._target

    @property
    def reason(self) -> GateReason:
        return self._reason

    @property
    def seq(self) -> int | None:
        """Sequence number of the most recent accepted command, for acking."""
        return self._seq

    @property
    def tick_interval(self) -> float:
        return self._tick_s

    # -- inputs -------------------------------------------------------------

    def command(self, left: float, right: float, seq: int | None = None) -> None:
        """Accept a drive command from the app and reset the watchdog.

        Called for every valid frame including unchanged ones -- a repeated
        command is the heartbeat, and feeding it here is what keeps the
        watchdog satisfied.
        """
        self._target = DriveState.from_command(left, right)
        self._last_command_at = self._clock()
        self._seq = seq
        if self._watchdog_tripped:
            log.info("watchdog cleared, commands resumed")
            self._watchdog_tripped = False

    def on_disconnect(self, reason: str = "disconnected") -> None:
        """Transport dropped. Stop now, synchronously, before returning.

        ARCHITECTURE.md 6.2. This must complete before the socket teardown, so
        that a client that vanishes mid-throttle cannot leave the car driving.
        """
        log.warning("transport %s; forcing stop", reason)
        self._target = DriveState.STOPPED
        self._last_command_at = None
        self._watchdog_tripped = True
        self._force_stop()

    def emergency_stop(self, fault: str) -> None:
        """Stop and latch a fault. Used when the hardware itself misbehaves."""
        log.error("emergency stop: %s", fault)
        self._fault = fault
        self._target = DriveState.STOPPED
        self._last_command_at = None
        self._force_stop()

    def _force_stop(self) -> None:
        now = self._clock()
        self._drive.stop()
        self._left.force_stop(now)
        self._right.force_stop(now)
        self._reason = GateReason(watchdog=self._watchdog_tripped, fault=self._fault)

    # -- the state machine --------------------------------------------------

    def tick(self) -> None:
        """Advance one step. Safe to call at any rate; idempotent when settled."""
        now = self._clock()

        if self._fault is not None:
            # A latched fault is not self-clearing. Something is wrong with the
            # hardware and a human should look at it.
            return

        target = self._target
        watchdog = self._is_stale(now)
        if watchdog:
            if not self._watchdog_tripped:
                log.warning("no command for %.0fms; stopping", self._config.watchdog_ms)
                self._watchdog_tripped = True
            target = DriveState.STOPPED

        left, left_reason = self._left.evaluate(target.left, now)
        right, right_reason = self._right.evaluate(target.right, now)
        next_state = DriveState(left, right)

        if next_state != self._drive.state:
            try:
                self._drive.apply(next_state)
            except GpioError as exc:
                # drive.apply has already forced the coils off.
                self._left.force_stop(now)
                self._right.force_stop(now)
                self.emergency_stop(f"gpio: {exc}")
                return
            self._left.commit(left, now)
            self._right.commit(right, now)

        self._reason = GateReason(
            watchdog=watchdog,
            dead_time=left_reason.dead_time or right_reason.dead_time,
            dwell=left_reason.dwell or right_reason.dwell,
            fault=None,
        )

    def _is_stale(self, now: float) -> bool:
        if self._last_command_at is None:
            return True
        return now - self._last_command_at > self._config.watchdog_s

    async def run(self) -> None:
        """Tick until cancelled, then stop the car on the way out."""
        log.info(
            "safety governor running: watchdog=%dms dead_time=%dms dwell=%dms tick=%.0fms",
            self._config.watchdog_ms,
            self._config.reversal_dead_time_ms,
            self._config.min_dwell_ms,
            self._tick_s * 1000,
        )
        try:
            while True:
                self.tick()
                await asyncio.sleep(self._tick_s)
        finally:
            # ARCHITECTURE.md 6.6 -- cancellation, shutdown, and crashes all
            # leave the car braked.
            self._drive.stop()
