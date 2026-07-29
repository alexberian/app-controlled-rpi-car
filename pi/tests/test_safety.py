"""Every invariant in docs/ARCHITECTURE.md section 6 gets a test here.

If a change makes something in this file fail, the change is wrong. Fix the
change, not the test -- and if the invariant itself needs to move, update
ARCHITECTURE.md in the same commit so the two cannot drift.
"""

from __future__ import annotations

import asyncio
import itertools

import pytest

from conftest import FailingRelayBank, FakeClock, Rig
from rpicar.config import SafetyConfig
from rpicar.drive import DriveController, DriveState, SideState
from rpicar.gpio import CoilStates, MockRelayBank
from rpicar.safety import SafetyGovernor

F, S, R = SideState.FORWARD, SideState.STOP, SideState.REVERSE


# -- 6.5 safe start ---------------------------------------------------------


def test_starts_stopped(rig: Rig) -> None:
    assert rig.applied == DriveState.STOPPED
    assert rig.bank.states == CoilStates.ALL_OFF
    assert not rig.bank.states.any_energized


def test_no_command_never_moves(rig: Rig) -> None:
    """A governor nobody talks to must sit still forever."""
    rig.advance(5.0)
    assert rig.applied == DriveState.STOPPED
    assert rig.bank.actuations == 0


# -- 6.1 command watchdog ---------------------------------------------------


def test_watchdog_stops_when_commands_cease(rig: Rig) -> None:
    rig.hold(1.0, 1.0, seconds=0.3)
    assert rig.applied == DriveState(F, F)

    # Silence. The operator has walked out of range.
    rig.advance(0.49)
    assert rig.applied == DriveState(F, F), "stopped before the watchdog window elapsed"

    rig.advance(0.05)
    assert rig.applied == DriveState.STOPPED
    assert rig.gov.reason.watchdog


def test_watchdog_recovers_when_commands_resume(rig: Rig) -> None:
    rig.hold(1.0, 1.0, seconds=0.3)
    rig.advance(0.8)
    assert rig.applied == DriveState.STOPPED

    rig.hold(1.0, 1.0, seconds=0.3)
    assert rig.applied == DriveState(F, F)
    assert not rig.gov.reason.watchdog


def test_heartbeat_of_unchanged_commands_keeps_car_alive(rig: Rig) -> None:
    """Holding the throttle steady for 3s must not trip the watchdog once."""
    rig.hold(1.0, 1.0, seconds=3.0)
    assert rig.applied == DriveState(F, F)
    # One actuation to start moving, and nothing after.
    assert rig.bank.actuations == 1, rig.bank.format_log()


# -- 6.2 disconnect ---------------------------------------------------------


def test_disconnect_stops_immediately(rig: Rig) -> None:
    rig.hold(1.0, 1.0, seconds=0.3)
    assert rig.applied == DriveState(F, F)

    rig.gov.on_disconnect()
    # Synchronous: stopped by the time the call returns, without a tick.
    assert rig.applied == DriveState.STOPPED
    assert rig.bank.states == CoilStates.ALL_OFF


def test_disconnect_holds_stopped_without_commands(rig: Rig) -> None:
    rig.hold(1.0, 1.0, seconds=0.3)
    rig.gov.on_disconnect()
    rig.advance(2.0)
    assert rig.applied == DriveState.STOPPED


# -- 6.3 reversal dead-time -------------------------------------------------


def test_direct_reversal_passes_through_stop(rig: Rig) -> None:
    rig.hold(1.0, 1.0, seconds=0.3)
    assert rig.applied == DriveState(F, F)

    # Slam both sides into reverse.
    rig.command(-1.0, -1.0)
    assert rig.applied == DriveState.STOPPED, "went straight to reverse"

    # Still held off partway through the dead-time.
    rig.hold(-1.0, -1.0, seconds=0.2)
    assert rig.applied == DriveState.STOPPED

    rig.hold(-1.0, -1.0, seconds=0.1)
    assert rig.applied == DriveState(R, R)


def test_reversal_never_shows_opposite_coils_adjacent(rig: Rig) -> None:
    """The log must never step from a forward coil straight to a reverse coil."""
    rig.hold(1.0, 1.0, seconds=0.3)
    rig.hold(-1.0, -1.0, seconds=0.5)
    rig.hold(1.0, 1.0, seconds=0.5)

    log = rig.bank.state_log
    for before, after in itertools.pairwise(log):
        assert not (before.left_a and after.left_b), rig.bank.format_log()
        assert not (before.left_b and after.left_a), rig.bank.format_log()
        assert not (before.right_a and after.right_b), rig.bank.format_log()
        assert not (before.right_b and after.right_a), rig.bank.format_log()


def test_release_then_opposite_is_still_gated(rig: Rig) -> None:
    """Releasing before reversing must not dodge the dead-time.

    A braked motor is not a stationary motor. Reverse, release, then
    immediately forward is the same contact-welding event as a direct reversal,
    so the gate keys off the last direction the motor was actually turning.
    """
    rig.hold(-1.0, -1.0, seconds=0.3)
    assert rig.applied == DriveState(R, R)

    rig.command(0.0, 0.0)
    assert rig.applied == DriveState.STOPPED

    rig.hold(1.0, 1.0, seconds=0.2)
    assert rig.applied == DriveState.STOPPED, "dodged dead-time by releasing first"

    rig.hold(1.0, 1.0, seconds=0.1)
    assert rig.applied == DriveState(F, F)


