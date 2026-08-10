from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Iterable

from .config_manager import (
    ConfigurationError,
    ConfigManager,
)


logger = logging.getLogger(__name__)


class PluginError(RuntimeError):
    """Raised when a plugin cannot be loaded."""


class PluginManager:
    """Discovers application manifests from plugin directories."""

    MANIFEST_FILENAME = "manifest.json"

    def __init__(
        self,
        *,
        plugin_directories: Iterable[str | Path] = (
            "/opt/zero2w-manager/apps",
            "/opt/rocky/apps",
        ),
        config_manager: ConfigManager | None = None,
    ) -> None:
        self.plugin_directories = [
            Path(directory)
            for directory in plugin_directories
        ]

        self.config_manager = (
            config_manager or ConfigManager()
        )

        self._plugins: dict[str, dict[str, Any]] = {}
        self._errors: list[dict[str, str]] = []

    @property
    def plugins(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._plugins)

    @property
    def errors(self) -> list[dict[str, str]]:
        return copy.deepcopy(self._errors)

    def _manifest_paths(self) -> list[Path]:
        manifests: list[Path] = []

        for directory in self.plugin_directories:
            if not directory.is_dir():
                continue

            manifests.extend(
                directory.glob(
                    f"*/{self.MANIFEST_FILENAME}"
                )
            )

        return sorted(manifests)

    def _load_manifest(
        self,
        manifest_path: Path,
    ) -> tuple[str, dict[str, Any]]:
        try:
            manifest = json.loads(
                manifest_path.read_text()
            )
        except json.JSONDecodeError as error:
            raise PluginError(
                f"Invalid JSON: {error}"
            ) from error
        except OSError as error:
            raise PluginError(
                f"Unable to read manifest: {error}"
            ) from error

        if not isinstance(manifest, dict):
            raise PluginError(
                "Manifest root must be an object"
            )

        plugin_directory = manifest_path.parent

        plugin_id = (
            manifest.get("id")
            or plugin_directory.name
        )

        if not isinstance(plugin_id, str):
            raise PluginError(
                "Plugin ID must be a string"
            )

        plugin_id = plugin_id.strip()

        if not plugin_id:
            raise PluginError(
                "Plugin ID cannot be empty"
            )

        service = copy.deepcopy(manifest)
        service.pop("id", None)

        command = service.get("command")

        if isinstance(command, list):
            service["command"] = [
                self._expand_plugin_token(
                    part,
                    plugin_directory,
                )
                for part in command
            ]
        elif isinstance(command, str):
            service["command"] = (
                self._expand_plugin_token(
                    command,
                    plugin_directory,
                )
            )

        working_directory = (
            service.get("working_directory")
            or service.get("cwd")
        )

        if working_directory:
            expanded = self._expand_plugin_token(
                str(working_directory),
                plugin_directory,
            )
            service["working_directory"] = expanded
            service["cwd"] = expanded
        else:
            service["working_directory"] = str(
                plugin_directory
            )
            service["cwd"] = str(plugin_directory)

        icon = service.get("icon")

        if isinstance(icon, str):
            service["icon"] = (
                self._expand_plugin_token(
                    icon,
                    plugin_directory,
                )
            )

        service["plugin_directory"] = str(
            plugin_directory
        )
        service["manifest_path"] = str(
            manifest_path
        )
        service["source"] = str(manifest_path)

        try:
            validated = (
                self.config_manager.validate_service(
                    plugin_id,
                    service,
                    source=str(manifest_path),
                )
            )
        except ConfigurationError as error:
            raise PluginError(str(error)) from error

        return plugin_id, validated

    @staticmethod
    def _expand_plugin_token(
        value: str,
        plugin_directory: Path,
    ) -> str:
        return value.replace(
            "${PLUGIN_DIR}",
            str(plugin_directory),
        )

    def discover(
        self,
        *,
        strict: bool = False,
    ) -> dict[str, dict[str, Any]]:
        discovered: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, str]] = []

        for manifest_path in self._manifest_paths():
            try:
                plugin_id, service = (
                    self._load_manifest(
                        manifest_path
                    )
                )

                if plugin_id in discovered:
                    raise PluginError(
                        f"Duplicate plugin ID: {plugin_id}"
                    )

                discovered[plugin_id] = service

                logger.info(
                    "Discovered plugin %s from %s",
                    plugin_id,
                    manifest_path,
                )

            except Exception as error:
                failure = {
                    "manifest": str(manifest_path),
                    "error": str(error),
                }
                errors.append(failure)

                logger.error(
                    "Unable to load plugin manifest %s: %s",
                    manifest_path,
                    error,
                )

                if strict:
                    raise PluginError(
                        f"{manifest_path}: {error}"
                    ) from error

        self._plugins = discovered
        self._errors = errors

        logger.info(
            "Plugin discovery completed: "
            "%d loaded, %d failed",
            len(discovered),
            len(errors),
        )

        return self.plugins

    def get(
        self,
        plugin_id: str,
    ) -> dict[str, Any] | None:
        plugin = self._plugins.get(plugin_id)

        if plugin is None:
            return None

        return copy.deepcopy(plugin)
