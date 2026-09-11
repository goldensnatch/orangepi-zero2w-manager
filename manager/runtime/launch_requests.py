from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any


LAUNCH_REQUEST_PATH = Path("/opt/zero2w-manager/runtime/config/launch-request.json")
RAGNAR_LOCAL_URL = "http://127.0.0.1:8000"
_PWNAGOTCHI_CONFIG_PATHS = (
    Path("/etc/pwnagotchi/config.toml"),
    Path("/opt/pwnagotchi/config.toml"),
    Path("/etc/pwnagotchi/config.yml"),
    Path("/etc/pwnagotchi/config.yaml"),
    Path("/opt/pwnagotchi/config.yml"),
    Path("/opt/pwnagotchi/config.yaml"),
)
_PORT_RE = re.compile(r"(?im)^\s*(?:port\s*=\s*|port:\s*)(\d{2,5})\s*$")
_ENABLED_RE = re.compile(r"(?im)^\s*(?:enabled\s*=\s*|enabled:\s*)(true|false)\s*$")


def write_launch_request(
    app_id: str,
    *,
    source: str = "console",
    path: Path | None = None,
) -> Path:
    target = Path(path or LAUNCH_REQUEST_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "app_id": str(app_id or "").strip(),
        "source": str(source or "console"),
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def consume_launch_request(path: Path | None = None) -> dict[str, Any] | None:
    target = Path(path or LAUNCH_REQUEST_PATH)
    if not target.is_file():
        return None
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        target.unlink()
    except OSError:
        pass
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    app_id = str(payload.get("app_id") or "").strip()
    if not app_id:
        return None
    return payload


def pwnagotchi_web_url(config_paths: tuple[Path, ...] | None = None) -> str | None:
    """Return the configured Pwnagotchi web UI, if present."""
    for config_path in config_paths or _PWNAGOTCHI_CONFIG_PATHS:
        if not config_path.is_file():
            continue
        try:
            text = config_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        enabled_match = _ENABLED_RE.search(text)
        if enabled_match and enabled_match.group(1).lower() == "false":
            continue
        port_match = _PORT_RE.search(text)
        port = int(port_match.group(1)) if port_match else 8080
        if 1 <= port <= 65535:
            return f"http://127.0.0.1:{port}"
    return None


def catalog_proxy_url(app_id: str, service: dict[str, Any] | None = None) -> str | None:
    spec = service if isinstance(service, dict) else {}
    raw = spec.get("url")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if str(app_id or "") == "ragnar":
        return RAGNAR_LOCAL_URL
    if str(app_id or "") == "pwnagotchi":
        return pwnagotchi_web_url()
    return None
