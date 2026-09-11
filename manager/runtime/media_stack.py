from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


MEDIA_STACK_ROOT = Path("/opt/zero2w-manager/runtime/media-stack")
MEDIA_STACK_COMPOSE = MEDIA_STACK_ROOT / "docker-compose.yml"
JELLYSEERR_CONFIG_DIRNAME = "jellyseerr-config"
JELLYSEERR_OVERLAY_NAME = "docker-compose.jellyseerr.yml"
_DEFAULT_DOCKER_GATEWAY = "172.17.0.1"
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")

MEDIA_STACK_APPS: dict[str, dict[str, Any]] = {
    "jellyfin": {
        "compose_service": "jellyfin",
        "container": "rocky-media-jellyfin",
        "port": 8096,
        "local_url": "http://127.0.0.1:8096/",
        "name": "Jellyfin",
        "description": "Open-source self-hosted media server, no account required",
        "caption": "Media server",
    },
    "jellyseerr": {
        "compose_service": "jellyseerr",
        "container": "rocky-media-jellyseerr",
        "port": 5055,
        "local_url": "http://127.0.0.1:5055/",
        "name": "Jellyseerr",
        "description": "Media request portal with Jellyfin support",
        "caption": "Request portal",
    },
    "prowlarr": {
        "compose_service": "prowlarr",
        "container": "rocky-media-prowlarr",
        "port": 9696,
        "local_url": "http://127.0.0.1:9696/",
        "name": "Prowlarr",
        "description": "Indexer manager and proxy for Radarr and Sonarr",
        "caption": "Indexer manager",
    },
    "radarr": {
        "compose_service": "radarr",
        "container": "rocky-media-radarr",
        "port": 7878,
        "local_url": "http://127.0.0.1:7878/",
        "name": "Radarr",
        "description": "Movie automation and library management",
        "caption": "Movie automation",
    },
    "sonarr": {
        "compose_service": "sonarr",
        "container": "rocky-media-sonarr",
        "port": 8989,
        "local_url": "http://127.0.0.1:8989/",
        "name": "Sonarr",
        "description": "Series automation and library management",
        "caption": "Series automation",
    },
    "bazarr": {
        "compose_service": "bazarr",
        "container": "rocky-media-bazarr",
        "port": 6767,
        "local_url": "http://127.0.0.1:6767/",
        "name": "Bazarr",
        "description": "Subtitle automation for movies and series",
        "caption": "Subtitle automation",
    },
}


def media_app_ids() -> tuple[str, ...]:
    return tuple(MEDIA_STACK_APPS)


def media_container_names() -> tuple[str, ...]:
    return tuple(str(app["container"]) for app in MEDIA_STACK_APPS.values())


def media_launch_target(app_id: str) -> dict[str, Any] | None:
    spec = MEDIA_STACK_APPS.get(app_id)
    if spec is None:
        return None
    return {
        "compose_service": str(spec["compose_service"]),
        "container": str(spec["container"]),
        "port": int(spec["port"]),
        "url": str(spec["local_url"]),
        "name": str(spec["name"]),
        "description": str(spec["description"]),
    }


def entertainment_menu_items() -> list[dict[str, str]]:
    items = [
        {
            "id": "transfer-stack",
            "label": "Torrentz",
            "caption": "qBittorrent via TorrentFortress",
        }
    ]
    for app_id, spec in MEDIA_STACK_APPS.items():
        items.append(
            {
                "id": app_id,
                "label": str(spec["name"]),
                "caption": str(spec["caption"]),
            }
        )
    return items


def _looks_like_ipv4(value: str) -> bool:
    if not _IPV4_RE.match(str(value or "")):
        return False
    return all(0 <= int(part) <= 255 for part in value.split("."))


