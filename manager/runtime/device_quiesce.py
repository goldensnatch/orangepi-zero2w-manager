"""Stop the workloads that pin a Zero 2W after Pwnagotchi/Bettercap are killed.

Stopping Pwnagotchi does not stop Jellyseerr, Radarr, Ragnar, or qBittorrent.
Entertainment mode will also start those containers again unless current-mode
is switched to safe.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from manager.runtime.media_stack import media_container_names


CURRENT_MODE_REQUEST_PATH = Path("/opt/zero2w-manager/runtime/config/current-mode.json")
RADIO_UNITS = ("pwnagotchi.service", "bettercap.service")
PRINT_UNITS = ("klipper.service", "moonraker.service")
TRANSFER_CONTAINERS = ("rocky-transfer-qbittorrent",)
RAGNAR_PATTERNS = ("Ragnar.py",)
RADIO_APP_IDS = frozenset({"pwnagotchi", "ragnar"})
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass
class QuiesceReport:
    stopped_units: list[str] = field(default_factory=list)
    stopped_containers: list[str] = field(default_factory=list)
    killed_patterns: list[str] = field(default_factory=list)
    mode_written: str | None = None
    actions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def desired_mode_signature(request: dict[str, Any] | None) -> str:
    """Identity for mode reconcile: file fields only, never live docker health."""
    payload = request if isinstance(request, dict) else {}
    return json.dumps(
        {
            "mode_id": str(payload.get("selected_mode_id") or ""),
            "requested_at": str(payload.get("requested_at") or ""),
        },
        sort_keys=True,
    )


def default_runner(
    command: list[str],
    *,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return subprocess.CompletedProcess(command, 124, stdout, stderr or "timeout")


def write_safe_mode(
    path: Path | None = None,
    *,
    reason: str = "device_quiesce",
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = Path(path or CURRENT_MODE_REQUEST_PATH)
    existing = dict(previous or {})
    if not existing and target.is_file():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}
    payload = {
        "version": int(existing.get("version", 1) or 1),
        "selected_mode_id": "safe",
        "previous_mode_id": str(existing.get("selected_mode_id") or "safe"),
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requested_by": "device_quiesce",
        "reason": reason,
        "override_flags": existing.get("override_flags", {})
        if isinstance(existing.get("override_flags"), dict)
        else {},
    }
    target.parent.mkdir(mode=0o775, parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return payload


def _run(
    runner: CommandRunner,
    command: list[str],
    report: QuiesceReport,
    *,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    result = runner(command, timeout=timeout)
    summary = " ".join(command)
    if result.returncode == 0:
        report.actions.append(summary)
    else:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        report.errors.append(f"{summary}: {detail}")
        report.actions.append(f"{summary} (failed)")
    return result


def stop_units(
    units: tuple[str, ...] | list[str],
    *,
    runner: CommandRunner | None = None,
    report: QuiesceReport | None = None,
) -> QuiesceReport:
    active = report or QuiesceReport()
    execute = runner or default_runner
    filtered = [str(unit).strip() for unit in units if str(unit).strip()]
    if not filtered:
        return active
    _run(execute, ["systemctl", "stop", *filtered], active, timeout=45)
    active.stopped_units.extend(filtered)
    return active


def stop_containers(
    names: tuple[str, ...] | list[str],
    *,
    runner: CommandRunner | None = None,
    report: QuiesceReport | None = None,
) -> QuiesceReport:
    active = report or QuiesceReport()
    execute = runner or default_runner
    filtered = [str(name).strip() for name in names if str(name).strip()]
    if not filtered:
        return active
    _run(execute, ["docker", "stop", "--time", "5", *filtered], active, timeout=60)
    active.stopped_containers.extend(filtered)
    return active


def kill_command_patterns(
    patterns: tuple[str, ...] | list[str],
    *,
    runner: CommandRunner | None = None,
    report: QuiesceReport | None = None,
) -> QuiesceReport:
    active = report or QuiesceReport()
    execute = runner or default_runner
    for pattern in patterns:
        token = str(pattern).strip()
        if not token:
            continue
        result = execute(["pkill", "-f", token], timeout=10)
        active.killed_patterns.append(token)
        if result.returncode in {0, 1}:
            active.actions.append(f"pkill -f {token}")
        else:
            detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
            active.errors.append(f"pkill -f {token}: {detail}")
    return active


def stop_radio_workloads(
    *,
    runner: CommandRunner | None = None,
    report: QuiesceReport | None = None,
) -> QuiesceReport:
    active = report or QuiesceReport()
    execute = runner or default_runner
    stop_units(RADIO_UNITS, runner=execute, report=active)
    kill_command_patterns(RAGNAR_PATTERNS, runner=execute, report=active)
    return active


def stop_media_workloads(
    *,
    runner: CommandRunner | None = None,
    report: QuiesceReport | None = None,
) -> QuiesceReport:
    active = report or QuiesceReport()
    execute = runner or default_runner
    stop_containers(media_container_names(), runner=execute, report=active)
    return active


def quiesce_device(
    *,
    write_mode: bool = True,
    stop_print_lab: bool = True,
    stop_transfer: bool = True,
    mode_path: Path | None = None,
    reason: str = "operator_recover",
    runner: CommandRunner | None = None,
) -> QuiesceReport:
    """Stop radio + media + leftover apps and pin Rocky to safe mode."""
    execute = runner or default_runner
    report = QuiesceReport()
    if write_mode:
        payload = write_safe_mode(mode_path, reason=reason)
        report.mode_written = str(payload.get("selected_mode_id") or "safe")
        report.actions.append(f"wrote safe mode to {mode_path or CURRENT_MODE_REQUEST_PATH}")
    stop_radio_workloads(runner=execute, report=report)
    stop_media_workloads(runner=execute, report=report)
    if stop_transfer:
        stop_containers(TRANSFER_CONTAINERS, runner=execute, report=report)
    if stop_print_lab:
        stop_units(PRINT_UNITS, runner=execute, report=report)
    return report


def top_cpu_snapshot(limit: int = 8, *, runner: CommandRunner | None = None) -> str:
    execute = runner or default_runner
    result = execute(["ps", "-eo", "pid,pcpu,pmem,comm,args", "--sort=-pcpu"], timeout=5)
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    if not lines:
        return "ps failed: " + (result.stderr or result.stdout or f"exit {result.returncode}").strip()
    return "\n".join(lines[: max(1, limit) + 1])


def is_radio_app(app_id: str) -> bool:
    return str(app_id or "").strip().lower() in RADIO_APP_IDS


def load_current_mode_request(path: Path | None = None) -> dict[str, Any]:
    target = Path(path or CURRENT_MODE_REQUEST_PATH)
    try:
        if target.is_file():
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        return {}
    return {}
