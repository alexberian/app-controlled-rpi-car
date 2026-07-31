"""Bluetooth Classic SPP transport, via BlueZ's external-profile API.

This is the deployment transport: RFCOMM channel 1, UUID 1101, one client at a
time, speaking the identical NDJSON protocol as ``tcp.py``
(``docs/ARCHITECTURE.md`` section 4).

**BlueZ owns the listening socket, not us.** ``ProfileManager1.RegisterProfile``
with ``Role: server`` makes bluetoothd publish the SDP record *and* bind the
RFCOMM channel; it then hands each accepted connection back as a Unix file
descriptor through ``org.bluez.Profile1.NewConnection``. Binding our own
``AF_BLUETOOTH`` socket on the same channel is therefore not just redundant, it
is actively harmful -- see the note in section 4.1 of ARCHITECTURE.md. All this
module does with the descriptor is wrap it in a stream pair and hand it to the
same ``StreamConnection`` / ``run_session`` machinery ``tcp.py`` uses, so the
framing, buffer bounding, and disconnect ordering are shared rather than
reimplemented.

Access control is bonding: with ``require_authentication`` set, BlueZ refuses
the connection unless the peer has paired. Pairing itself needs a
``NoInputNoOutput`` agent and a pairable adapter, which is deployment setup
rather than the service's job -- ``scripts/setup_bluetooth.sh``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from typing import Any

from dbus_next import BusType, DBusError, Variant
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method

from ..config import SppConfig
from ..protocol import MAX_LINE_BYTES
from .base import Session, StreamConnection, Transport, TransportError, run_session

__all__ = ["SPP_UUID", "SppTransport"]

log = logging.getLogger(__name__)

# The Serial Port Profile UUID. Android's createRfcommSocketToServiceRecord()
# takes this same constant, and the SDP lookup it performs is the only reason
# the profile has to be registered at all.
SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"

_BLUEZ = "org.bluez"
_BLUEZ_ROOT = "/org/bluez"
_PROFILE_MANAGER = "org.bluez.ProfileManager1"
_PROFILE_PATH = "/org/rpicar/spp"

# The service name inside the SDP record. Deliberately not `car.name`: that one
# is the adapter *alias*, which is the identity the app matches a bonded device
# on, and having two independently-editable copies of it is how they end up
# disagreeing. This string is only ever seen by someone reading an SDP browse.
_SERVICE_NAME = "rpi-car control"


def _peer_name(device_path: str) -> str:
    """``/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF`` -> ``AA:BB:CC:DD:EE:FF``."""
    tail = device_path.rsplit("/dev_", 1)[-1]
    return tail.replace("_", ":") if tail != device_path else device_path


class SppTransport(Transport):
    """RFCOMM listener driven by BlueZ profile callbacks, one client at a time."""

    def __init__(self, config: SppConfig) -> None:
        self._config = config
        self._active: StreamConnection | None = None
        self._active_task: asyncio.Task[None] | None = None
        self._session: Session | None = None
        # Completed only on failure. `serve` awaits it, so a BlueZ-side
        # `Release` becomes an exception out of `serve` rather than a service
        # that stays up holding a registration it no longer has.
        self._failed: asyncio.Future[None] | None = None

    @property
    def connected(self) -> bool:
        return self._active is not None

    async def serve(self, session: Session) -> None:
        self._session = session
        self._failed = asyncio.get_running_loop().create_future()

        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        except Exception as exc:
            # No adapter, no bluetoothd, no D-Bus, or no permission on it. All
            # of them are "the link cannot be published", and all of them are
            # worth a restart with backoff rather than a traceback.
            raise TransportError(f"cannot connect to the system bus: {exc}") from exc

        try:
            manager = await self._profile_manager(bus)
            bus.export(_PROFILE_PATH, _Profile(self))
            await self._register(manager)
            log.info(
                "spp transport registered: uuid %s, rfcomm channel %d, authentication %s",
                SPP_UUID,
                self._config.channel,
                "required" if self._config.require_authentication else "NOT required",
            )
            try:
                # BlueZ is already accepting by now; connections arrive as
                # `NewConnection` callbacks on the bus, so there is nothing to
                # poll. Same shape as `tcp.py`'s idle wait, and for the same
                # reason -- this module owns its teardown ordering below.
                await self._failed
            finally:
                await self._disconnect_active("shutting down")
                with contextlib.suppress(Exception):
                    await manager.call_unregister_profile(_PROFILE_PATH)
                bus.unexport(_PROFILE_PATH)
        finally:
            bus.disconnect()

    async def _profile_manager(self, bus: MessageBus) -> Any:
        try:
            introspection = await bus.introspect(_BLUEZ, _BLUEZ_ROOT)
            root = bus.get_proxy_object(_BLUEZ, _BLUEZ_ROOT, introspection)
            return root.get_interface(_PROFILE_MANAGER)
        except Exception as exc:
            raise TransportError(f"bluez is not available on the system bus: {exc}") from exc

    async def _register(self, manager: Any) -> None:
        options = {
            "Name": Variant("s", _SERVICE_NAME),
            "Role": Variant("s", "server"),
            "Channel": Variant("q", self._config.channel),
            # Bonding is the access control on this link; there is no
            # authentication anywhere above it.
            "RequireAuthentication": Variant("b", self._config.require_authentication),
            # Authorization is a per-connection "allow this service?" prompt.
            # The car has no UI to answer it and the agent would auto-accept
            # anyway, so it would only add a round trip to every connect.
            "RequireAuthorization": Variant("b", False),
        }
        try:
            await manager.call_register_profile(_PROFILE_PATH, SPP_UUID, options)
        except DBusError as exc:
            raise TransportError(
                f"cannot register the SPP profile on channel {self._config.channel}: {exc}"
            ) from exc

    async def _new_connection(self, device_path: str, fd: int) -> None:
        """Take ownership of an accepted RFCOMM descriptor from BlueZ.

        The fd belongs to us the moment this is called -- ``dbus-next`` does not
        close received descriptors -- so every path out of here either hands it
        to a socket that will close it or closes it directly.
        """
        peer = _peer_name(device_path)
        sock = socket.socket(fileno=fd)

        # One driver at a time (ARCHITECTURE.md section 4). Refusing with a
        # D-Bus error rather than silently dropping the fd is what tells BlueZ
        # to tear the RFCOMM channel down instead of leaving the second client
        # thinking it is connected.
        if self._active is not None:
            log.warning("refusing %s: %s is already driving", peer, self._active.peer)
            sock.close()
            raise DBusError("org.bluez.Error.Rejected", "another client is already driving")

        try:
            reader, writer = await asyncio.open_connection(sock=sock, limit=MAX_LINE_BYTES)
        except OSError as exc:
            log.warning("could not wrap the rfcomm link from %s: %s", peer, exc)
            sock.close()
            raise DBusError("org.bluez.Error.Failed", f"cannot use the connection: {exc}") from exc

        connection = StreamConnection(reader, writer, peer)
        assert self._session is not None  # serve() sets it before registering
        # Claimed before the task starts, so a second NewConnection racing this
        # one is refused above rather than both being accepted.
        self._active = connection
        self._active_task = asyncio.create_task(
            self._run(connection, self._session), name=f"spp-session-{peer}"
        )

    async def _run(self, connection: StreamConnection, session: Session) -> None:
        try:
            await run_session(connection, session)
        finally:
            self._active = None
            self._active_task = None

    async def _disconnect_active(self, reason: str) -> None:
        """Cancel the live session and wait for it to report the disconnect.

        The wait is the point: ``run_session``'s ``finally`` is what calls
        ``on_disconnect``, which is what stops the car (ARCHITECTURE.md 6.2).
        Returning before it has run would leave the relays energized for as long
        as the caller takes to get around to its own teardown.
        """
        task = self._active_task
        if task is None or task.done():
            return
        log.info("dropping the live link: %s", reason)
        task.cancel()
        await asyncio.wait({task})

    def _release(self) -> None:
        """BlueZ has dropped our registration; the transport is finished."""
        if self._failed is not None and not self._failed.done():
            self._failed.set_exception(
                TransportError("bluez released the SPP profile registration")
            )


class _Profile(ServiceInterface):
    """``org.bluez.Profile1`` -- how BlueZ hands us connections.

    Kept separate from :class:`SppTransport` so that the transport is not also a
    D-Bus object: ``ServiceInterface`` reads the type annotations on every
    method to build the interface, and mixing the two would put the transport's
    own methods on the bus.


    The methods below deliberately carry **no return annotation**, which is the
    one place this file breaks the repo's type-hint convention.
    ``ServiceInterface`` turns annotations into D-Bus signatures, and this module
    uses ``from __future__ import annotations``, so a written-out ``-> None``
    reaches the library as the *string* ``"None"`` and it rejects it ("service
    annotations must be a string constant"). An absent annotation is read as the
    empty signature, which is what these methods actually return.
    """

    def __init__(self, transport: SppTransport) -> None:
        super().__init__("org.bluez.Profile1")
        self._transport = transport

    @method()
    async def NewConnection(self, device: "o", fd: "h", properties: "a{sv}"):
        log.debug("NewConnection from %s (fd %d, %s)", device, fd, dict(properties))
        await self._transport._new_connection(device, fd)

    @method()
    async def RequestDisconnection(self, device: "o"):
        # Awaited rather than fired and forgotten: BlueZ is entitled to close
        # the link as soon as this returns, and the car has to be stopped before
        # that happens.
        await self._transport._disconnect_active(f"bluez requested disconnection of {device}")

    @method()
    def Release(self):
        log.error("bluez released the SPP profile; the link can no longer be published")
        self._transport._release()