def docker_bridge_gateway_ip() -> str:
    """Literal IPv4 for the Docker host from a container. Avoids host.docker.internal DNS."""
    try:
        result = subprocess.run(
            [
                "docker",
                "network",
                "inspect",
                "bridge",
                "-f",
                "{{range .IPAM.Config}}{{.Gateway}}\n{{end}}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        for line in (result.stdout or "").splitlines():
            candidate = line.strip()
            if _looks_like_ipv4(candidate):
                return candidate
    try:
        addr = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", "docker0"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        addr = None
    if addr is not None and addr.returncode == 0:
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", addr.stdout or "")
        if match and _looks_like_ipv4(match.group(1)):
            return match.group(1)
    return _DEFAULT_DOCKER_GATEWAY


def jellyseerr_overlay_text(*, include_config_volume: bool = True) -> str:
    gateway = docker_bridge_gateway_ip()
    lines = [
        "services:",
        "  jellyseerr:",
        "    extra_hosts:",
        f'      - "host.docker.internal:{gateway}"',
        f'      - "jellyfin:{gateway}"',
    ]
    if include_config_volume:
        lines.extend(
            [
                "    volumes:",
                "      - ./jellyseerr-config:/app/config",
            ]
        )
    return "\n".join(lines) + "\n"


def ensure_jellyseerr_config_volume(root: Path | None = None) -> Path:
    """Create ./jellyseerr-config and a compose overlay that bind-mounts /app/config."""
    stack_root = Path(root or MEDIA_STACK_ROOT)
    config_dir = stack_root / JELLYSEERR_CONFIG_DIRNAME
    config_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chown(config_dir, 1000, 1000)
    except OSError:
        pass
    overlay = stack_root / JELLYSEERR_OVERLAY_NAME
    compose = stack_root / "docker-compose.yml"
    include_volume = True
    if compose.is_file() and "/app/config" in compose.read_text(encoding="utf-8", errors="replace"):
        include_volume = False
    desired = jellyseerr_overlay_text(include_config_volume=include_volume)
    current = overlay.read_text(encoding="utf-8", errors="replace") if overlay.is_file() else ""
    if current != desired:
        overlay.write_text(desired, encoding="utf-8")
    return config_dir


def media_compose_files(root: Path | None = None) -> list[Path]:
    stack_root = Path(root or MEDIA_STACK_ROOT)
    files: list[Path] = []
    compose = stack_root / "docker-compose.yml"
    if compose.is_file():
        files.append(compose)
    overlay = stack_root / JELLYSEERR_OVERLAY_NAME
    if overlay.is_file():
        files.append(overlay)
    return files


def media_compose_up_command(
    service: str,
    root: Path | None = None,
    *,
    force_recreate: bool = False,
) -> list[str] | None:
    stack_root = Path(root or MEDIA_STACK_ROOT)
    ensure_jellyseerr_config_volume(stack_root)
    files = media_compose_files(stack_root)
    if not files or files[0].name != "docker-compose.yml":
        return None
    command = ["docker", "compose"]
    for path in files:
        command.extend(["-f", str(path)])
    command.extend(["up", "-d"])
    if force_recreate:
        command.append("--force-recreate")
    command.append(service)
    return command


def container_has_destination_mount(container: str, destination: str = "/app/config") -> bool:
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Mounts}}", container],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        mounts = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return False
    if not isinstance(mounts, list):
        return False
    wanted = str(destination).rstrip("/") or "/"
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        dest = str(mount.get("Destination") or "").rstrip("/") or "/"
        if dest == wanted:
            return True
    return False


def container_has_jellyfin_host_alias(container: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{json .HostConfig.ExtraHosts}}", container],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        hosts = json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        return False
    if not isinstance(hosts, list):
        return False
    return any(
        str(host).startswith("jellyfin:") or str(host).startswith("host.docker.internal:")
        for host in hosts
    )


def _docker_inspect(container: str) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return None
    return payload[0]


def jellyfin_connect_hostname(container: str | None = None) -> str:
    """IPv4 Jellyseerr should use; never a DNS name like host.docker.internal."""
    spec = MEDIA_STACK_APPS["jellyfin"]
    inspected = _docker_inspect(container or str(spec["container"]))
    if inspected:
        mode = str((inspected.get("HostConfig") or {}).get("NetworkMode") or "")
        if mode != "host":
            networks = (inspected.get("NetworkSettings") or {}).get("Networks") or {}
            if isinstance(networks, dict):
                seerr = _docker_inspect(str(MEDIA_STACK_APPS["jellyseerr"]["container"]))
                seerr_nets = set()
                if seerr:
                    names = (seerr.get("NetworkSettings") or {}).get("Networks") or {}
                    if isinstance(names, dict):
                        seerr_nets = set(names)
                for name, net in networks.items():
                    if not isinstance(net, dict):
                        continue
                    ip = str(net.get("IPAddress") or "").strip()
                    if ip and _looks_like_ipv4(ip) and (not seerr_nets or name in seerr_nets):
                        return ip
                for net in networks.values():
                    if not isinstance(net, dict):
                        continue
                    ip = str(net.get("IPAddress") or "").strip()
                    if ip and _looks_like_ipv4(ip):
                        return ip
    return docker_bridge_gateway_ip()


def _container_created_timestamp(container: str) -> float | None:
    inspected = _docker_inspect(container)
    if not inspected:
        return None
    raw = str(inspected.get("Created") or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        try:
            return datetime.fromisoformat(raw[:19]).timestamp()
        except ValueError:
            return None


def jellyseerr_needs_volume_recreate(
    container: str,
    root: Path | None = None,
) -> bool:
    stack_root = Path(root or MEDIA_STACK_ROOT)
    ensure_jellyseerr_config_volume(stack_root)
    overlay = stack_root / JELLYSEERR_OVERLAY_NAME
    created = _container_created_timestamp(container)
    if overlay.is_file() and (created is None or overlay.stat().st_mtime > created + 1):
        return True
    if not container_has_destination_mount(container, "/app/config"):
        return True
    return not container_has_jellyfin_host_alias(container)
