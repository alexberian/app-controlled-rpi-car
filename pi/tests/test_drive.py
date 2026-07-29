"""Truth table and controller behaviour for the relay H-bridge mapping."""

from __future__ import annotations

import pytest

from conftest import FailingRelayBank
from rpicar.drive import (
    COMMAND_DEADZONE,
    DriveController,
    DriveState,
    SideState,
    to_coils,
)
from rpicar.gpio import CoilStates, GpioError, MockRelayBank

F, S, R = SideState.FORWARD, SideState.STOP, SideState.REVERSE


# -- truth table -----------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (DriveState(S, S), CoilStates()),
        (DriveState(F, S), CoilStates(left_a=True)),
        (DriveState(R, S), CoilStates(left_b=True)),
        (DriveState(S, F), CoilStates(right_a=True)),
        (DriveState(S, R), CoilStates(right_b=True)),
        (DriveState(F, F), CoilStates(left_a=True, right_a=True)),
        (DriveState(R, R), CoilStates(left_b=True, right_b=True)),
        (DriveState(F, R), CoilStates(left_a=True, right_b=True)),
        (DriveState(R, F), CoilStates(left_b=True, right_a=True)),
    ],
)
def test_truth_table(state: DriveState, expected: CoilStates) -> None:
    assert to_coils(state) == expected


def test_a_and_b_never_energized_together() -> None:
    """Both coils on is an electrical brake, but it should be unreachable.

    Keeping it out of the mapping means observing it on real hardware is
    unambiguous evidence of a stuck relay rather than a software state.
    """
    for left in SideState:
        for right in SideState:
            coils = to_coils(DriveState(left, right))
            assert not (coils.left_a and coils.left_b)
            assert not (coils.right_a and coils.right_b)


def test_stopped_energizes_nothing() -> None:
    assert to_coils(DriveState.STOPPED) == CoilStates.ALL_OFF
    assert not to_coils(DriveState.STOPPED).any_energized


# -- quantisation ----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.0, F),
        (0.9, F),
        (-1.0, R),
        (-0.9, R),
        (0.0, S),
        (0.5, S),
        (-0.5, S),
        (0.49, S),
        (-0.49, S),
    ],
)
def test_from_command_quantises(value: float, expected: SideState) -> None:
    assert SideState.from_command(value) == expected


def test_deadzone_boundary_is_exclusive() -> None:
    """Exactly at the deadzone is STOP; a hair beyond it moves."""
    assert SideState.from_command(COMMAND_DEADZONE) is S
    assert SideState.from_command(COMMAND_DEADZONE + 1e-6) is F
    assert SideState.from_command(-COMMAND_DEADZONE) is S
    assert SideState.from_command(-COMMAND_DEADZONE - 1e-6) is R


def test_drive_state_from_command() -> None:
    assert DriveState.from_command(1.0, -1.0) == DriveState(F, R)
    assert DriveState.from_command(0.0, 0.0) == DriveState.STOPPED


# -- opposes ---------------------------------------------------------------


def test_opposes_only_between_opposite_motion() -> None:
    assert F.opposes(R)
    assert R.opposes(F)
    # Nothing involving STOP is a reversal -- the motor is already braked.
    assert not F.opposes(S)
    assert not S.opposes(F)
    assert not S.opposes(S)
    # Same direction is not a reversal.
    assert not F.opposes(F)
    assert not R.opposes(R)


# -- controller ------------------------------------------------------------


def test_controller_applies_and_tracks_state() -> None:
    bank = MockRelayBank()
    drive = DriveController(bank)

    assert drive.state == DriveState.STOPPED
    drive.apply(DriveState(F, R))
    assert drive.state == DriveState(F, R)
    assert bank.states == CoilStates(left_a=True, right_b=True)


def test_repeated_command_does_not_actuate() -> None:
    """The 10Hz heartbeat must not consume relay contact life."""
    bank = MockRelayBank()
    drive = DriveController(bank)

    for _ in range(50):
        drive.apply(DriveState(F, F))

    assert bank.actuations == 1


def test_stop_deenergizes_everything() -> None:
    bank = MockRelayBank()
    drive = DriveController(bank)
    drive.apply(DriveState(F, R))
    drive.stop()

    assert drive.state == DriveState.STOPPED
    assert bank.states == CoilStates.ALL_OFF


def test_gpio_failure_forces_stop_then_raises() -> None:
    """A partial write leaves the hardware unknown, and unknown must collapse to stopped."""
    bank = FailingRelayBank(fail_after=0)
    drive = DriveController(bank)

    with pytest.raises(GpioError):
        drive.apply(DriveState(F, F))

    assert bank.forced_off == 1
    assert drive.state == DriveState.STOPPED


def test_close_stops_and_releases() -> None:
    bank = MockRelayBank()
    drive = DriveController(bank)
    drive.apply(DriveState(F, F))
    drive.close()

    assert bank.states == CoilStates.ALL_OFF
    assert bank.released
    assert drive.state == DriveState.STOPPED
