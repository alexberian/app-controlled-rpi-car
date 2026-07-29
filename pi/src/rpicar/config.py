"""Load and validate ``car.toml`` into frozen, typed configuration objects.

Nothing else in the codebase reads the TOML file or reasons about defaults. If a
value needs to be tunable, it is added here and to ``config/car.toml`` -- see
``docs/ARCHITECTURE.md`` section 8.

Timings are expressed in milliseconds in the file because that is how a human
thinks about relay dwell, and exposed additionally as seconds because that is
what ``asyncio`` wants.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "CarConfig",
    "Config",
    "ConfigError",
    "ControlConfig",
    "GpioConfig",
    "PinConfig",
    "SafetyConfig",
    "SppConfig",
    "TcpConfig",
    "TelemetryConfig",
    "TransportConfig",
    "default_config_path",
]

# BCM pins exposed on the 40-pin header. 0 and 1 are ID_SD/ID_SC, reserved for
# HAT EEPROM probing at boot, and are excluded on purpose.
_MIN_BCM_PIN = 2
_MAX_BCM_PIN = 27

_BACKENDS = ("mock", "lgpio")
_TRANSPORTS = ("tcp", "spp")


class ConfigError(Exception):
    """Raised when the configuration file is missing, malformed, or invalid."""


# --------------------------------------------------------------------------
# typed extraction helpers
# --------------------------------------------------------------------------


def _key_name(path: str, key: str) -> str:
    """Fully-qualified TOML key, for error messages. Top-level sections have no prefix."""
    return f"{path}.{key}" if path else key


def _require(table: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in table:
        raise ConfigError(f"missing required key '{_key_name(path, key)}'")
    return table[key]


def _sub_table(table: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any]:
    value = _require(table, key, path)
    if not isinstance(value, Mapping):
        raise ConfigError(f"'{_key_name(path, key)}' must be a table, got {type(value).__name__}")
    return value


def _str_value(table: Mapping[str, Any], key: str, path: str, *, choices: tuple[str, ...]) -> str:
    value = _require(table, key, path)
    if not isinstance(value, str):
        raise ConfigError(f"'{_key_name(path, key)}' must be a string, got {type(value).__name__}")
    if value not in choices:
        allowed = ", ".join(repr(c) for c in choices)
        raise ConfigError(f"'{_key_name(path, key)}' must be one of {allowed}, got {value!r}")
    return value


def _free_str(table: Mapping[str, Any], key: str, path: str) -> str:
    value = _require(table, key, path)
    if not isinstance(value, str):
        raise ConfigError(f"'{_key_name(path, key)}' must be a string, got {type(value).__name__}")
    if not value:
        raise ConfigError(f"'{_key_name(path, key)}' must not be empty")
    return value


def _int_value(
    table: Mapping[str, Any],
    key: str,
    path: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = _require(table, key, path)
    # bool is a subclass of int; accepting it here would silently turn `true`
    # into pin 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"'{_key_name(path, key)}' must be an integer, got {type(value).__name__}"
        )
    if minimum is not None and value < minimum:
        raise ConfigError(f"'{_key_name(path, key)}' must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"'{_key_name(path, key)}' must be <= {maximum}, got {value}")
    return value


def _float_value(
    table: Mapping[str, Any],
    key: str,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = _require(table, key, path)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"'{_key_name(path, key)}' must be a number, got {type(value).__name__}")
    value = float(value)
    if minimum is not None and value < minimum:
        raise ConfigError(f"'{_key_name(path, key)}' must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"'{_key_name(path, key)}' must be <= {maximum}, got {value}")
    return value


def _bool_value(table: Mapping[str, Any], key: str, path: str) -> bool:
    value = _require(table, key, path)
    if not isinstance(value, bool):
        raise ConfigError(f"'{_key_name(path, key)}' must be a boolean, got {type(value).__name__}")
    return value


# --------------------------------------------------------------------------
# configuration objects
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CarConfig:
    name: str

    @classmethod
    def _parse(cls, table: Mapping[str, Any]) -> CarConfig:
        return cls(name=_free_str(table, "name", "car"))


@dataclass(frozen=True, slots=True)
class TcpConfig:
    host: str
    port: int

    @classmethod
    def _parse(cls, table: Mapping[str, Any]) -> TcpConfig:
        return cls(
            host=_free_str(table, "host", "transport.tcp"),
            port=_int_value(table, "port", "transport.tcp", minimum=1, maximum=65535),
        )


@dataclass(frozen=True, slots=True)
class SppConfig:
    channel: int

    @classmethod
    def _parse(cls, table: Mapping[str, Any]) -> SppConfig:
        return cls(
            # RFCOMM server channels are 1-30.
            channel=_int_value(table, "channel", "transport.spp", minimum=1, maximum=30),
        )


@dataclass(frozen=True, slots=True)
class TransportConfig:
    kind: str
    tcp: TcpConfig
    spp: SppConfig

    @classmethod
    def _parse(cls, table: Mapping[str, Any]) -> TransportConfig:
        return cls(
            kind=_str_value(table, "kind", "transport", choices=_TRANSPORTS),
            tcp=TcpConfig._parse(_sub_table(table, "tcp", "transport")),
            spp=SppConfig._parse(_sub_table(table, "spp", "transport")),
        )


@dataclass(frozen=True, slots=True)
class PinConfig:
    left_a: int
    left_b: int
    right_a: int
    right_b: int

    @classmethod
    def _parse(cls, table: Mapping[str, Any]) -> PinConfig:
        pins = {
            name: _int_value(table, name, "gpio.pins", minimum=_MIN_BCM_PIN, maximum=_MAX_BCM_PIN)
            for name in ("left_a", "left_b", "right_a", "right_b")
        }
        # Two relay coils sharing a pin would make the truth table in
        # ARCHITECTURE.md 2.1 unreachable, and the failure would look like a
        # wiring fault rather than a config typo.
        seen: dict[int, str] = {}
        for name, pin in pins.items():
            if pin in seen:
                raise ConfigError(
                    f"'gpio.pins.{name}' and 'gpio.pins.{seen[pin]}' are both BCM {pin}; "
                    "each relay coil needs its own pin"
                )
            seen[pin] = name
        return cls(**pins)

    def as_tuple(self) -> tuple[int, int, int, int]:
        """Pins in a stable order: left A, left B, right A, right B."""
        return (self.left_a, self.left_b, self.right_a, self.right_b)


@dataclass(frozen=True, slots=True)
class GpioConfig:
    backend: str
    chip: int
    active_low: bool
    pins: PinConfig

    @classmethod
    def _parse(cls, table: Mapping[str, Any]) -> GpioConfig:
        return cls(
            backend=_str_value(table, "backend", "gpio", choices=_BACKENDS),
            chip=_int_value(table, "chip", "gpio", minimum=0),
            active_low=_bool_value(table, "active_low", "gpio"),
            pins=PinConfig._parse(_sub_table(table, "pins", "gpio")),
        )


@dataclass(frozen=True, slots=True)
class SafetyConfig:
    watchdog_ms: int
    reversal_dead_time_ms: int
    min_dwell_ms: int

    @classmethod
    def _parse(cls, table: Mapping[str, Any]) -> SafetyConfig:
        return cls(
            watchdog_ms=_int_value(table, "watchdog_ms", "safety", minimum=1),
            reversal_dead_time_ms=_int_value(table, "reversal_dead_time_ms", "safety", minimum=0),
            min_dwell_ms=_int_value(table, "min_dwell_ms", "safety", minimum=0),
        )

    @property
    def watchdog_s(self) -> float:
        return self.watchdog_ms / 1000.0

    @property
    def reversal_dead_time_s(self) -> float:
        return self.reversal_dead_time_ms / 1000.0

    @property
    def min_dwell_s(self) -> float:
        return self.min_dwell_ms / 1000.0


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    state_hz: float

    @classmethod
    def _parse(cls, table: Mapping[str, Any]) -> TelemetryConfig:
        return cls(state_hz=_float_value(table, "state_hz", "telemetry", minimum=0.1))

    @property
    def interval_s(self) -> float:
        return 1.0 / self.state_hz


@dataclass(frozen=True, slots=True)
class ControlConfig:
    command_hz: float

    @classmethod
    def _parse(cls, table: Mapping[str, Any]) -> ControlConfig:
        return cls(command_hz=_float_value(table, "command_hz", "control", minimum=0.1))

    @property
    def interval_s(self) -> float:
        return 1.0 / self.command_hz


@dataclass(frozen=True, slots=True)
class Config:
    car: CarConfig
    transport: TransportConfig
    gpio: GpioConfig
    safety: SafetyConfig
    telemetry: TelemetryConfig
    control: ControlConfig
    source: Path | None = None

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> Config:
        """Read and validate a config file.

        Raises ``ConfigError`` for anything wrong with the file. Callers are
        expected to let that propagate at startup rather than fall back to
        defaults -- a car that drives with a half-understood pin map is worse
        than a car that refuses to start.
        """
        resolved = Path(path) if path is not None else default_config_path()
        try:
            raw = resolved.read_bytes()
        except FileNotFoundError as exc:
            raise ConfigError(f"config file not found: {resolved}") from exc
        except OSError as exc:
            raise ConfigError(f"could not read config file {resolved}: {exc}") from exc

        try:
            table = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError(f"invalid TOML in {resolved}: {exc}") from exc

        config = cls.from_table(table, source=resolved)
        return config

    @classmethod
    def from_table(cls, table: Mapping[str, Any], *, source: Path | None = None) -> Config:
        """Validate an already-parsed table. Split out so tests can skip the file."""
        config = cls(
            car=CarConfig._parse(_sub_table(table, "car", "")),
            transport=TransportConfig._parse(_sub_table(table, "transport", "")),
            gpio=GpioConfig._parse(_sub_table(table, "gpio", "")),
            safety=SafetyConfig._parse(_sub_table(table, "safety", "")),
            telemetry=TelemetryConfig._parse(_sub_table(table, "telemetry", "")),
            control=ControlConfig._parse(_sub_table(table, "control", "")),
            source=source,
        )
        config._cross_validate()
        return config

    def _cross_validate(self) -> None:
        """Checks that span sections, where individually-valid values conflict."""
        # The watchdog must tolerate at least one dropped heartbeat, otherwise
        # ordinary packet loss reads as a link failure and the car stutters.
        command_period_ms = 1000.0 / self.control.command_hz
        if self.safety.watchdog_ms < 2 * command_period_ms:
            raise ConfigError(
                f"'safety.watchdog_ms' ({self.safety.watchdog_ms}) is less than two "
                f"command periods ({2 * command_period_ms:.0f}ms at "
                f"{self.control.command_hz}Hz); the watchdog would trip during normal "
                "operation"
            )
        # Dead-time is applied as a STOP that must itself satisfy the dwell
        # limiter; a dead-time shorter than the dwell would be silently extended.
        if 0 < self.safety.reversal_dead_time_ms < self.safety.min_dwell_ms:
            raise ConfigError(
                f"'safety.reversal_dead_time_ms' ({self.safety.reversal_dead_time_ms}) is "
                f"shorter than 'safety.min_dwell_ms' ({self.safety.min_dwell_ms}); the "
                "dwell limiter would silently extend it"
            )


def default_config_path() -> Path:
    """Where to look for ``car.toml`` when no path is given.

    ``RPICAR_CONFIG`` wins, then the system location used by the systemd unit,
    then the in-repo copy so a checkout runs without installation.
    """
    env = os.environ.get("RPICAR_CONFIG")
    if env:
        return Path(env)

    system = Path("/etc/rpicar/car.toml")
    if system.is_file():
        return system

    # src/rpicar/config.py -> src/rpicar -> src -> pi
    return Path(__file__).resolve().parents[2] / "config" / "car.toml"
