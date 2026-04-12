"""``python -m proxialpha_agent`` entry point.

Wires together: settings → public key → supervisor → SIGTERM
handler → ``supervisor.boot()`` → ``supervisor.run()``.

The SIGTERM handler is installed on the running event loop via
``loop.add_signal_handler`` so a kill signal just sets the
supervisor's shutdown event — the loop cleans up naturally on
its next tick rather than having the signal interrupt some
in-flight heartbeat. On Windows (no loop.add_signal_handler) we
fall back to ``signal.signal`` which is slightly racier but
adequate for the supported Windows use case (docker-desktop).
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import NoReturn

from .cli import _configure_logging, _load_public_key_pem
from .license import LicenseError
from .settings import get_settings
from .supervisor import build_supervisor_from_settings

log = logging.getLogger(__name__)


async def run_agent() -> int:
    """Build the supervisor, install signal handlers, run the loop.

    Returns the exit code the caller should pass to ``sys.exit``:
    0 on a clean shutdown, 1 on a fatal license / heartbeat error.
    """
    settings = get_settings()
    _configure_logging(settings.log_level)

    try:
        pem = _load_public_key_pem(settings)
    except FileNotFoundError as exc:
        log.error("no public key: %s", exc)
        return 1

    shutdown_event = asyncio.Event()
    supervisor = build_supervisor_from_settings(
        settings,
        public_key_pem=pem,
        shutdown_event=shutdown_event,
    )

    _install_signal_handlers(shutdown_event)

    try:
        await supervisor.boot()
    except LicenseError as exc:
        log.error(
            "license boot failed: reason=%s message=%s", exc.reason, exc
        )
        return 1
    except Exception as exc:  # pragma: no cover — defensive
        log.exception("unexpected boot error: %s", exc)
        return 1

    try:
        await supervisor.run()
    except Exception as exc:  # pragma: no cover
        log.exception("unexpected run error: %s", exc)
        return 1

    return supervisor.exit_code or 0


def _install_signal_handlers(shutdown_event: asyncio.Event) -> None:
    """Hook SIGTERM and SIGINT into the shutdown event.

    We use the loop's signal handler API on POSIX for clean
    integration with asyncio; on Windows we fall back to the
    stdlib ``signal.signal``. Either way, the handler just sets
    the event — actual cleanup happens on the event loop's next
    iteration.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("signal handlers: no running loop; skipping install")
        return

    def _handler() -> None:
        log.info("signal received; requesting shutdown")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: shutdown_event.set())
        except Exception as exc:
            log.warning("failed to install %s handler: %s", sig, exc)


def main() -> NoReturn:
    """Synchronous entry point used by ``python -m proxialpha_agent``."""
    try:
        exit_code = asyncio.run(run_agent())
    except KeyboardInterrupt:
        exit_code = 0
    sys.exit(exit_code)


if __name__ == "__main__":  # pragma: no cover
    main()
