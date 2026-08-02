from __future__ import annotations

import logging
import signal
import threading
import time
from abc import ABC, abstractmethod
from typing import Any


class RockyApp(ABC):
    """Base class for foreground Rocky applications.

    Applications should implement:
        setup()
        update()
        cleanup()

    The default run loop handles:
        SIGINT
        SIGTERM
        graceful shutdown
        exception logging
        periodic updates
    """

    name = "Rocky Application"
    update_interval = 1.0

    def __init__(self) -> None:
        self.log = logging.getLogger(
            self.__class__.__module__
        )

        self.running = False
        self.stop_reason: str | None = None

        self._stop_event = threading.Event()
        self._signal_handlers_installed = False

    def install_signal_handlers(self) -> None:
        if self._signal_handlers_installed:
            return

        signal.signal(
            signal.SIGINT,
            self._handle_signal,
        )
        signal.signal(
            signal.SIGTERM,
            self._handle_signal,
        )

        self._signal_handlers_installed = True

    def _handle_signal(
        self,
        signum: int,
        _frame: Any,
    ) -> None:
        try:
            signal_name = signal.Signals(
                signum
            ).name
        except ValueError:
            signal_name = str(signum)

        self.log.info(
            "Received %s; shutting down",
            signal_name,
        )

        self.request_stop(
            f"signal:{signal_name}"
        )

    def request_stop(
        self,
        reason: str = "requested",
    ) -> None:
        self.stop_reason = reason
        self.running = False
        self._stop_event.set()

    def wait(
        self,
        seconds: float,
    ) -> bool:
        """Wait while remaining responsive to shutdown.

        Returns True when shutdown was requested.
        """

        return self._stop_event.wait(
            max(0.0, float(seconds))
        )

    def setup(self) -> None:
        """Initialize application resources."""

    @abstractmethod
    def update(self) -> None:
        """Perform one unit of application work."""

    def cleanup(self) -> None:
        """Release application resources."""

    def on_error(
        self,
        error: Exception,
    ) -> None:
        self.log.exception(
            "Application failure: %s",
            error,
        )

    def run(self) -> int:
        self.install_signal_handlers()
        self.running = True

        self.log.info(
            "Starting %s",
            self.name,
        )

        return_code = 0

        try:
            self.setup()

            while self.running:
                started = time.monotonic()

                self.update()

                elapsed = (
                    time.monotonic()
                    - started
                )

                remaining = max(
                    0.0,
                    float(self.update_interval)
                    - elapsed,
                )

                if remaining:
                    self.wait(remaining)

        except KeyboardInterrupt:
            self.request_stop(
                "keyboard-interrupt"
            )

        except Exception as error:
            return_code = 1
            self.on_error(error)

        finally:
            try:
                self.cleanup()
            except Exception:
                return_code = 1
                self.log.exception(
                    "Application cleanup failed"
                )

            self.running = False
            self._stop_event.set()

            self.log.info(
                "%s stopped: %s",
                self.name,
                self.stop_reason
                or "normal exit",
            )

        return return_code
