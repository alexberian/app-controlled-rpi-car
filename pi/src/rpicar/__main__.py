"""Service entry point: wire the stack together and supervise it.

Construction order is a safety property, not a style choice
(``docs/ARCHITECTURE.md`` 6.5). ``SafetyGovernor.__init__`` de-energizes the
relays, and it runs before ``Transport.serve`` is ever awaited, so the car is
provably stopped before anything can connect to it.

Teardown is the mirror image (6.6). Three independent things drive the coils off
on the way out, because this is the one path where "probably" is not good
enough:

* ``SafetyGovernor.run``'s own ``finally``, on cancellation;
* ``DriveController.close`` in the ``finally`` here, which also releases the pins;
* an ``atexit`` hook on ``all_off``, which is idempotent, never raises, and is
  safe after ``close`` -- it exists for the paths that skip a ``finally``.

Only ``SIGKILL`` and a power cut defeat all three, and both of those already
leave the relays de-energized, which is the same thing as stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import logging
import signal
import sys
from collections.abc import Coroutine, Sequence
from typing import Any

from .config import Config, ConfigError
from .drive import DriveController
from .gpio import GpioError, create_relay_bank
from .safety import SafetyGovernor
from .session import DriveSession
from .telemetry import TelemetryPublisher
from .transport import TransportError, create_transport

__all__ = ["main"]

log = logging.getLogger("rpicar")

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# Exit codes. Distinguished so the systemd unit's restart policy can tell a
# typo in car.toml -- which restarting will never fix -- from a transient
# runtime failure that it should back off and retry.
EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rpicar",
        description="Relay drive service for a Bluetooth-controlled Raspberry Pi car.",
    )
    parser.add_argument(
        "-c",
        "--config",
        metavar="PATH",
        help=(
            "path to car.toml (default: $RPICAR_CONFIG, then /etc/rpicar/car.toml, "
            "then the in-repo copy)"
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=_LOG_LEVELS,
        help="default: INFO. DEBUG logs every frame, which at 10Hz will fill an SD card.",
    )
    return parser.parse_args(argv)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        # The journal adds its own timestamp, but this also gets read from a
        # redirected file during bring-up, where the time is the whole point.
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Ask for a clean shutdown on SIGTERM/SIGINT.

    Setting an event rather than cancelling from the handler keeps the teardown
    on one path: the same code runs whether we were signalled, a task died, or
    the transport gave up. ``systemctl stop`` sends SIGTERM, which is why
    catching ``KeyboardInterrupt`` alone would not be enough.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: _request_stop(stop, s))


def _request_stop(stop: asyncio.Event, sig: signal.Signals) -> None:
    if not stop.is_set():
        log.info("%s received, shutting down", sig.name)
        stop.set()


async def _supervise(*named: tuple[str, Coroutine[Any, Any, None]], stop: asyncio.Event) -> None:
    """Run the service tasks until one finishes, one fails, or ``stop`` is set.

    Any task returning at all is a shutdown: none of them is supposed to. The
    governor is the one whose death is unacceptable -- a dead telemetry loop is
    a car you cannot see, a dead governor is a car that nothing is watching --
    so its exception is re-raised rather than logged and swallowed.
    """
    tasks = [asyncio.create_task(coro, name=name) for name, coro in named]
    waiter = asyncio.create_task(stop.wait(), name="shutdown")
    everything = [*tasks, waiter]
    try:
        done, _ = await asyncio.wait(everything, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in everything:
            task.cancel()
        # `finally` on every task -- including the governor's, which stops the
        # car -- runs during this gather. Nothing below it may be skipped.
        await asyncio.gather(*everything, return_exceptions=True)

    for task in done:
        if task is waiter:
            continue
        # Safe to inspect: these finished before the cancel above.
        exc = task.exception()
        if exc is not None:
            raise exc
        log.warning("task %r returned unexpectedly; shutting down", task.get_name())


async def _serve(config: Config) -> None:
    bank = create_relay_bank(config.gpio)
    # Belt and braces for the paths a `finally` does not cover. `all_off` is
    # idempotent, never raises, and is safe after `close`.
    atexit.register(bank.all_off)
    drive = DriveController(bank)
    try:
        # Safe start (ARCHITECTURE.md 6.5): this drives the coils off, and it
        # happens before the transport below can accept anything.
        governor = SafetyGovernor(drive, config.safety)
        telemetry = TelemetryPublisher(governor, config.telemetry)
        session = DriveSession(governor, telemetry)
        transport = create_transport(config.transport)

        stop = asyncio.Event()
        _install_signal_handlers(stop)

        log.info(
            "%s ready: %s backend, %s transport, %s",
            config.car.name,
            config.gpio.backend,
            config.transport.kind,
            "active-low relays" if config.gpio.active_low else "active-high relays",
        )
        await _supervise(
            ("governor", governor.run()),
            ("telemetry", telemetry.run()),
            ("transport", transport.serve(session)),
            stop=stop,
        )
    finally:
        # De-energize and release the pins. The governor's own `finally` has
        # already stopped the car by now; this is what hands the GPIO back.
        drive.close()
        log.info("stopped, relays de-energized")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.log_level)

    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        # No traceback: a bad config file is a user error, and the message
        # already names the offending key.
        log.error("configuration error: %s", exc)
        return EXIT_CONFIG

    log.info("loaded config from %s", config.source or "<none>")
    try:
        # KeyboardInterrupt is still possible in the window before the signal
        # handlers are installed, and suppressing it there is correct: nothing
        # has been energized yet.
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_serve(config))
    except (GpioError, TransportError) as exc:
        log.error("%s", exc)
        return EXIT_RUNTIME
    except Exception:
        log.exception("unhandled error")
        return EXIT_RUNTIME
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
