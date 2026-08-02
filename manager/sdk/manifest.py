from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


PLUGIN_ID_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9_-]*$"
)


class ManifestValidationError(
    RuntimeError
):
    """Raised when a plugin manifest is invalid."""


def load_manifest(
    path: str | Path,
) -> dict[str, Any]:
    manifest_path = Path(path)

    try:
        manifest = json.loads(
            manifest_path.read_text()
        )
    except FileNotFoundError as error:
        raise ManifestValidationError(
            f"Manifest does not exist: "
            f"{manifest_path}"
        ) from error
    except json.JSONDecodeError as error:
        raise ManifestValidationError(
            f"Invalid manifest JSON: {error}"
        ) from error
    except OSError as error:
        raise ManifestValidationError(
            f"Unable to read manifest: {error}"
        ) from error

    return validate_manifest(
        manifest,
        source=str(manifest_path),
    )


def validate_manifest(
    manifest: Any,
    *,
    source: str = "manifest",
) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ManifestValidationError(
            f"{source}: root must be an object"
        )

    normalized = dict(manifest)

    plugin_id = normalized.get("id")

    if not isinstance(plugin_id, str):
        raise ManifestValidationError(
            f"{source}: id must be a string"
        )

    plugin_id = plugin_id.strip()

    if not PLUGIN_ID_PATTERN.fullmatch(
        plugin_id
    ):
        raise ManifestValidationError(
            f"{source}: invalid plugin id "
            f"{plugin_id!r}"
        )

    normalized["id"] = plugin_id

    name = normalized.get("name")

    if not isinstance(name, str) or not name.strip():
        raise ManifestValidationError(
            f"{source}: name is required"
        )

    normalized["name"] = name.strip()

    command = normalized.get("command")

    if not isinstance(command, list) or not command:
        raise ManifestValidationError(
            f"{source}: command must be "
            f"a non-empty list"
        )

    if not all(
        isinstance(part, str)
        and part.strip()
        for part in command
    ):
        raise ManifestValidationError(
            f"{source}: command entries "
            f"must be non-empty strings"
        )

    normalized.setdefault(
        "description",
        "",
    )
    normalized.setdefault(
        "menu_visible",
        True,
    )
    normalized.setdefault(
        "menu_order",
        100,
    )
    normalized.setdefault(
        "display_owner",
        True,
    )
    normalized.setdefault(
        "network_owner",
        False,
    )
    normalized.setdefault(
        "background_allowed",
        False,
    )
    normalized.setdefault(
        "working_directory",
        "${PLUGIN_DIR}",
    )
    normalized.setdefault(
        "environment",
        {},
    )
    normalized.setdefault(
        "type",
        "application",
    )

    return normalized
