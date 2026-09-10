from __future__ import annotations



import base64
from http.cookies import SimpleCookie

from email import policy
from email.parser import BytesParser

import hashlib

import hmac

import html

import io

import json

import mimetypes

import os
import re

import secrets

import shutil

import socket

import subprocess

import tempfile

try:
    import grp
    import pwd
except ImportError:  # pragma: no cover - Windows/local development only
    grp = None
    pwd = None

import threading

import time

from collections import defaultdict, deque

from datetime import datetime, timezone

from http import HTTPStatus

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pathlib import Path

from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse, urlunparse
from urllib.request import Request, urlopen

from manager.api import runtime_status as runtime_status_api
from manager.runtime import network_transfer as network_transfer_runtime
from manager.runtime.service_catalog import ServiceCatalog
from manager.runtime.media_stack import MEDIA_STACK_APPS, media_app_ids, media_launch_target
from manager.runtime.workspace import (

    WorkspaceError,

    WorkspaceRegistry,

)





HOST = os.environ.get("ROCKY_WEB_HOST", "0.0.0.0")

PORT = int(os.environ.get("ROCKY_WEB_PORT", "8090"))



USERNAME = os.environ.get("ROCKY_WEB_USERNAME", "rocky")

PASSWORD = os.environ.get("ROCKY_WEB_PASSWORD", "")



PROJECT_ROOT = Path("/opt/zero2w-manager").resolve()

RUNTIME_ROOT = Path("/run/rocky").resolve()
PROXY_TOKEN_SECRET_PATH = Path("/opt/zero2w-manager/runtime/config/proxy-token-secret")
MODE_CATALOG_PATH = Path("/opt/zero2w-manager/runtime/config/modes.json")
CURRENT_MODE_REQUEST_PATH = Path("/opt/zero2w-manager/runtime/config/current-mode.json")
MODE_CONFIG_OWNER = os.environ.get(
    "ROCKY_MODE_CONFIG_OWNER",
    os.environ.get("ROCKY_MODE_CONFIG_USER", "rocky-web"),
).strip()
MODE_CONFIG_GROUP = os.environ.get("ROCKY_MODE_CONFIG_GROUP", MODE_CONFIG_OWNER).strip()
PUBLIC_BASE_URL = os.environ.get("ROCKY_PUBLIC_BASE_URL", "").strip()

WORKSPACE_REGISTRY = WorkspaceRegistry.create_default()



MAX_REQUEST_BYTES = int(

    os.environ.get("ROCKY_WEB_MAX_REQUEST_BYTES", str(64 * 1024 * 1024))

)

RATE_LIMIT_WINDOW_SECONDS = int(

    os.environ.get("ROCKY_WEB_RATE_LIMIT_WINDOW_SECONDS", "60")

)

RATE_LIMIT_DEFAULT = int(

    os.environ.get("ROCKY_WEB_RATE_LIMIT_DEFAULT", "30")

)

RATE_LIMIT_WRITE = int(

    os.environ.get("ROCKY_WEB_RATE_LIMIT_WRITE", "10")

)

AUDIT_LOG_PATH = Path(

    os.environ.get(

        "ROCKY_WEB_AUDIT_LOG",

        "/var/log/rocky-web/audit.log",

    )

)

CSRF_TOKEN = os.environ.get("ROCKY_WEB_CSRF_TOKEN") or secrets.token_urlsafe(32)
PROXY_TOKEN_TTL_SECONDS = int(os.environ.get("ROCKY_WEB_PROXY_TOKEN_TTL_SECONDS", "900"))

def load_proxy_token_secret() -> str:

    env_secret = os.environ.get("ROCKY_WEB_PROXY_TOKEN_SECRET")
    if env_secret:
        return env_secret
    try:
        if PROXY_TOKEN_SECRET_PATH.is_file():
            return PROXY_TOKEN_SECRET_PATH.read_text(encoding="utf-8").strip()
        PROXY_TOKEN_SECRET_PATH.parent.mkdir(mode=0o775, parents=True, exist_ok=True)
        generated = secrets.token_urlsafe(32)
        PROXY_TOKEN_SECRET_PATH.write_text(generated + "\n", encoding="utf-8")
        return generated
    except OSError:
        return CSRF_TOKEN

PROXY_TOKEN_SECRET = load_proxy_token_secret()



SECURITY_HEADERS = {

    "Content-Security-Policy": (

        "default-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'"

    ),

    "Permissions-Policy": (

        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "

        "microphone=(), usb=(), magnetometer=(), midi=()"

    ),

    "Cache-Control": "no-store",

    "Cross-Origin-Resource-Policy": "same-origin",

    "Referrer-Policy": "no-referrer",

    "X-Content-Type-Options": "nosniff",

    "X-Frame-Options": "DENY",

}



DELETE_STAGING_ROOT = Path(
    os.environ.get(
        "ROCKY_WEB_DELETE_STAGING_ROOT",
        "/tmp/rocky-delete-staging",
    )
)

TEXT_EXTENSIONS = {

    ".py",

    ".json",

    ".toml",

    ".yaml",

    ".yml",

    ".ini",

    ".cfg",

    ".conf",

    ".service",

    ".md",

    ".txt",

    ".log",

    ".sh",

    ".html",

    ".css",

    ".js",

}





class RateLimiter:

    def __init__(self) -> None:

        self._lock = threading.RLock()

        self._buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)



    def allow(self, client_ip: str, action: str, limit: int) -> tuple[bool, int]:

        now = time.time()

        window_start = now - RATE_LIMIT_WINDOW_SECONDS

        key = (client_ip, action)



        with self._lock:

            bucket = self._buckets[key]



            while bucket and bucket[0] < window_start:

                bucket.popleft()



            if len(bucket) >= limit:

                retry_after = max(1, int(bucket[0] + RATE_LIMIT_WINDOW_SECONDS - now))

                return False, retry_after



            bucket.append(now)

            return True, 0





RATE_LIMITER = RateLimiter()
AUDIT_LOCK = threading.RLock()
RECENT_AUDIT_EVENTS: deque[dict[str, object]] = deque(maxlen=200)


def utc_now() -> str:

    return datetime.now(timezone.utc).isoformat()


def mode_config_owner_ids() -> tuple[int | None, int | None]:
    uid: int | None = None
    gid: int | None = None
    if MODE_CONFIG_OWNER and pwd is not None:
        try:
            owner = pwd.getpwnam(MODE_CONFIG_OWNER)
            uid = owner.pw_uid
            gid = owner.pw_gid
        except KeyError:
            uid = None
    if MODE_CONFIG_GROUP and grp is not None:
        try:
            gid = grp.getgrnam(MODE_CONFIG_GROUP).gr_gid
        except KeyError:
            pass
    return uid, gid


def chown_mode_config_path(path: Path) -> None:
    geteuid = getattr(os, "geteuid", None)
    if not callable(geteuid) or geteuid() != 0:
        return
    uid, gid = mode_config_owner_ids()
    if uid is None and gid is None:
        return
    try:
        os.chown(path, -1 if uid is None else uid, -1 if gid is None else gid)
    except OSError:
        return


def _try_chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        return


def write_mode_config_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o775, parents=True, exist_ok=True)
    _try_chmod(path.parent, 0o775)
    chown_mode_config_path(path.parent)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _try_chmod(temporary_path, 0o664)
        chown_mode_config_path(temporary_path)
        os.replace(temporary_path, path)
        _try_chmod(path, 0o664)
        chown_mode_config_path(path)
    except Exception:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


def ensure_mode_config_files() -> None:
    path = CURRENT_MODE_REQUEST_PATH
    try:
        path.parent.mkdir(mode=0o775, parents=True, exist_ok=True)
    except OSError:
        return
    _try_chmod(path.parent, 0o775)
    chown_mode_config_path(path.parent)
    if not path.exists():
        try:
            write_mode_config_json(
                path,
                {"version": 1, "selected_mode_id": "safe", "override_flags": {}},
            )
        except OSError:
            return
        return
    _try_chmod(path, 0o664)
    chown_mode_config_path(path)


def shell_output(command: list[str], timeout: float = 4.0) -> str:

    try:

        result = subprocess.run(

            command,

            capture_output=True,

            text=True,

            timeout=timeout,

            check=False,

        )

    except Exception as exc:

        return f"Unable to execute command: {exc}"



    output = result.stdout.strip()

    error = result.stderr.strip()



    if output:

        return output

    if error:

        return error



    return f"Command exited with status {result.returncode}."





def runtime_status() -> str:

    return shell_output(["rocky", "status"])




def local_build_version() -> str:

    explicit = os.environ.get("ROCKY_BUILD_VERSION", "").strip()
    if explicit:
        return explicit
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        revision = result.stdout.strip()
        if revision:
            return f"rocky@{revision}"
    except Exception:
        pass
    return "rocky@local"




def _image_tag(image_ref: str) -> str | None:

    ref = image_ref.split("@", 1)[0].strip()
    if not ref:
        return None
    last_segment = ref.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        return None
    return last_segment.rsplit(":", 1)[-1].strip() or None




def docker_container_version(container: str) -> str | None:

    try:
        result = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        inspected = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(inspected, list) or not inspected:
        return None
    attrs = inspected[0] if isinstance(inspected[0], dict) else {}
    config = attrs.get("Config", {}) if isinstance(attrs, dict) else {}
    labels = config.get("Labels", {}) if isinstance(config, dict) else {}
    if isinstance(labels, dict):
        for key in (
            "org.opencontainers.image.version",
            "org.label-schema.version",
            "build_version",
            "version",
        ):
            value = str(labels.get(key) or "").strip()
            if value:
                return value[:24]
    image_ref = str(config.get("Image") or "").strip() if isinstance(config, dict) else ""
    tag = _image_tag(image_ref)
    if tag and tag.lower() != "latest":
        return tag[:24]
    image_id = str(attrs.get("Image") or "").strip() if isinstance(attrs, dict) else ""
    if image_id.startswith("sha256:"):
        digest = image_id.split(":", 1)[1][:7]
        if tag:
            return f"{tag}#{digest}"
        return f"sha256:{digest}"
    return tag[:24] if tag else None



def _short_version(value: object) -> str | None:
    text = str(value or "").strip().lstrip("v")
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d+){1,4}(?:[-+][A-Za-z0-9_.-]+)?", text)
    return (match.group(0) if match else text)[:24]



def _read_xml_value(path: Path, tag: str) -> str | None:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(rf"<\s*{re.escape(tag)}\s*>\s*([^<]+?)\s*<\s*/\s*{re.escape(tag)}\s*>", content)
    return match.group(1).strip() if match else None



def _read_bazarr_api_key(path: Path) -> str | None:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"(?im)^\s*(?:apikey|api_key)\s*[:=]\s*['\"]?([^'\"\s#]+)", content)
    return match.group(1).strip() if match else None



def _json_get(url: str, *, headers: dict[str, str] | None = None, timeout: float = 1.5) -> dict[str, object] | None:
    try:
        request = Request(url, headers=headers or {})
        with urlopen(request, timeout=timeout) as response:
            body = response.read(256 * 1024)
    except (HTTPError, URLError, TimeoutError, OSError):
        return None
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None



def service_reported_version(service_id: str, url: str | None = None) -> str | None:
    endpoints = {
        "jellyfin": ("http://127.0.0.1:8096/System/Info/Public", None, ("Version",)),
        "jellyseerr": ("http://127.0.0.1:5055/api/v1/status", None, ("version",)),
    }
    if service_id in endpoints:
        endpoint, headers, keys = endpoints[service_id]
        payload = _json_get(endpoint, headers=headers)
        if payload:
            for key in keys:
                version = _short_version(payload.get(key))
                if version:
                    return version

    arr_services = {
        "radarr": (7878, PROJECT_ROOT / "runtime" / "media-stack" / "radarr-config" / "config.xml"),
        "sonarr": (8989, PROJECT_ROOT / "runtime" / "media-stack" / "sonarr-config" / "config.xml"),
        "prowlarr": (9696, PROJECT_ROOT / "runtime" / "media-stack" / "prowlarr-config" / "config.xml"),
    }
    if service_id in arr_services:
        port, config_path = arr_services[service_id]
        api_key = _read_xml_value(config_path, "ApiKey")
        if api_key:
            payload = _json_get(f"http://127.0.0.1:{port}/api/v3/system/status?apikey={quote(api_key)}")
            if payload:
                return _short_version(payload.get("version"))

    if service_id == "bazarr":
        for config_path in (
            PROJECT_ROOT / "runtime" / "media-stack" / "bazarr-config" / "config" / "config.yaml",
            PROJECT_ROOT / "runtime" / "media-stack" / "bazarr-config" / "config.yaml",
        ):
            api_key = _read_bazarr_api_key(config_path)
            if not api_key:
                continue
            for endpoint in (
                f"http://127.0.0.1:6767/api/system/status?apikey={quote(api_key)}",
                "http://127.0.0.1:6767/api/system/status",
            ):
                headers = {"X-API-KEY": api_key} if endpoint.endswith("/status") else None
                payload = _json_get(endpoint, headers=headers)
                if payload:
                    version = _short_version(payload.get("version") or payload.get("bazarr_version"))
                    if version:
                        return version

    return None





