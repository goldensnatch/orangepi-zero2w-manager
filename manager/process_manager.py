from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

from .state import StateStore


CONFIG_FILE = Path("/opt/zero2w-manager/config/services.json")
LOG_DIRECTORY = Path("/opt/zero2w-manager/runtime/logs")


class ProcessManager:
    def __init__(self) -> None:
        self.state = StateStore()
        self.services = json.loads(CONFIG_FILE.read_text())
        LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)

    def _pid_running(self, pid: int | None) -> bool:
        if not pid:
            return False

        try:
            process = psutil.Process(pid)
            return (
                process.is_running()
                and process.status() != psutil.STATUS_ZOMBIE
            )
        except psutil.Error:
            return False

    def status(self) -> dict[str, Any]:
        state = self.state.load()
        state["running"] = self._pid_running(state.get("active_pid"))
        return state

    def stop(self) -> dict[str, Any]:
        state = self.state.load()
        pid = state.get("active_pid")

        if self._pid_running(pid):
            try:
                os.killpg(pid, signal.SIGTERM)

                deadline = time.monotonic() + 10

                while time.monotonic() < deadline:
                    if not self._pid_running(pid):
                        break
                    time.sleep(0.25)

                if self._pid_running(pid):
                    os.killpg(pid, signal.SIGKILL)

            except ProcessLookupError:
                pass
            except Exception as exc:
                return self.state.update(
                    status="error",
                    last_error=str(exc),
                )

        return self.state.update(
            active_mode="idle",
            active_pid=None,
            status="stopped",
            last_error=None,
        )

    def start(self, mode: str) -> dict[str, Any]:
        if mode not in self.services:
            raise ValueError(f"Unknown mode: {mode}")

        self.stop()

        service = self.services[mode]
        command = service.get("command")

        if not command:
            return self.state.update(
                active_mode=mode,
                active_pid=None,
                status="unconfigured",
                last_error=None,
            )

        environment = os.environ.copy()
        environment.update(service.get("environment", {}))

        log_path = LOG_DIRECTORY / f"{mode}.log"
        log_file = log_path.open("a", buffering=1)

        try:
            process = subprocess.Popen(
                command,
                cwd=service.get("working_directory") or "/",
                env=environment,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            log_file.close()
            raise

        time.sleep(2)

        if process.poll() is not None:
            log_file.close()

            return self.state.update(
                active_mode=mode,
                active_pid=None,
                status="failed",
                last_error=(
                    f"{mode} exited with code {process.returncode}"
                ),
            )

        return self.state.update(
            active_mode=mode,
            active_pid=process.pid,
            status="running",
            last_error=None,
        )
