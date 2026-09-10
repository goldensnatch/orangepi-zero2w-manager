from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from manager.system_probe import network_header_token, network_links_snapshot, process_snapshot
from manager.runtime import ServiceCatalog
from manager.runtime.media_stack import media_container_names


DEFAULT_SERVICE = "zero2w-manager.service"
DEFAULT_APPS_DIR = Path("/opt/rocky/apps")
DEFAULT_STATE_FILE = Path(
    "/run/rocky/state.json"
)
DEFAULT_BUTTON_SOCKET = Path(
    "/run/rocky/buttons.sock"
)
DEFAULT_TESSERAE_MANAGED_IDS = {
    "pwnagotchi",
    "tesserae",
}
DEFAULT_DOCKER_CONTAINERS = (
    "rocky-pihole",
    "rocky-transfer-gluetun",
    "rocky-transfer-qbittorrent",
    *media_container_names(),
)


def utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat().replace("+00:00", "Z")


def read_json_file(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, None

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        return None, str(error)

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        return None, (
            f"invalid JSON at line {error.lineno}, "
            f"column {error.colno}: {error.msg}"
        )

    if not isinstance(value, dict):
        return None, "state file root must be a JSON object"

    return value, None


def service_status(service: str) -> dict[str, Any]:
    command = [
        "systemctl",
        "is-active",
        service,
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except FileNotFoundError:
        return {
            "name": service,
            "active": None,
            "state": "unknown",
            "error": "systemctl is not installed",
        }
    except subprocess.TimeoutExpired:
        return {
            "name": service,
            "active": None,
            "state": "unknown",
            "error": "systemctl timed out",
        }
    except OSError as error:
        return {
            "name": service,
            "active": None,
            "state": "unknown",
            "error": str(error),
        }

    state = result.stdout.strip() or result.stderr.strip()
    active = result.returncode == 0 and state == "active"

    return {
        "name": service,
        "active": active,
        "state": state or "unknown",
        "error": None,
    }


def system_uptime_seconds() -> float | None:
    uptime_path = Path("/proc/uptime")

    try:
        first_field = uptime_path.read_text(
            encoding="utf-8"
        ).split()[0]

        return float(first_field)
    except (
        OSError,
        IndexError,
        ValueError,
    ):
        return None


def process_uptime_seconds(pid: int | None) -> float | None:
    if pid is None or pid < 1:
        return None

    stat_path = Path(f"/proc/{pid}/stat")

    try:
        fields = stat_path.read_text(
            encoding="utf-8"
        ).split()

        start_ticks = int(fields[21])
        clock_ticks = os.sysconf(
            os.sysconf_names["SC_CLK_TCK"]
        )

        system_uptime = system_uptime_seconds()

        if system_uptime is None:
            return None

        process_start_seconds = start_ticks / clock_ticks
        return max(
            0.0,
            system_uptime - process_start_seconds,
        )
    except (
        OSError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return None


def service_main_pid(service: str) -> int | None:
    command = [
        "systemctl",
        "show",
        service,
        "--property",
        "MainPID",
        "--value",
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
        OSError,
    ):
        return None

    if result.returncode != 0:
        return None

    try:
        pid = int(result.stdout.strip())
    except ValueError:
        return None

    return pid if pid > 0 else None


def inspect_socket(path: Path) -> dict[str, Any]:
    exists = path.exists()
    is_socket = False

    if exists:
        try:
            is_socket = path.is_socket()
        except OSError:
            is_socket = False

    return {
        "path": str(path),
        "exists": exists,
        "is_socket": is_socket,
    }


def read_manifest(
    manifest_path: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except OSError as error:
        return None, str(error)
    except json.JSONDecodeError as error:
        return None, (
            f"invalid JSON at line {error.lineno}, "
            f"column {error.colno}"
        )

    if not isinstance(value, dict):
        return None, "manifest root must be an object"

    return value, None


def discover_applications(
    apps_dir: Path,
) -> list[dict[str, Any]]:
    if not apps_dir.is_dir():
        return []

    applications: list[dict[str, Any]] = []

    try:
        entries = sorted(
            apps_dir.iterdir(),
            key=lambda path: path.name.lower(),
        )
    except OSError:
        return []

    for directory in entries:
        if not directory.is_dir():
            continue

        manifest_path = directory / "manifest.json"

        if not manifest_path.is_file():
            applications.append(
                {
                    "id": directory.name,
                    "name": directory.name,
                    "valid_manifest": False,
                    "hidden": False,
                    "menu_order": None,
                    "error": "manifest.json is missing",
                }
            )
            continue

        manifest, error = read_manifest(manifest_path)

        if manifest is None:
            applications.append(
                {
                    "id": directory.name,
                    "name": directory.name,
                    "valid_manifest": False,
                    "hidden": False,
                    "menu_order": None,
                    "error": error,
                }
            )
            continue

        app_id = manifest.get("id")
        name = manifest.get("name")
        hidden = manifest.get("hidden", False)
        menu_order = manifest.get("menu_order")

        applications.append(
            {
                "id": (
                    app_id
                    if isinstance(app_id, str) and app_id
                    else directory.name
                ),
                "directory": directory.name,
                "name": (
                    name
                    if isinstance(name, str) and name
                    else directory.name
                ),
                "valid_manifest": True,
                "hidden": bool(hidden),
                "menu_order": (
                    menu_order
                    if isinstance(menu_order, int)
                    else None
                ),
                "error": None,
            }
        )

    applications.sort(
        key=lambda app: (
            app["menu_order"] is None,
            (
                app["menu_order"]
                if app["menu_order"] is not None
                else 0
            ),
            str(app["name"]).lower(),
        )
    )

    return applications


def docker_container_status(name: str) -> dict[str, Any]:
    try:
        state_result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as error:
        return {
            "name": name,
            "status": "unknown",
            "healthy": False,
            "running": False,
            "error": str(error),
        }

    if state_result.returncode != 0:
        return {
            "name": name,
            "status": "stopped",
            "healthy": False,
            "running": False,
            "error": None,
        }

    state = state_result.stdout.strip() or "unknown"
    running = state == "running"
    health = None
    if running:
        health_result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if health_result.returncode == 0:
            health = health_result.stdout.strip() or None

    status = health or state
    healthy = status == "healthy" or state == "running"
    return {
        "name": name,
        "status": status,
        "healthy": healthy,
        "running": running,
        "error": None,
    }


def docker_snapshot() -> dict[str, Any]:
    names: set[str] = set(DEFAULT_DOCKER_CONTAINERS)
    try:
        services = ServiceCatalog().load()
    except Exception:
        services = {}

    if isinstance(services, dict):
        for service in services.values():
            if not isinstance(service, dict):
                continue
            container = service.get("docker_container")
            if isinstance(container, str) and container.strip():
                names.add(container.strip())

    items = [docker_container_status(name) for name in sorted(names)]
    running = sum(1 for item in items if item.get("running"))
    return {
        "count": len(items),
        "running": running,
        "items": items,
    }


def managed_services_snapshot(
    published_state: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        services = ServiceCatalog().load()
    except Exception:
        services = {}

    active_app_id = None
    if isinstance(published_state, dict):
        application_state = published_state.get("application")
        if isinstance(application_state, dict):
            active_app_id = str(
                application_state.get("active_id") or ""
            ).strip() or None

    items: list[dict[str, Any]] = []

    if not isinstance(services, dict):
        services = {}

    for app_id, service in services.items():
        if not isinstance(service, dict):
            continue

        service_id = str(app_id).strip()
        unit = str(service.get("systemd_service") or "").strip()
        if service_id not in DEFAULT_TESSERAE_MANAGED_IDS or not unit:
            continue

        service_info = service_status(unit)
        state = str(service_info.get("state") or "unknown")
        active = bool(service_info.get("active") is True)
        status = state
        if active_app_id == service_id and active:
            status = "foreground"

        items.append(
            {
                "id": service_id,
                "name": str(service.get("name") or service_id.title()),
                "description": str(service.get("description") or ""),
                "unit": unit,
                "kind": "systemd_display",
                "resident_display": bool(service.get("resident_display")),
                "menu_visible": bool(service.get("menu_visible", True)),
                "active": active,
                "healthy": active,
                "status": status,
                "foreground": active_app_id == service_id,
                "working_directory": str(
                    service.get("working_directory") or ""
                ),
            }
        )

    items.sort(key=lambda item: str(item.get("name") or "").lower())

    return {
        "count": len(items),
        "items": items,
    }


def build_runtime_status(
    *,
    service: str = DEFAULT_SERVICE,
    apps_dir: Path = DEFAULT_APPS_DIR,
    state_file: Path = DEFAULT_STATE_FILE,
    button_socket: Path = DEFAULT_BUTTON_SOCKET,
) -> dict[str, Any]:
    published_state, state_error = read_json_file(state_file)

    service_info = service_status(service)
    pid = service_main_pid(service)

    applications = discover_applications(apps_dir)

    valid_apps = sum(
        1
        for app in applications
        if app["valid_manifest"]
    )

    invalid_apps = len(applications) - valid_apps

    observed: dict[str, Any] = {
        "service": {
            **service_info,
            "pid": pid,
            "uptime_seconds": process_uptime_seconds(pid),
        },
        "system": {
            "hostname": socket.gethostname(),
            "uptime_seconds": system_uptime_seconds(),
        },
        "buttons": inspect_socket(button_socket),
        "network": {
            **network_links_snapshot(),
            "header_token": network_header_token(),
        },
        "managed_services": managed_services_snapshot(
            published_state
            if isinstance(published_state, dict)
            else None
        ),
        "docker": docker_snapshot(),
        "processes": process_snapshot(),
        "applications": {
            "directory": str(apps_dir),
            "count": len(applications),
            "valid": valid_apps,
            "invalid": invalid_apps,
            "items": applications,
        },
    }

    healthy = (
        service_info["active"] is True
        and invalid_apps == 0
    )

    warnings: list[str] = []

    if not state_file.exists():
        warnings.append(
            "Runtime has not published an authoritative state file yet."
        )
    elif state_error:
        warnings.append(
            f"Runtime state file could not be read: {state_error}"
        )

    if service_info["active"] is not True:
        warnings.append(
            f"{service} is not active."
        )

    if invalid_apps:
        warnings.append(
            f"{invalid_apps} application manifest(s) are invalid."
        )

    if not observed["buttons"]["is_socket"]:
        warnings.append(
            "Button socket was not detected at the configured path."
        )

    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "healthy": healthy,
        "published": {
            "path": str(state_file),
            "available": published_state is not None,
            "error": state_error,
            "state": published_state,
        },
        "observed": observed,
        "warnings": warnings,
    }


def compact_runtime_status(status: dict[str, Any]) -> dict[str, Any]:
    """Return the small operator-facing status payload for the web API.

    The full runtime status intentionally keeps noisy diagnostics such as the
    process table and the complete published state.  The browser/API default is
    for humans checking Rocky, so keep only the fields that answer "is it up,
    what mode is it in, where do I click, and what needs attention?".
    """

    observed = status.get("observed", {})
    if not isinstance(observed, dict):
        observed = {}

    published = status.get("published", {})
    published_state: dict[str, Any] = {}
    if isinstance(published, dict) and isinstance(published.get("state"), dict):
        published_state = published["state"]

    network = observed.get("network", {})
    if not isinstance(network, dict):
        network = {}
    interfaces = []
    for item in network.get("interfaces", []):
        if not isinstance(item, dict):
            continue
        interfaces.append(
            {
                "name": item.get("name"),
                "kind": item.get("kind"),
                "ipv4": item.get("ipv4"),
                "active": item.get("active"),
                "signal_dbm": item.get("signal_dbm"),
            }
        )

    web_services = published_state.get("web_services", {})
    compact_services: dict[str, Any] = {}
    if isinstance(web_services, dict):
        for service_id, service in sorted(web_services.items()):
            if not isinstance(service, dict):
                continue
            compact_services[str(service_id)] = {
                "active": service.get("active"),
                "available": service.get("available"),
                "url": service.get("url"),
                "proxy_available": bool(service.get("tokenized_proxy_url")),
            }

    docker = observed.get("docker", {})
    if not isinstance(docker, dict):
        docker = {}
    docker_items = []
    for item in docker.get("items", []):
        if not isinstance(item, dict):
            continue
        docker_items.append(
            {
                "name": item.get("name"),
                "status": item.get("status"),
                "running": item.get("running"),
                "healthy": item.get("healthy"),
            }
        )

    return {
        "schema_version": status.get("schema_version", 1),
        "generated_at": status.get("generated_at"),
        "healthy": status.get("healthy"),
        "service": observed.get("service", {}),
        "system": observed.get("system", {}),
        "network": {
            "primary": network.get("primary"),
            "header_token": network.get("header_token"),
            "interfaces": interfaces,
        },
        "mode": published_state.get("mode", {}),
        "runtime": published_state.get("runtime", {}),
        "display": published_state.get("display", {}),
        "buttons": published_state.get("buttons") or observed.get("buttons", {}),
        "storage": published_state.get("storage", {}),
        "transfer": published_state.get("transfer", {}),
        "web_services": compact_services,
        "docker": {
            "count": docker.get("count"),
            "running": docker.get("running"),
            "items": docker_items,
        },
        "applications": observed.get("applications", {}),
        "warnings": status.get("warnings", []),
        "diagnostics_url": "/api/runtime/status?detail=full",
    }


def format_duration(
    seconds: float | int | None,
) -> str:
    if seconds is None:
        return "unknown"

    total = max(0, int(seconds))

    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds_left = divmod(remainder, 60)

    parts: list[str] = []

    if days:
        parts.append(f"{days}d")

    if hours or days:
        parts.append(f"{hours}h")

    if minutes or hours or days:
        parts.append(f"{minutes}m")

    parts.append(f"{seconds_left}s")

    return " ".join(parts)
