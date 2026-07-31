"""Shared test rig.

Time is faked everywhere. Safety behaviour is defined in milliseconds, and
asserting on real elapsed time would make these tests flaky on a loaded machine
-- which is the worst possible property for the suite that guards the relays.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from rpicar.config import SafetyConfig
from rpicar.drive import DriveController, DriveState, SideState
from rpicar.gpio import CoilStates, GpioError, MockRelayBank, RelayBank
from rpicar.safety import SafetyGovernor
from rpicar.transport import Connection


class FailingRelayBank(RelayBank):
    """Fails on the Nth write, to exercise the partial-write recovery path.

    Forced writes (the ``all_off`` fail-safe) always succeed, so tests can
    assert that the recovery path actually reached the hardware.
    """

    def __init__(self, fail_after: int = 0) -> None:
        super().__init__()
        self.writes = 0
        self.forced_off = 0
        self.fail_after = fail_after

    def _write(self, states: CoilStates, *, force: bool = False) -> None:
        if force:
            self.forced_off += 1
            return
        self.writes += 1
        if self.writes > self.fail_after:
            raise GpioError("simulated write failure")


class FakeConnection(Connection):
    """A :class:`Connection` that only records what was sent to it.

    Shared by the telemetry and session tests. ``closed=True`` stands in for a
    link that has already gone away, which must silently drop sends rather than
    raise -- the read loop is the authority on disconnection.
    """

    def __init__(self, closed: bool = False) -> None:
        self.sent: list[bytes] = []
        self._closed = closed

    @property
    def peer(self) -> str:
        return "fake"

    @property
    def closed(self) -> bool:
        return self._closed

    async def send(self, data: bytes) -> None:
        if self._closed:
            return
        self.sent.append(data)

    def close(self) -> None:
        self._closed = True


@dataclass
class RecordingSession:
    """A session that only remembers what it was told.

    Shared by the TCP and SPP transport tests: both have to prove the same three
    properties of the link layer, and the two transports differ only in where the
    socket came from.
    """

    lines: list[bytes] = field(default_factory=list)
    connects: list[Connection] = field(default_factory=list)
    disconnects: list[str] = field(default_factory=list)
    # Whether the connection was still open at the moment we were told it had
    # ended. ARCHITECTURE.md 6.2 requires that it is -- the stop has to happen
    # before the teardown, not after it.
    open_at_disconnect: list[bool] = field(default_factory=list)

    def on_connect(self, connection: Connection) -> None:
        self.connects.append(connection)

    def on_line(self, line: bytes) -> None:
        self.lines.append(line)

    def on_disconnect(self, reason: str) -> None:
        self.disconnects.append(reason)
        self.open_at_disconnect.append(not self.connects[-1].closed)


class FakeClock:
    """A monotonic clock that only moves when a test tells it to."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Rig:
    """A governor wired to a mock relay bank, driven by a fake clock."""

    clock: FakeClock
    bank: MockRelayBank
    drive: DriveController
    gov: SafetyGovernor

    def command(self, left: float, right: float, seq: int | None = None) -> None:
        """Send one command frame and let the governor act on it."""
        self.gov.command(left, right, seq)
        self.gov.tick()

    def advance(self, seconds: float) -> None:
        """Run the governor forward, ticking as its own run loop would.

        Stepping at the governor's real tick interval rather than jumping
        straight to the end is what makes dead-time and dwell assertions
        meaningful -- a single giant tick would hide an ordering bug.
        """
        remaining = seconds
        step = self.gov.tick_interval
        while remaining > 1e-9:
            delta = min(step, remaining)
            self.clock.advance(delta)
            self.gov.tick()
            remaining -= delta

    def hold(self, left: float, right: float, seconds: float, hz: float = 10.0) -> None:
        """Hold a command for a duration, heartbeating at the app's rate.

        Sends a frame at both ends of the window, so on return the time since
        the last command is zero. Without the trailing frame a hold would
        silently eat one heartbeat period of the watchdog budget, and every
        watchdog assertion downstream would be off by that much.
        """
        period = 1.0 / hz
        elapsed = 0.0
        self.command(left, right)
        while elapsed < seconds - 1e-9:
            step = min(period, seconds - elapsed)
            self.advance(step)
            elapsed += step
            self.command(left, right)

    @property
    def applied(self) -> DriveState:
        return self.gov.applied

    @property
    def left(self) -> SideState:
        return self.gov.applied.left

    @property
    def right(self) -> SideState:
        return self.gov.applied.right


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def safety_config() -> SafetyConfig:
    """Matches the shipped config/car.toml."""
    return SafetyConfig(watchdog_ms=500, reversal_dead_time_ms=250, min_dwell_ms=80)


@pytest.fixture
def rig(clock: FakeClock, safety_config: SafetyConfig) -> Rig:
    bank = MockRelayBank(clock=clock)
    drive = DriveController(bank)
    gov = SafetyGovernor(drive, safety_config, clock=clock)
    return Rig(clock=clock, bank=bank, drive=drive, gov=gov)
