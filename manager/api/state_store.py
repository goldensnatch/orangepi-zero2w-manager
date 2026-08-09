from __future__ import annotations

import json
import os
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


DEFAULT_RUNTIME_DIRECTORY = Path("/run/rocky")
DEFAULT_STATE_FILE = DEFAULT_RUNTIME_DIRECTORY / "state.json"
STATE_SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_state() -> dict[str, Any]:
    now = utc_now()

    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "runtime": {
            "name": "Rocky Runtime",
            "version": None,
            "pid": os.getpid(),
            "started_at": now,
            "updated_at": now,
        },
        "foreground_application": None,
        "launcher": {
            "active": True,
            "selected_application": None,
        },
        "display": {
            "connected": None,
            "mode": "unknown",
            "last_update_at": None,
        },
        "buttons": {
            "service_active": None,
            "socket_path": "/run/rocky/buttons.sock",
            "connected_clients": None,
            "last_event_at": None,
        },
        "network": {
            "transport_mode": "direct",
            "vpn": {
                "provider": "none",
                "policy": "optional",
                "connected": False,
                "kill_switch": False,
                "allow_lan": True,
                "endpoint": None,
                "last_error": None,
            },
            "privacy_relay": {
                "mode": "off",
                "tor": {
                    "enabled": False,
                    "socks_host": "127.0.0.1",
                    "socks_port": 9050,
                    "healthy": False,
                    "last_error": None,
                },
            },
            "remote_access": {
                "mode": "disabled",
                "healthy": False,
                "url": None,
                "last_error": None,
            },
        },
        "transfer": {
            "engine": "qbittorrent-nox",
            "healthy": False,
            "vpn_required": True,
            "active_destination_id": "mini_pc_smb",
            "destination_ready": False,
            "download_path": "/mnt/rocky-seed/complete",
            "incomplete_path": "/mnt/rocky-seed/incomplete",
            "last_error": None,
        },
        "storage": {
            "destinations": [
                {
                    "id": "mini_pc_smb",
                    "kind": "smb",
                    "label": "Mini-PC SMB Seed Share",
                    "path": "/mnt/rocky-seed",
                    "present": False,
                    "writable": False,
                    "available_bytes": None,
                    "selected": True,
                }
            ]
        },
        "last_transition": {
            "type": "runtime_initialized",
            "application": None,
            "at": now,
        },
    }


class RuntimeStatePublisher:
    """Thread-safe, atomic publisher for Rocky Runtime state."""

    def __init__(
        self,
        path: Path = DEFAULT_STATE_FILE,
        *,
        runtime_version: str | None = None,
    ) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state = _default_state()

        if runtime_version is not None:
            self._state["runtime"]["version"] = runtime_version

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state)

    def update(
        self,
        values: Mapping[str, Any],
        *,
        publish: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            self._merge(self._state, values)
            self._touch()

            if publish:
                self._publish_locked()

            return deepcopy(self._state)

    def set_foreground_application(
        self,
        application_id: str | None,
        *,
        transition: str | None = None,
        publish: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            self._state["foreground_application"] = application_id
            self._state["launcher"]["active"] = application_id is None

            transition_type = transition

            if transition_type is None:
                transition_type = (
                    "launcher_activated"
                    if application_id is None
                    else "application_activated"
                )

            self._state["last_transition"] = {
                "type": transition_type,
                "application": application_id,
                "at": utc_now(),
            }

            self._touch()

            if publish:
                self._publish_locked()

            return deepcopy(self._state)

    def set_launcher_selection(
        self,
        application_id: str | None,
        *,
        publish: bool = True,
    ) -> dict[str, Any]:
        return self.update(
            {
                "launcher": {
                    "selected_application": application_id,
                }
            },
            publish=publish,
        )

    def set_display(
        self,
        *,
        connected: bool | None = None,
        mode: str | None = None,
        publish: bool = True,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {
            "last_update_at": utc_now(),
        }

        if connected is not None:
            values["connected"] = connected

        if mode is not None:
            values["mode"] = mode

        return self.update(
            {
                "display": values,
            },
            publish=publish,
        )

    def set_buttons(
        self,
        *,
        service_active: bool | None = None,
        connected_clients: int | None = None,
        last_event: bool = False,
        publish: bool = True,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}

        if service_active is not None:
            values["service_active"] = service_active

        if connected_clients is not None:
            values["connected_clients"] = connected_clients

        if last_event:
            values["last_event_at"] = utc_now()

        return self.update(
            {
                "buttons": values,
            },
            publish=publish,
        )

    def publish(self) -> dict[str, Any]:
        with self._lock:
            self._touch()
            self._publish_locked()
            return deepcopy(self._state)

    def remove(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                return

    def _touch(self) -> None:
        self._state["runtime"]["updated_at"] = utc_now()
        self._state["runtime"]["pid"] = os.getpid()

    def _publish_locked(self) -> None:
        self.path.parent.mkdir(
            mode=0o755,
            parents=True,
            exist_ok=True,
        )

        payload = json.dumps(
            self._state,
            indent=2,
            sort_keys=True,
        ) + "\n"

        temporary_path: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)

            os.chmod(temporary_path, 0o644)
            os.replace(temporary_path, self.path)

            directory_fd = os.open(
                self.path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )

            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    @classmethod
    def _merge(
        cls,
        target: dict[str, Any],
        values: Mapping[str, Any],
    ) -> None:
        for key, value in values.items():
            existing = target.get(key)

            if (
                isinstance(existing, dict)
                and isinstance(value, Mapping)
            ):
                cls._merge(existing, value)
            else:
                target[key] = deepcopy(value)
