"""Poll today's UCAS courses at a fixed interval, reauthenticating each pass."""

from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime, timedelta

from common.api import UCAS_TIMEZONE, UcasClient

from .autosign import AutosignConfig, AutosignConfigError, run_once
from .logging import attach_notify_handler, configure_logging, get_logger
from .notify import Notifier, build_notifier

logger = get_logger(__name__)


def install_signal_handlers() -> None:
    """Make SIGTERM and SIGINT stop the polling loop gracefully."""

    def _raise_keyboard_interrupt(signum: int, frame: object) -> None:
        logger.info("received signal %s, shutting down gracefully", signum)
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _raise_keyboard_interrupt)
        except (ValueError, OSError):
            pass


class Server:
    """Run immediately, then wait between passes; the default wait is 10 minutes."""

    def __init__(
        self, config: AutosignConfig, client: UcasClient, notifier: Notifier | None = None
    ) -> None:
        self.config = config
        self.client = client
        self.notifier = notifier

    def run_forever(self) -> None:
        if self.config.run_on_start:
            self.run_pass()
        while True:
            delay = self.config.poll_minutes * 60
            target = datetime.now(UCAS_TIMEZONE) + timedelta(seconds=delay)
            logger.info(
                "next refresh in %d minutes (%s)",
                self.config.poll_minutes,
                target.isoformat(timespec="seconds"),
            )
            time.sleep(delay)
            self.run_pass()

    def run_pass(self) -> None:
        # The client logs in again for every query/sign request. Transient
        # failures are reported by run_once and retried on the next pass.
        run_once(self.client, self.config)

    def shutdown(self) -> None:
        self.client.close()
        if self.notifier is not None:
            self.notifier.close()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(arg in ("-h", "--help") for arg in args):
        print(_USAGE, end="")
        return 0
    if args:
        print(f"Unknown argument: {args[0]}", file=sys.stderr)
        return 2

    configure_logging()
    try:
        config = AutosignConfig.from_env()
    except AutosignConfigError as exc:
        logger.error("configuration error: %s", exc)
        return 2

    notifier = build_notifier() if os.environ.get("UCAS_FEISHU_WEBHOOK", "").strip() else None
    if notifier is not None:
        attach_notify_handler(notifier)
    server = Server(config, UcasClient(), notifier)
    install_signal_handlers()
    logger.info("server started (timezone=Asia/Shanghai, refresh=%d minutes)", config.poll_minutes)
    try:
        server.run_forever()
    except KeyboardInterrupt:
        logger.info("server exiting gracefully")
    finally:
        server.shutdown()
    return 0


_USAGE = """\
usage: server

Automatically refresh today's courses every 10 minutes and sign in for
unsigned courses inside their sign-in window. Each request logs in again.
Network failures and rejected sign-ins are retried on the next refresh.
Press Ctrl+C to stop.

environment:
  UCAS_USERNAME                 UCAS mail (required)
  UCAS_PASSWORD                 password (required)
  UCAS_POLL_MINUTES             refresh interval: 10, 20, 30, ... (default: 10)
  UCAS_RUN_ON_START             refresh immediately on startup (default: 1)
  UCAS_FEISHU_WEBHOOK           optional Feishu webhook; unset for local logs only
  UCAS_FEISHU_SECRET            optional Feishu signature secret

Example: uv run --env-file .env server
The date and sign-in windows use the UCAS clock in Asia/Shanghai.
Keep the computer awake and connected while this process runs.
"""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
