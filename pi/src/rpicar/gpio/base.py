"""The relay bank interface: four coils, and nothing above this layer knows how
they are driven.

This is deliberately dumb. It has no concept of forward, reverse, sides, or
safety -- it energizes coils. The mapping from a side's commanded direction to
coil states lives in ``drive.py``, and the rules about *when* a change is
allowed live in ``safety.py``.

Two invariants every implementation must honour:

* ``all_off()`` is the fail-safe and must work at any time, including after
  ``close()`` and from a signal handler. It never raises.
* Construction leaves the coils de-energized before anything else can run.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import ClassVar

__all__ = ["CoilStates", "GpioError", "RelayBank"]


class GpioError(Exception):
    """Raised when the underlying GPIO layer fails in a way we cannot recover from."""


@dataclass(frozen=True, slots=True)
class CoilStates:
    """Which relay coils are energized.

    ``True`` means energized (contacts pulled to NO). Whether that corresponds
    to a high or a low voltage on the pin is the backend's problem -- see
    ``active_low`` in ``car.toml``.

    Per side, A energized drives forward and B energized drives reverse. Both
    off and both on are equivalent brakes; see the truth table in
    ``docs/ARCHITECTURE.md`` section 2.1.
    """

    left_a: bool = False
    left_b: bool = False
    right_a: bool = False
    right_b: bool = False

    ALL_OFF: ClassVar[CoilStates]

    def as_tuple(self) -> tuple[bool, bool, bool, bool]:
        """Coils in the same order as ``PinConfig.as_tuple()``."""
        return (self.left_a, self.left_b, self.right_a, self.right_b)

    @property
    def any_energized(self) -> bool:
        return any(self.as_tuple())

    def __str__(self) -> str:
        return "".join(
            name if on else "-" for name, on in zip("LlRr", self.as_tuple(), strict=True)
        )


CoilStates.ALL_OFF = CoilStates()


class RelayBank(abc.ABC):
    """Four relay coils driven as two H-bridge pairs."""

    def __init__(self) -> None:
        self._states = CoilStates.ALL_OFF
        self._closed = False

    @property
    def states(self) -> CoilStates:
        """The last states successfully applied."""
        return self._states

    @property
    def closed(self) -> bool:
        return self._closed

    def apply(self, states: CoilStates) -> None:
        """Drive the coils to ``states``.

        A no-op when nothing changed, so that a repeated heartbeat command does
        not consume relay contact life.

        On ``GpioError`` the write may have been partial, and ``states`` is left
        reporting the last known-good value. Callers must treat that as an
        unknown hardware state and immediately call ``all_off()``, which
        force-writes rather than trusting the cache.
        """
        if self._closed:
            raise GpioError("relay bank is closed")
        if states == self._states:
            return
        self._write(states)
        self._states = states

    def all_off(self) -> None:
        """De-energize every coil. The fail-safe path -- must never raise.

        Both motor terminals return to GND, which brakes rather than coasts.
        Safe to call repeatedly, and safe to call after ``close()``.

        Forces a write to every pin instead of diffing against the cached
        state. After a failed ``apply()`` the cache is stale for precisely the
        pin that is still energized, which is the one case where this matters.
        """
        try:
            self._write(CoilStates.ALL_OFF, force=True)
        except Exception:
            # A failure here means the hardware is already beyond our control.
            # Raising would only mask the original error in a `finally` block.
            pass
        finally:
            self._states = CoilStates.ALL_OFF

    def close(self) -> None:
        """De-energize and release the pins. Idempotent."""
        if self._closed:
            return
        self.all_off()
        self._closed = True
        self._release()

    def __enter__(self) -> RelayBank:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- implemented by backends -------------------------------------------

    @abc.abstractmethod
    def _write(self, states: CoilStates, *, force: bool = False) -> None:
        """Push ``states`` to the hardware.

        Called only when something changed, except from ``all_off()``, which
        passes ``force=True`` to mean "write every pin, do not trust the cache".
        """

    def _release(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Free any OS resources.

        Deliberately concrete and empty: a backend with nothing to release
        should not be forced to write a stub, and forgetting to override it is
        harmless. Contrast ``_write``, where a missing implementation would
        silently do nothing to the relays.
        """
