"""SPP transport tests -- ``docs/ARCHITECTURE.md`` sections 4 and 4.1.

Two things are faked here and nothing else: BlueZ, and the radio.

*BlueZ* is faked because the behaviour worth testing is what we ask it for --
the UUID, the channel, and above all whether authentication is required -- and
that is a dictionary we build, not something a real bluetoothd would tell us more
about. The registration options are the security boundary of this transport, so
they get asserted field by field.

*The radio* is not faked at all. ``NewConnection`` hands over a Unix file
descriptor for an already-accepted stream socket, and a ``socketpair`` is exactly
that, so every test below that involves data runs through the real
``StreamConnection``, the real reader limit, and the real ``run_session``
teardown ordering. What a socketpair cannot reproduce is pairing, SDP discovery,
and BlueZ's own channel binding -- those are proven by hand on the Pi, and the
bring-up notes in ``docs/HANDOFF.md`` are the record of it.
"""

from __future__ import annotations

import asyncio
import os
import socket
from contextlib import suppress
from functools import partial
from typing import Any

import pytest
from dbus_next import DBusError

from conftest import RecordingSession, Rig
from rpicar.config import SppConfig, TcpConfig, TelemetryConfig, TransportConfig
from rpicar.drive import DriveState, SideState
from rpicar.gpio import CoilStates
from rpicar.protocol import MAX_LINE_BYTES
from rpicar.session import DriveSession
from rpicar.telemetry import TelemetryPublisher
from rpicar.transport import TransportError, create_transport
from rpicar.transport.spp import SPP_UUID, SppTransport, _peer_name

TIMEOUT_S = 1.0

DEVICE = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
OTHER_DEVICE = "/org/bluez/hci0/dev_11_22_33_44_55_66"


# --------------------------------------------------------------------------
# fake bluez
# --------------------------------------------------------------------------


class FakeProfileManager:
    """``org.bluez.ProfileManager1``, remembering what it was asked to publish."""

    def __init__(self, register_error: Exception | None = None) -> None:
        self.registered: list[tuple[str, str, dict[str, Any]]] = []
        self.unregistered: list[str] = []
        self.register_error = register_error

    async def call_register_profile(self, path: str, uuid: str, options: dict[str, Any]) -> None:
        if self.register_error is not None:
            raise self.register_error
        self.registered.append((path, uuid, options))

    async def call_unregister_profile(self, path: str) -> None:
        self.unregistered.append(path)


class FakeProxyObject:
    def __init__(self, manager: FakeProfileManager) -> None:
        self._manager = manager

    def get_interface(self, name: str) -> FakeProfileManager:
        assert name == "org.bluez.ProfileManager1"
        return self._manager


class FakeBus:
    """Just enough of ``dbus_next.aio.MessageBus`` for ``serve()``."""

    def __init__(
        self,
        manager: FakeProfileManager,
        *,
        introspect_error: Exception | None = None,
    ) -> None:
        self.manager = manager
        self.introspect_error = introspect_error
        self.exported: dict[str, Any] = {}
        self.unexported: list[str] = []
        self.disconnected = False
        self.negotiated_unix_fd: bool | None = None

    async def connect(self) -> FakeBus:
        return self

    async def introspect(self, service: str, path: str) -> object:
        if self.introspect_error is not None:
            raise self.introspect_error
        return object()

    def get_proxy_object(self, service: str, path: str, introspection: object) -> FakeProxyObject:
        return FakeProxyObject(self.manager)

    def export(self, path: str, interface: Any) -> None:
        self.exported[path] = interface

    def unexport(self, path: str) -> None:
        self.unexported.append(path)

    def disconnect(self) -> None:
        self.disconnected = True


def install_fake_bus(monkeypatch: pytest.MonkeyPatch, bus: FakeBus) -> None:
    """Replace ``spp.MessageBus`` with something that yields ``bus``."""

    def factory(**kwargs: Any) -> FakeBus:
        bus.negotiated_unix_fd = kwargs.get("negotiate_unix_fd")
        return bus

    monkeypatch.setattr("rpicar.transport.spp.MessageBus", factory)


