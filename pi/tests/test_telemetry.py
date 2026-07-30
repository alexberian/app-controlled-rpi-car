"""Telemetry tests -- ``docs/ARCHITECTURE.md`` section 5.2.

Two properties carry the weight here. The first is that what goes out is the
*applied* state and not the commanded one, because that is the difference
between an app that shows the car sitting in a dead-time hold and one that shows
it driving through it. The second is that the immediate-frame rule stays a
*change* rule: `ack` and `up` move on every heartbeat, so a signature that
included either would quietly turn 2Hz telemetry into 10Hz and fill the link
with frames the app already knows about.

Time is faked for the same reason as everywhere else in this suite -- the
interval under test is 500ms and asserting on real elapsed time would only buy
flakiness. The synchronous ``tick`` is what makes that possible; the two async
tests at the bottom only check that ``run`` does the I/O it is supposed to.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from itertools import pairwise
from typing import Any

import pytest

from conftest import FakeClock, FakeConnection, Rig
from rpicar.config import TelemetryConfig
from rpicar.drive import SideState
from rpicar.telemetry import TelemetryPublisher

# Real time only appears in the two ``run`` tests, and only as "long enough for
# a handful of polls at the governor's 20ms tick".
POLLS_S = 0.08


@pytest.fixture
def telemetry_config() -> TelemetryConfig:
    """Matches the shipped config/car.toml: 2Hz, so a 500ms period."""
    return TelemetryConfig(state_hz=2.0)


@pytest.fixture
def pub(rig: Rig, telemetry_config: TelemetryConfig, clock: FakeClock) -> TelemetryPublisher:
    return TelemetryPublisher(rig.gov, telemetry_config, clock=clock)


def sent(publisher: TelemetryPublisher) -> dict[str, Any]:
    """Assert a frame was due and return it decoded."""
    frame = publisher.tick()
    assert frame is not None, "expected a frame"
    assert frame.endswith(b"\n"), "frames must be newline-terminated"
    message = json.loads(frame)
    assert message["t"] == "state"
    return message


def quiet(publisher: TelemetryPublisher) -> None:
    """Assert no frame was due."""
    assert publisher.tick() is None


# --------------------------------------------------------------------------
# connection lifecycle
# --------------------------------------------------------------------------


def test_nothing_is_published_before_a_client_attaches(pub: TelemetryPublisher) -> None:
    quiet(pub)
    assert not pub.connected


def test_attaching_publishes_an_immediate_first_frame(pub: TelemetryPublisher) -> None:
    pub.attach(FakeConnection())
    assert pub.connected
    sent(pub)


def test_detaching_stops_publishing(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    sent(pub)
    pub.detach()
    # Even a change produces nothing: there is nobody to send it to.
    rig.command(1.0, 1.0)
    quiet(pub)
    assert not pub.connected


def test_detach_is_idempotent(pub: TelemetryPublisher) -> None:
    pub.attach(FakeConnection())
    pub.detach()
    pub.detach()
    quiet(pub)


def test_reattaching_publishes_a_fresh_immediate_frame(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    sent(pub)
    pub.detach()
    # A second client must not have to wait out the first one's period to find
    # out what the car is doing.
    pub.attach(FakeConnection())
    sent(pub)


def test_a_gap_with_no_client_does_not_consume_the_next_clients_frame(
    pub: TelemetryPublisher, rig: Rig
) -> None:
    rig.advance(2.0)
    quiet(pub)
    pub.attach(FakeConnection())
    sent(pub)


# --------------------------------------------------------------------------
# applied, not commanded -- ARCHITECTURE.md 5.2
# --------------------------------------------------------------------------


def test_reports_applied_state_not_the_commanded_one(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    sent(pub)
    rig.command(1.0, 1.0)
    assert sent(pub)["l"] == int(SideState.FORWARD)

    # A direct reversal is gated: the governor forces STOP and holds the
    # reversal off. The target is now REVERSE while the relays are at STOP, and
    # STOP is what the app must be told.
    rig.command(-1.0, -1.0)
    assert rig.gov.target.left is SideState.REVERSE
    assert rig.applied.left is SideState.STOP

    message = sent(pub)
    assert (message["l"], message["r"]) == (0, 0)
    assert message["err"] == "dead_time"


def test_side_states_go_on_the_wire_as_plain_ints(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    sent(pub)
    rig.command(-1.0, 1.0)
    message = sent(pub)
    # SideState is an IntEnum; an enum repr here would be a protocol break.
    assert (message["l"], message["r"]) == (-1, 1)
    assert isinstance(message["l"], int)


# --------------------------------------------------------------------------
# periodic rate
# --------------------------------------------------------------------------


def test_no_second_frame_before_the_period_elapses(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    sent(pub)
    rig.advance(0.4)
    quiet(pub)


def test_a_periodic_frame_arrives_after_the_period(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    sent(pub)
    rig.advance(0.6)
    sent(pub)


def test_periodic_frames_land_one_period_apart(
    pub: TelemetryPublisher, rig: Rig, clock: FakeClock
) -> None:
    pub.attach(FakeConnection())
    sent(pub)
    times = []
    # Two seconds of a stationary car with nothing changing.
    for _ in range(100):
        rig.advance(0.02)
        if pub.tick() is not None:
            times.append(clock.now)

    assert len(times) >= 3
    gaps = [later - earlier for earlier, later in pairwise(times)]
    for gap in gaps:
        # Due-ness is only checked once per poll, so a period may overrun by up
        # to one poll but must never come up short. Asserting the interval
        # rather than a frame count is deliberate: an exact count over a fixed
        # window depends on where the poll phase happens to fall, which is not
        # a property worth pinning.
        assert gap >= 0.5 - 1e-9
        assert gap <= 0.5 + pub.poll_interval + 1e-9


def test_a_change_frame_resets_the_periodic_timer(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    sent(pub)
    rig.advance(0.4)
    # A change here goes out immediately...
    rig.command(1.0, 1.0)
    sent(pub)
    # ...and the periodic clock restarts from it rather than from the frame
    # 400ms ago, which would otherwise produce two frames 100ms apart.
    rig.advance(0.2)
    quiet(pub)


# --------------------------------------------------------------------------
# what counts as a change
# --------------------------------------------------------------------------


def test_a_state_change_is_published_immediately(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    sent(pub)
    rig.command(1.0, 1.0)
    # No clock advance at all: the frame is due because the car changed.
    assert sent(pub)["l"] == int(SideState.FORWARD)


def test_a_new_ack_alone_does_not_publish_a_frame(pub: TelemetryPublisher, rig: Rig) -> None:
    rig.command(1.0, 1.0, seq=5)
    pub.attach(FakeConnection())
    assert sent(pub)["ack"] == 5

    # Same command, next sequence number: the heartbeat. If this published, the
    # link would carry state frames at the 10Hz command rate.
    rig.advance(0.1)
    rig.command(1.0, 1.0, seq=6)
    quiet(pub)


def test_uptime_alone_does_not_publish_a_frame(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    sent(pub)
    rig.advance(0.2)
    quiet(pub)


def test_a_repeated_command_is_a_heartbeat_not_an_actuation(
    pub: TelemetryPublisher, rig: Rig
) -> None:
    pub.attach(FakeConnection())
    sent(pub)
    rig.command(1.0, 1.0)
    sent(pub)
    for _ in range(4):
        rig.advance(0.1)
        rig.command(1.0, 1.0)
    # 400ms of heartbeating at an unchanged state: still inside one period.
    quiet(pub)


# --------------------------------------------------------------------------
# ack
# --------------------------------------------------------------------------


def test_ack_is_null_before_any_command(pub: TelemetryPublisher) -> None:
    pub.attach(FakeConnection())
    assert sent(pub)["ack"] is None


def test_ack_carries_the_last_accepted_sequence_number(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    sent(pub)
    rig.command(1.0, 1.0, seq=41)
    assert sent(pub)["ack"] == 41
    rig.command(-1.0, -1.0, seq=42)
    assert sent(pub)["ack"] == 42


def test_ack_is_null_for_an_unnumbered_command(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    sent(pub)
    rig.command(1.0, 1.0)
    assert sent(pub)["ack"] is None


# --------------------------------------------------------------------------
# err -- the gate reason
# --------------------------------------------------------------------------


def test_err_reports_the_watchdog_before_any_command(pub: TelemetryPublisher) -> None:
    # Safe start leaves the watchdog tripped: no command has ever arrived, and
    # the app should be told that rather than shown a healthy-looking stop.
    pub.attach(FakeConnection())
    assert sent(pub)["err"] == "watchdog"


def test_err_clears_once_commands_arrive(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    sent(pub)
    rig.command(1.0, 1.0)
    assert sent(pub)["err"] is None


def test_a_watchdog_stop_is_published_immediately(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    rig.command(1.0, 1.0)
    sent(pub)
    # The operator has walked out of range. This is the frame that matters most:
    # at 2Hz alone it could sit unreported for half a second.
    rig.advance(0.6)
    message = sent(pub)
    assert message["err"] == "watchdog"
    assert (message["l"], message["r"]) == (0, 0)


def test_err_reports_dwell(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    rig.command(1.0, 1.0)
    sent(pub)
    # Released and immediately pushed again, inside the 80ms dwell window.
    rig.command(0.0, 0.0)
    sent(pub)
    rig.command(1.0, 1.0)
    assert sent(pub)["err"] == "dwell"


def test_err_reports_a_latched_fault(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    sent(pub)
    rig.gov.emergency_stop("gpio: simulated write failure")
    message = sent(pub)
    assert message["err"] == "gpio: simulated write failure"
    assert (message["l"], message["r"]) == (0, 0)


def test_a_latched_fault_does_not_flap_back_to_clear(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    rig.gov.emergency_stop("gpio: boom")
    sent(pub)
    # A fault is permanent for the process lifetime; commands must not appear
    # to clear it.
    rig.command(1.0, 1.0)
    rig.advance(1.0)
    assert sent(pub)["err"] == "gpio: boom"


# --------------------------------------------------------------------------
# uptime
# --------------------------------------------------------------------------


def test_uptime_counts_from_construction(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    assert sent(pub)["up"] == 0.0
    rig.advance(2.0)
    assert sent(pub)["up"] == pytest.approx(2.0, abs=0.05)


def test_uptime_survives_a_reconnect(pub: TelemetryPublisher, rig: Rig) -> None:
    pub.attach(FakeConnection())
    sent(pub)
    rig.advance(3.0)
    pub.detach()
    pub.attach(FakeConnection())
    # Uptime is the service's, not the connection's.
    assert sent(pub)["up"] == pytest.approx(3.0, abs=0.05)


# --------------------------------------------------------------------------
# polling
# --------------------------------------------------------------------------


def test_polls_no_slower_than_the_governor_ticks(pub: TelemetryPublisher, rig: Rig) -> None:
    # Nothing can change between governor ticks, so polling faster is waste and
    # polling slower makes a change frame late.
    assert pub.poll_interval <= rig.gov.tick_interval


# --------------------------------------------------------------------------
# the async loop
# --------------------------------------------------------------------------


@asynccontextmanager
async def running(publisher: TelemetryPublisher) -> AsyncIterator[None]:
    """Run the publish loop for long enough to poll a few times, then stop it.

    The fake clock never moves in here, so only the immediate on-attach frame is
    ever due -- which is the point. These two tests are about ``run`` doing the
    I/O and surviving; periodic timing is asserted against the fake clock above,
    where it cannot be flaky.
    """
    task = asyncio.create_task(publisher.run())
    try:
        await asyncio.sleep(POLLS_S)
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_sends_frames_on_the_attached_connection(pub: TelemetryPublisher) -> None:
    connection = FakeConnection()
    pub.attach(connection)
    async with running(pub):
        assert connection.sent
        assert json.loads(connection.sent[0])["t"] == "state"


@pytest.mark.asyncio
async def test_run_keeps_going_when_the_connection_is_dead(pub: TelemetryPublisher) -> None:
    connection = FakeConnection(closed=True)
    pub.attach(connection)
    async with running(pub):
        # A dead link must not take down the task the next client needs.
        assert not connection.sent
