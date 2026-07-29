"""Relay bank backends.

``lgpio`` is imported only when it is actually selected, so a development
machine without the wheel can still run the tests and the TCP transport.
"""

from __future__ import annotations

from ..config import GpioConfig
from .base import CoilStates, GpioError, RelayBank
from .mock_backend import MockRelayBank, Transition

__all__ = [
    "CoilStates",
    "GpioError",
    "MockRelayBank",
    "RelayBank",
    "Transition",
    "create_relay_bank",
]


def create_relay_bank(config: GpioConfig) -> RelayBank:
    """Build the relay bank named by ``gpio.backend``.

    The returned bank is already de-energized -- see the safe-start invariant in
    ``docs/ARCHITECTURE.md`` section 6.5.
    """
    if config.backend == "mock":
        return MockRelayBank()
    if config.backend == "lgpio":
        from .lgpio_backend import LgpioRelayBank

        return LgpioRelayBank(config)
    # config.py restricts this to the known set, so reaching here means the two
    # lists have drifted apart.
    raise GpioError(f"unknown gpio backend {config.backend!r}")
