"""In-memory relay bank that records what it was asked to do.

This is not a toy. Steps 1-3 of the bring-up order in ``docs/ARCHITECTURE.md``
section 9 run against this backend, and every safety test asserts on the
transition log it produces. A bug caught here costs a test run; the same bug
caught in step 5 costs a wall.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .base import CoilStates, RelayBank

__all__ = ["MockRelayBank", "Transition"]


@dataclass(frozen=True, slots=True)
class Transition:
    """One coil state change, and when it happened."""

    at: float
    states: CoilStates

    def __str__(self) -> str:
        return f"{self.at:8.3f}s  {self.states}"


class MockRelayBank(RelayBank):
    """Records transitions instead of touching hardware.

    The clock is injectable so tests can drive time explicitly rather than
    sleeping. Timing-sensitive safety tests should use a fake clock -- asserting
    on real elapsed time makes them flaky on a loaded machine.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self._clock = clock
        # The starting de-energized state is recorded so tests can assert that
        # the bank came up safe without special-casing an empty log.
        self.transitions: list[Transition] = [Transition(clock(), CoilStates.ALL_OFF)]
        self.released = False

    def _write(self, states: CoilStates, *, force: bool = False) -> None:
        # A forced write that changes nothing actuates no relay, so it must not
        # appear in the log -- `actuations` is a contact-life count, not a
        # syscall count.
        if force and states == self._states:
            return
        self.transitions.append(Transition(self._clock(), states))

    def _release(self) -> None:
        self.released = True

    # -- test helpers -------------------------------------------------------

    @property
    def state_log(self) -> list[CoilStates]:
        """Just the states, in order. Convenient for sequence assertions."""
        return [t.states for t in self.transitions]

    @property
    def actuations(self) -> int:
        """Number of changes after the initial safe state.

        This is the relay contact budget being spent, so it is what chatter
        tests assert on.
        """
        return len(self.transitions) - 1

    def reset_log(self) -> None:
        """Drop history, keeping the current state as the new baseline."""
        self.transitions = [Transition(self._clock(), self._states)]

    def format_log(self) -> str:
        """Human-readable transition log, for test failure output."""
        return "\n".join(str(t) for t in self.transitions)