def runtime_status_payload(*, detail: str = "summary") -> dict[str, object]:

    status = runtime_status_api.build_runtime_status()
    if detail == "full":
        return status
    return runtime_status_api.compact_runtime_status(status)





def recent_logs(lines: int = 150) -> str:

    candidates = [

        PROJECT_ROOT / "runtime" / "manager.log",

        Path("/opt/zero2w-manager/runtime/manager.log"),

    ]



    for candidate in candidates:

        if candidate.is_file():

            try:

                content = candidate.read_text(

                    encoding="utf-8",

                    errors="replace",

                )

                return "\n".join(content.splitlines()[-lines:])

            except OSError as exc:

                return f"Unable to read {candidate}: {exc}"



    return shell_output(

        [

            "journalctl",

            "-u",

            "zero2w-manager.service",

            "-n",

            str(lines),

            "--no-pager",

        ]

    )





def resolve_allowed_path(

    root_name: str,

    relative_path: str,

    *,

    for_write: bool = False,

) -> Optional[Path]:

    try:

        return WORKSPACE_REGISTRY.resolve(

            root_name,

            relative_path,

            for_write=for_write,

        )

    except WorkspaceError:

        return None





def _json_bool(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _cleanup_directory_if_empty(directory: Path) -> None:
    current = directory
    while True:
        try:
            current.rmdir()
        except OSError:
            return
        parent = current.parent
        if parent == current:
            return
        current = parent


def _remove_tree(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _atomic_write_bytes(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)

        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _stage_for_delete(target: Path) -> Path:
    DELETE_STAGING_ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
    staged = DELETE_STAGING_ROOT / f"{int(time.time() * 1000)}-{target.name}"
    os.replace(target, staged)
    return staged


def human_size(size: int) -> str:

    units = ["B", "KB", "MB", "GB"]

    value = float(size)



    for unit in units:

        if value < 1024 or unit == units[-1]:

            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"

        value /= 1024



    return f"{size} B"





def page(title: str, body: str) -> bytes:

    document = f"""<!doctype html>

<html lang="en">

<head>

<meta charset="utf-8">

<meta name="viewport" content="width=device-width, initial-scale=1">

<meta name="rocky-csrf-token" content="{html.escape(CSRF_TOKEN)}">

<title>{html.escape(title)} â Rocky</title>

<style>

@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

:root {{

    color-scheme: dark;

    font-family: 'Inter', system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;

    --bg: #060a0f;
    --bg-card: rgba(10, 16, 26, 0.85);
    --bg-section: rgba(8, 13, 22, 0.9);
    --border: rgba(0, 212, 255, 0.12);
    --border-hover: rgba(0, 212, 255, 0.55);
    --cyan: #00d4ff;
    --cyan-dim: rgba(0, 212, 255, 0.08);
    --purple: #7c3aed;
    --purple-dim: rgba(124, 58, 237, 0.1);
    --purple-border: rgba(124, 58, 237, 0.3);
    --text: #e2e8f0;
    --muted: #64748b;
    --green: #22c55e;
    --red: #ef4444;

}}

body {{

    margin: 0;

    background: var(--bg);

    background-image:
        radial-gradient(ellipse at 20% 0%, rgba(0, 212, 255, 0.04) 0%, transparent 60%),
        radial-gradient(ellipse at 80% 100%, rgba(124, 58, 237, 0.04) 0%, transparent 60%);

    color: var(--text);

    min-height: 100vh;

}}

header {{

    padding: 18px 24px;

    background: rgba(6, 10, 15, 0.95);

    border-bottom: 1px solid var(--border);

    backdrop-filter: blur(12px);

    -webkit-backdrop-filter: blur(12px);

    position: sticky;

    top: 0;

    z-index: 100;

}}

header h1 {{

    margin: 0;

    font-size: 1.2rem;

    font-weight: 600;

    letter-spacing: 0.05em;

    color: var(--cyan);

    text-transform: uppercase;

}}

nav {{

    margin-top: 10px;

}}

nav a {{

    color: var(--muted);

    margin-right: 18px;

    text-decoration: none;

    font-size: 0.8rem;

    font-weight: 500;

    letter-spacing: 0.08em;

    text-transform: uppercase;

    transition: color 0.2s;

}}

nav a:hover {{

    color: var(--cyan);

}}

/* ── App Grid ────────────────────────────────── */

.app-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 16px;
}}

.app-card {{
    position: relative;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 20px;
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    transition: border-color 0.25s, box-shadow 0.25s, transform 0.2s;
    display: flex;
    flex-direction: column;
    gap: 10px;
    cursor: default;
}}

.app-card:hover {{
    border-color: var(--border-hover);
    box-shadow: 0 0 24px rgba(0, 212, 255, 0.1), 0 4px 24px rgba(0, 0, 0, 0.4);
    transform: translateY(-2px);
}}

.app-card.media-card {{
    border-color: var(--purple-border);
}}

.app-card.media-card:hover {{
    border-color: rgba(124, 58, 237, 0.7);
    box-shadow: 0 0 24px rgba(124, 58, 237, 0.15), 0 4px 24px rgba(0, 0, 0, 0.4);
}}

.card-top {{
    display: flex;
    align-items: center;
    gap: 12px;
}}

.card-icon {{
    font-size: 1.8rem;
    line-height: 1;
    flex-shrink: 0;
}}

.card-title-block {{
    flex: 1;
    min-width: 0;
}}

.card-name {{
    font-size: 1.05rem;
    font-weight: 600;
    color: var(--text);
    margin: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}}

.card-type {{
    font-size: 0.65rem;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--muted);
    margin-top: 2px;
}}

.version-pill {{
    flex-shrink: 0;
    max-width: 96px;
    padding: 3px 7px;
    border-radius: 999px;
    border: 1px solid rgba(0, 212, 255, 0.24);
    background: rgba(0, 212, 255, 0.08);
    color: var(--cyan);
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    line-height: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}}

.media-card .version-pill {{
    border-color: rgba(124, 58, 237, 0.35);
    background: rgba(124, 58, 237, 0.12);
    color: #c4b5fd;
}}

.status-dot {{
    width: 9px;
    height: 9px;
    border-radius: 50%;
    flex-shrink: 0;
    margin-left: auto;
}}

.status-dot.running {{
    background: var(--green);
    box-shadow: 0 0 6px var(--green);
    animation: pulse-green 2s infinite;
}}

.status-dot.stopped {{
    background: var(--red);
}}

.status-dot.unknown {{
    background: var(--muted);
}}

@keyframes pulse-green {{
    0%, 100% {{ box-shadow: 0 0 4px var(--green); opacity: 1; }}
    50% {{ box-shadow: 0 0 10px var(--green); opacity: 0.7; }}
}}

.card-status-text {{
    font-size: 0.7rem;
    font-weight: 500;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-left: 4px;
}}

.card-status-text.running {{ color: var(--green); }}
.card-status-text.stopped {{ color: var(--red); }}
.card-status-text.unknown {{ color: var(--muted); }}

.card-version-line {{
    margin-left: auto;
    color: var(--muted);
    font-size: 0.66rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}}

.status-row {{
    display: flex;
    align-items: center;
    gap: 6px;
}}

.card-desc {{
    font-size: 0.82rem;
    color: var(--muted);
    margin: 0;
    line-height: 1.5;
    flex: 1;
}}

.app-actions {{
    display: flex;
    gap: 8px;
    margin-top: auto;
    padding-top: 4px;
}}

.btn-launch {{
    flex: 1;
    background: var(--cyan);
    color: #000;
    border: none;
    border-radius: 8px;
    padding: 9px 14px;
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    text-decoration: none;
    text-align: center;
    cursor: pointer;
    transition: background 0.2s, box-shadow 0.2s;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
}}

.btn-launch:hover {{
    background: #33ddff;
    box-shadow: 0 0 14px rgba(0, 212, 255, 0.5);
}}

.btn-launch.media {{
    background: var(--purple);
    color: #fff;
}}

.btn-launch.media:hover {{
    background: #9461f7;
    box-shadow: 0 0 14px rgba(124, 58, 237, 0.5);
}}

.btn-qr {{
    background: transparent;
    color: var(--cyan);
    border: 1px solid rgba(0, 212, 255, 0.35);
    border-radius: 8px;
    padding: 9px 12px;
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    cursor: pointer;
    transition: border-color 0.2s, background 0.2s;
    white-space: nowrap;
}}

.btn-qr:hover {{
    border-color: var(--cyan);
    background: var(--cyan-dim);
}}

/* ── QR Modal ────────────────────────────────── */

.qr-modal-overlay {{
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.85);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    z-index: 500;
    align-items: center;
    justify-content: center;
}}

.qr-modal-overlay.active {{
    display: flex;
}}

.qr-modal-box {{
    background: rgba(10, 16, 26, 0.98);
    border: 1px solid var(--border-hover);
    border-radius: 16px;
    padding: 32px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 16px;
    box-shadow: 0 0 60px rgba(0, 212, 255, 0.15);
    min-width: 340px;
}}

.qr-modal-box img {{
    width: 300px;
    height: 300px;
    display: block;
    background: white;
    border-radius: 8px;
}}

.qr-modal-url {{
    font-size: 0.75rem;
    color: var(--muted);
    text-align: center;
    word-break: break-all;
}}

.qr-modal-close {{
    background: transparent;
    color: var(--muted);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 20px;
    font-size: 0.78rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    cursor: pointer;
    transition: color 0.2s, border-color 0.2s;
}}

.qr-modal-close:hover {{
    color: var(--text);
    border-color: var(--cyan);
}}

/* ── Section Headers ─────────────────────────── */

.section-label {{
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    margin: 0 0 16px 0;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 10px;
}}

.section-label.cyan {{
    color: var(--cyan);
    border-color: rgba(0, 212, 255, 0.2);
}}

.section-label.purple {{
    color: var(--purple);
    border-color: var(--purple-border);
}}

.section-label::after {{
    content: '';
    flex: 1;
    height: 1px;
    background: currentColor;
    opacity: 0.15;
}}

/* ── Mode Selector Pills ─────────────────────── */

.mode-pill-row {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 12px;
}}

.mode-pill {{
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 7px 18px;
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
    cursor: pointer;
    transition: all 0.2s;
}}

.mode-pill:hover {{
    border-color: var(--cyan);
    color: var(--cyan);
    background: var(--cyan-dim);
}}

.mode-pill.active {{
    border-color: var(--cyan);
    color: var(--cyan);
    background: rgba(0, 212, 255, 0.12);
    box-shadow: 0 0 10px rgba(0, 212, 255, 0.2);
    cursor: default;
}}

/* ── Control Grid ────────────────────────────── */

.control-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 16px;
}}

label {{
    display: block;
    margin: 10px 0 4px 0;
    font-size: 0.78rem;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
}}

input, select {{
    border-radius: 8px;
    border: 1px solid rgba(83, 97, 114, 0.5);
    background: rgba(6, 10, 15, 0.8);
    color: var(--text);
    padding: 9px 12px;
    width: 100%;
    box-sizing: border-box;
    font-size: 0.85rem;
    transition: border-color 0.2s;
}}

input:focus, select:focus {{
    outline: none;
    border-color: var(--cyan);
}}

button {{
    cursor: pointer;
    font-family: inherit;
}}

.btn-apply {{
    background: var(--cyan);
    color: #000;
    border: none;
    border-radius: 8px;
    padding: 10px 20px;
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    cursor: pointer;
    transition: background 0.2s, box-shadow 0.2s;
    margin-top: 12px;
}}

.btn-apply:hover {{
    background: #33ddff;
    box-shadow: 0 0 14px rgba(0, 212, 255, 0.4);
}}

/* ── Main Layout ─────────────────────────────── */

main {{

    max-width: 1200px;

    margin: 0 auto;

    padding: 28px 24px;

}}

section {{

    background: var(--bg-section);

    border: 1px solid var(--border);

    border-radius: 14px;

    padding: 22px;

    margin-bottom: 20px;

    backdrop-filter: blur(6px);

    -webkit-backdrop-filter: blur(6px);

}}

table {{

    width: 100%;

    border-collapse: collapse;

}}

th, td {{

    padding: 10px;

    border-bottom: 1px solid var(--border);

    text-align: left;

    font-size: 0.85rem;

}}

th {{

    font-size: 0.68rem;

    font-weight: 600;

    letter-spacing: 0.12em;

    text-transform: uppercase;

    color: var(--muted);

}}

a {{

    color: var(--cyan);

    text-decoration: none;

}}

a:hover {{

    text-decoration: underline;

}}

pre {{

    overflow-x: auto;

    white-space: pre-wrap;

    word-break: break-word;

    background: rgba(6, 10, 15, 0.8);

    border: 1px solid var(--border);

    border-radius: 10px;

    padding: 14px;

    font-size: 0.82rem;

    color: var(--muted);

}}

.badge {{

    display: inline-block;

    border: 1px solid var(--border);

    border-radius: 999px;

    padding: 3px 10px;

    margin-right: 6px;

    font-size: 0.65rem;

    font-weight: 600;

    letter-spacing: 0.1em;

    text-transform: uppercase;

    color: var(--muted);

}}

.muted {{

    color: var(--muted);

}}

.metrics-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 12px;
    margin-top: 12px;
}}

.metric-card {{
    background: #0d1117;
    border: 1px solid #1e3a5f;
    border-radius: 10px;
    padding: 16px;
    text-align: center;
}}

.metric-label {{
    font-size: 0.65rem;
    letter-spacing: 0.12em;
    color: var(--accent);
    text-transform: uppercase;
    margin-bottom: 8px;
}}

.metric-value {{
    font-size: 1.05rem;
    font-weight: 600;
    color: var(--text);
}}

</style>

</head>

<body>

<header>

<h1>Rocky Developer Console</h1>

<nav>

<a href="/">Status</a>

<a href="/apps">Apps</a>

<a href="/files?root=project">Files</a>

<a href="/logs">Logs</a>

<a href="/api/runtime/status">API</a>

</nav>

</header>

<main>

{body}

</main>

</body>

</html>

"""

    return document.encode("utf-8")





def sanitize_audit_details(details: dict[str, object]) -> dict[str, object]:

    redacted_markers = ("password", "token", "key", "secret", "content")

    sanitized: dict[str, object] = {}



    for key, value in details.items():

        lower_key = key.lower()

        if any(marker in lower_key for marker in redacted_markers):

            sanitized[key] = "[redacted]"

        else:

            sanitized[key] = value



    return sanitized





def append_audit_event(action: str, **details: object) -> None:

    payload = {

        "timestamp": utc_now(),

        "action": action,

        **sanitize_audit_details(details),

    }

    encoded = json.dumps(payload, sort_keys=True)

    with AUDIT_LOCK:

        RECENT_AUDIT_EVENTS.append(dict(payload))

    print(f"AUDIT {encoded}", flush=True)

    try:

        AUDIT_LOG_PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)

        with AUDIT_LOCK:

            with AUDIT_LOG_PATH.open("a", encoding="utf-8") as handle:

                handle.write(encoded + "\n")

    except OSError:

        return





def recent_audit_events(limit: int = 20) -> list[dict[str, object]]:

    safe_limit = max(1, min(limit, 100))

    with AUDIT_LOCK:

        return list(RECENT_AUDIT_EVENTS)[-safe_limit:]


class UploadedField:
    def __init__(self, *, filename: str, data: bytes, content_type: str | None = None) -> None:
        self.filename = filename
        self.file = io.BytesIO(data)
        self.type = content_type or "application/octet-stream"
        self.value = data


class MultipartForm(dict[str, object]):
    def add(self, name: str, value: object) -> None:
        existing = self.get(name)
        if existing is None:
            self[name] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            self[name] = [existing, value]

    def getfirst(self, name: str, default: str = "") -> str:
        value = self.get(name, default)
        if isinstance(value, list):
            value = value[0] if value else default
        if isinstance(value, UploadedField):
            raw = value.value
            return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        return str(value)


def parse_multipart_form(rfile, headers, *, max_bytes: int) -> MultipartForm:
    content_type = headers.get("Content-Type", "")
    if not content_type:
        raise ValueError("missing Content-Type")
    try:
        length = int(headers.get("Content-Length", "0") or "0")
    except ValueError as exc:
        raise ValueError("invalid Content-Length") from exc
    if length < 0 or length > max_bytes:
        raise ValueError("invalid upload size")

    body = rfile.read(length)
    form = MultipartForm()
    lowered = content_type.lower()
    if lowered.startswith("application/x-www-form-urlencoded"):
        for key, values in parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True).items():
            for value in values:
                form.add(key, value)
        return form
    if not lowered.startswith("multipart/form-data"):
        raise ValueError("expected multipart/form-data")

    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + content_type.encode("utf-8")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + body
    )
    if not message.is_multipart():
        raise ValueError("invalid multipart body")

    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        data = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            form.add(name, UploadedField(filename=filename, data=data, content_type=part.get_content_type()))
        else:
            charset = part.get_content_charset() or "utf-8"
            form.add(name, data.decode(charset, errors="replace"))
    return form


