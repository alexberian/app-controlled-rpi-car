"""Real hardware backend, driving relay coils through ``lgpio``.

``lgpio`` talks to the kernel gpiochip character device, which is the only
interface that still works on Pi OS Bookworm. ``RPi.GPIO`` pokes ``/dev/mem``
and is broken there; ``pigpio`` needs a background daemon and buys us nothing,
because its advantage is hardware PWM and you never PWM a relay.

Only imported when ``gpio.backend = "lgpio"``, so a development machine without
the wheel installed can still run everything else.
"""

from __future__ import annotations

import contextlib
import logging

from ..config import GpioConfig
from .base import CoilStates, GpioError, RelayBank

__all__ = ["LgpioRelayBank"]

log = logging.getLogger(__name__)


class LgpioRelayBank(RelayBank):
    """Drives four BCM pins, one per relay coil."""

    def __init__(self, config: GpioConfig) -> None:
        super().__init__()
        try:
            import lgpio
        except ImportError as exc:  # pragma: no cover - depends on host
            raise GpioError(
                "the 'lgpio' package is required for gpio.backend = 'lgpio'. "
                "Install it with: pip install 'rpicar[hw]'"
            ) from exc

        self._lgpio = lgpio
        self._config = config
        self._pins = config.pins.as_tuple()
        self._active_low = config.active_low

        try:
            self._handle = lgpio.gpiochip_open(config.chip)
        except lgpio.error as exc:
            raise GpioError(f"could not open gpiochip{config.chip}: {exc}") from exc

        # Claim every pin as an output already at the de-energized level. This
        # is the "safe start" invariant (ARCHITECTURE.md 6.5) -- the pins are
        # never briefly driven to the energized level on the way up.
        idle = self._level(energized=False)
        claimed: list[int] = []
        try:
            for pin in self._pins:
                lgpio.gpio_claim_output(self._handle, pin, idle)
                claimed.append(pin)
        except lgpio.error as exc:
            # Partial claim: give back what we took before surfacing the error,
            # otherwise a retry hits "GPIO busy" on the pins that did succeed.
            for pin in claimed:
                with contextlib.suppress(lgpio.error):
                    lgpio.gpio_free(self._handle, pin)
            lgpio.gpiochip_close(self._handle)
            raise GpioError(
                f"could not claim BCM {pin} as an output: {exc}. "
                "Another process may hold it, or the user may lack gpio group access."
            ) from exc

        log.info(
            "lgpio bank ready: chip=%d pins=%s active_low=%s",
            config.chip,
            self._pins,
            self._active_low,
        )

    def _level(self, *, energized: bool) -> int:
        """Pin level for a coil state.

        The inversion is done here in software rather than with lgpio's
        SET_ACTIVE_LOW flag, so that what the code says and what a meter on the
        pin reads are the same thing while debugging.
        """
        return int(energized != self._active_low)

    def _write(self, states: CoilStates, *, force: bool = False) -> None:
        desired = states.as_tuple()
        current = self._states.as_tuple()
        failures: list[str] = []

        for pin, want, have in zip(self._pins, desired, current, strict=True):
            if want == have and not force:
                continue
            try:
                self._lgpio.gpio_write(self._handle, pin, self._level(energized=want))
            except self._lgpio.error as exc:
                if not force:
                    raise GpioError(f"could not write BCM {pin}: {exc}") from exc
                # Fail-safe path: one dead pin must not stop us de-energizing
                # the other three. Try them all, then report.
                failures.append(f"BCM {pin}: {exc}")

        if failures:
            raise GpioError("could not de-energize " + "; ".join(failures))

    def _release(self) -> None:
        for pin in self._pins:
            try:
                self._lgpio.gpio_free(self._handle, pin)
            except self._lgpio.error as exc:
                log.warning("could not free BCM %d: %s", pin, exc)
        try:
            self._lgpio.gpiochip_close(self._handle)
        except self._lgpio.error as exc:
            log.warning("could not close gpiochip: %s", exc)
