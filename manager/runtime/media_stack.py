from __future__ import annotations

from pathlib import Path
from typing import Any


MEDIA_STACK_ROOT = Path("/opt/zero2w-manager/runtime/media-stack")
MEDIA_STACK_COMPOSE = MEDIA_STACK_ROOT / "docker-compose.yml"

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
