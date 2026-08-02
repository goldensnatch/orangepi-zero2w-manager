from __future__ import annotations

import argparse
import json
from pathlib import Path


DEFAULT_APPS_DIR = Path("/opt/rocky/apps")

REQUIRED_FIELDS = (
    "id",
    "name",
    "command",
)


class ValidationResult:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.checks: list[str] = []

    @property
    def valid(self) -> bool:
        return not self.errors

    def pass_check(self, message: str) -> None:
        self.checks.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)


def resolve_app_directory(
    value: str,
    apps_dir: Path,
) -> Path:
    candidate = Path(value)

    if candidate.exists():
        return candidate.resolve()

    return (apps_dir / value).resolve()


def load_manifest(
    manifest_path: Path,
    result: ValidationResult,
) -> dict | None:
    if not manifest_path.is_file():
        result.error("manifest.json is missing")
        return None

    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as error:
        result.error(f"manifest.json contains invalid JSON: {error}")
        return None
    except OSError as error:
        result.error(f"unable to read manifest.json: {error}")
        return None

    if not isinstance(manifest, dict):
        result.error("manifest.json must contain a JSON object")
        return None

    result.pass_check("manifest.json is valid JSON")
    return manifest


def validate_manifest(
    manifest: dict,
    app_dir: Path,
    result: ValidationResult,
) -> None:
    for field in REQUIRED_FIELDS:
        if field not in manifest:
            result.error(
                f"manifest is missing required field: {field}"
            )

    app_id = manifest.get("id")

    if app_id is not None:
        if not isinstance(app_id, str) or not app_id.strip():
            result.error("manifest id must be a non-empty string")
        elif app_id != app_dir.name:
            result.warning(
                "manifest id does not match directory name: "
                f"{app_id!r} != {app_dir.name!r}"
            )
        else:
            result.pass_check(
                "manifest id matches the application directory"
            )

    name = manifest.get("name")

    if name is not None:
        if not isinstance(name, str) or not name.strip():
            result.error(
                "manifest name must be a non-empty string"
            )
        else:
            result.pass_check("manifest name is valid")

    command = manifest.get("command")

    if command is not None:
        if not isinstance(command, list) or not command:
            result.error(
                "manifest command must be a non-empty list"
            )
        elif not all(
            isinstance(item, str) and item
            for item in command
        ):
            result.error(
                "every manifest command item must be a string"
            )
        else:
            result.pass_check("manifest command is valid")

    environment = manifest.get("environment", {})

    if not isinstance(environment, dict):
        result.error("manifest environment must be an object")
    elif not all(
        isinstance(key, str)
        and isinstance(value, str)
        for key, value in environment.items()
    ):
        result.error(
            "manifest environment keys and values "
            "must be strings"
        )
    else:
        result.pass_check("manifest environment is valid")

    menu_order = manifest.get("menu_order", 500)

    if not isinstance(menu_order, int):
        result.error("manifest menu_order must be an integer")

    menu_visible = manifest.get("menu_visible", True)

    if not isinstance(menu_visible, bool):
        result.error("manifest menu_visible must be a boolean")


def validate_entry_point(
    app_dir: Path,
    result: ValidationResult,
) -> None:
    main_path = app_dir / "main.py"

    if not main_path.is_file():
        result.error("main.py is missing")
        return

    result.pass_check("main.py exists")

    try:
        source = main_path.read_text(
            encoding="utf-8",
        )

        compile(
            source,
            str(main_path),
            "exec",
        )
    except SyntaxError as error:
        result.error(
            "main.py contains a syntax error: "
            f"line {error.lineno}: {error.msg}"
        )
        return
    except UnicodeDecodeError as error:
        result.error(
            f"main.py is not valid UTF-8: {error}"
        )
        return
    except OSError as error:
        result.error(
            f"unable to read main.py: {error}"
        )
        return

    result.pass_check(
        "main.py passes Python syntax validation"
    )


def validate_readme(
    app_dir: Path,
    result: ValidationResult,
) -> None:
    readme_path = app_dir / "README.md"

    if readme_path.is_file():
        result.pass_check("README.md exists")
    else:
        result.warning("README.md is missing")


def validate_application(
    app_dir: Path,
) -> ValidationResult:
    result = ValidationResult()

    if not app_dir.exists():
        result.error(
            f"application directory does not exist: {app_dir}"
        )
        return result

    if not app_dir.is_dir():
        result.error(
            f"application path is not a directory: {app_dir}"
        )
        return result

    result.pass_check(
        f"application directory exists: {app_dir}"
    )

    manifest = load_manifest(
        app_dir / "manifest.json",
        result,
    )

    if manifest is not None:
        validate_manifest(
            manifest,
            app_dir,
            result,
        )

    validate_entry_point(app_dir, result)
    validate_readme(app_dir, result)

    return result


def configure_parser(
    parser: argparse.ArgumentParser,
) -> None:
    parser.add_argument(
        "application",
        help="Application ID or application directory",
    )

    parser.add_argument(
        "--apps-directory",
        default=str(DEFAULT_APPS_DIR),
        help="Rocky applications directory",
    )

    parser.set_defaults(command_handler=run_command)


def run_command(args: argparse.Namespace) -> int:
    apps_dir = Path(args.apps_directory).resolve()

    app_dir = resolve_app_directory(
        args.application,
        apps_dir,
    )

    result = validate_application(app_dir)

    print(f"Validating: {app_dir}")
    print()

    for check in result.checks:
        print(f"[PASS] {check}")

    for warning in result.warnings:
        print(f"[WARN] {warning}")

    for error in result.errors:
        print(f"[FAIL] {error}")

    print()

    if result.valid:
        print(
            "Validation passed "
            f"with {len(result.warnings)} warning(s)."
        )
        return 0

    print(
        "Validation failed "
        f"with {len(result.errors)} error(s)."
    )
    return 1
