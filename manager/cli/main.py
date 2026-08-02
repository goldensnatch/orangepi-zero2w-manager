from __future__ import annotations

import argparse
from collections.abc import Sequence

from manager.cli import apps
from manager.cli import doctor
from manager.cli import logs
from manager.cli import status
from manager.cli import new_app
from manager.cli import validate


CLI_VERSION = "0.4.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rocky",
        description=(
            "Rocky Runtime development and "
            "administration tools"
        ),
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"Rocky CLI {CLI_VERSION}",
    )

    commands = parser.add_subparsers(
        dest="command",
        title="commands",
    )

    new_parser = commands.add_parser(
        "new",
        help="Create Rocky resources",
    )

    new_commands = new_parser.add_subparsers(
        dest="new_command",
        title="resources",
    )

    app_parser = new_commands.add_parser(
        "app",
        help="Create a Rocky application",
    )

    new_app.configure_parser(app_parser)

    apps_parser = commands.add_parser(
        "apps",
        help="List installed Rocky applications",
    )

    apps.configure_parser(apps_parser)

    validate_parser = commands.add_parser(
        "validate",
        help="Validate a Rocky application",
    )

    validate.configure_parser(validate_parser)

    doctor_parser = commands.add_parser(
        "doctor",
        help="Check Rocky Runtime health",
    )

    doctor.configure_parser(doctor_parser)

    logs_parser = commands.add_parser(
        "logs",
        help="View Rocky Runtime logs",
    )

    logs.configure_parser(logs_parser)

    status_parser = commands.add_parser(
        "status",
        help="Show Rocky Runtime state",
    )

    status.configure_parser(status_parser)

    return parser


def main(
    argv: Sequence[str] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    command_handler = getattr(
        args,
        "command_handler",
        None,
    )

    if command_handler is None:
        parser.print_help()
        return 0

    return int(command_handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
