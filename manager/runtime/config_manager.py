from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Iterable


logger = logging.getLogger(__name__)


class ConfigurationError(RuntimeError):
    """Raised when runtime configuration is invalid."""


class ConfigManager:
    """Loads and validates Rocky application configuration."""

    REQUIRED_APPLICATION_FIELDS = (
        "name",
        "command",
    )

    def __init__(
        self,
        *,
        services_file: str | Path = (
            "/opt/zero2w-manager/config/services.json"
        ),
    ) -> None:
        self.services_file = Path(services_file)
        self._services: dict[str, dict[str, Any]] = {}

    @property
    def services(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._services)

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.services_file.is_file():
            raise ConfigurationError(
                f"Services file does not exist: "
                f"{self.services_file}"
            )

        try:
            raw = json.loads(
                self.services_file.read_text()
            )
        except json.JSONDecodeError as error:
            raise ConfigurationError(
                f"Invalid JSON in {self.services_file}: "
                f"{error}"
            ) from error
        except OSError as error:
            raise ConfigurationError(
                f"Unable to read {self.services_file}: "
                f"{error}"
            ) from error

        if not isinstance(raw, dict):
            raise ConfigurationError(
                "Top-level services configuration "
                "must be an object"
            )

        services: dict[str, dict[str, Any]] = {}

        for service_id, service in raw.items():
            normalized = self.validate_service(
                service_id,
                service,
                source=str(self.services_file),
            )
            services[service_id] = normalized

        self._services = services

        logger.info(
            "Loaded %d configured services from %s",
            len(services),
            self.services_file,
        )

        return self.services

    def validate_service(
        self,
        service_id: str,
        service: Any,
        *,
        source: str = "configuration",
    ) -> dict[str, Any]:
        if not isinstance(service_id, str):
            raise ConfigurationError(
                f"Service ID from {source} must be a string"
            )

        service_id = service_id.strip()

        if not service_id:
            raise ConfigurationError(
                f"Service ID from {source} cannot be empty"
            )

        if not isinstance(service, dict):
            raise ConfigurationError(
                f"Service {service_id!r} from {source} "
                f"must be an object"
            )

        normalized = copy.deepcopy(service)
        normalized["id"] = service_id

        name = normalized.get("name")

        if not isinstance(name, str) or not name.strip():
            raise ConfigurationError(
                f"Service {service_id!r} has no valid name"
            )

        normalized["name"] = name.strip()

        command = normalized.get("command")

        if command is not None:
            if isinstance(command, str):
                if not command.strip():
                    raise ConfigurationError(
                        f"Service {service_id!r} has "
                        f"an empty command"
                    )
            elif isinstance(command, list):
                if not command:
                    raise ConfigurationError(
                        f"Service {service_id!r} has "
                        f"an empty command list"
                    )

                if not all(
                    isinstance(part, str) and part
                    for part in command
                ):
                    raise ConfigurationError(
                        f"Service {service_id!r} command "
                        f"must contain only strings"
                    )
            else:
                raise ConfigurationError(
                    f"Service {service_id!r} command "
                    f"must be a string, list, or null"
                )

        working_directory = (
            normalized.get("working_directory")
            or normalized.get("cwd")
        )

        if working_directory is not None:
            if not isinstance(working_directory, str):
                raise ConfigurationError(
                    f"Service {service_id!r} working "
                    f"directory must be a string"
                )

            normalized["working_directory"] = (
                working_directory
            )
            normalized["cwd"] = working_directory

        environment = (
            normalized.get("environment")
            or normalized.get("env")
            or {}
        )

        if not isinstance(environment, dict):
            raise ConfigurationError(
                f"Service {service_id!r} environment "
                f"must be an object"
            )

        normalized_environment = {
            str(key): str(value)
            for key, value in environment.items()
        }

        normalized["environment"] = (
            normalized_environment
        )
        normalized["env"] = normalized_environment

        for field in (
            "menu_visible",
            "display_owner",
            "network_owner",
            "background_allowed",
        ):
            if field in normalized:
                normalized[field] = bool(
                    normalized[field]
                )

        if "menu_order" in normalized:
            try:
                normalized["menu_order"] = int(
                    normalized["menu_order"]
                )
            except (TypeError, ValueError) as error:
                raise ConfigurationError(
                    f"Service {service_id!r} menu_order "
                    f"must be an integer"
                ) from error
        else:
            normalized["menu_order"] = 1000

        normalized.setdefault(
            "menu_visible",
            service_id != "idle",
        )
        installed_path = normalized.get("installed_path")
        service_type = normalized.get("type", "application")

        if service_type == "background_service":
            configured = bool(
                installed_path
                and Path(str(installed_path)).is_file()
            )
        else:
            configured = command is not None

        normalized.setdefault(
            "configured",
            configured,
        )
        normalized.setdefault(
            "type",
            "application",
        )
        normalized.setdefault(
            "source",
            source,
        )

        return normalized

    def merge_services(
        self,
        additional_services: dict[
            str,
            dict[str, Any],
        ],
        *,
        allow_override: bool = False,
        source: str = "plugin discovery",
    ) -> dict[str, dict[str, Any]]:
        merged = self.services

        for service_id, service in (
            additional_services.items()
        ):
            if (
                service_id in merged
                and not allow_override
            ):
                raise ConfigurationError(
                    f"Duplicate service ID {service_id!r} "
                    f"from {source}"
                )

            merged[service_id] = self.validate_service(
                service_id,
                service,
                source=source,
            )

        self._services = merged
        return self.services

    def get(
        self,
        service_id: str,
    ) -> dict[str, Any] | None:
        service = self._services.get(service_id)

        if service is None:
            return None

        return copy.deepcopy(service)

    def menu_services(
        self,
    ) -> list[dict[str, Any]]:
        visible = [
            copy.deepcopy(service)
            for service in self._services.values()
            if service.get("menu_visible", False)
        ]

        visible.sort(
            key=lambda item: (
                int(item.get("menu_order", 1000)),
                str(item.get("name", "")).lower(),
                str(item.get("id", "")),
            )
        )

        return visible

    def configured_services(
        self,
    ) -> Iterable[dict[str, Any]]:
        for service in self._services.values():
            if service.get("configured", False):
                yield copy.deepcopy(service)