def config(channel: int = 1, *, require_authentication: bool = True) -> SppConfig:
    return SppConfig(channel=channel, require_authentication=require_authentication)


def options_of(bus: FakeBus) -> dict[str, Any]:
    """The registration options, with the Variant wrappers unwrapped."""
    assert bus.manager.registered, "the profile was never registered"
    _, _, options = bus.manager.registered[-1]
    return {key: variant.value for key, variant in options.items()}


def profile_of(bus: FakeBus) -> Any:
    """The exported ``org.bluez.Profile1`` object."""
    assert len(bus.exported) == 1
    return next(iter(bus.exported.values()))


def dbus_call(interface: Any, name: str) -> Any:
    """The handler as the message bus invokes it, bound to ``interface``.

    Not a nicety. ``dbus-next``'s ``@method()`` replaces the function with a
    wrapper that calls it and **discards the result**, so ``profile.Release()``
    returns ``None`` and ``await profile.NewConnection(...)`` awaits nothing --
    the coroutine is created and dropped. The bus never calls the wrapper; it
    dispatches through the ``_Method`` record stashed on it, and so does this.
    """
    record = getattr(interface, name).__func__.__dict__["__DBUS_METHOD"]
    return partial(record.fn, interface)


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


class Link:
    """The peer end of a connection BlueZ would have handed us.

    ``take_fd`` detaches a descriptor for the transport to own, which is the
    same ownership transfer ``NewConnection`` performs.
    """

    def __init__(self) -> None:
        self.ours, self.peer = socket.socketpair()
        self.peer.setblocking(False)

    def take_fd(self) -> int:
        return self.ours.detach()

    def close(self) -> None:
        for sock in (self.ours, self.peer):
            with suppress(OSError):
                sock.close()

    async def send(self, data: bytes) -> None:
        await asyncio.get_running_loop().sock_sendall(self.peer, data)

    async def recv(self, size: int = 4096) -> bytes:
        return await asyncio.get_running_loop().sock_recv(self.peer, size)

    @property
    def closed_by_us(self) -> bool:
        """Whether our end of the pair is closed, as the peer can observe it."""
        try:
            return self.peer.recv(1, socket.MSG_PEEK) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True


async def until(predicate: Any) -> None:
    async with asyncio.timeout(TIMEOUT_S):
        while not predicate():
            await asyncio.sleep(0.002)


class Serving:
    """A served transport with a fake bus, torn down on exit."""

    def __init__(self, transport: SppTransport, bus: FakeBus, session: Any) -> None:
        self.transport = transport
        self.bus = bus
        self.session = session
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Serving:
        self.task = asyncio.create_task(self.transport.serve(self.session))
        await until(lambda: bool(self.bus.manager.registered) or self.task.done())
        if self.task.done():
            self.task.result()  # re-raise whatever serve() failed with
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.task is not None
        self.task.cancel()
        with suppress(asyncio.CancelledError, TransportError):
            await self.task

    async def new_connection(self, device: str, fd: int) -> None:
        await dbus_call(profile_of(self.bus), "NewConnection")(device, fd, {})


def serving(
    monkeypatch: pytest.MonkeyPatch,
    session: Any,
    *,
    spp_config: SppConfig | None = None,
    manager: FakeProfileManager | None = None,
) -> Serving:
    bus = FakeBus(manager or FakeProfileManager())
    install_fake_bus(monkeypatch, bus)
    return Serving(SppTransport(spp_config or config()), bus, session)


def drive_session(rig: Rig) -> DriveSession:
    """The real session, so a disconnect assertion is about the real car."""
    return DriveSession(rig.gov, TelemetryPublisher(rig.gov, TelemetryConfig(state_hz=2.0)))


# --------------------------------------------------------------------------
# object paths
# --------------------------------------------------------------------------


def test_peer_name_reads_the_address_out_of_a_device_path() -> None:
    assert _peer_name(DEVICE) == "AA:BB:CC:DD:EE:FF"


def test_peer_name_passes_an_unrecognised_path_through() -> None:
    """A log line is not worth an exception, and BlueZ owns this format."""
    assert _peer_name("/org/bluez/hci0") == "/org/bluez/hci0"


