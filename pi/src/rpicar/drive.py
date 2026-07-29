"""Commanded direction per side, translated to relay coil states.

This layer knows about forward, reverse, and the H-bridge truth table. It does
not know about Bluetooth, JSON, or time -- when a change is *allowed* is
``safety.py``'s problem, and this module will faithfully apply whatever it is
handed.

Quantisation from the wire's numeric ``l``/``r`` to a three-valued
``SideState`` happens here, in ``SideState.from_command``. Everything upstream
carries floats so that adding PWM later widens a range rather than changing a
type.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import ClassVar

from .gpio import CoilStates, GpioError, RelayBank

__all__ = ["DriveController", "DriveState", "SideState"]

log = logging.getLogger(__name__)

# A commanded magnitude at or below this reads as STOP. Relays have no
# intermediate, so the only question is which side of the threshold we are on.
# The app already applies its own deadzone; this is the backstop.
COMMAND_DEADZONE = 0.5


class SideState(IntEnum):
    """What one side of the car has been told to do."""

    REVERSE = -1
    STOP = 0
    FORWARD = 1

    @classmethod
    def from_command(cls, value: float, deadzone: float = COMMAND_DEADZONE) -> SideState:
        """Quantise a wire value in [-1.0, 1.0] to a relay-achievable state."""
        if value > deadzone:
            return cls.FORWARD
        if value < -deadzone:
            return cls.REVERSE
        return cls.STOP

    @property
    def is_moving(self) -> bool:
        return self is not SideState.STOP

    def opposes(self, other: SideState) -> bool:
        """True if going from ``other`` to ``self`` reverses a spinning motor.

        This is the condition that requires dead-time. Anything involving STOP
        is not a reversal, because the motor is already being braked.
        """
        return self.is_moving and other.is_moving and self is not other


@dataclass(frozen=True, slots=True)
class DriveState:
    """Both sides. Skid steer -- there is no mixer, the operator steers."""

    left: SideState = SideState.STOP
    right: SideState = SideState.STOP

    STOPPED: ClassVar[DriveState]

    @classmethod
    def from_command(cls, left: float, right: float) -> DriveState:
        return cls(SideState.from_command(left), SideState.from_command(right))

    @property
    def is_moving(self) -> bool:
        return self.left.is_moving or self.right.is_moving

    def __str__(self) -> str:
        symbol = {SideState.FORWARD: "^", SideState.STOP: "0", SideState.REVERSE: "v"}
        return f"{symbol[self.left]}{symbol[self.right]}"


DriveState.STOPPED = DriveState()


def to_coils(state: DriveState) -> CoilStates:
    """The relay H-bridge truth table from ``docs/ARCHITECTURE.md`` section 2.1.

    Per side, A energized pulls one motor terminal to V+ while B stays on GND;
    B energized does the reverse. Neither energized leaves both terminals on
    GND, which brakes the motor rather than letting it coast.

    Note that A and B are never energized together. That combination is also a
    brake, but it holds two coils for no benefit, and this way a stuck relay is
    visible as a state that should never occur.
    """
    return CoilStates(
        left_a=state.left is SideState.FORWARD,
        left_b=state.left is SideState.REVERSE,
        right_a=state.right is SideState.FORWARD,
        right_b=state.right is SideState.REVERSE,
    )


class DriveController:
    """Owns the relay bank and holds the last applied drive state."""

    def __init__(self, bank: RelayBank) -> None:
        self._bank = bank
        self._state = DriveState.STOPPED

    @property
    def state(self) -> DriveState:
        """The last state actually applied to the relays."""
        return self._state

    @property
    def coils(self) -> CoilStates:
        return self._bank.states

    def apply(self, state: DriveState) -> None:
        """Drive the relays to ``state``.

        On a GPIO failure the coils are forced off before the error propagates:
        a partial write leaves the hardware in an unknown state, and unknown
        must always collapse to stopped.
        """
        if state == self._state:
            return
        try:
            self._bank.apply(to_coils(state))
        except GpioError:
            log.exception("relay write failed applying %s; forcing stop", state)
            self._bank.all_off()
            self._state = DriveState.STOPPED
            raise
        log.info("drive %s -> %s", self._state, state)
        self._state = state

    def stop(self) -> None:
        """De-energize everything. Never raises -- this is a fail-safe path."""
        self._bank.all_off()
        self._state = DriveState.STOPPED

    def close(self) -> None:
        """Stop and release the hardware."""
        self._bank.close()
        self._state = DriveState.STOPPED
