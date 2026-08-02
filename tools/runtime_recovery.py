from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import Any


STATE_FILE = Path(
    "/opt/zero2w-manager/runtime/state.json"
)

KNOWN_ABANDONED_PATTERNS = (
    "apps.diagnostic",
    "apps.run_diagnostics_overlay",
    "apps.reader",
    "/opt/ragnar/app/Ragnar.py",
)


def process_exists(pid: int) -> bool:
    if pid <= 1:
        return False

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_process_command(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""

    return raw.replace(b"\0", b" ").decode(
        "utf-8",
        errors="replace",
    ).strip()


def read_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text())

        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass

    return {}


def write_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, indent=2) + "\n"
    )
    temporary.replace(STATE_FILE)


def reset_active_state(
    state: dict[str, Any],
    reason: str,
) -> None:
    state.update(
        {
            "active_mode": "idle",
            "active_pid": None,
            "background_pid": None,
            "overlay_pid": None,
            "status": "stopped",
            "last_error": reason,
        }
    )


def terminate_process(pid: int) -> None:
    if not process_exists(pid):
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + 3

    while process_exists(pid):
        if time.monotonic() >= deadline:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            break

        time.sleep(0.1)


def clean_recorded_process(
    state: dict[str, Any],
) -> None:
    pid = state.get("active_pid")

    if not isinstance(pid, int) or pid <= 1:
        reset_active_state(
            state,
            "Startup recovery cleared empty state",
        )
        return

    if not process_exists(pid):
        reset_active_state(
            state,
            f"Startup recovery cleared stale PID {pid}",
        )
        return

    command = read_process_command(pid)

    # On a new manager startup, an old foreground application
    # should not retain ownership of the display.
    if any(
        pattern in command
        for pattern in KNOWN_ABANDONED_PATTERNS
    ):
        print(
            f"Stopping abandoned application PID {pid}: "
            f"{command}"
        )

        terminate_process(pid)

        reset_active_state(
            state,
            f"Startup recovery stopped abandoned PID {pid}",
        )
        return

    # Do not kill an unknown process merely because its PID is
    # recorded. Clear the state and report it for inspection.
    print(
        f"Recorded PID {pid} exists but is not a known "
        f"managed application: {command}"
    )

    reset_active_state(
        state,
        f"Startup recovery detached unknown PID {pid}",
    )


def main() -> int:
    state = read_state()
    clean_recorded_process(state)
    write_state(state)

    print("Recovered runtime state:")
    print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