# --------------------------------------------------------------------------
# registration -- what we ask BlueZ to publish
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serve_negotiates_unix_fd_passing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this the fd in ``NewConnection`` arrives as ``None``.

    It is the single easiest way to build an SPP transport that registers
    perfectly and then cannot accept anything.
    """
    async with serving(monkeypatch, RecordingSession()) as s:
        assert s.bus.negotiated_unix_fd is True


@pytest.mark.asyncio
async def test_serve_registers_the_spp_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    async with serving(monkeypatch, RecordingSession()) as s:
        _, uuid, _ = s.bus.manager.registered[0]
        assert uuid == SPP_UUID


@pytest.mark.asyncio
async def test_serve_registers_as_a_server_on_the_configured_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with serving(monkeypatch, RecordingSession(), spp_config=config(channel=7)) as s:
        options = options_of(s.bus)
        assert options["Role"] == "server"
        assert options["Channel"] == 7


@pytest.mark.asyncio
async def test_serve_requires_authentication_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bonding is the only access control on this link."""
    async with serving(monkeypatch, RecordingSession()) as s:
        assert options_of(s.bus)["RequireAuthentication"] is True


@pytest.mark.asyncio
async def test_serve_can_be_told_not_to_require_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bring-up needs this; ``car.toml`` documents what turning it off costs."""
    spp = config(require_authentication=False)
    async with serving(monkeypatch, RecordingSession(), spp_config=spp) as s:
        assert options_of(s.bus)["RequireAuthentication"] is False


@pytest.mark.asyncio
async def test_serve_never_requires_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no UI to answer an authorization prompt with."""
    async with serving(monkeypatch, RecordingSession()) as s:
        assert options_of(s.bus)["RequireAuthorization"] is False


@pytest.mark.asyncio
async def test_serve_exports_a_profile_object_to_receive_connections_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with serving(monkeypatch, RecordingSession()) as s:
        path, _, _ = s.bus.manager.registered[0]
        assert path in s.bus.exported


# --------------------------------------------------------------------------
# registration failures -- all of them are TransportError
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unavailable_bus_is_a_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(**kwargs: Any) -> Any:
        raise OSError("no such file or directory")

    monkeypatch.setattr("rpicar.transport.spp.MessageBus", factory)
    with pytest.raises(TransportError, match="system bus"):
        await SppTransport(config()).serve(RecordingSession())


@pytest.mark.asyncio
async def test_a_missing_bluez_is_a_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus(
        FakeProfileManager(),
        introspect_error=DBusError("org.freedesktop.DBus.Error.ServiceUnknown", "no bluez"),
    )
    install_fake_bus(monkeypatch, bus)
    with pytest.raises(TransportError, match="bluez is not available"):
        await SppTransport(config()).serve(RecordingSession())
    # Still cleaned up: the bus was connected before we found out.
    assert bus.disconnected


@pytest.mark.asyncio
async def test_a_taken_channel_is_a_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit 1, so systemd retries -- something else may be letting go of it."""
    manager = FakeProfileManager(
        register_error=DBusError("org.bluez.Error.AlreadyExists", "channel in use")
    )
    bus = FakeBus(manager)
    install_fake_bus(monkeypatch, bus)
    with pytest.raises(TransportError, match="channel 1"):
        await SppTransport(config()).serve(RecordingSession())
    assert bus.disconnected


@pytest.mark.asyncio
async def test_bluez_releasing_the_profile_ends_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    """A released registration is an unpublishable link, not a quiet idle.

    Returning normally would let the supervisor exit 0 and never be restarted,
    leaving a car that is powered up and permanently unreachable.
    """
    bus = FakeBus(FakeProfileManager())
    install_fake_bus(monkeypatch, bus)
    transport = SppTransport(config())
    task = asyncio.create_task(transport.serve(RecordingSession()))
    await until(lambda: bool(bus.manager.registered))

    dbus_call(profile_of(bus), "Release")()

    with pytest.raises(TransportError, match="released"):
        async with asyncio.timeout(TIMEOUT_S):
            await task


# --------------------------------------------------------------------------
# teardown
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serve_unregisters_the_profile_on_the_way_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise a restart finds its own stale registration on the channel."""
    async with serving(monkeypatch, RecordingSession()) as s:
        path, _, _ = s.bus.manager.registered[0]
    assert s.bus.manager.unregistered == [path]


