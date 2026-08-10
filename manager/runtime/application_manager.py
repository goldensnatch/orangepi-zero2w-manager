from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .event_bus import EventBus
from .events import event_bus as default_event_bus


logger = logging.getLogger(__name__)


class ApplicationError(RuntimeError):
    """Base error raised by the application runtime."""


class ApplicationAlreadyRunning(ApplicationError):
    """Raised when a second application launch is attempted."""


class ApplicationLaunchError(ApplicationError):
    """Raised when an application cannot be started."""


class ApplicationManager:
    """Owns external application process lifecycle.

    Responsibilities:
    - normalize service commands
    - launch each app in its own process group
    - track the active process and service
    - persist lightweight runtime state
    - recover from stale state
    - stop apps gracefully and forcefully when necessary
    - notify the launcher when an app exits
    """

    def __init__(
        self,
        *,
        state_file: str | Path = (
            "/opt/zero2w-manager/runtime/"
            "active_application.json"
        ),
        stop_timeout: float = 8.0,
        kill_timeout: float = 3.0,
        poll_interval: float = 0.25,
        on_exit: Optional[
            Callable[[dict[str, Any]], None]
        ] = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.state_file = Path(state_file)
        self.stop_timeout = max(0.5, float(stop_timeout))
        self.kill_timeout = max(0.5, float(kill_timeout))
        self.poll_interval = max(
            0.05,
            float(poll_interval),
        )
        self.on_exit = on_exit
        self.event_bus = (
            event_bus
            if event_bus is not None
            else default_event_bus
        )

        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen] = None
        self._service: Optional[dict[str, Any]] = None
        self._monitor_thread: Optional[
            threading.Thread
        ] = None
        self._stopping = False
        self._generation = 0

        self.state_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.recover_stale_state()

    @property
    def process(self) -> Optional[subprocess.Popen]:
        with self._lock:
            return self._process

    @property
    def service(self) -> Optional[dict[str, Any]]:
        with self._lock:
            if self._service is None:
                return None

            return dict(self._service)

    @property
    def is_running(self) -> bool:
        with self._lock:
            process = self._process

            return (
                process is not None
                and process.poll() is None
            )

    @property
    def pid(self) -> Optional[int]:
        with self._lock:
            process = self._process

            if process is None:
                return None

            if process.poll() is not None:
                return None

            return process.pid

    @property
    def active_service_id(self) -> Optional[str]:
        with self._lock:
            if not self.is_running:
                return None

            if self._service is None:
                return None

            value = self._service.get("id")
            return str(value) if value else None

    def _normalize_command(
        self,
        command: str | Sequence[str],
    ) -> list[str]:
        if isinstance(command, str):
            command = shlex.split(command)

        normalized = [
            str(part)
            for part in command
            if str(part).strip()
        ]

        if not normalized:
            raise ApplicationLaunchError(
                "Application command is empty"
            )

        return normalized

    def _write_state(
        self,
        *,
        process: subprocess.Popen,
        service: dict[str, Any],
        command: Sequence[str],
    ) -> None:
        state = {
            "service_id": service.get("id"),
            "name": service.get("name"),
            "pid": process.pid,
            "pgid": process.pid,
            "command": list(command),
            "started_at": time.time(),
        }

        temporary = self.state_file.with_suffix(
            self.state_file.suffix + ".tmp"
        )

        temporary.write_text(
            json.dumps(
                state,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

        temporary.replace(self.state_file)

    def _remove_state(self) -> None:
        try:
            self.state_file.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning(
                "Unable to remove application state file",
                exc_info=True,
            )

    def _read_state(self) -> Optional[dict[str, Any]]:
        try:
            return json.loads(
                self.state_file.read_text()
            )
        except FileNotFoundError:
            return None
        except Exception:
            logger.warning(
                "Ignoring unreadable application state",
                exc_info=True,
            )
            return None

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        if pid <= 0:
            return False

        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

        return True

    def recover_stale_state(self) -> None:
        """Remove state left behind after a daemon restart.

        This method does not adopt a process that was launched by an
        earlier daemon instance. The current daemon must explicitly
        decide whether to terminate or preserve such a process.
        """

        state = self._read_state()

        if not state:
            return

        try:
            pid = int(state.get("pid", 0))
        except (TypeError, ValueError):
            pid = 0

        if self._pid_exists(pid):
            logger.warning(
                "Found unmanaged application process %s "
                "from previous runtime state",
                pid,
            )
        else:
            logger.info(
                "Removing stale application state for PID %s",
                pid,
            )

        self._remove_state()

    def launch(
        self,
        service: dict[str, Any],
        *,
        env: Optional[dict[str, str]] = None,
    ) -> subprocess.Popen:
        """Launch a configured application."""

        with self._lock:
            if self.is_running:
                raise ApplicationAlreadyRunning(
                    "An application is already running"
                )

            command = service.get("command")
            if command is None:
                raise ApplicationLaunchError(
                    "Service does not define a command"
                )

            argv = self._normalize_command(command)

            cwd_value = (
                service.get("cwd")
                or service.get("working_directory")
            )

            cwd = (
                str(Path(cwd_value))
                if cwd_value
                else None
            )

            if cwd and not Path(cwd).is_dir():
                raise ApplicationLaunchError(
                    f"Application working directory "
                    f"does not exist: {cwd}"
                )

            child_env = os.environ.copy()

            configured_env = (
                service.get("environment")
                or service.get("env")
                or {}
            )
            if isinstance(configured_env, dict):
                child_env.update(
                    {
                        str(key): str(value)
                        for key, value
                        in configured_env.items()
                    }
                )

            if env:
                child_env.update(
                    {
                        str(key): str(value)
                        for key, value in env.items()
                    }
                )

            logger.info(
                "Launching application %s: %s",
                service.get("id")
                or service.get("name")
                or "unknown",
                shlex.join(argv),
            )

            try:
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    env=child_env,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                )
            except Exception as error:
                raise ApplicationLaunchError(
                    f"Unable to launch application: {error}"
                ) from error

            self._process = process
            self._service = dict(service)
            self._stopping = False
            self._generation += 1
            generation = self._generation

            self._write_state(
                process=process,
                service=self._service,
                command=argv,
            )

            self._start_monitor_locked(
                process,
                generation,
            )

            logger.info(
                "Application started with PID %s",
                process.pid,
            )

            return process

    def _start_monitor_locked(
        self,
        process: subprocess.Popen,
        generation: int,
    ) -> None:
        monitor = threading.Thread(
            target=self._monitor_process,
            args=(process, generation),
            name=(
                f"application-monitor-"
                f"{process.pid}"
            ),
            daemon=True,
        )

        self._monitor_thread = monitor
        monitor.start()

    def _monitor_process(
        self,
        process: subprocess.Popen,
        generation: int,
    ) -> None:
        return_code = process.wait()

        with self._lock:
            if generation != self._generation:
                return

            service = (
                dict(self._service)
                if self._service
                else {}
            )

            stopping = self._stopping

            self._process = None
            self._service = None
            self._monitor_thread = None
            self._stopping = False
            self._remove_state()

        event = {
            "service": service,
            "pid": process.pid,
            "return_code": return_code,
            "stopped_by_manager": stopping,
        }

        if stopping:
            logger.info(
                "Application PID %s stopped with code %s",
                process.pid,
                return_code,
            )
        else:
            logger.warning(
                "Application PID %s exited with code %s",
                process.pid,
                return_code,
            )

        callback = self.on_exit

        if callback is not None:
            try:
                callback(event)
            except Exception:
                logger.exception(
                    "Application exit callback failed"
                )

    def _signal_process_group(
        self,
        process: subprocess.Popen,
        sig: signal.Signals,
    ) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except Exception:
            logger.warning(
                "Unable to signal application group %s",
                process.pid,
                exc_info=True,
            )

    def _wait_for_exit(
        self,
        process: subprocess.Popen,
        timeout: float,
    ) -> bool:
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if process.poll() is not None:
                return True

            time.sleep(self.poll_interval)

        return process.poll() is not None

    def stop(
        self,
        *,
        graceful_signal: signal.Signals = (
            signal.SIGTERM
        ),
    ) -> Optional[int]:
        """Stop the active application and its child processes."""

        with self._lock:
            process = self._process

            if process is None:
                self._remove_state()
                return None

            if process.poll() is not None:
                return process.returncode

            self._stopping = True

        logger.info(
            "Stopping application PID %s",
            process.pid,
        )

        self._signal_process_group(
            process,
            graceful_signal,
        )

        if self._wait_for_exit(
            process,
            self.stop_timeout,
        ):
            return process.returncode

        logger.warning(
            "Application PID %s did not stop within %.1fs; "
            "sending SIGKILL",
            process.pid,
            self.stop_timeout,
        )

        self._signal_process_group(
            process,
            signal.SIGKILL,
        )

        self._wait_for_exit(
            process,
            self.kill_timeout,
        )

        return process.poll()

    def terminate_stale_process(
        self,
        pid: int,
        *,
        timeout: float = 3.0,
    ) -> bool:
        """Terminate a process not owned by this runtime instance."""

        if not self._pid_exists(pid):
            return True

        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return True
        except Exception:
            pgid = pid

        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except Exception:
            logger.warning(
                "Unable to terminate stale PID %s",
                pid,
                exc_info=True,
            )
            return False

        deadline = (
            time.monotonic()
            + max(0.5, float(timeout))
        )

        while time.monotonic() < deadline:
            if not self._pid_exists(pid):
                return True

            time.sleep(self.poll_interval)

        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except Exception:
            logger.warning(
                "Unable to kill stale PID %s",
                pid,
                exc_info=True,
            )
            return False

        return not self._pid_exists(pid)

    def _publish_event(
        self,
        name: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.event_bus.publish(
                name,
                data or {},
                source="application-manager",
                asynchronous=True,
            )
        except Exception:
            logger.exception(
                "Unable to publish runtime event %s",
                name,
            )

    def _application_event_data(
        self,
        *,
        return_code: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "service_id": self.active_service_id,
            "pid": self.pid,
        }

        if self.active_service is not None:
            data["name"] = self.active_service.get(
                "name",
                self.active_service_id,
            )

            data["command"] = self.active_service.get(
                "command"
            )

        if return_code is not None:
            data["return_code"] = return_code

        if reason is not None:
            data["reason"] = reason

        return data

    def close(self) -> None:
        self.stop()

    def __enter__(
        self,
    ) -> "ApplicationManager":
        return self

    def __exit__(
        self,
        exc_type,
        exc_value,
        traceback,
    ) -> None:
        self.close()