class RockyConsoleHandler(BaseHTTPRequestHandler):

    server_version = "RockyConsole/0.3"



    def log_message(self, fmt: str, *args: object) -> None:

        print(

            f"{self.client_address[0]} "

            f"[{self.log_date_time_string()}] "

            f"{fmt % args}"

        )



    @property

    def client_ip(self) -> str:

        return self.client_address[0]



    def authenticated(self) -> bool:

        if not PASSWORD:

            return False

        authorization = self.headers.get("Authorization", "")

        if not authorization.startswith("Basic "):

            return False

        encoded = authorization.split(" ", 1)[1].strip()

        try:

            decoded = base64.b64decode(encoded).decode("utf-8")

        except Exception:

            return False

        supplied_user, separator, supplied_password = decoded.partition(":")

        if not separator:

            return False

        return supplied_user == USERNAME and supplied_password == PASSWORD

    def build_proxy_token(self, app_id: str, *, ttl_seconds: int = PROXY_TOKEN_TTL_SECONDS) -> str:

        issued_at = int(time.time())
        expires_at = issued_at + max(60, int(ttl_seconds))
        payload = json.dumps({"app": app_id, "exp": expires_at}, separators=(",", ":")).encode("utf-8")
        payload_b64 = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        signature = hmac.new(PROXY_TOKEN_SECRET.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
        signature_b64 = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
        return f"{payload_b64}.{signature_b64}"

    def validate_proxy_token(self, token: str, app_id: str) -> bool:

        try:
            payload_b64, signature_b64 = token.split('.', 1)
        except ValueError:
            return False

        expected_signature = hmac.new(PROXY_TOKEN_SECRET.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
        expected_signature_b64 = base64.urlsafe_b64encode(expected_signature).decode("ascii").rstrip("=")
        if not hmac.compare_digest(signature_b64, expected_signature_b64):
            return False

        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        except Exception:
            return False

        if str(payload.get("app")) != app_id:
            return False
        try:
            expires_at = int(payload.get("exp", 0))
        except Exception:
            return False
        return expires_at >= int(time.time())



    def _send_security_headers(self) -> None:

        for name, value in SECURITY_HEADERS.items():

            self.send_header(name, value)

        self.send_header("X-Rocky-CSRF", CSRF_TOKEN)



    def _send_bytes(

        self,

        status: int,

        payload: bytes,

        content_type: str,

        *,

        extra_headers: dict[str, str] | None = None,

    ) -> None:

        self.send_response(status)

        self.send_header("Content-Type", content_type)

        self.send_header("Content-Length", str(len(payload)))

        self._send_security_headers()

        if extra_headers:

            for name, value in extra_headers.items():

                self.send_header(name, value)

        self.end_headers()

        self.wfile.write(payload)



    def send_json(

        self,

        payload: dict[str, object] | list[object],

        *,

        status: int = 200,

        extra_headers: dict[str, str] | None = None,

    ) -> None:

        body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")

        self._send_bytes(

            status,

            body,

            "application/json; charset=utf-8",

            extra_headers=extra_headers,

        )



    def require_authentication(self) -> bool:

        if self.authenticated():

            return True



        payload = b"Authentication required.\n"

        self.send_response(HTTPStatus.UNAUTHORIZED)

        self.send_header("WWW-Authenticate", 'Basic realm="Rocky Console"')

        self.send_header("Content-Type", "text/plain; charset=utf-8")

        self.send_header("Content-Length", str(len(payload)))

        self._send_security_headers()

        self.end_headers()

        self.wfile.write(payload)

        return False



    def send_html(self, title: str, body: str, status: int = 200) -> None:

        payload = page(title, body)

        self._send_bytes(status, payload, "text/html; charset=utf-8")



    def send_error_page(self, status: int, message: str) -> None:

        self.send_html(

            HTTPStatus(status).phrase,

            f"<section><h2>{html.escape(message)}</h2></section>",

            status=status,

        )



    def require_rate_limit(self, action: str, *, limit: int = RATE_LIMIT_DEFAULT) -> bool:

        allowed, retry_after = RATE_LIMITER.allow(self.client_ip, action, limit)

        if allowed:

            return True



        append_audit_event(

            "RATE_LIMIT",

            actor=USERNAME,

            client_ip=self.client_ip,

            action_name=action,

            retry_after=retry_after,

        )

        self.send_json(

            {

                "error": "rate_limited",

                "action": action,

                "retry_after": retry_after,

            },

            status=HTTPStatus.TOO_MANY_REQUESTS,

            extra_headers={"Retry-After": str(retry_after)},

        )

        return False



    def require_request_size_allowed(self) -> bool:

        header = self.headers.get("Content-Length")

        if not header:

            return True



        try:

            length = int(header)

        except ValueError:

            self.send_json(

                {"error": "invalid_content_length"},

                status=HTTPStatus.BAD_REQUEST,

            )

            return False



        if length > MAX_REQUEST_BYTES:

            append_audit_event(

                "REQUEST_TOO_LARGE",

                actor=USERNAME,

                client_ip=self.client_ip,

                content_length=length,

                limit=MAX_REQUEST_BYTES,

                path=self.path,

            )

            self.send_json(

                {

                    "error": "request_too_large",

                    "limit_bytes": MAX_REQUEST_BYTES,

                    "content_length": length,

                },

                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,

            )

            return False



        return True



    def require_csrf(self) -> bool:

        supplied = self.headers.get("X-Rocky-CSRF", "")

        if secrets.compare_digest(supplied, CSRF_TOKEN):

            return True



        append_audit_event(

            "CSRF_REJECTED",

            actor=USERNAME,

            client_ip=self.client_ip,

            path=self.path,

        )

        self.send_json(

            {"error": "invalid_csrf"},

            status=HTTPStatus.FORBIDDEN,

        )

        return False



    def send_json_error(self, status: int, error: str, **details: object) -> None:
        payload: dict[str, object] = {"error": error}
        payload.update(details)
        self.send_json(payload, status=status)

    def parse_json_body(self) -> dict[str, object] | None:
        length_header = self.headers.get("Content-Length", "0")
        try:
            length = int(length_header)
        except ValueError:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "invalid_content_length")
            return None

        try:
            raw = self.rfile.read(length)
        except Exception as exc:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "unable_to_read_body", detail=str(exc))
            return None

        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "invalid_json", detail=str(exc))
            return None

        if not isinstance(value, dict):
            self.send_json_error(HTTPStatus.BAD_REQUEST, "invalid_json_root")
            return None

        return value

    def parse_upload_form(self) -> tuple[MultipartForm, UploadedField | None]:
        try:
            form = parse_multipart_form(self.rfile, self.headers, max_bytes=MAX_REQUEST_BYTES)
        except ValueError as exc:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "invalid_upload_form", detail=str(exc))
            return MultipartForm(), None
        file_field = form["file"] if "file" in form else None
        return form, file_field if isinstance(file_field, UploadedField) else None

    def resolve_workspace_target(
        self,
        root_name: str,
        relative_path: str,
        *,
        for_write: bool = False,
        must_exist: bool = False,
    ) -> Path | None:
        target = resolve_allowed_path(root_name, relative_path, for_write=for_write)
        if target is None:
            self.send_json_error(HTTPStatus.FORBIDDEN, "path_outside_workspace")
            return None
        if must_exist and not target.exists():
            self.send_json_error(HTTPStatus.NOT_FOUND, "path_not_found")
            return None
        return target

    def audit_success(self, action: str, **details: object) -> None:
        append_audit_event(action, actor=USERNAME, client_ip=self.client_ip, result="ok", **details)

    def audit_failure(self, action: str, **details: object) -> None:
        append_audit_event(action, actor=USERNAME, client_ip=self.client_ip, result="error", **details)

    def handle_mkdir(self, request: dict[str, object]) -> None:
        root_name = str(request.get("root", "")).strip()
        relative_path = str(request.get("path", "")).strip()

        if not root_name or not relative_path:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "root_and_path_required")
            return

        target = self.resolve_workspace_target(root_name, relative_path, for_write=True)
        if target is None:
            return

        if target.exists():
            self.audit_failure("MKDIR", workspace=root_name, path=relative_path, reason="already_exists")
            self.send_json_error(HTTPStatus.CONFLICT, "path_already_exists")
            return

        try:
            target.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            self.audit_failure("MKDIR", workspace=root_name, path=relative_path, reason=str(exc))
            self.send_json_error(HTTPStatus.BAD_REQUEST, "mkdir_failed", detail=str(exc))
            return

        self.audit_success("MKDIR", workspace=root_name, path=relative_path)
        self.send_json({"ok": True, "root": root_name, "path": relative_path})

    def handle_rename(self, request: dict[str, object]) -> None:
        root_name = str(request.get("root", "")).strip()
        old_path = str(request.get("path", "")).strip()
        new_path = str(request.get("new_path", "")).strip()

        if not root_name or not old_path or not new_path:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "root_path_new_path_required")
            return

        source = self.resolve_workspace_target(root_name, old_path, for_write=True, must_exist=True)
        if source is None:
            return
        destination = self.resolve_workspace_target(root_name, new_path, for_write=True)
        if destination is None:
            return

        if destination.exists():
            self.audit_failure("RENAME", workspace=root_name, path=old_path, new_path=new_path, reason="destination_exists")
            self.send_json_error(HTTPStatus.CONFLICT, "destination_exists")
            return

        destination.parent.mkdir(parents=True, exist_ok=True)

        try:
            os.replace(source, destination)
        except OSError as exc:
            self.audit_failure("RENAME", workspace=root_name, path=old_path, new_path=new_path, reason=str(exc))
            self.send_json_error(HTTPStatus.BAD_REQUEST, "rename_failed", detail=str(exc))
            return

        self.audit_success("RENAME", workspace=root_name, path=old_path, new_path=new_path)
        self.send_json({"ok": True, "root": root_name, "path": old_path, "new_path": new_path})

    def handle_delete(self, request: dict[str, object]) -> None:
        root_name = str(request.get("root", "")).strip()
        relative_path = str(request.get("path", "")).strip()

        if not root_name or not relative_path:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "root_and_path_required")
            return

        target = self.resolve_workspace_target(root_name, relative_path, for_write=True, must_exist=True)
        if target is None:
            return

        try:
            staged = _stage_for_delete(target)
            _remove_tree(staged)
        except OSError as exc:
            self.audit_failure("DELETE", workspace=root_name, path=relative_path, reason=str(exc))
            self.send_json_error(HTTPStatus.BAD_REQUEST, "delete_failed", detail=str(exc))
            return

        self.audit_success("DELETE", workspace=root_name, path=relative_path)
        self.send_json({"ok": True, "root": root_name, "path": relative_path})

    def handle_upload(self) -> None:
        form, file_field = self.parse_upload_form()
        root_name = form.getfirst("root", "").strip()
        relative_path = form.getfirst("path", "").strip()
        overwrite = _json_bool(form.getfirst("overwrite", "false"))

        if not root_name or not relative_path:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "root_and_path_required")
            return
        if file_field is None or not getattr(file_field, "filename", ""):
            self.send_json_error(HTTPStatus.BAD_REQUEST, "file_required")
            return

        destination = self.resolve_workspace_target(root_name, relative_path, for_write=True)
        if destination is None:
            return

        if destination.exists() and destination.is_dir():
            self.send_json_error(HTTPStatus.BAD_REQUEST, "destination_is_directory")
            return
        if destination.exists() and not overwrite:
            self.audit_failure("UPLOAD", workspace=root_name, path=relative_path, reason="destination_exists")
            self.send_json_error(HTTPStatus.CONFLICT, "destination_exists")
            return

        try:
            payload = file_field.file.read()
            if not isinstance(payload, bytes):
                payload = payload.encode("utf-8")
            _atomic_write_bytes(destination, payload)
        except OSError as exc:
            self.audit_failure("UPLOAD", workspace=root_name, path=relative_path, reason=str(exc))
            self.send_json_error(HTTPStatus.BAD_REQUEST, "upload_failed", detail=str(exc))
            return

        self.audit_success("UPLOAD", workspace=root_name, path=relative_path, filename=file_field.filename, bytes_written=len(payload), overwrite=overwrite)
        self.send_json({"ok": True, "root": root_name, "path": relative_path, "bytes_written": len(payload)})

    def do_GET(self) -> None:

        request = urlparse(self.path)

        if request.path.startswith("/proxy/"):

            self.handle_proxy(request, method="GET")

            return

        if not self.require_authentication():

            return

        if request.path == "/":

            self.handle_status()

        elif request.path == "/logs":

            self.handle_logs()

        elif request.path == "/apps":

            self.handle_apps()

        elif request.path.startswith("/proxy/"):

            self.handle_proxy(request, method="GET")

        elif request.path == "/files":

            self.handle_files(parse_qs(request.query))

        elif request.path == "/view":

            self.handle_view(parse_qs(request.query))

        elif request.path == "/download":

            self.handle_download(parse_qs(request.query))

        elif request.path == "/api/runtime/status":

            self.handle_api_runtime_status()

        elif request.path == "/api/security/csrf":

            self.handle_api_csrf()

        elif request.path == "/api/mode/config":

            self.handle_api_mode_config()

        elif request.path == "/api/network/config":

            self.handle_api_network_config()

        elif request.path == "/api/transfer/config":

            self.handle_api_transfer_config()

        elif request.path == "/api/storage/destinations":

            self.handle_api_storage_destinations()

        elif request.path == "/api/audit/recent":

            self.handle_api_audit_recent(parse_qs(request.query))

        else:

            self.send_error_page(404, "Page not found")



    def do_POST(self) -> None:
        request = urlparse(self.path)

        if request.path.startswith("/proxy/"):

            if not self.require_request_size_allowed():

                return

            if not self.require_rate_limit("write", limit=RATE_LIMIT_WRITE):

                return

            self.handle_proxy(request, method="POST")

            return

        if not self.require_authentication():

            return

        if not self.require_request_size_allowed():

            return

        if not self.require_rate_limit("write", limit=RATE_LIMIT_WRITE):

            return

        if not self.require_csrf():

            return

        if request.path == "/api/mode/select":

            if not self.require_csrf():
                return

            payload = self.parse_json_body()
            if payload is None:
                return

            self.handle_update_mode_config(payload)
            return

        if request.path == "/api/apps/launch":
            payload = self.parse_json_body()
            if payload is None:
                return
            self.handle_app_launch(payload)
            return
        if request.path == "/api/security/validate":

            append_audit_event(

                "CSRF_VALIDATE",

                actor=USERNAME,

                client_ip=self.client_ip,

                path=request.path,

                result="accepted",

            )

            self.send_json(

                {

                    "ok": True,

                    "csrf_valid": True,

                    "max_request_bytes": MAX_REQUEST_BYTES,

                }

            )

            return

        if request.path == "/api/network/config":

            payload = self.parse_json_body()

            if payload is None:

                return

            self.handle_update_network_config(payload)

            return

        if request.path == "/api/transfer/config":

            payload = self.parse_json_body()

            if payload is None:

                return

            self.handle_update_transfer_config(payload)

            return

        if request.path == "/api/runtime/docker/action":

            payload = self.parse_json_body()

            if payload is None:

                return

            self.handle_runtime_docker_action(payload)

            return

        if request.path == "/api/runtime/managed-service/action":

            payload = self.parse_json_body()

            if payload is None:

                return

            self.handle_runtime_managed_service_action(payload)

            return

        if request.path == "/mkdir":

            payload = self.parse_json_body()

            if payload is None:

                return

            self.handle_mkdir(payload)

            return

        if request.path == "/rename":

            payload = self.parse_json_body()

            if payload is None:

                return

            self.handle_rename(payload)

            return

        if request.path == "/delete":

            payload = self.parse_json_body()

            if payload is None:

                return

            self.handle_delete(payload)

            return

        if request.path == "/upload":

            self.handle_upload()

            return



        append_audit_event(

            "POST_REJECTED",

            actor=USERNAME,

            client_ip=self.client_ip,

            path=request.path,

            result="unknown_endpoint",

        )

        self.send_json(

            {"error": "unknown_endpoint", "path": request.path},

            status=HTTPStatus.NOT_FOUND,

        )



    def handle_api_runtime_status(self) -> None:

        if not self.require_rate_limit("api-status"):

            return

        query = parse_qs(urlparse(self.path).query)
        detail = str((query.get("detail") or ["summary"])[0]).strip().lower()
        if detail not in {"summary", "full"}:
            self.send_json_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_runtime_status_detail",
                detail="detail must be summary or full",
            )
            return

        self.send_json(runtime_status_payload(detail=detail))



    def handle_api_csrf(self) -> None:

        if not self.require_rate_limit("api-csrf"):

            return

        self.send_json(

            {

                "csrf_token": CSRF_TOKEN,

                "header_name": "X-Rocky-CSRF",

                "max_request_bytes": MAX_REQUEST_BYTES,

            }

        )



    def load_mode_catalog_snapshot(self) -> dict[str, object]:

        try:
            if MODE_CATALOG_PATH.is_file():
                data = json.loads(MODE_CATALOG_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {"version": 1, "modes": []}

    def load_current_mode_request_snapshot(self) -> dict[str, object]:

        try:
            if CURRENT_MODE_REQUEST_PATH.is_file():
                data = json.loads(CURRENT_MODE_REQUEST_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {"version": 1, "selected_mode_id": "safe", "override_flags": {}}

    def handle_api_mode_config(self) -> None:

        if not self.require_rate_limit("api-mode-config"):
            return

        current = self.load_current_mode_request_snapshot()
        catalog = self.load_mode_catalog_snapshot()
        published_mode: dict[str, object] = {}
        state_path = RUNTIME_ROOT / "state.json"
        try:
            if state_path.is_file():
                runtime_payload = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(runtime_payload, dict):
                    mode_payload = runtime_payload.get("mode")
                    if isinstance(mode_payload, dict):
                        published_mode = mode_payload
        except Exception:
            published_mode = {}
        self.send_json(
            {
                "ok": True,
                "current_mode": current,
                "modes": catalog.get("modes", []),
                "published_mode": published_mode,
                "sources": {
                    "catalog": str(MODE_CATALOG_PATH),
                    "current": str(CURRENT_MODE_REQUEST_PATH),
                },
            }
        )

    def handle_update_mode_config(self, payload: dict[str, object]) -> None:

        selected_mode_id = str(payload.get("selected_mode_id") or "").strip()
        if not selected_mode_id:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "invalid_mode_request", detail="selected_mode_id is required")
            return

        existing = self.load_current_mode_request_snapshot()
        updated = {
            "version": int(existing.get("version", 1)),
            "selected_mode_id": selected_mode_id,
            "previous_mode_id": str(existing.get("selected_mode_id") or "safe"),
            "requested_at": utc_now(),
            "requested_by": USERNAME,
            "reason": str(payload.get("reason") or "browser_mode_switch"),
            "override_flags": payload.get("override_flags") if isinstance(payload.get("override_flags"), dict) else existing.get("override_flags", {}),
        }

        try:
            write_mode_config_json(CURRENT_MODE_REQUEST_PATH, updated)
        except OSError as exc:
            self.send_json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "mode_config_write_failed", detail=str(exc))
            return

        append_audit_event(
            "MODE_UPDATED",
            actor=USERNAME,
            client_ip=self.client_ip,
            path=self.path,
        )
        self.send_json({"ok": True, "current_mode": updated, "source": str(CURRENT_MODE_REQUEST_PATH)})

    def apply_app_activation_mode(self, service: dict[str, object], app_id: str) -> dict[str, object] | None:
        target_mode = str(service.get("activate_mode") or "").strip()
        if not target_mode:
            return None
        existing = self.load_current_mode_request_snapshot()
        current_mode = str(existing.get("selected_mode_id") or "safe")
        updated = {
            "version": int(existing.get("version", 1)),
            "selected_mode_id": target_mode,
            "previous_mode_id": current_mode,
            "requested_at": utc_now(),
            "requested_by": USERNAME,
            "reason": f"launcher_open:{app_id}",
            "override_flags": existing.get("override_flags", {}) if isinstance(existing.get("override_flags"), dict) else {},
        }
        write_mode_config_json(CURRENT_MODE_REQUEST_PATH, updated)
        return updated

    def handle_api_network_config(self) -> None:

        if not self.require_rate_limit("api-network-config"):

            return

        config = network_transfer_runtime.NetworkTransferConfig().snapshot()

        self.send_json(

            {

                "privacy_relay": config.get("privacy_relay", {}),

                "vpn": config.get("vpn", {}),

                "remote_access": config.get("remote_access", {}),

                "source": str(network_transfer_runtime.DEFAULT_NETWORK_TRANSFER_CONFIG_PATH),

            }

        )


    def handle_api_transfer_config(self) -> None:

        if not self.require_rate_limit("api-transfer-config"):

            return

        config = network_transfer_runtime.NetworkTransferConfig().snapshot()

        self.send_json(

            {

                "transfer": config.get("transfer", {}),

                "source": str(network_transfer_runtime.DEFAULT_NETWORK_TRANSFER_CONFIG_PATH),

            }

        )


    def handle_api_storage_destinations(self) -> None:

        if not self.require_rate_limit("api-storage-destinations"):

            return

        runtime_payload = runtime_status_payload()
        published_state = runtime_payload.get("published", {}).get("state")
        if isinstance(published_state, dict):
            storage_state = published_state.get("storage")
            if isinstance(storage_state, dict):
                self.send_json(storage_state)
                return

        config = network_transfer_runtime.NetworkTransferConfig().snapshot()

        destination_id = str(
            config.get("transfer", {}).get("destination_id", "internal")
        )

        storage_state = network_transfer_runtime.StorageManager().state_snapshot(
            selected_destination_id=destination_id,
        )

        self.send_json(storage_state)


    def handle_update_network_config(self, payload: dict[str, object]) -> None:

        try:
            config = network_transfer_runtime.NetworkTransferConfig()
            updated = config.update_network(payload)
        except ValueError as exc:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "invalid_network_config", detail=str(exc))
            return
        except OSError as exc:
            self.send_json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "network_config_write_failed", detail=str(exc))
            return

        append_audit_event(
            "NETWORK_CONFIG_UPDATED",
            actor=USERNAME,
            client_ip=self.client_ip,
            path=self.path,
        )
        self.send_json(
            {
                "ok": True,
                "privacy_relay": updated.get("privacy_relay", {}),
                "vpn": updated.get("vpn", {}),
                "remote_access": updated.get("remote_access", {}),
                "source": str(network_transfer_runtime.DEFAULT_NETWORK_TRANSFER_CONFIG_PATH),
            }
        )


    def handle_update_transfer_config(self, payload: dict[str, object]) -> None:

        try:
            config = network_transfer_runtime.NetworkTransferConfig()
            updated = config.update_transfer(payload)
        except ValueError as exc:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "invalid_transfer_config", detail=str(exc))
            return
        except OSError as exc:
            self.send_json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "transfer_config_write_failed", detail=str(exc))
            return

        append_audit_event(
            "TRANSFER_CONFIG_UPDATED",
            actor=USERNAME,
            client_ip=self.client_ip,
            path=self.path,
        )
        self.send_json(
            {
                "ok": True,
                "transfer": updated.get("transfer", {}),
                "source": str(network_transfer_runtime.DEFAULT_NETWORK_TRANSFER_CONFIG_PATH),
            }
        )

    def handle_runtime_docker_action(self, payload: dict[str, object]) -> None:

        name = str(payload.get("name") or "").strip()
        action = str(payload.get("action") or "").strip().lower()
        allowed_actions = {"start", "stop", "restart"}

        if not name:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "invalid_docker_request", detail="name is required")
            return
        if action not in allowed_actions:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "invalid_docker_request", detail="action must be start, stop, or restart")
            return

        runtime = runtime_status_payload()
        observed = runtime.get("observed", {}) if isinstance(runtime, dict) else {}
        docker_state = observed.get("docker", {}) if isinstance(observed, dict) else {}
        items = docker_state.get("items", []) if isinstance(docker_state, dict) else []
        known_names = {
            str(item.get("name")).strip()
            for item in items
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        }

        if name not in known_names:
            self.send_json_error(HTTPStatus.NOT_FOUND, "docker_container_unknown", detail=name)
            return

        try:
            result = subprocess.run(
                ["docker", action, name],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            self.send_json_error(HTTPStatus.BAD_GATEWAY, "docker_action_failed", detail=str(exc))
            return

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"docker {action} failed"
            self.send_json_error(HTTPStatus.BAD_GATEWAY, "docker_action_failed", detail=detail)
            return

        append_audit_event(
            "DOCKER_ACTION",
            actor=USERNAME,
            client_ip=self.client_ip,
            path=self.path,
            container=name,
            action=action,
        )
        self.send_json(
            {
                "ok": True,
                "name": name,
                "action": action,
                "stdout": (result.stdout or "").strip(),
                "stderr": (result.stderr or "").strip(),
            }
        )

    def handle_runtime_managed_service_action(self, payload: dict[str, object]) -> None:

        service_id = str(payload.get("id") or "").strip()
        action = str(payload.get("action") or "").strip().lower()
        allowed_actions = {"start", "stop", "restart"}

        if not service_id:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "invalid_managed_service_request", detail="id is required")
            return
        if action not in allowed_actions:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "invalid_managed_service_request", detail="action must be start, stop, or restart")
            return

        catalog = ServiceCatalog()
        service = catalog.get(service_id)
        if not isinstance(service, dict):
            self.send_json_error(HTTPStatus.NOT_FOUND, "managed_service_unknown", detail=service_id)
            return

        unit = str(service.get("systemd_service") or "").strip()
        user_unit = str(service.get("systemd_user_service") or "").strip()
        if not unit and not user_unit:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "managed_service_uncontrollable", detail=f"{service_id} is not backed by systemd")
            return
        systemctl_cmd = ["systemctl", "--user", action, user_unit] if user_unit else ["systemctl", action, unit]

        try:
            result = subprocess.run(
                systemctl_cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            self.send_json_error(HTTPStatus.BAD_GATEWAY, "managed_service_action_failed", detail=str(exc))
            return

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"systemctl {action} failed"
            self.send_json_error(HTTPStatus.BAD_GATEWAY, "managed_service_action_failed", detail=detail)
            return

        append_audit_event(
            "MANAGED_SERVICE_ACTION",
            actor=USERNAME,
            client_ip=self.client_ip,
            path=self.path,
            service_id=service_id,
            unit=unit,
            action=action,
        )
        self.send_json(
            {
                "ok": True,
                "id": service_id,
                "unit": unit,
                "action": action,
                "stdout": (result.stdout or "").strip(),
                "stderr": (result.stderr or "").strip(),
            }
        )


    def handle_api_audit_recent(self, query: dict[str, list[str]]) -> None:

        if not self.require_rate_limit("api-audit"):

            return

        raw_limit = query.get("limit", ["20"])[0]

        try:

            limit = int(raw_limit)

        except ValueError:

            self.send_json({"error": "invalid_limit"}, status=HTTPStatus.BAD_REQUEST)

            return

        events = recent_audit_events(limit)

        self.send_json(

            {

                "events": events,

                "limit": max(1, min(limit, 100)),

                "source": "in-memory audit ring buffer (live process)",

            }

        )

    def console_origin(self) -> str:

        host = self.headers.get("Host", f"{socket.gethostname()}:{PORT}")
        parsed = urlparse(f"http://{host}")
        return f"{parsed.scheme}://{parsed.netloc}"

    def preferred_console_base(self) -> str:

        configured = PUBLIC_BASE_URL.rstrip("/")
        if configured:
            parsed = urlparse(configured if "://" in configured else f"http://{configured}")
            if parsed.netloc:
                base_path = parsed.path.rstrip("/")
                return urlunparse((parsed.scheme or "http", parsed.netloc, base_path, "", "", ""))
        return self.console_origin()

    def proxy_public_url(self, app_id: str) -> str:

        return self.preferred_console_base().rstrip("/") + f"/proxy/{quote(app_id)}/"

    def publicize_service_url(self, raw_url: str) -> str:

        parsed = urlparse(raw_url)
        console = urlparse(self.console_origin())
        hostname = parsed.hostname or console.hostname or socket.gethostname()
        if hostname in {"127.0.0.1", "localhost"}:
            hostname = console.hostname or hostname
        netloc = hostname
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urlunparse((parsed.scheme or console.scheme or "http", netloc, parsed.path or "/", "", parsed.query, parsed.fragment))

    def qr_image_url(self, target_url: str) -> str:

        return "https://api.qrserver.com/v1/create-qr-code/?size=180x180&data=" + quote(target_url, safe="")

    def proxy_base_for_app(self, app_id: str) -> str | None:

        if app_id == "transfer-stack":
            return "http://127.0.0.1:8088"
        service = ServiceCatalog().get(app_id)
        if not service:
            return None
        raw_url = service.get("url")
        if not isinstance(raw_url, str) or not raw_url:
            return None
        return raw_url

    def token_authorized_proxy(self, request, app_id: str) -> bool:

        supplied = parse_qs(request.query).get("access_token", [""])[0]
        if bool(supplied) and self.validate_proxy_token(supplied, app_id):
            return True

        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return False
        try:
            cookie = SimpleCookie()
            cookie.load(cookie_header)
        except Exception:
            return False
        morsel = cookie.get(f"rocky_proxy_{app_id}")
        if morsel is None:
            return False
        return self.validate_proxy_token(morsel.value, app_id)

    def _tcp_port_open(self, port: int, host: str = "127.0.0.1", timeout: float = 0.8) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _media_launch_target(self, app_id: str) -> dict[str, object] | None:
        return media_launch_target(app_id)

    def _tokenized_media_url(self, app_id: str) -> str:
        return self.proxy_public_url(app_id) + f"?access_token={quote(self.build_proxy_token(app_id))}"

    def _docker_status(self, container: str) -> str:
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", container],
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
        except Exception:
            return "unknown"
        if result.returncode != 0:
            return "missing"
        return result.stdout.strip() or "unknown"

    def _start_media_app(self, app_id: str) -> dict[str, object]:
        target = self._media_launch_target(app_id)
        if not target:
            return {"ok": False, "error": "unknown_media_app", "app_id": app_id}

        port = int(target["port"])
        container = str(target["container"])
        compose_service = str(target["compose_service"])
        compose_dir = PROJECT_ROOT / "runtime" / "media-stack"
        compose_file = compose_dir / "docker-compose.yml"
        open_url = self._tokenized_media_url(app_id)

        if self._tcp_port_open(port):
            return {"ok": True, "app_id": app_id, "status": "running", "already_running": True, "open_url": open_url}

        # Radarr/Sonarr/Bazarr can exit cleanly because stale pid files survive an earlier crash.
        if compose_dir.exists():
            try:
                for pid_file in compose_dir.glob(f"{app_id}-config/**/*.pid"):
                    pid_file.unlink(missing_ok=True)
            except OSError:
                pass

        started_by = "none"
        details: list[str] = []
        if compose_file.is_file():
            command = ["docker", "compose", "-f", str(compose_file), "up", "-d", compose_service]
            try:
                result = subprocess.run(command, cwd=str(compose_dir), capture_output=True, text=True, check=False, timeout=45)
                started_by = "docker_compose"
                details.append((result.stdout or result.stderr or "").strip())
                if result.returncode != 0:
                    started_by = "docker_compose_failed"
            except Exception as exc:
                details.append(f"docker compose failed: {exc}")
        else:
            try:
                result = subprocess.run(["docker", "start", container], capture_output=True, text=True, check=False, timeout=20)
                started_by = "docker_start"
                details.append((result.stdout or result.stderr or "").strip())
                if result.returncode != 0:
                    started_by = "docker_start_failed"
            except Exception as exc:
                details.append(f"docker start failed: {exc}")

        deadline = time.time() + 12
        while time.time() < deadline:
            if self._tcp_port_open(port):
                return {
                    "ok": True,
                    "app_id": app_id,
                    "status": "running",
                    "already_running": False,
                    "started_by": started_by,
                    "open_url": open_url,
                    "detail": "\n".join(d for d in details if d),
                }
            time.sleep(1)

        if started_by in {"docker_compose", "docker_start"}:
            return {
                "ok": True,
                "app_id": app_id,
                "status": "starting",
                "already_running": False,
                "started_by": started_by,
                "open_url": open_url,
                "detail": "\n".join(d for d in details if d),
            }

        return {
            "ok": False,
            "app_id": app_id,
            "status": self._docker_status(container),
            "started_by": started_by,
            "open_url": open_url,
            "error": "service_port_not_ready",
            "detail": "\n".join(d for d in details if d),
        }

    def handle_app_launch(self, request: dict[str, object]) -> None:
        app_id = str(request.get("id") or request.get("app_id") or "").strip()
        if not app_id:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "app_id_required")
            return

        media_target = self._media_launch_target(app_id)
        if media_target:
            service = ServiceCatalog().get(app_id) or {"activate_mode": "entertainment"}
            if not str(service.get("activate_mode") or "").strip():
                service["activate_mode"] = "entertainment"
            try:
                self.apply_app_activation_mode(service, app_id)
            except OSError as exc:
                self.send_json_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "mode_activation_failed",
                    app_id=app_id,
                    detail=str(exc),
                    source=str(CURRENT_MODE_REQUEST_PATH),
                )
                return
            payload = self._start_media_app(app_id)
            payload["open_url"] = self._tokenized_media_url(app_id)
            self.send_json(payload, status=HTTPStatus.OK if payload.get("ok") else HTTPStatus.BAD_GATEWAY)
            return

        service = ServiceCatalog().get(app_id)
        if not service:
            self.send_json_error(HTTPStatus.NOT_FOUND, "unknown_app", app_id=app_id)
            return

        unit = str(service.get("systemd_service") or service.get("service") or "").strip()
        user_unit = str(service.get("systemd_user_service") or "").strip()
        container = str(service.get("docker_container") or "").strip()
        raw_url = service.get("url")
        open_url = self.publicize_service_url(str(raw_url)) if isinstance(raw_url, str) and raw_url else None

        actions: list[str] = []
        mode_request: dict[str, object] | None = None
        try:
            mode_request = self.apply_app_activation_mode(service, app_id)
        except OSError as exc:
            self.send_json_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "mode_activation_failed",
                app_id=app_id,
                detail=str(exc),
                source=str(CURRENT_MODE_REQUEST_PATH),
            )
            return
        if mode_request is not None:
            actions.append(
                f"requested mode {mode_request.get('selected_mode_id')} via {CURRENT_MODE_REQUEST_PATH}"
            )

        ok = True
        if unit:
            result = subprocess.run(["systemctl", "start", unit], capture_output=True, text=True, check=False, timeout=20)
            ok = ok and result.returncode == 0
            actions.append((result.stdout or result.stderr or f"systemctl start {unit}: {result.returncode}").strip())
        elif user_unit:
            result = subprocess.run(["systemctl", "--user", "start", user_unit], capture_output=True, text=True, check=False, timeout=20)
            ok = ok and result.returncode == 0
            actions.append((result.stdout or result.stderr or f"systemctl --user start {user_unit}: {result.returncode}").strip())
        elif container:
            status = self._docker_status(container)
            if status != "running":
                result = subprocess.run(["docker", "start", container], capture_output=True, text=True, check=False, timeout=20)
                ok = ok and result.returncode == 0
                actions.append((result.stdout or result.stderr or f"docker start {container}: {result.returncode}").strip())
        else:
            actions.append("no managed service/container for this app; opening URL only")

        self.send_json({"ok": ok, "app_id": app_id, "open_url": open_url, "detail": "\n".join(a for a in actions if a)})

    def apps_payload(self) -> dict[str, object]:

        runtime = runtime_status_payload(detail="full")
        published = runtime.get("published", {}).get("state", {})
        transfer_state = published.get("transfer", {}) if isinstance(published, dict) else {}
        published_web_services = published.get("web_services", {}) if isinstance(published, dict) else {}
        published_web_cache = published.get("web_service_cache", {}) if isinstance(published, dict) else {}
        active_app = published.get("application", {}).get("active_id") if isinstance(published, dict) else None
        config = network_transfer_runtime.NetworkTransferConfig().snapshot()
        entries: list[dict[str, object]] = []
        version_cache: dict[str, str | None] = {}
        app_version_cache: dict[str, str | None] = {}
        catalog = ServiceCatalog()

        for service in catalog.menu_services():
            app_id = str(service.get("id"))
            service_type = str(service.get("type", "application"))
            status = "ready"
            live_service = published_web_services.get(app_id) if isinstance(published_web_services, dict) else None
            cached_service = published_web_cache.get(app_id) if isinstance(published_web_cache, dict) else None
            if service_type == "background_service":
                unit = str(service.get("systemd_service") or service.get("service") or "")
                container = str(service.get("docker_container") or "")
                if isinstance(live_service, dict) and isinstance(live_service.get("active"), bool):
                    status = "running" if live_service.get("active") else "stopped"
                elif unit:
                    state = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, check=False, timeout=5).stdout.strip()
                    status = state or "unknown"
                elif str(service.get("systemd_user_service") or "").strip():
                    user_unit = str(service.get("systemd_user_service") or "").strip()
                    state = subprocess.run(["systemctl", "--user", "is-active", user_unit], capture_output=True, text=True, check=False, timeout=5).stdout.strip()
                    status = state or "unknown"
                elif container:
                    inspect = subprocess.run(["docker", "inspect", "--format", "{{.State.Status}}", container], capture_output=True, text=True, check=False, timeout=5)
                    status = inspect.stdout.strip() or "stopped"
                else:
                    status = "unknown"
            elif active_app == app_id:
                status = "running"
            raw_url = service.get("url")
            public_url = self.publicize_service_url(str(raw_url)) if isinstance(raw_url, str) and raw_url else None
            tokenized_url = None
            if isinstance(live_service, dict):
                tokenized_url = str(live_service.get("tokenized_proxy_url") or "").strip() or None
                public_url = str(live_service.get("url") or public_url or "").strip() or public_url
            if tokenized_url is None and isinstance(cached_service, dict):
                tokenized_url = str(cached_service.get("last_tokenized_proxy_url") or "").strip() or None
                if public_url is None:
                    cached_public = str(cached_service.get("last_url") or "").strip()
                    public_url = cached_public or public_url
            if tokenized_url is None and public_url:
                tokenized_url = self.proxy_public_url(app_id) + f"?access_token={quote(self.build_proxy_token(app_id))}"
            version = local_build_version()
            if app_id not in app_version_cache:
                app_version_cache[app_id] = service_reported_version(app_id, str(raw_url) if isinstance(raw_url, str) else None)
            reported_version = app_version_cache.get(app_id)
            if reported_version:
                version = reported_version
            container = str(service.get("docker_container") or "")
            if container:
                if container not in version_cache:
                    version_cache[container] = docker_container_version(container)
                version = reported_version or version_cache.get(container) or version
            entries.append({
                "id": app_id,
                "name": str(service.get("name", app_id.title())),
                "description": str(service.get("description", "")),
                "status": status,
                "type": service_type,
                "version": version,
                "open_url": public_url,
                "mobile_url": tokenized_url or public_url,
                "qr_url": self.qr_image_url(tokenized_url or public_url) if (tokenized_url or public_url) else None,
            })

        downloader = transfer_state.get("downloader", {}) if isinstance(transfer_state, dict) else {}
        web_ui_url = downloader.get("web_ui_url") if isinstance(downloader, dict) else None
        if isinstance(web_ui_url, str) and web_ui_url:
            proxy_url = self.proxy_public_url("transfer-stack")
            tokenized_proxy_url = proxy_url + f"?access_token={quote(self.build_proxy_token('transfer-stack'))}"
            entries.append({
                "id": "transfer-stack",
                "name": "Transfer Stack",
                "description": "Gluetun + qBittorrent downloader stack",
                "status": "running" if transfer_state.get("healthy") else "degraded",
                "type": "web_interface",
                "version": local_build_version(),
                "open_url": tokenized_proxy_url,
                "mobile_url": tokenized_proxy_url,
                "qr_url": self.qr_image_url(tokenized_proxy_url),
            })

        # Always inject media stack services regardless of menu_visible flag
        existing_ids = {str(e.get("id")) for e in entries}
        for app_id, spec in MEDIA_STACK_APPS.items():
            if app_id in existing_ids:
                continue
            service = catalog.get(app_id) or {}
            container = str(service.get("docker_container") or spec["container"])
            raw_url = str(service.get("url") or spec["local_url"])
            live_service = published_web_services.get(app_id) if isinstance(published_web_services, dict) else None
            cached_service = published_web_cache.get(app_id) if isinstance(published_web_cache, dict) else None
            try:
                r = subprocess.run(["docker", "inspect", "--format", "{{.State.Status}}", container], capture_output=True, text=True, check=False, timeout=5)
                raw = r.stdout.strip()
                if raw == "running":
                    status = "running"
                elif isinstance(live_service, dict) and isinstance(live_service.get("active"), bool):
                    status = "running" if live_service.get("active") else "stopped"
                elif r.returncode != 0:
                    try:
                        with socket.create_connection(("127.0.0.1", int(spec["port"])), timeout=1):
                            status = "running"
                    except OSError:
                        status = "stopped"
                else:
                    status = "stopped"
            except Exception:
                status = "unknown"
            public_url = self.publicize_service_url(raw_url)
            tokenized_url = None
            if isinstance(live_service, dict):
                tokenized_url = str(live_service.get("tokenized_proxy_url") or "").strip() or None
                public_url = str(live_service.get("url") or public_url or "").strip() or public_url
            if tokenized_url is None and isinstance(cached_service, dict):
                tokenized_url = str(cached_service.get("last_tokenized_proxy_url") or "").strip() or None
                if public_url is None:
                    cached_public = str(cached_service.get("last_url") or "").strip()
                    public_url = cached_public or public_url
            if tokenized_url is None:
                tokenized_url = self._tokenized_media_url(app_id)
            if container and container not in version_cache:
                version_cache[container] = docker_container_version(container)
            if app_id not in app_version_cache:
                app_version_cache[app_id] = service_reported_version(app_id, raw_url)
            version = app_version_cache.get(app_id) or version_cache.get(container) or local_build_version()
            entries.append({
                "id": app_id,
                "name": str(service.get("name") or spec["name"]),
                "description": str(service.get("description") or spec["description"]),
                "status": status,
                "type": "background_service",
                "version": version,
                "open_url": tokenized_url or public_url,
                "mobile_url": tokenized_url or public_url,
                "qr_url": self.qr_image_url(tokenized_url or public_url) if (tokenized_url or public_url) else None,
            })

        return {"entries": entries, "config": config}

    def proxy_response(
        self,
        status: int,
        payload: bytes,
        headers: dict[str, str],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:

        self.send_response(status)
        excluded = {"content-length", "transfer-encoding", "connection", "content-encoding"}
        for name, value in headers.items():
            if name.lower() in excluded:
                continue
            self.send_header(name, value)
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def handle_proxy(self, request, *, method: str) -> None:

        proxy_path = request.path[len("/proxy/"):]
        app_id, _, remainder = proxy_path.partition("/")
        if not (self.authenticated() or self.token_authorized_proxy(request, app_id)):
            self.send_error_page(401, "Authentication required")
            return
        extra_headers: dict[str, str] = {}
        supplied_token = parse_qs(request.query).get("access_token", [""])[0]
        if supplied_token and self.validate_proxy_token(supplied_token, app_id):
            extra_headers["Set-Cookie"] = (
                f"rocky_proxy_{app_id}={supplied_token}; "
                f"Path=/proxy/{quote(app_id)}/; HttpOnly; SameSite=Lax"
            )
        base = self.proxy_base_for_app(app_id)
        if not base:
            self.send_error_page(404, "Unknown proxied application")
            return
        target_path = "/" + remainder if remainder else "/"
        target = base.rstrip("/") + target_path
        forwarded_query = request.query
        if forwarded_query:
            parsed_query = parse_qs(forwarded_query, keep_blank_values=True)
            parsed_query.pop("access_token", None)
            query_parts: list[str] = []
            for key, values in parsed_query.items():
                for value in values:
                    query_parts.append(f"{quote(str(key))}={quote(str(value))}")
            if query_parts:
                target += "?" + "&".join(query_parts)
        body = None
        if method == "POST":
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length)
        upstream = urlparse(base)
        host_override = upstream.netloc
        proxy_request = Request(target, data=body, method=method)
        for header_name in ("Content-Type", "Cookie", "User-Agent"):
            header_value = self.headers.get(header_name)
            if header_value:
                proxy_request.add_header(header_name, header_value)
        proxy_request.add_header("Host", host_override)
        proxy_request.add_header("X-Forwarded-Host", self.headers.get("Host", ""))
        proxy_request.add_header("X-Forwarded-Proto", "http")
        try:
            with urlopen(proxy_request, timeout=20) as response:
                payload = response.read()
                headers = {name: value for name, value in response.headers.items()}
                self.proxy_response(response.status, payload, headers, extra_headers=extra_headers)
                return
        except HTTPError as exc:
            payload = exc.read()
            headers = {name: value for name, value in exc.headers.items()}
            self.proxy_response(exc.code, payload, headers, extra_headers=extra_headers)
            return
        except URLError as exc:
            self.send_json_error(HTTPStatus.BAD_GATEWAY, "proxy_failed", detail=str(exc))
            return

    def handle_apps(self) -> None:

        payload = self.apps_payload()
        entries = payload["entries"]
        config = payload["config"]
        vpn = config.get("vpn", {})
        privacy = config.get("privacy_relay", {})
        transfer = config.get("transfer", {})
        MEDIA_IDS = set(media_app_ids()) | {"transfer-stack"}
        EMOJI_MAP = {
            "radarr": "🎬", "sonarr": "📺", "bazarr": "💬",
            "prowlarr": "🔎", "jellyseerr": "🎯", "jellyfin": "🎞️", "transfer-stack": "⚡", "torrentz": "⚡", "pihole": "🛡️",
            "3d_printer": "🖨️", "diagnostic": "🔧", "reader": "📖",
            "tesserae": "🖼️", "pikvm": "🖥️", "pwnagotchi": "👾",
            "ragnar": "🔍", "entertainment": "🎭", "ns_usbloader": "🔌",
        }
        cards: list[str] = []
        media_cards: list[str] = []
        for entry in entries:
            eid = str(entry.get("id", ""))
            emoji = EMOJI_MAP.get(eid, "📦")
            name = html.escape(str(entry["name"]))
            desc = html.escape(str(entry["description"]))
            etype = html.escape(str(entry["type"]))
            version = html.escape(str(entry.get("version") or local_build_version()))
            status_raw = str(entry.get("status", "unknown")).lower()
            if status_raw in ("running", "healthy", "up", "ok"):
                dot_cls = "running"
                status_label = "RUNNING"
            elif status_raw in ("stopped", "down", "error", "failed"):
                dot_cls = "stopped"
                status_label = "STOPPED"
            else:
                dot_cls = "unknown"
                status_label = status_raw.upper() or "UNKNOWN"
            is_media = eid in MEDIA_IDS
            card_cls = "app-card media-card" if is_media else "app-card"
            launch_cls = "btn-launch media" if is_media else "btn-launch"
            actions_html = ""
            if entry.get("open_url"):
                safe_url = html.escape(str(entry["open_url"]))
                safe_app_id = html.escape(eid)
                if is_media:
                    app_id_attr = f' data-app-id="{safe_app_id}"' if eid != "transfer-stack" else ""
                    actions_html += f'<a href="{safe_url}" target="_blank" rel="noreferrer" class="{launch_cls}"{app_id_attr}>&#x25BA; LAUNCH</a>'
                else:
                    actions_html += f'<button class="{launch_cls}" data-app-id="{safe_app_id}" data-open-url="{safe_url}" type="button">&#x25BA; LAUNCH</button>'
            if entry.get("qr_url") and entry.get("mobile_url"):
                safe_qr = html.escape(str(entry["qr_url"]))
                safe_mob = html.escape(str(entry["mobile_url"]))
                actions_html += f'<button class="btn-qr" onclick="openQR(\'{safe_qr}\',\'{safe_mob}\')" type="button">QR</button>'
            card_html = (
                f'<div class="{card_cls}">'
                f'<div class="card-top">'
                f'<span class="card-icon">{emoji}</span>'
                f'<div class="card-title-block">'
                f'<div class="card-name">{name}</div>'
                f'<div class="card-type">{etype}</div>'
                f'</div>'
                f'<span class="version-pill" title="Version: {version}">{version}</span>'
                f'<span class="status-dot {dot_cls}" title="{status_label}"></span>'
                f'</div>'
                f'<div class="status-row">'
                f'<span class="card-status-text {dot_cls}">{status_label}</span>'
                f'<span class="card-version-line">APP VERSION {version}</span>'
                f'</div>'
                f'<p class="card-desc">{desc}</p>'
                f'<div class="app-actions">{actions_html}</div>'
                f'</div>'
            )
            if is_media:
                media_cards.append(card_html)
            else:
                cards.append(card_html)

        provider = str(vpn.get("provider", "none"))
        privacy_mode = str(privacy.get("mode", "off"))
        engine = str(transfer.get("engine", "qbittorrent-nox"))
        allow_lan_checked = "checked" if vpn.get("allow_lan", True) else ""
        kill_switch_checked = "checked" if vpn.get("kill_switch", False) else ""
        require_vpn_checked = "checked" if transfer.get("require_vpn", True) else ""
        auto_use_usb_checked = "checked" if transfer.get("auto_use_usb", False) else ""

        media_grid = "".join(media_cards)
        apps_grid = "".join(cards)
        media_section = f"""
<section>
<div class="section-label purple">🎭 MEDIA CENTER</div>
<div class="app-grid">{media_grid}</div>
</section>
""" if media_cards else ""

        body = f"""
<!-- QR Modal -->
<div class="qr-modal-overlay" id="qr-modal" onclick="if(event.target===this)closeQR()">
  <div class="qr-modal-box">
    <img id="qr-modal-img" alt="Mobile QR Code" src="">
    <div class="qr-modal-url" id="qr-modal-url"></div>
    <button class="qr-modal-close" onclick="closeQR()" type="button">✕ CLOSE</button>
  </div>
</div>

<section>
<div class="section-label cyan">⬡ APPLICATIONS</div>
<div class="app-grid">{apps_grid}</div>
</section>
{media_section}
<section>
<div class="section-label cyan">⚙ WORKFLOW &amp; MODE CONTROLS</div>
<div class="control-grid">
  <section>
    <div class="section-label cyan" style="font-size:0.6rem;margin-bottom:12px;">NETWORK MODE</div>
    <form id="network-form">
      <label for="vpn-provider">VPN provider</label>
      <select id="vpn-provider" name="provider">
        <option value="none" {"selected" if provider == "none" else ""}>none</option>
        <option value="openvpn" {"selected" if provider == "openvpn" else ""}>openvpn</option>
        <option value="gluetun" {"selected" if provider == "gluetun" else ""}>gluetun</option>
      </select>
      <label for="privacy-mode">Privacy relay</label>
      <select id="privacy-mode" name="privacy_mode">
        <option value="off" {"selected" if privacy_mode == "off" else ""}>off</option>
        <option value="tor_socks5" {"selected" if privacy_mode == "tor_socks5" else ""}>tor_socks5</option>
      </select>
      <label style="text-transform:none;letter-spacing:0;font-size:0.85rem;margin-top:12px;display:flex;align-items:center;gap:8px;"><input type="checkbox" id="allow-lan" {allow_lan_checked} style="width:auto;"> Allow LAN</label>
      <label style="text-transform:none;letter-spacing:0;font-size:0.85rem;display:flex;align-items:center;gap:8px;"><input type="checkbox" id="kill-switch" {kill_switch_checked} style="width:auto;"> Kill Switch</label>
      <p><button type="submit" class="btn-apply">Apply Network</button></p>
    </form>
  </section>
  <section>
    <div class="section-label cyan" style="font-size:0.6rem;margin-bottom:12px;">TRANSFER MODE</div>
    <form id="transfer-form">
      <label for="transfer-engine">Transfer engine</label>
      <select id="transfer-engine" name="engine">
        <option value="qbittorrent-nox" {"selected" if engine == "qbittorrent-nox" else ""}>qbittorrent-nox</option>
        <option value="transmission" {"selected" if engine == "transmission" else ""}>transmission</option>
      </select>
      <label style="text-transform:none;letter-spacing:0;font-size:0.85rem;margin-top:12px;display:flex;align-items:center;gap:8px;"><input type="checkbox" id="require-vpn" {require_vpn_checked} style="width:auto;"> Require VPN</label>
      <label style="text-transform:none;letter-spacing:0;font-size:0.85rem;display:flex;align-items:center;gap:8px;"><input type="checkbox" id="auto-use-usb" {auto_use_usb_checked} style="width:auto;"> Auto-Use USB</label>
      <p><button type="submit" class="btn-apply">Apply Transfer</button></p>
    </form>
  </section>
  <section>
    <div class="section-label cyan" style="font-size:0.6rem;margin-bottom:4px;">ROCKY MODE</div>
    <p class="muted" id="mode-current-label" style="font-size:0.8rem;margin:8px 0;">Loading...</p>
    <div id="mode-cards" class="mode-pill-row"></div>
    <pre id="mode-feedback" class="muted" style="margin-top:12px;font-size:0.75rem;"></pre>
  </section>
</div>
<pre id="apps-feedback" class="muted" style="font-size:0.75rem;margin-top:16px;">Ready.</pre>
</section>
<script>
const csrfToken = document.querySelector('meta[name="rocky-csrf-token"]').content;
const feedback = document.getElementById('apps-feedback');
async function startAndOpenApp(button) {{
  const appId = button.dataset.appId;
  const fallbackUrl = button.dataset.openUrl;
  const oldText = button.textContent;
  const popup = fallbackUrl ? window.open(fallbackUrl, '_blank', 'noreferrer') : null;
  button.disabled = true;
  button.textContent = 'Starting...';
  try {{
    const response = await fetch('/api/apps/launch', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json', 'X-Rocky-CSRF': csrfToken}},
      body: JSON.stringify({{id: appId}})
    }});
    const payload = await response.json();
    if (!response.ok || payload.ok === false) {{
      throw new Error(payload.error || payload.detail || 'launch_failed');
    }}
    const url = payload.open_url || fallbackUrl;
    if (url && popup) {{
      try {{ popup.location.href = url; }} catch (e) {{}}
    }} else if (url) {{
      window.location.href = url;
    }}
    if (feedback) feedback.textContent = JSON.stringify(payload, null, 2);
  }} catch (error) {{
    if (feedback) feedback.textContent = 'Launch failed for ' + appId + ': ' + error;
  }} finally {{
    button.disabled = false;
    button.textContent = oldText;
  }}
}}
document.querySelectorAll('.btn-launch[data-app-id]').forEach((button) => {{
  if (button.tagName === 'A') {{
    button.addEventListener('click', () => {{
      fetch('/api/apps/launch', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json', 'X-Rocky-CSRF': csrfToken}},
        body: JSON.stringify({{id: button.dataset.appId}})
      }}).catch(() => {{}});
    }});
    return;
  }}
  button.addEventListener('click', () => startAndOpenApp(button));
}});

function openQR(src, url) {{
  document.getElementById('qr-modal-img').src = src;
  document.getElementById('qr-modal-url').textContent = url;
  document.getElementById('qr-modal').classList.add('active');
}}
function closeQR() {{
  document.getElementById('qr-modal').classList.remove('active');
  document.getElementById('qr-modal-img').src = '';
}}
document.addEventListener('keydown', (e) => {{ if (e.key === 'Escape') closeQR(); }});

async function submitJson(path, payload) {{
  const response = await fetch(path, {{method: 'POST', headers: {{'Content-Type': 'application/json', 'X-Rocky-CSRF': csrfToken}}, body: JSON.stringify(payload)}});
  feedback.textContent = await response.text();
}}
document.getElementById('network-form').addEventListener('submit', async (event) => {{
  event.preventDefault();
  await submitJson('/api/network/config', {{vpn: {{provider: document.getElementById('vpn-provider').value, allow_lan: document.getElementById('allow-lan').checked, kill_switch: document.getElementById('kill-switch').checked}}, privacy_relay: {{mode: document.getElementById('privacy-mode').value, tor: {{enabled: document.getElementById('privacy-mode').value === 'tor_socks5', listen_host: '127.0.0.1', socks_port: 9050}}}}}});
}});
document.getElementById('transfer-form').addEventListener('submit', async (event) => {{
  event.preventDefault();
  await submitJson('/api/transfer/config', {{transfer: {{engine: document.getElementById('transfer-engine').value, require_vpn: document.getElementById('require-vpn').checked, auto_use_usb: document.getElementById('auto-use-usb').checked}}}});
}});

// Mode Selector
(async function() {{
  const csrf = document.querySelector('meta[name="rocky-csrf-token"]').content;
  const fb = document.getElementById('mode-feedback');
  const lbl = document.getElementById('mode-current-label');
  const pillRow = document.getElementById('mode-cards');
  const COLORS = {{blue:'#3b82f6',red:'#ef4444',green:'#22c55e',yellow:'#eab308',gray:'#6b7280',purple:'#7c3aed'}};
  async function load() {{
    try {{
      const d = await (await fetch('/api/mode/config')).json();
      const cur = (d.current_mode || {{}}).selected_mode_id || 'safe';
      const live = ((d.published_mode || {{}}).live || {{}}).mode_id || cur;
      lbl.textContent = 'Active: ' + live + (live !== cur ? ' → ' + cur : '');
      pillRow.innerHTML = '';
      for (const m of (d.modes || [])) {{
        const active = m.mode_id === cur;
        const btn = document.createElement('button');
        btn.textContent = (m.label || m.mode_id).toUpperCase();
        btn.title = m.description || '';
        btn.className = 'mode-pill' + (active ? ' active' : '');
        if (!active) btn.addEventListener('click', async () => {{
          fb.textContent = 'Switching to ' + (m.label || m.mode_id) + '...';
          try {{
            const r = await fetch('/api/mode/select', {{method:'POST',headers:{{'Content-Type':'application/json','X-Rocky-CSRF':csrf}},body:JSON.stringify({{selected_mode_id:m.mode_id,reason:'browser_mode_selector'}})}});
            const j = await r.json();
            fb.textContent = j.ok ? '✓ Mode set to ' + m.mode_id : JSON.stringify(j);
            if (j.ok) setTimeout(load, 2000);
          }} catch (err) {{
            fb.textContent = 'Mode switch failed: console is not reachable (' + err + '). Check rocky-web.service.';
          }}
        }});
        pillRow.appendChild(btn);
      }}
    }} catch(e) {{ lbl.textContent = 'Error: ' + e; }}
  }}
  load();
  setInterval(load, 8000);
}})();
</script>
"""

        self.send_html("Apps", body)

    def handle_status(self) -> None:

        state_path = RUNTIME_ROOT / "state.json"
        state_available = state_path.is_file()

        def _cpu_percent() -> str:
            try:
                vals1 = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
                import time as _t; _t.sleep(0.3)
                vals2 = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
                dt = sum(vals2) - sum(vals1)
                return f"{100.0 * (1 - (vals2[3] - vals1[3]) / dt):.1f}%" if dt else "N/A"
            except Exception:
                return "N/A"

        def _mem() -> str:
            try:
                d = {k.strip(): int(v.split()[0]) for line in Path("/proc/meminfo").read_text().splitlines() for k, v in [line.split(":", 1)]}
                total, avail = d.get("MemTotal", 0), d.get("MemAvailable", 0)
                used = total - avail
                return f"{used // 1024} MB / {total // 1024} MB ({100 * used // total if total else 0}%)"
            except Exception:
                return "N/A"

        def _disk() -> str:
            try:
                r = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, check=False, timeout=5)
                parts = r.stdout.strip().splitlines()[1].split()
                return f"{parts[2]} used / {parts[1]} total ({parts[4]})"
            except Exception:
                return "N/A"

        def _uptime() -> str:
            try:
                secs = float(Path("/proc/uptime").read_text().split()[0])
                d, r = divmod(int(secs), 86400); h, r = divmod(r, 3600)
                return f"{d}d {h}h {r // 60}m"
            except Exception:
                return "N/A"

        def _temp() -> str:
            for p in ["/sys/class/thermal/thermal_zone0/temp", "/sys/class/thermal/thermal_zone1/temp"]:
                try:
                    return f"{int(Path(p).read_text().strip()) / 1000:.1f} \u00b0C"
                except Exception:
                    continue
            return "N/A"

        def _load() -> str:
            try:
                v = Path("/proc/loadavg").read_text().split()
                return f"{v[0]} / {v[1]} / {v[2]}"
            except Exception:
                return "N/A"

        cpu, mem, disk, uptime, temp, load = _cpu_percent(), _mem(), _disk(), _uptime(), _temp(), _load()

        body = f"""
<section>
<div class="section-label">&#x26A1; SYSTEM METRICS</div>
<div class="metrics-grid">
  <div class="metric-card"><div class="metric-label">CPU USAGE</div><div class="metric-value">{html.escape(cpu)}</div></div>
  <div class="metric-card"><div class="metric-label">CPU TEMP</div><div class="metric-value">{html.escape(temp)}</div></div>
  <div class="metric-card"><div class="metric-label">MEMORY</div><div class="metric-value">{html.escape(mem)}</div></div>
  <div class="metric-card"><div class="metric-label">DISK</div><div class="metric-value">{html.escape(disk)}</div></div>
  <div class="metric-card"><div class="metric-label">UPTIME</div><div class="metric-value">{html.escape(uptime)}</div></div>
  <div class="metric-card"><div class="metric-label">LOAD AVG</div><div class="metric-value">{html.escape(load)}</div></div>
</div>
</section>
<section>
<div class="section-label">&#x1F5A5; RUNTIME</div>
<p>
<span class="badge">Host: {html.escape(socket.gethostname())}</span>
<span class="badge">Console: 0.4</span>
<span class="badge">Generated: {html.escape(utc_now())}</span>
</p>
<pre>{html.escape(runtime_status())}</pre>
</section>
<section>
<div class="section-label">&#x1F512; SECURITY FOUNDATION</div>
<p>
<span class="badge">CSRF: X-Rocky-CSRF</span>
<span class="badge">Body limit: {html.escape(human_size(MAX_REQUEST_BYTES))}</span>
<span class="badge">Audit: {html.escape(str(AUDIT_LOG_PATH))}</span>
</p>
</section>
<section>
<div class="section-label">&#x1F4E1; PUBLISHED STATE</div>
<p>Available: <strong>{"yes" if state_available else "no"}</strong></p>
<p class="muted">{html.escape(str(state_path))}</p>
</section>
"""
        self.send_html("Status", body)

    def handle_logs(self) -> None:

        body = f"""

<section>

<h2>Recent Rocky logs</h2>

<p class="muted">Read-only view of the most recent runtime entries.</p>

<pre>{html.escape(recent_logs())}</pre>

</section>

"""

        self.send_html("Logs", body)



    def handle_files(self, query: dict[str, list[str]]) -> None:

        root_name = query.get("root", ["project"])[0]

        relative_path = query.get("path", [""])[0]



        target = resolve_allowed_path(root_name, relative_path)

        if target is None:

            self.send_error_page(403, "Path is outside the allowed roots")

            return



        if not target.exists():

            self.send_error_page(404, "Path does not exist")

            return



        if not target.is_dir():

            self.send_error_page(400, "Requested path is not a directory")

            return



        root = WORKSPACE_REGISTRY.get(root_name).path

        current_relative = target.relative_to(root)



        rows: list[str] = []



        if target != root:

            parent = current_relative.parent

            rows.append(

                "<tr>"

                '<td>ð <a href="/files?root={root}&path={path}">..</a></td>'

                "<td>Directory</td><td></td><td></td>"

                "</tr>".format(

                    root=quote(root_name),

                    path=quote(str(parent)),

                )

            )



        try:

            entries = sorted(

                target.iterdir(),

                key=lambda entry: (not entry.is_dir(), entry.name.lower()),

            )

        except OSError as exc:

            self.send_error_page(403, f"Unable to list directory: {exc}")

            return



        for entry in entries:

            try:

                stat = entry.stat()

            except OSError:

                continue



            child_relative = entry.relative_to(root)

            escaped_name = html.escape(entry.name)



            if entry.is_dir():

                link = (

                    f"/files?root={quote(root_name)}"

                    f"&path={quote(str(child_relative))}"

                )

                name_cell = f'ð <a href="{link}">{escaped_name}</a>'

                kind = "Directory"

                action = ""

            else:

                view_link = (

                    f"/view?root={quote(root_name)}"

                    f"&path={quote(str(child_relative))}"

                )

                download_link = (

                    f"/download?root={quote(root_name)}"

                    f"&path={quote(str(child_relative))}"

                )

                name_cell = f'ð <a href="{view_link}">{escaped_name}</a>'

                kind = entry.suffix or "File"

                action = f'<a href="{download_link}">Download</a>'



            modified = datetime.fromtimestamp(

                stat.st_mtime,

                timezone.utc,

            ).isoformat()



            rows.append(

                "<tr>"

                f"<td>{name_cell}</td>"

                f"<td>{html.escape(kind)}</td>"

                f"<td>{human_size(stat.st_size)}</td>"

                f"<td>{html.escape(modified)}</td>"

                f"<td>{action}</td>"

                "</tr>"

            )



        roots = " ".join(

            (

                f'<a class="badge" '

                f'href="/files?root={quote(workspace.id)}">'

                f'{html.escape(workspace.name)}'

                f'{" Â· read-only" if workspace.read_only else ""}'

                f'</a>'

            )

            for workspace in WORKSPACE_REGISTRY.list()

        )



        body = f"""

<section>

<h2>Files</h2>

<p>{roots}</p>

<p class="muted">

Root: {html.escape(root_name)} /

{html.escape(str(current_relative))}

</p>

<table>

<thead>

<tr>

<th>Name</th>

<th>Type</th>

<th>Size</th>

<th>Modified UTC</th>

<th>Action</th>

</tr>

</thead>

<tbody>

{"".join(rows)}

</tbody>

</table>

</section>

"""

        self.send_html("Files", body)



    def handle_view(self, query: dict[str, list[str]]) -> None:

        root_name = query.get("root", ["project"])[0]

        relative_path = query.get("path", [""])[0]



        target = resolve_allowed_path(root_name, relative_path)

        if target is None:

            self.send_error_page(403, "Path is outside the allowed roots")

            return



        if not target.is_file():

            self.send_error_page(404, "File does not exist")

            return



        if target.suffix.lower() not in TEXT_EXTENSIONS:

            self.send_error_page(

                415,

                "This file type is download-only in console version 0.3",

            )

            return



        try:

            size = target.stat().st_size

        except OSError as exc:

            self.send_error_page(403, f"Unable to inspect file: {exc}")

            return



        if size > 2 * 1024 * 1024:

            self.send_error_page(

                413,

                "File is too large for the browser viewer",

            )

            return



        try:

            content = target.read_text(encoding="utf-8", errors="replace")

        except OSError as exc:

            self.send_error_page(403, f"Unable to read file: {exc}")

            return



        root = WORKSPACE_REGISTRY.get(root_name).path

        relative = target.relative_to(root)



        download_link = (

            f"/download?root={quote(root_name)}"

            f"&path={quote(str(relative))}"

        )

        parent_link = (

            f"/files?root={quote(root_name)}"

            f"&path={quote(str(relative.parent))}"

        )



        body = f"""

<section>

<h2>{html.escape(target.name)}</h2>

<p>

<a href="{parent_link}">Back to directory</a>

&nbsp; Â· &nbsp;

<a href="{download_link}">Download</a>

</p>

<p class="muted">{html.escape(str(target))}</p>

<pre>{html.escape(content)}</pre>

</section>

"""

        self.send_html(target.name, body)



    def handle_download(self, query: dict[str, list[str]]) -> None:

        root_name = query.get("root", ["project"])[0]

        relative_path = query.get("path", [""])[0]



        target = resolve_allowed_path(root_name, relative_path)

        if target is None:

            self.send_error_page(403, "Path is outside the allowed roots")

            return



        if not target.is_file():

            self.send_error_page(404, "File does not exist")

            return



        try:

            payload = target.read_bytes()

        except OSError as exc:

            self.send_error_page(403, f"Unable to read file: {exc}")

            return



        content_type, _ = mimetypes.guess_type(target.name)

        if not content_type:

            content_type = "application/octet-stream"



        safe_name = target.name.replace('"', "")



        self._send_bytes(

            200,

            payload,

            content_type,

            extra_headers={

                "Content-Disposition": f'attachment; filename="{safe_name}"',

            },

        )





def main() -> None:

    if not PASSWORD:

        raise SystemExit(

            "ROCKY_WEB_PASSWORD is not set. "

            "Refusing to start without authentication."

        )



    try:
        ensure_mode_config_files()
    except OSError:
        print("Unable to prepare mode config files; continuing")

    server = ThreadingHTTPServer((HOST, PORT), RockyConsoleHandler)



    print(f"Rocky Developer Console listening on http://{HOST}:{PORT}")

    print(f"Allowed project root: {PROJECT_ROOT}")

    print(f"Allowed runtime root: {RUNTIME_ROOT}")

    print(f"Request body limit: {MAX_REQUEST_BYTES} bytes")

    print(f"Audit log: {AUDIT_LOG_PATH}")



    try:

        server.serve_forever()

    except KeyboardInterrupt:

        pass

    finally:

        server.server_close()





if __name__ == "__main__":

    main()