def test_same_direction_restart_is_not_dead_time_gated(rig: Rig) -> None:
    """Stop and go the *same* way only waits out the dwell, not the dead-time."""
    rig.hold(1.0, 1.0, seconds=0.3)
    rig.command(0.0, 0.0)
    assert rig.applied == DriveState.STOPPED

    rig.hold(1.0, 1.0, seconds=0.12)  # > 80ms dwell, < 250ms dead-time
    assert rig.applied == DriveState(F, F)


def test_sides_are_gated_independently(rig: Rig) -> None:
    """One side reversing must not hold up the other side."""
    rig.hold(1.0, 0.0, seconds=0.3)
    assert rig.applied == DriveState(F, S)

    # Left reverses (gated), right starts forward from rest (not gated).
    rig.command(-1.0, 1.0)
    assert rig.applied == DriveState(S, F)

    rig.hold(-1.0, 1.0, seconds=0.3)
    assert rig.applied == DriveState(R, F)


# -- 6.4 dwell / chatter ----------------------------------------------------


def test_stopping_is_never_gated(rig: Rig) -> None:
    """A released thumb stops the car now, dwell or no dwell."""
    rig.hold(1.0, 1.0, seconds=0.3)
    rig.command(0.0, 0.0)
    assert rig.applied == DriveState.STOPPED

    # And again immediately after a start, well inside the dwell window.
    rig.hold(1.0, 1.0, seconds=0.2)
    assert rig.applied == DriveState(F, F)
    rig.command(0.0, 0.0)
    assert rig.applied == DriveState.STOPPED


def test_dwell_limits_restart_rate(rig: Rig) -> None:
    rig.hold(1.0, 1.0, seconds=0.3)
    rig.command(0.0, 0.0)
    assert rig.applied == DriveState.STOPPED

    rig.hold(1.0, 1.0, seconds=0.05)  # inside the 80ms dwell
    assert rig.applied == DriveState.STOPPED
    assert rig.gov.reason.dwell

    rig.hold(1.0, 1.0, seconds=0.05)
    assert rig.applied == DriveState(F, F)


def test_chatter_is_bounded(rig: Rig) -> None:
    """A jittery thumb at the command rate must not burn the contact budget.

    Alternating start/stop for two seconds is bounded by the dwell, not by how
    fast the app can send. Without the limiter this would be ~40 actuations.
    """
    for i in range(20):
        rig.hold(1.0 if i % 2 == 0 else 0.0, 0.0, seconds=0.1)

    # 2s of alternating commands, floor of 80ms per start plus its stop.
    assert rig.bank.actuations <= 2 * (2.0 / 0.080), rig.bank.format_log()
    assert rig.bank.actuations < 30, rig.bank.format_log()


# -- 6.6 safe stop ----------------------------------------------------------


def test_run_loop_stops_car_on_cancel() -> None:
    """systemctl stop, Ctrl-C, and a crash all have to leave the car braked."""
    bank = MockRelayBank()
    drive = DriveController(bank)
    gov = SafetyGovernor(
        drive, SafetyConfig(watchdog_ms=500, reversal_dead_time_ms=250, min_dwell_ms=80)
    )

    async def scenario() -> None:
        task = asyncio.create_task(gov.run())
        gov.command(1.0, 1.0)
        # Let the loop tick a few times so the car is genuinely moving.
        for _ in range(5):
            await asyncio.sleep(gov.tick_interval)
        assert gov.applied == DriveState(F, F)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert bank.states == CoilStates.ALL_OFF


# -- hardware faults --------------------------------------------------------


def test_gpio_fault_latches_emergency_stop(clock: FakeClock, safety_config) -> None:
    bank = FailingRelayBank(fail_after=0)
    drive = DriveController(bank)
    gov = SafetyGovernor(drive, safety_config, clock=clock)

    gov.command(1.0, 1.0)
    gov.tick()

    assert gov.applied == DriveState.STOPPED
    assert gov.reason.fault is not None
    assert "gpio" in gov.reason.fault

    # Latched: further commands do not resurrect the car.
    gov.command(1.0, 1.0)
    for _ in range(10):
        clock.advance(gov.tick_interval)
        gov.tick()
    assert gov.applied == DriveState.STOPPED


# -- telemetry surface ------------------------------------------------------


def test_target_and_applied_diverge_while_gated(rig: Rig) -> None:
    """The app renders `applied`, so the two must be separately observable."""
    rig.hold(1.0, 1.0, seconds=0.3)
    rig.command(-1.0, -1.0)

    assert rig.gov.target == DriveState(R, R)
    assert rig.gov.applied == DriveState.STOPPED
    assert rig.gov.reason.dead_time


def test_seq_is_tracked_for_acking(rig: Rig) -> None:
    assert rig.gov.seq is None
    rig.command(0.0, 0.0, seq=7)
    assert rig.gov.seq == 7
