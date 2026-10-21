"""Command-line entry point for the persistent Minisa server."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import signal
import threading

from runtime.server.application import MinisaApplication
from runtime.server.config import ServerConfig
from runtime.server.http import MinisaHTTPServer


LOGGER = logging.getLogger("minisa.server")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def run(config: ServerConfig) -> None:
    configure_logging(config.log_level)
    application = MinisaApplication(config)
    server = MinisaHTTPServer((config.host, config.port), application)
    shutdown_started = threading.Event()

    def request_shutdown(signum=None, frame=None) -> None:
        del frame
        if shutdown_started.is_set():
            return
        shutdown_started.set()
        LOGGER.info("Shutdown requested", extra={"signal": signum})
        application.stop()
        threading.Thread(target=server.shutdown, name="minisa-shutdown", daemon=True).start()

    previous_handlers = {}
    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[handled_signal] = signal.signal(handled_signal, request_shutdown)

    try:
        application.start()
        LOGGER.info("Minisa started on http://%s:%s", config.host, server.server_port)
        server.serve_forever(poll_interval=0.25)
    finally:
        application.stop()
        server.server_close()
        for handled_signal, previous in previous_handlers.items():
            signal.signal(handled_signal, previous)
        LOGGER.info("Minisa stopped")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the persistent Minisa HTTP server.")
    parser.add_argument("--config", type=Path, help="Path to a TOML configuration file.")
    arguments = parser.parse_args(argv)
    try:
        config = ServerConfig.load(arguments.config)
        run(config)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()

