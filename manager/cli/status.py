from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from manager.api.runtime_status import (
    DEFAULT_APPS_DIR,
    DEFAULT_BUTTON_SOCKET,
    DEFAULT_SERVICE,
    DEFAULT_STATE_FILE,
    build_runtime_status,
    format_duration,
)


def configure_parser(
    parser: argparse.ArgumentParser,
) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )

    parser.add_argument(
        "--service",
        default=DEFAULT_SERVICE,
        help=(
            "systemd service to inspect "
            f"(default: {DEFAULT_SERVICE})"
        ),
    )

    parser.add_argument(
        "--apps-directory",
        default=str(DEFAULT_APPS_DIR),
        help="Rocky applications directory",
    )

    parser.add_argument(
        "--state-file",
        default=str(DEFAULT_STATE_FILE),
        help="Published runtime state file",
    )

    parser.add_argument(
        "--button-socket",
        default=str(DEFAULT_BUTTON_SOCKET),
        help="Rocky button socket path",
    )

    parser.set_defaults(command_handler=run_command)


def format_boolean(
    value: bool | None,
) -> str:
    if value is True:
        return "yes"

    if value is False:
        return "no"

    return "unknown"


def published_value(
    published: dict[str, Any],
    key: str,
) -> Any:
    state = published.get("state")

    if not isinstance(state, dict):
        return None

    return state.get(key)


def print_human_status(
    status: dict[str, Any],
) -> None:
    observed = status["observed"]
    service = observed["service"]
    system = observed["system"]
    buttons = observed["buttons"]
    applications = observed["applications"]
    published = status["published"]

    overall = (
        "healthy"
        if status["healthy"]
        else "attention required"
    )

    print("Rocky Runtime Status")
    print()
    print(f"Overall:             {overall}")
    print(f"Generated:           {status['generated_at']}")
    print(f"Host:                {system['hostname']}")
    print(
        "System uptime:       "
        f"{format_duration(system['uptime_seconds'])}"
    )
    print()
    print("Runtime Service")
    print(
        f"  Name:              {service['name']}"
    )
    print(
        f"  Active:            "
        f"{format_boolean(service['active'])}"
    )
    print(
        f"  State:             {service['state']}"
    )
    print(
        f"  PID:               "
        f"{service['pid'] or 'unknown'}"
    )
    print(
        "  Process uptime:    "
        f"{format_duration(service['uptime_seconds'])}"
    )
    print()
    print("Published State")
    print(
        f"  Available:         "
        f"{format_boolean(published['available'])}"
    )
    print(
        f"  Path:              {published['path']}"
    )

    foreground = published_value(
        published,
        "foreground_application",
    )

    launcher = published_value(
        published,
        "launcher",
    )

    if foreground is not None:
        print(
            f"  Foreground app:    {foreground}"
        )

    if launcher is not None:
        print(
            f"  Launcher:          {launcher}"
        )

    if published["error"]:
        print(
            f"  Error:             {published['error']}"
        )

    print()
    print("Buttons")
    print(
        f"  Socket exists:     "
        f"{format_boolean(buttons['exists'])}"
    )
    print(
        f"  Valid socket:      "
        f"{format_boolean(buttons['is_socket'])}"
    )
    print(
        f"  Path:              {buttons['path']}"
    )
    print()
    print("Applications")
    print(
        f"  Installed:         {applications['count']}"
    )
    print(
        f"  Valid:             {applications['valid']}"
    )
    print(
        f"  Invalid:           {applications['invalid']}"
    )

    for app in applications["items"]:
        marker = "OK" if app["valid_manifest"] else "INVALID"
        hidden = " [hidden]" if app["hidden"] else ""

        print(
            f"  [{marker}] {app['id']}: "
            f"{app['name']}{hidden}"
        )

        if app["error"]:
            print(
                f"      {app['error']}"
            )

    if status["warnings"]:
        print()
        print("Warnings")

        for warning in status["warnings"]:
            print(f"  - {warning}")


def run_command(args: argparse.Namespace) -> int:
    status = build_runtime_status(
        service=args.service,
        apps_dir=Path(args.apps_directory).resolve(),
        state_file=Path(args.state_file).resolve(),
        button_socket=Path(args.button_socket).resolve(),
    )

    if args.json:
        print(
            json.dumps(
                status,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print_human_status(status)

    return 0 if status["healthy"] else 1
