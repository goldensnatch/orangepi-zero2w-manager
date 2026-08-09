from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


class ServiceCatalogError(RuntimeError):
    """Raised when the service catalog cannot load."""


class ServiceCatalog:
    """Unified source for Rocky service definitions.

    The current repo keeps its authoritative catalog in config/services.json.
    Older console/runtime code imports this through manager.runtime, so keep the
    loader small, dependency-free, and safe at import time.
    """

    def __init__(
        self,
        *,
        services_file: str | Path = "/opt/zero2w-manager/config/services.json",
        plugin_directories: Iterable[str | Path] = (
            "/opt/zero2w-manager/apps",
            "/opt/rocky/apps",
        ),
        allow_plugin_override: bool = False,
        strict_plugins: bool = False,
    ) -> None:
        self.services_file = Path(services_file)
        self.plugin_directories = tuple(Path(directory) for directory in plugin_directories)
        self.allow_plugin_override = bool(allow_plugin_override)
        self.strict_plugins = bool(strict_plugins)
        self._lock = threading.RLock()
        self._services: dict[str, dict[str, Any]] = {}
        self._plugin_errors: list[dict[str, str]] = []
        self._loaded = False

    @property
    def loaded(self) -> bool:
        with self._lock:
            return self._loaded

    @property
    def services(self) -> dict[str, dict[str, Any]]:
        self.ensure_loaded()
        with self._lock:
            return copy.deepcopy(self._services)

    @property
    def plugin_errors(self) -> list[dict[str, str]]:
        with self._lock:
            return copy.deepcopy(self._plugin_errors)

    @property
    def service_count(self) -> int:
        self.ensure_loaded()
        with self._lock:
            return len(self._services)

    def load(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            try:
                raw = json.loads(self.services_file.read_text(encoding="utf-8"))
            except FileNotFoundError as error:
                raise ServiceCatalogError(f"Services file not found: {self.services_file}") from error
            except json.JSONDecodeError as error:
                raise ServiceCatalogError(
                    f"Invalid services JSON at line {error.lineno}, column {error.colno}: {error.msg}"
                ) from error
            except OSError as error:
                raise ServiceCatalogError(f"Unable to read {self.services_file}: {error}") from error

            if not isinstance(raw, dict):
                raise ServiceCatalogError("services catalog root must be a JSON object")

            services: dict[str, dict[str, Any]] = {}
            for service_id, definition in raw.items():
                if not isinstance(definition, dict):
                    raise ServiceCatalogError(f"service {service_id!r} must be an object")
                service = copy.deepcopy(definition)
                service.setdefault("id", str(service_id))
                services[str(service_id)] = service

            self._services = services
            self._plugin_errors = []
            self._loaded = True
            logger.info("Service catalog loaded: %d services", len(services))
            return copy.deepcopy(self._services)

    def reload(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            self._services = {}
            self._plugin_errors = []
            self._loaded = False
        return self.load()

    def ensure_loaded(self) -> None:
        if not self.loaded:
            self.load()

    def get(self, service_id: str) -> dict[str, Any] | None:
        self.ensure_loaded()
        with self._lock:
            service = self._services.get(service_id)
            return copy.deepcopy(service) if service is not None else None

    def require(self, service_id: str) -> dict[str, Any]:
        service = self.get(service_id)
        if service is None:
            raise ServiceCatalogError(f"Unknown service: {service_id}")
        return service

    def menu_services(self) -> list[dict[str, Any]]:
        self.ensure_loaded()
        with self._lock:
            visible = [
                copy.deepcopy(service)
                for service in self._services.values()
                if service.get("menu_visible", False)
            ]
        visible.sort(
            key=lambda service: (
                int(service.get("menu_order", 1000)),
                str(service.get("name", "")).casefold(),
                str(service.get("id", "")),
            )
        )
        return visible

    def configured_services(self) -> list[dict[str, Any]]:
        self.ensure_loaded()
        with self._lock:
            return [
                copy.deepcopy(service)
                for service in self._services.values()
                if service.get("configured", False)
            ]

    def plugin_services(self) -> list[dict[str, Any]]:
        self.ensure_loaded()
        with self._lock:
            return [
                copy.deepcopy(service)
                for service in self._services.values()
                if service.get("manifest_path")
            ]
