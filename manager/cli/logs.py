from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path


DEFAULT_SERVICE = "zero2w-manager.service"
DEFAULT_LINES = 100
DEFAULT_APPS_DIR = Path("/opt/rocky/apps")


def build_journal_command(
    *,
    service: str,
    lines: int,
    follow: bool,
    errors: bool,
    since: str | None,
) -> list[str]:
    command = [
        "journalctl",
        "--unit",
        service,
        "--no-pager",
        "--output",
        "cat",
    ]

    if follow:
        command.append("--follow")
    else:
        command.extend(
            [
                "--lines",
                str(lines),
            ]
        )

    if errors:
        command.extend(
            [
                "--priority",
                "err",
            ]
        )

    if since:
        command.extend(
            [
                "--since",
                since,
            ]
        )

    return command


def read_application_manifest(
    app_id: str,
    apps_dir: Path,
) -> dict | None:
    import json

    manifest_path = apps_dir / app_id / "manifest.json"

    if not manifest_path.is_file():
        return None

    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(manifest, dict):
        return None

    return manifest


def build_application_patterns(
    app_id: str,
    apps_dir: Path,
) -> list[str]:
    patterns = [app_id]

    manifest = read_application_manifest(
        app_id,
        apps_dir,
    )

    if manifest is None:
        return patterns

    name = manifest.get("name")

    if isinstance(name, str) and name.strip():
        patterns.append(name.strip())

    return list(dict.fromkeys(patterns))


def run_journal(
    command: list[str],
) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return 127, "journalctl is not installed"
    except OSError as error:
        return 1, str(error)

    output = result.stdout

    if result.stderr:
        if output and not output.endswith("\n"):
            output += "\n"

        output += result.stderr

    return result.returncode, output


def stream_journal(
    command: list[str],
    patterns: list[str] | None,
) -> int:
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        print("rocky: journalctl is not installed")
        return 127
    except OSError as error:
        print(f"rocky: unable to start journalctl: {error}")
        return 1

    assert process.stdout is not None

    try:
        for line in process.stdout:
            if patterns is None:
                print(line, end="")
                continue

            lowered = line.lower()

            if any(
                pattern.lower() in lowered
                for pattern in patterns
            ):
                print(line, end="")

    except KeyboardInterrupt:
        process.terminate()

        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()

        return 130

    return int(process.wait())


def filter_output(
    output: str,
    patterns: list[str],
) -> str:
    matched_lines: list[str] = []

    for line in output.splitlines():
        lowered = line.lower()

        if any(
            pattern.lower() in lowered
            for pattern in patterns
        ):
            matched_lines.append(line)

    if not matched_lines:
        return ""

    return "\n".join(matched_lines) + "\n"


def configure_parser(
    parser: argparse.ArgumentParser,
) -> None:
    parser.add_argument(
        "application",
        nargs="?",
        help=(
            "Filter log lines by application ID "
            "or application name"
        ),
    )

    parser.add_argument(
        "--last",
        type=int,
        default=DEFAULT_LINES,
        metavar="LINES",
        help=(
            "Number of recent log lines to show "
            f"(default: {DEFAULT_LINES})"
        ),
    )

    parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Follow new runtime log messages",
    )

    parser.add_argument(
        "--errors",
        action="store_true",
        help="Show error-priority journal entries only",
    )

    parser.add_argument(
        "--since",
        help=(
            'Show entries since a journalctl time expression, '
            'such as "30 minutes ago"'
        ),
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

    parser.set_defaults(command_handler=run_command)


def run_command(args: argparse.Namespace) -> int:
    if shutil.which("journalctl") is None:
        print("rocky: journalctl is not installed")
        return 127

    if args.last < 1:
        print("rocky: --last must be at least 1")
        return 2

    command = build_journal_command(
        service=args.service,
        lines=args.last,
        follow=args.follow,
        errors=args.errors,
        since=args.since,
    )

    patterns: list[str] | None = None

    if args.application:
        apps_dir = Path(
            args.apps_directory
        ).resolve()

        patterns = build_application_patterns(
            args.application,
            apps_dir,
        )

    if args.follow:
        return stream_journal(
            command,
            patterns,
        )

    return_code, output = run_journal(command)

    if return_code != 0:
        print(
            output.rstrip()
            or "rocky: journalctl failed"
        )
        return return_code

    if patterns is not None:
        output = filter_output(
            output,
            patterns,
        )

    if output:
        print(output, end="")
    elif args.application:
        print(
            "No matching log entries found for "
            f"{args.application!r}."
        )
    else:
        print("No Rocky Runtime log entries found.")

    return 0