@pytest.mark.asyncio
async def test_serve_disconnects_the_bus_on_the_way_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with serving(monkeypatch, RecordingSession()) as s:
        pass
    assert s.bus.disconnected
    assert s.bus.unexported == [next(iter(s.bus.exported))]


# --------------------------------------------------------------------------
# an accepted descriptor becomes a session
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_connection_starts_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    session = RecordingSession()
    link = Link()
    try:
        async with serving(monkeypatch, session) as s:
            await s.new_connection(DEVICE, link.take_fd())
            await until(lambda: session.connects != [])
            assert session.connects[0].peer == "AA:BB:CC:DD:EE:FF"
            assert s.transport.connected
    finally:
        link.close()


@pytest.mark.asyncio
async def test_lines_from_the_peer_reach_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    session = RecordingSession()
    link = Link()
    try:
        async with serving(monkeypatch, session) as s:
            await s.new_connection(DEVICE, link.take_fd())
            await until(lambda: session.connects != [])
            await link.send(b'{"t":"drive","l":1,"r":-1,"seq":1}\n')
            await until(lambda: len(session.lines) == 1)
            assert session.lines == [b'{"t":"drive","l":1,"r":-1,"seq":1}\n']
    finally:
        link.close()


@pytest.mark.asyncio
async def test_telemetry_reaches_the_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the descriptor is writable, not just readable."""
    session = RecordingSession()
    link = Link()
    try:
        async with serving(monkeypatch, session) as s:
            await s.new_connection(DEVICE, link.take_fd())
            await until(lambda: session.connects != [])
            await session.connects[0].send(b'{"t":"state"}\n')
            assert await link.recv() == b'{"t":"state"}\n'
    finally:
        link.close()


@pytest.mark.asyncio
async def test_the_read_buffer_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARCHITECTURE.md 5.3, over a real descriptor rather than by inspection.

    The transport passes ``MAX_LINE_BYTES`` as the stream limit; this is what
    proves the value actually took effect on the BlueZ path too.
    """
    session = RecordingSession()
    link = Link()
    try:
        async with serving(monkeypatch, session) as s:
            await s.new_connection(DEVICE, link.take_fd())
            await until(lambda: session.connects != [])
            await link.send(b"x" * (MAX_LINE_BYTES * 4) + b"\n")
            await link.send(b'{"t":"drive","l":0,"r":0,"seq":2}\n')
            await until(lambda: len(session.lines) == 1)
            # Dropped, and the link survived it.
            assert session.lines[0].startswith(b'{"t":"drive"')
            assert session.disconnects == []
    finally:
        link.close()


@pytest.mark.asyncio
async def test_eof_from_the_peer_is_a_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    session = RecordingSession()
    link = Link()
    try:
        async with serving(monkeypatch, session) as s:
            await s.new_connection(DEVICE, link.take_fd())
            await until(lambda: session.connects != [])
            link.peer.close()
            await until(lambda: session.disconnects != [])
            assert session.disconnects == ["closed by peer"]
            # ARCHITECTURE.md 6.2: reported before the socket was torn down.
            assert session.open_at_disconnect == [True]
            await until(lambda: not s.transport.connected)
    finally:
        link.close()


