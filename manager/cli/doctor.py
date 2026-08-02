from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path

from manager.api.runtime_status import DEFAULT_BUTTON_SOCKET


RUNTIME_ROOT = Path("/opt/zero2w-manager")
APPS_DIR = Path("/opt/rocky/apps")
PYTHON_BIN = RUNTIME_ROOT / "venv/bin/python3"
BUTTON_SOCKET = DEFAULT_BUTTON_SOCKET
SYSTEMD_SERVICE = "zero2w-manager.service"


class Doctor:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def passed(self, message: str) -> None:
        print(f"[PASS] {message}")

    def warning(self, message: str) -> None:
        self.warnings += 1
        print(f"[WARN] {message}")

    def failed(self, message: str) -> None:
        self.failures += 1
        print(f"[FAIL] {message}")


def check_path(
    doctor: Doctor,
    path: Path,
    description: str,
    *,
    directory: bool = False,
    executable: bool = False,
) -> None:
    if directory:
        exists = path.is_dir()
    else:
        exists = path.exists()

    if not exists:
        doctor.failed(f"{description} missing: {path}")
        return

    if executable and not os.access(path, os.X_OK):
        doctor.failed(
            f"{description} is not executable: {path}"
        )
        return

    doctor.passed(f"{description}: {path}")


def check_python_imports(doctor: Doctor) -> None:
    try:
        import manager  # noqa: F401
        from manager.cli.main import main  # noqa: F401
    except Exception as error:
        doctor.failed(
            f"Rocky Python imports failed: {error}"
        )
        return

    doctor.passed("Rocky Python packages import successfully")


def check_service(doctor: Doctor) -> None:
    try:
        result = subprocess.run(
            [
                "systemctl",
                "is-active",
                SYSTEMD_SERVICE,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        doctor.warning(
            "systemctl is unavailable; service not checked"
        )
        return
    except subprocess.TimeoutExpired:
        doctor.warning(
            "systemd service check timed out"
        )
        return

    state = result.stdout.strip() or result.stderr.strip()

    if result.returncode == 0 and state == "active":
        doctor.passed(
            f"{SYSTEMD_SERVICE} is active"
        )
    else:
        doctor.failed(
            f"{SYSTEMD_SERVICE} is not active: {state}"
        )


def check_button_socket(doctor: Doctor) -> None:
    if not BUTTON_SOCKET.exists():
        doctor.warning(
            f"button socket is not present: {BUTTON_SOCKET}"
        )
        return

    try:
        mode = BUTTON_SOCKET.stat().st_mode
    except OSError as error:
        doctor.warning(
            f"unable to inspect button socket: {error}"
        )
        return

    if not stat.S_ISSOCK(mode):
        doctor.failed(
            f"button path is not a Unix socket: {BUTTON_SOCKET}"
        )
        return

    doctor.passed(
        f"button socket exists: {BUTTON_SOCKET}"
    )


def configure_parser(
    parser: argparse.ArgumentParser,
) -> None:
    parser.set_defaults(command_handler=run_command)


def run_command(args: argparse.Namespace) -> int:
    del args

    doctor = Doctor()

    print("Rocky Runtime Doctor")
    print("====================")
    print()

    check_path(
        doctor,
        RUNTIME_ROOT,
        "runtime root",
        directory=True,
    )

    check_path(
        doctor,
        RUNTIME_ROOT / "manager",
        "manager package",
        directory=True,
    )

    check_path(
        doctor,
        PYTHON_BIN,
        "runtime Python",
        executable=True,
    )

    check_path(
        doctor,
        APPS_DIR,
        "applications directory",
        directory=True,
    )

    check_python_imports(doctor)
    check_service(doctor)
    check_button_socket(doctor)

    print()
    print(
        f"Result: {doctor.failures} failure(s), "
        f"{doctor.warnings} warning(s)"
    )

    return 1 if doctor.failures else 0
