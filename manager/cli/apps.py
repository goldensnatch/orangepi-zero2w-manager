from __future__ import annotations

import argparse
import json
from pathlib import Path


DEFAULT_APPS_DIR = Path("/opt/rocky/apps")


def load_manifest(path: Path) -> tuple[dict | None, str | None]:
    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
    except FileNotFoundError:
        return None, "manifest.json is missing"
    except json.JSONDecodeError as error:
        return None, f"invalid JSON: {error}"
    except OSError as error:
        return None, str(error)

    if not isinstance(data, dict):
        return None, "manifest must contain a JSON object"

    return data, None


def discover_apps(apps_dir: Path) -> list[dict]:
    applications: list[dict] = []

    if not apps_dir.exists():
        return applications

    for app_dir in sorted(apps_dir.iterdir()):
        if not app_dir.is_dir():
            continue

        manifest, error = load_manifest(
            app_dir / "manifest.json"
        )

        if manifest is None:
            applications.append(
                {
                    "id": app_dir.name,
                    "name": app_dir.name,
                    "path": str(app_dir),
                    "valid": False,
                    "error": error,
                    "menu_visible": False,
                    "menu_order": 9999,
                }
            )
            continue

        applications.append(
            {
                "id": manifest.get("id", app_dir.name),
                "name": manifest.get(
                    "name",
                    manifest.get("id", app_dir.name),
                ),
                "description": manifest.get(
                    "description",
                    "",
                ),
                "path": str(app_dir),
                "valid": True,
                "error": None,
                "menu_visible": manifest.get(
                    "menu_visible",
                    True,
                ),
                "menu_order": manifest.get(
                    "menu_order",
                    500,
                ),
            }
        )

    applications.sort(
        key=lambda item: (
            item["menu_order"],
            str(item["name"]).lower(),
        )
    )

    return applications


def configure_parser(
    parser: argparse.ArgumentParser,
) -> None:
    parser.add_argument(
        "--apps-directory",
        default=str(DEFAULT_APPS_DIR),
        help="Rocky applications directory",
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help="Include applications hidden from the launcher",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON",
    )

    parser.set_defaults(command_handler=run_command)


def run_command(args: argparse.Namespace) -> int:
    apps_dir = Path(args.apps_directory).resolve()
    applications = discover_apps(apps_dir)

    if not args.all:
        applications = [
            app
            for app in applications
            if app["menu_visible"] or not app["valid"]
        ]

    if args.json:
        print(
            json.dumps(
                applications,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if not applications:
        print(f"No Rocky applications found in {apps_dir}")
        return 0

    print(
        f"{'ORDER':<7} "
        f"{'ID':<24} "
        f"{'NAME':<28} "
        "STATUS"
    )

    print(
        f"{'-' * 5:<7} "
        f"{'-' * 22:<24} "
        f"{'-' * 26:<28} "
        f"{'-' * 10}"
    )

    for app in applications:
        status = (
            "valid"
            if app["valid"]
            else f"invalid: {app['error']}"
        )

        print(
            f"{str(app['menu_order']):<7} "
            f"{str(app['id']):<24} "
            f"{str(app['name']):<28} "
            f"{status}"
        )

    return 0
