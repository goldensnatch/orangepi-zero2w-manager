from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path


DEFAULT_APPS_DIR = Path("/opt/rocky/apps")
RUNTIME_ROOT = "/opt/zero2w-manager"
PYTHON_BIN = "/opt/zero2w-manager/venv/bin/python3"


def normalize_app_id(value: str) -> str:
    app_id = value.strip().lower()
    app_id = re.sub(r"[^a-z0-9]+", "-", app_id)
    app_id = app_id.strip("-")

    if not app_id:
        raise ValueError("Application ID cannot be empty")

    return app_id


def make_class_name(app_id: str) -> str:
    parts = [part for part in app_id.split("-") if part]
    name = "".join(part.capitalize() for part in parts)

    if not name:
        name = "Rocky"

    if name[0].isdigit():
        name = "Rocky" + name

    return name + "App"


def build_manifest(
    app_id: str,
    name: str,
    description: str,
    order: int,
    hidden: bool,
    buttons: bool,
) -> dict:
    return {
        "id": app_id,
        "name": name,
        "description": description,
        "command": [
            PYTHON_BIN,
            "${PLUGIN_DIR}/main.py",
        ],
        "working_directory": "${PLUGIN_DIR}",
        "environment": {
            "PYTHONPATH": RUNTIME_ROOT,
            "PYTHONUNBUFFERED": "1",
        },
        "menu_order": order,
        "menu_visible": not hidden,
        "configured": True,
        "sdk": {
            "version": 1,
            "buttons": buttons,
        },
    }


def build_button_app(
    app_id: str,
    app_name: str,
    class_name: str,
) -> str:
    return f'''from __future__ import annotations

import logging
import time

from manager.runtime import ButtonEvent
from manager.sdk import RockyButtonApp


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger("{app_id}")


class {class_name}(RockyButtonApp):
    """{app_name} Rocky Runtime application."""

    def setup(self) -> None:
        logger.info("{app_name} started")

    def update(self) -> None:
        time.sleep(0.25)

    def on_button(self, event: ButtonEvent) -> None:
        logger.info(
            "Button event: button=%s action=%s",
            event.button,
            event.action,
        )

        if (
            event.button == "navigate"
            and event.action == "short_press"
        ):
            logger.info("Navigate pressed")

        elif (
            event.button == "select"
            and event.action == "short_press"
        ):
            logger.info("Select pressed")

        elif (
            event.button == "select"
            and event.action == "long_press"
        ):
            logger.info("Home requested")

    def cleanup(self) -> None:
        logger.info("{app_name} stopped")


if __name__ == "__main__":
    {class_name}().run()
'''


def build_standard_app(
    app_id: str,
    app_name: str,
    class_name: str,
) -> str:
    return f'''from __future__ import annotations

import logging
import time

from manager.sdk import RockyApp


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger("{app_id}")


class {class_name}(RockyApp):
    """{app_name} Rocky Runtime application."""

    def setup(self) -> None:
        logger.info("{app_name} started")

    def update(self) -> None:
        time.sleep(0.25)

    def cleanup(self) -> None:
        logger.info("{app_name} stopped")


if __name__ == "__main__":
    {class_name}().run()
'''


def build_readme(
    app_id: str,
    app_name: str,
    description: str,
    app_dir: Path,
    buttons: bool,
) -> str:
    button_section = ""

    if buttons:
        button_section = """
## Button input

This application receives button events through Rocky Runtime.

Supported inputs:

- Navigate short press
- Select short press
- Select long press

Applications must not read LRADC, GPIO, or Linux input devices directly.
"""

    return f"""# {app_name}

{description}

## Application ID

{app_id}

## Application files

- manifest.json: Rocky plugin metadata
- main.py: application entry point
- README.md: application documentation

## Run manually

Change into the application directory:

    cd {app_dir}

Launch it with:

    PYTHONPATH=/opt/zero2w-manager \\
    /opt/zero2w-manager/venv/bin/python3 main.py

## Runtime behavior

Rocky Runtime discovers this application from manifest.json.

When selected from the launcher, Rocky Runtime:

1. Starts main.py.
2. Marks the process as the foreground application.
3. Routes hardware-button events to the application.
4. Monitors the application process.
5. Stops it when the universal Home gesture is used.
6. Restores the launcher after the application exits.

The application does not restore the launcher itself.
{button_section}
"""


def create_application(args: argparse.Namespace) -> Path:
    app_id = normalize_app_id(args.app_id or args.name)
    class_name = make_class_name(app_id)

    apps_dir = Path(args.apps_directory).resolve()
    app_dir = apps_dir / app_id

    if app_dir.exists():
        if not args.force:
            raise FileExistsError(
                f"Application already exists: {app_dir}"
            )

        shutil.rmtree(app_dir)

    app_dir.mkdir(parents=True)

    description = (
        args.description.strip()
        or f"{args.name} Rocky Runtime application"
    )

    buttons = not args.no_buttons

    manifest = build_manifest(
        app_id=app_id,
        name=args.name,
        description=description,
        order=args.order,
        hidden=args.hidden,
        buttons=buttons,
    )

    if buttons:
        main_source = build_button_app(
            app_id,
            args.name,
            class_name,
        )
    else:
        main_source = build_standard_app(
            app_id,
            args.name,
            class_name,
        )

    readme = build_readme(
        app_id,
        args.name,
        description,
        app_dir,
        buttons,
    )

    (app_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    (app_dir / "main.py").write_text(
        main_source,
        encoding="utf-8",
    )

    (app_dir / "README.md").write_text(
        readme,
        encoding="utf-8",
    )

    return app_dir


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name")
    parser.add_argument("--id", dest="app_id")
    parser.add_argument("--description", default="")
    parser.add_argument("--order", type=int, default=500)
    parser.add_argument("--hidden", action="store_true")
    parser.add_argument("--no-buttons", action="store_true")
    parser.add_argument(
        "--apps-directory",
        default=str(DEFAULT_APPS_DIR),
    )
    parser.add_argument("--force", action="store_true")
    parser.set_defaults(command_handler=run_command)


def run_command(args: argparse.Namespace) -> int:
    try:
        app_dir = create_application(args)
    except Exception as error:
        print(f"rocky: {error}")
        return 1

    print("Rocky application created.")
    print(f"Directory: {app_dir}")
    print(
        "Button support: "
        + ("disabled" if args.no_buttons else "enabled")
    )

    return 0