# --------------------------------------------------------------------------
# one client at a time (ARCHITECTURE.md 4)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_client_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    session = RecordingSession()
    first, second = Link(), Link()
    try:
        async with serving(monkeypatch, session) as s:
            await s.new_connection(DEVICE, first.take_fd())
            await until(lambda: session.connects != [])

            with pytest.raises(DBusError) as caught:
                await s.new_connection(OTHER_DEVICE, second.take_fd())
            # A D-Bus error, not a silent drop: BlueZ has to tear the second
            # RFCOMM channel down rather than leave that client believing it is
            # connected to a car that is ignoring it.
            assert caught.value.type == "org.bluez.Error.Rejected"

            # The live session is untouched.
            assert len(session.connects) == 1
            assert session.disconnects == []
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_a_refused_client_has_its_descriptor_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """We own the fd the moment ``NewConnection`` is called.

    ``dbus-next`` closes nothing, so a refusal path that just returns leaks one
    descriptor per rejected connection attempt -- unbounded, on a device with a
    1024 limit and no supervision.
    """
    session = RecordingSession()
    first, second = Link(), Link()
    try:
        async with serving(monkeypatch, session) as s:
            await s.new_connection(DEVICE, first.take_fd())
            await until(lambda: session.connects != [])

            fd = second.take_fd()
            with pytest.raises(DBusError):
                await s.new_connection(OTHER_DEVICE, fd)

            with pytest.raises(OSError):
                os.fstat(fd)
            assert second.closed_by_us
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_the_next_client_is_accepted_after_the_first_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = RecordingSession()
    first, second = Link(), Link()
    try:
        async with serving(monkeypatch, session) as s:
            await s.new_connection(DEVICE, first.take_fd())
            await until(lambda: session.connects != [])
            first.peer.close()
            await until(lambda: not s.transport.connected)

            await s.new_connection(OTHER_DEVICE, second.take_fd())
            await until(lambda: len(session.connects) == 2)
            assert session.connects[1].peer == "11:22:33:44:55:66"
    finally:
        first.close()
        second.close()


# --------------------------------------------------------------------------
# stopping the car (ARCHITECTURE.md 6.2)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_disconnects_the_live_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling ``serve`` must report the disconnect, not just drop the socket."""
    session = RecordingSession()
    link = Link()
    try:
        async with serving(monkeypatch, session) as s:
            await s.new_connection(DEVICE, link.take_fd())
            await until(lambda: session.connects != [])
        assert session.disconnects == ["shutting down"]
        assert session.open_at_disconnect == [True]
    finally:
        link.close()


@pytest.mark.asyncio
async def test_request_disconnection_stops_the_car_before_returning(
    monkeypatch: pytest.MonkeyPatch, rig: Rig
) -> None:
    """BlueZ may close the link the instant this returns.

    So the stop has to have happened by then -- not be scheduled for whenever
    the loop next comes around.
    """
    link = Link()
    try:
        async with serving(monkeypatch, drive_session(rig)) as s:
            await s.new_connection(DEVICE, link.take_fd())
            await until(lambda: s.transport.connected)

            await link.send(b'{"t":"drive","l":1,"r":1,"seq":1}\n')
            await until(lambda: rig.left is SideState.FORWARD)

            await dbus_call(profile_of(s.bus), "RequestDisconnection")(DEVICE)

            # Already stopped, with no further awaiting.
            assert rig.applied == DriveState.STOPPED
            assert rig.drive.coils == CoilStates()
    finally:
        link.close()


@pytest.mark.asyncio
async def test_a_peer_that_vanishes_stops_the_car(
    monkeypatch: pytest.MonkeyPatch, rig: Rig
) -> None:
    link = Link()
    try:
        async with serving(monkeypatch, drive_session(rig)) as s:
            await s.new_connection(DEVICE, link.take_fd())
            await until(lambda: s.transport.connected)
            await link.send(b'{"t":"drive","l":1,"r":1,"seq":1}\n')
            await until(lambda: rig.left is SideState.FORWARD)

            link.peer.close()
            await until(lambda: not s.transport.connected)
            assert rig.applied == DriveState.STOPPED
            assert rig.drive.coils == CoilStates()
    finally:
        link.close()


# --------------------------------------------------------------------------
# factory wiring
# --------------------------------------------------------------------------


def test_create_transport_builds_an_spp_transport() -> None:
    """`kind = "spp"` in car.toml is the whole integration -- nothing above the
    transport changes."""
    transport = create_transport(
        TransportConfig(
            kind="spp",
            tcp=TcpConfig(host="127.0.0.1", port=9999),
            spp=config(channel=3),
        )
    )
    assert isinstance(transport, SppTransport)
    assert not transport.connected
