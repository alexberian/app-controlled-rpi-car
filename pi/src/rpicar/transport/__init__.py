"""Link backends.

``spp`` is imported only when it is actually selected, so a development machine
without ``dbus-next`` -- or without a Bluetooth adapter at all -- can still run
the tests and the TCP transport.
"""

from __future__ import annotations

from ..config import TransportConfig
from .base import (
    Connection,
    Session,
    StreamConnection,
    Transport,
    TransportError,
    run_session,
)
from .tcp import TcpTransport

__all__ = [
    "Connection",
    "Session",
    "StreamConnection",
    "TcpTransport",
    "Transport",
    "TransportError",
    "create_transport",
    "run_session",
]


def create_transport(config: TransportConfig) -> Transport:
    """Build the transport named by ``transport.kind``.

    Nothing is bound or published until ``serve()`` is awaited, which is what
    lets the caller finish safe start first (ARCHITECTURE.md 6.5).
    """
    if config.kind == "tcp":
        return TcpTransport(config.tcp)
    if config.kind == "spp":
        from .spp import SppTransport

        return SppTransport(config.spp)
    # config.py restricts this to the known set, so reaching here means the two
    # lists have drifted apart.
    raise TransportError(f"unknown transport kind {config.kind!r}")
