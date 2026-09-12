from __future__ import annotations



import base64
from http.cookies import SimpleCookie

from email import policy
from email.parser import BytesParser

import html
import io
import json
import mimetypes
import os
import re
import secrets
import select
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

from http import HTTPStatus, client as http_client

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pathlib import Path

from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from manager.api import runtime_status as runtime_status_api
from manager.runtime import network_transfer as network_transfer_runtime
from manager.runtime.app_proxy import (
    LAST_PROXY_APP_COOKIE,
    build_websocket_upstream_request,
    decode_upstream_payload,
    filter_browser_cookies_for_upstream,
    forwarded_upstream_query,
    is_console_chrome_path,
    is_media_health_endpoint,
    is_proxy_bridge_path,
    is_public_proxy_app,
    is_upstream_unavailable,
    is_websocket_upgrade,
    leaked_proxy_app_id,
    map_jellyfin_upstream_path,
    proxied_app_login_location,
    proxied_response_headers,
    proxy_app_id_from_path,
    proxy_bridge_js,
    proxy_retry_seconds,
    rewrite_arr_initialize_json,
    rewrite_html_root_paths,
    rewrite_jellyseerr_jellyfin_connect_body,
    rewrite_jellyfin_system_info,
    select_upstream_request_headers,
    static_asset_from_login_query,
    suppress_login_redirect_for_asset,
    transfer_loopback_headers,
    upstream_starting_page,
    upstream_unavailable_error_response,
    wants_upstream_wait_page,
)
from manager.runtime.service_catalog import ServiceCatalog
from manager.runtime.launch_requests import catalog_proxy_url, needs_daemon_launch, write_launch_request
from manager.runtime.media_stack import (
    MEDIA_STACK_APPS,
    TRANSFER_PROXY_APP_IDS,
    TRANSFER_STACK_CONTAINERS,
    TRANSFER_STACK_LOCAL_URL,
    is_transfer_proxy_app,
    jellyseerr_needs_volume_recreate,
    media_app_ids,
    media_compose_up_command,
    media_launch_target,
    media_stack_activate_mode,
    print_lab_activate_mode,
    transfer_stack_activate_mode,
)
from manager.runtime.proxy_tokens import (
    build_proxy_token as mint_proxy_token,
    validate_proxy_token as check_proxy_token,
)
from manager.runtime.workspace import (

    WorkspaceError,

    WorkspaceRegistry,

)





class NoFollowRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


PROXY_OPENER = build_opener(NoFollowRedirect)
_TRANSFER_ENSURE_LOCK = threading.Lock()
_TRANSFER_ENSURE_AT = 0.0
_MEDIA_ENSURE_LOCK = threading.Lock()
_MEDIA_ENSURE_AT: dict[str, float] = {}
_CONSOLE_FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    b'<rect width="32" height="32" rx="6" fill="#060a0f"/>'
    b'<circle cx="16" cy="16" r="8" fill="#00d4ff"/>'
    b"</svg>"
)


def _env_credential(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    value = str(value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value


HOST = os.environ.get("ROCKY_WEB_HOST", "0.0.0.0")

PORT = int(os.environ.get("ROCKY_WEB_PORT", "8090"))

USERNAME = _env_credential("ROCKY_WEB_USERNAME", "rocky") or "rocky"
PASSWORD = _env_credential("ROCKY_WEB_PASSWORD")
SESSION_COOKIE_NAME = "rocky_session"
SESSION_APP_ID = "rocky-console"


def _credentials_match(user: str, password: str) -> bool:
    if not PASSWORD:
        return False

    def same(left: str, right: str) -> bool:
        left_bytes = left.encode("utf-8")
        right_bytes = right.encode("utf-8")
        if len(left_bytes) != len(right_bytes):
            return False
        return secrets.compare_digest(left_bytes, right_bytes)

    return same(user, USERNAME) and same(password, PASSWORD)


def _safe_next_path(value: str) -> str:
    path = str(value or "").strip() or "/apps"
    if not path.startswith("/") or path.startswith("//") or "://" in path:
        return "/apps"
    return path



PROJECT_ROOT = Path("/opt/zero2w-manager").resolve()

RUNTIME_ROOT = Path("/run/rocky").resolve()
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
PROXY_TOKEN_TTL_SECONDS = int(os.environ.get("ROCKY_WEB_PROXY_TOKEN_TTL_SECONDS", "43200"))



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

    "Referrer-Policy": "same-origin",

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




def local_build_version(project_root: Path | None = None) -> str:
    root = Path(project_root or PROJECT_ROOT)
    explicit = os.environ.get("ROCKY_BUILD_VERSION", "").strip()
    if explicit:
        return explicit
    version_file = root / "ROCKY_BUILD_VERSION"
    try:
        if version_file.is_file():
            revision = version_file.read_text(encoding="utf-8").strip()
            if revision:
                return f"rocky@{revision}"
    except OSError:
        pass
    git_dir = root / ".git"
    if git_dir.exists():
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(root),
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





def published_console_state() -> dict[str, object]:
    """Read /run/rocky/state.json without Docker inspects or HTTP version probes."""
    path = Path("/run/rocky/state.json")
    try:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


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

<title>{html.escape(title)} - Rocky</title>
<link rel="icon" href="/favicon.ico">

<style>

:root {{

    color-scheme: dark;

    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;

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

    def send_console_failure(self, where: str, exc: BaseException) -> None:
        self.log_error("%s failed: %s", where, exc)
        try:
            detail = f"{type(exc).__name__}: {exc}"
            self.send_html(
                "Console error",
                "<section><h2>Rocky console request failed</h2>"
                f"<p>{html.escape(where)}</p>"
                f"<pre>{html.escape(detail)}</pre></section>",
                status=500,
            )
        except Exception:
            self.close_connection = True

    def handle_one_request(self) -> None:
        """Never close the socket with zero bytes; Chrome shows ERR_EMPTY_RESPONSE."""
        try:
            super().handle_one_request()
        except Exception as exc:
            self.send_console_failure("Request", exc)



    @property

    def client_ip(self) -> str:

        return self.client_address[0]



    def _cookie(self, name: str) -> str | None:
        header = self.headers.get("Cookie", "")
        if not header:
            return None
        try:
            cookie = SimpleCookie()
            cookie.load(header)
        except Exception:
            return None
        morsel = cookie.get(name)
        if morsel is None:
            return None
        return str(morsel.value or "")

    def _serve_console_chrome(self, path: str, *, head_only: bool = False) -> bool:
        """Serve Rocky /favicon.ico (or 404 other chrome) without proxying to media apps."""
        if not is_console_chrome_path(path):
            return False
        resource = str(path or "").split("?", 1)[0].lower()
        if resource == "/favicon.ico":
            payload = _CONSOLE_FAVICON_SVG
            content_type = "image/svg+xml"
            status = HTTPStatus.OK
        else:
            payload = b""
            content_type = "text/plain; charset=utf-8"
            status = HTTPStatus.NOT_FOUND
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self._send_security_headers()
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)
        return True

    def _session_authorized(self) -> bool:
        token = self._cookie(SESSION_COOKIE_NAME)
        return bool(token) and check_proxy_token(token, SESSION_APP_ID)

    def _basic_authorized(self) -> bool:
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
        return _credentials_match(supplied_user, supplied_password)

    def authenticated(self) -> bool:
        if self._session_authorized():
            return True
        return self._basic_authorized()

    def _session_cookie_header(self) -> str:
        token = mint_proxy_token(SESSION_APP_ID, ttl_seconds=max(PROXY_TOKEN_TTL_SECONDS, 86400))
        return (
            f"{SESSION_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400"
        )

    def _wants_html(self) -> bool:
        accept = str(self.headers.get("Accept") or "")
        if "text/html" in accept:
            return True
        return not self.path.startswith("/api/")

    def build_proxy_token(self, app_id: str, *, ttl_seconds: int = PROXY_TOKEN_TTL_SECONDS) -> str:

        return mint_proxy_token(app_id, ttl_seconds=ttl_seconds)

    def validate_proxy_token(self, token: str, app_id: str) -> bool:

        return check_proxy_token(token, app_id)



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



    def _send_redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self._send_security_headers()
        self.end_headers()

    def require_authentication(self) -> bool:
        if self.authenticated():
            return True
        if self._wants_html():
            next_path = _safe_next_path(self.path)
            self._send_redirect(f"/login?next={quote(next_path)}")
            return False
        payload = b"Authentication required.\n"
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(payload)
        return False

    def handle_login_page(self, *, error: str | None = None, next_path: str | None = None) -> None:
        request = urlparse(self.path)
        if next_path is None:
            next_path = parse_qs(request.query).get("next", [""])[0]
        passthrough = proxied_app_login_location(
            next_path=_safe_next_path(str(next_path)) if next_path else "",
            referer=self.headers.get("Referer", ""),
            request_query=request.query,
            last_app_id=self._cookie(LAST_PROXY_APP_COOKIE) or "",
        )
        if passthrough:
            self._send_redirect(passthrough)
            return
        next_path = _safe_next_path(str(next_path) or "/apps")
        error_html = (
            f'<p class="card-status-text stopped">{html.escape(error)}</p>'
            if error
            else ""
        )
        body = f"""
<section>
  <div class="section-label cyan">ROCKY LOGIN</div>
  <p class="muted">Sign in with the Rocky Developer Console username and password from <code>/etc/rocky-web.env</code>. Default username is <code>rocky</code>.</p>
  {error_html}
  <form method="post" action="/login" style="max-width:420px;">
    <input type="hidden" name="next" value="{html.escape(next_path)}">
    <input type="hidden" name="csrf" value="{html.escape(CSRF_TOKEN)}">
    <label for="username">Username</label>
    <input id="username" name="username" value="{html.escape(USERNAME)}" autocomplete="username" required>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <p><button type="submit" class="btn-apply">Sign in</button></p>
  </form>
</section>
"""
        extra = None
        if error:
            extra = {"Set-Cookie": f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0"}
        payload = page("Login", body)
        self._send_bytes(200, payload, "text/html; charset=utf-8", extra_headers=extra)

    def handle_login_submit(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(max(0, length)).decode("utf-8", errors="replace")
        form = parse_qs(raw, keep_blank_values=True)
        username = str((form.get("username") or [""])[0])
        password = str((form.get("password") or [""])[0])
        next_path = _safe_next_path(str((form.get("next") or [""])[0]))
        csrf = str((form.get("csrf") or [""])[0])
        if csrf != CSRF_TOKEN:
            self.handle_login_page(error="Login form expired. Refresh and try again.", next_path=next_path or "/apps")
            return
        if not _credentials_match(username, password):
            self.handle_login_page(
                error="Username or password did not match ROCKY_WEB_USERNAME / ROCKY_WEB_PASSWORD.",
                next_path=next_path or "/apps",
            )
            return
        if not next_path:
            next_path = "/apps"
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", next_path)
        self.send_header("Set-Cookie", self._session_cookie_header())
        self.send_header("Content-Length", "0")
        self._send_security_headers()
        self.end_headers()



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

    def _leaked_proxy_request(self, request):
        app_id = leaked_proxy_app_id(
            path=request.path,
            referer=self.headers.get("Referer", ""),
            last_app_id=self._cookie(LAST_PROXY_APP_COOKIE) or "",
        )
        if not app_id:
            return None
        proxied = f"/proxy/{app_id}{request.path}"
        if request.query:
            proxied += f"?{request.query}"
        return urlparse(proxied)

    def do_GET(self) -> None:
        try:
            self._handle_get()
        except Exception as exc:
            self.send_console_failure("GET", exc)

    def _handle_get(self) -> None:

        request = urlparse(self.path)

        if self._serve_console_chrome(request.path):
            return

        if request.path.startswith("/proxy/"):

            self.handle_proxy(request, method="GET")

            return

        leaked = self._leaked_proxy_request(request)
        if leaked is not None:
            self.handle_proxy(leaked, method="GET")
            return

        if request.path == "/login":
            self.handle_login_page()
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
        try:
            self._handle_post()
        except Exception as exc:
            self.send_console_failure("POST", exc)

    def _handle_post(self) -> None:
        request = urlparse(self.path)

        if request.path.startswith("/proxy/"):

            if not self.require_request_size_allowed():

                return

            if not self.require_rate_limit("write", limit=RATE_LIMIT_WRITE):

                return

            self.handle_proxy(request, method="POST")

            return

        leaked = self._leaked_proxy_request(request)
        if leaked is not None:
            if not self.require_request_size_allowed():
                return
            if not self.require_rate_limit("write", limit=RATE_LIMIT_WRITE):
                return
            self.handle_proxy(leaked, method="POST")
            return

        if request.path == "/login":
            referer_app = proxy_app_id_from_path(urlparse(self.headers.get("Referer", "")).path)
            if referer_app and is_public_proxy_app(referer_app):
                proxied_path = self.path.replace("/login", f"/proxy/{referer_app}/login", 1)
                self.handle_proxy(urlparse(proxied_path), method="POST")
                return
            self.handle_login_submit()
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



    def _dispatch_app_proxy(self, method: str) -> None:
        request = urlparse(self.path)
        if method == "HEAD" and self._serve_console_chrome(request.path, head_only=True):
            return
        if request.path.startswith("/proxy/"):
            self.handle_proxy(request, method=method)
            return
        leaked = self._leaked_proxy_request(request)
        if leaked is not None:
            self.handle_proxy(leaked, method=method)
            return
        self.send_error_page(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")

    def do_PUT(self) -> None:
        self._dispatch_app_proxy("PUT")

    def do_PATCH(self) -> None:
        self._dispatch_app_proxy("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch_app_proxy("DELETE")

    def do_HEAD(self) -> None:
        self._dispatch_app_proxy("HEAD")

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

    def _tcp_port_open(self, port: int, host: str = "127.0.0.1", timeout: float = 0.8) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _media_port_listening(self, port: int, timeout: float = 0.2) -> bool:
        return self._tcp_port_open(port, "127.0.0.1", timeout=timeout) or self._tcp_port_open(
            port, "::1", timeout=timeout
        )

    def proxy_base_for_app(self, app_id: str) -> str | None:

        if is_transfer_proxy_app(app_id):
            return TRANSFER_STACK_LOCAL_URL
        spec = MEDIA_STACK_APPS.get(app_id)
        if spec:
            port = int(spec["port"])
            if self._tcp_port_open(port, "127.0.0.1", timeout=0.15):
                return f"http://127.0.0.1:{port}"
            if self._tcp_port_open(port, "::1", timeout=0.15):
                return f"http://[::1]:{port}"
            return str(spec["local_url"]).rstrip("/")
        service = ServiceCatalog().get(app_id)
        if not service:
            return None
        return catalog_proxy_url(app_id, service)

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
        open_url = self._tokenized_media_url(app_id)

        recreate = app_id == "jellyseerr" and jellyseerr_needs_volume_recreate(container, compose_dir)
        if self._media_port_listening(port) and not recreate:
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
        command = media_compose_up_command(compose_service, compose_dir, force_recreate=recreate)
        if command:
            try:
                result = subprocess.run(command, cwd=str(compose_dir), capture_output=True, text=True, check=False, timeout=90)
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
            if self._media_port_listening(port):
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

    def _activate_transfer_mode(self, *, force_torrent_fortress: bool = False) -> None:
        current = str(self.load_current_mode_request_snapshot().get("selected_mode_id") or "")
        mode = transfer_stack_activate_mode(
            current, force_torrent_fortress=force_torrent_fortress
        )
        if not mode:
            return
        self.apply_app_activation_mode({"activate_mode": mode}, "transfer-stack")

    def _start_transfer_containers(self) -> None:
        for container in TRANSFER_STACK_CONTAINERS:
            try:
                subprocess.run(
                    ["docker", "start", container],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                )
            except Exception as exc:
                self.log_error("docker start %s failed: %s", container, exc)

    def _ensure_transfer_stack_starting(self, *, app_id: str = "transfer-stack") -> None:
        global _TRANSFER_ENSURE_AT
        with _TRANSFER_ENSURE_LOCK:
            now = time.monotonic()
            if now - _TRANSFER_ENSURE_AT < 4.0:
                return
            _TRANSFER_ENSURE_AT = now
        try:
            self._activate_transfer_mode()
        except OSError as exc:
            self.log_error("Torrentz mode activation failed: %s", exc)
        try:
            write_launch_request(app_id, source="console-proxy")
        except OSError as exc:
            self.log_error("Torrentz launch request failed: %s", exc)
        self._start_transfer_containers()

    def _kick_media_container(self, app_id: str) -> None:
        target = self._media_launch_target(app_id)
        if not target:
            return
        port = int(target["port"])
        if self._media_port_listening(port, timeout=0.2):
            return
        container = str(target["container"])
        compose_service = str(target["compose_service"])
        compose_dir = PROJECT_ROOT / "runtime" / "media-stack"
        command = media_compose_up_command(compose_service, compose_dir, force_recreate=False)
        try:
            subprocess.Popen(
                command or ["docker", "start", container],
                cwd=str(compose_dir) if command and compose_dir.exists() else None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            self.log_error("media start %s failed: %s", app_id, exc)

    def _ensure_media_app_starting(self, app_id: str) -> None:
        if app_id not in MEDIA_STACK_APPS:
            return
        global _MEDIA_ENSURE_AT
        with _MEDIA_ENSURE_LOCK:
            now = time.monotonic()
            if now - _MEDIA_ENSURE_AT.get(app_id, 0.0) < 4.0:
                return
            _MEDIA_ENSURE_AT[app_id] = now
        try:
            current = str(self.load_current_mode_request_snapshot().get("selected_mode_id") or "")
            mode = media_stack_activate_mode(current)
            if mode:
                self.apply_app_activation_mode({"activate_mode": mode}, app_id)
        except OSError as exc:
            self.log_error("Entertainment mode activation failed: %s", exc)
        try:
            write_launch_request(app_id, source="console-proxy")
        except OSError as exc:
            self.log_error("Media launch request failed: %s", exc)
        self._kick_media_container(app_id)

    def _launch_transfer_stack(self, app_id: str) -> None:
        try:
            self._activate_transfer_mode()
        except OSError as exc:
            self.send_json_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "mode_activation_failed",
                app_id=app_id,
                detail=str(exc),
                source=str(CURRENT_MODE_REQUEST_PATH),
            )
            return
        try:
            write_launch_request(app_id, source="console")
        except OSError as exc:
            self.log_error("Torrentz launch request failed: %s", exc)
        self._start_transfer_containers()
        open_url = (
            self.proxy_public_url(app_id)
            + f"?access_token={quote(self.build_proxy_token(app_id))}"
        )
        self.send_json(
            {
                "ok": True,
                "app_id": app_id,
                "status": "starting",
                "open_url": open_url,
                "detail": "starting qBittorrent; entertainment apps stay running",
            }
        )

    def handle_app_launch(self, request: dict[str, object]) -> None:
        app_id = str(request.get("id") or request.get("app_id") or "").strip()
        if not app_id:
            self.send_json_error(HTTPStatus.BAD_REQUEST, "app_id_required")
            return

        media_target = self._media_launch_target(app_id)
        if media_target:
            try:
                current = str(self.load_current_mode_request_snapshot().get("selected_mode_id") or "")
                mode = media_stack_activate_mode(current)
                if mode:
                    self.apply_app_activation_mode({"activate_mode": mode}, app_id)
            except OSError as exc:
                self.send_json_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "mode_activation_failed",
                    app_id=app_id,
                    detail=str(exc),
                    source=str(CURRENT_MODE_REQUEST_PATH),
                )
                return
            try:
                write_launch_request(app_id, source="console")
            except OSError as exc:
                self.log_error("Media launch request failed: %s", exc)
            payload = self._start_media_app(app_id)
            payload["open_url"] = self._tokenized_media_url(app_id)
            self.send_json(payload, status=HTTPStatus.OK if payload.get("ok") else HTTPStatus.BAD_GATEWAY)
            return

        if app_id in TRANSFER_PROXY_APP_IDS:
            self._launch_transfer_stack(app_id)
            return

        service = ServiceCatalog().get(app_id)
        if not service:
            self.send_json_error(HTTPStatus.NOT_FOUND, "unknown_app", app_id=app_id)
            return

        user_unit = str(service.get("systemd_user_service") or "").strip()
        container = str(service.get("docker_container") or "").strip()
        raw_url = catalog_proxy_url(app_id, service)
        proxy_base = self.proxy_base_for_app(app_id)
        open_url = (
            self.proxy_public_url(app_id) + f"?access_token={quote(self.build_proxy_token(app_id))}"
            if proxy_base
            else (self.publicize_service_url(str(raw_url)) if isinstance(raw_url, str) and raw_url else None)
        )

        actions: list[str] = []
        mode_request: dict[str, object] | None = None
        try:
            requested_mode = str(service.get("activate_mode") or "").strip()
            if requested_mode == "print_lab":
                current = str(self.load_current_mode_request_snapshot().get("selected_mode_id") or "")
                mode = print_lab_activate_mode(current)
                if mode:
                    mode_request = self.apply_app_activation_mode({"activate_mode": mode}, app_id)
            else:
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

        if needs_daemon_launch(service):
            write_launch_request(app_id, source="console")
            actions.append(f"requested {app_id} via zero2w-manager.service")
            self.send_json(
                {
                    "ok": True,
                    "app_id": app_id,
                    "status": "starting",
                    "open_url": open_url,
                    "detail": "\n".join(a for a in actions if a),
                }
            )
            return

        ok = True
        if user_unit:
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

        published = published_console_state()
        transfer_state = published.get("transfer", {}) if isinstance(published, dict) else {}
        published_web_services = published.get("web_services", {}) if isinstance(published, dict) else {}
        published_web_cache = published.get("web_service_cache", {}) if isinstance(published, dict) else {}
        active_app = published.get("application", {}).get("active_id") if isinstance(published, dict) else None
        config = network_transfer_runtime.NetworkTransferConfig().snapshot()
        entries: list[dict[str, object]] = []
        catalog = ServiceCatalog()

        for service in catalog.menu_services():
            app_id = str(service.get("id"))
            service_type = str(service.get("type", "application"))
            status = "ready"
            live_service = published_web_services.get(app_id) if isinstance(published_web_services, dict) else None
            cached_service = published_web_cache.get(app_id) if isinstance(published_web_cache, dict) else None
            if service_type == "background_service":
                unit = str(service.get("systemd_service") or service.get("service") or "")
                if isinstance(live_service, dict) and isinstance(live_service.get("active"), bool):
                    status = "running" if live_service.get("active") else "stopped"
                elif unit:
                    state = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, check=False, timeout=2).stdout.strip()
                    status = state or "unknown"
                elif str(service.get("systemd_user_service") or "").strip():
                    user_unit = str(service.get("systemd_user_service") or "").strip()
                    state = subprocess.run(["systemctl", "--user", "is-active", user_unit], capture_output=True, text=True, check=False, timeout=2).stdout.strip()
                    status = state or "unknown"
                else:
                    status = "unknown"
            elif active_app == app_id:
                status = "running"
            raw_url = catalog_proxy_url(app_id, service)
            public_url = self.publicize_service_url(str(raw_url)) if isinstance(raw_url, str) and raw_url else None
            proxy_base = self.proxy_base_for_app(app_id)
            tokenized_url = (
                self.proxy_public_url(app_id) + f"?access_token={quote(self.build_proxy_token(app_id))}"
                if proxy_base
                else None
            )
            if isinstance(live_service, dict):
                public_url = str(live_service.get("url") or public_url or "").strip() or public_url
            if public_url is None and isinstance(cached_service, dict):
                cached_public = str(cached_service.get("last_url") or "").strip()
                public_url = cached_public or public_url
            version = local_build_version()
            entries.append({
                "id": app_id,
                "name": str(service.get("name", app_id.title())),
                "description": str(service.get("description", "")),
                "status": status,
                "type": service_type,
                "version": version,
                "open_url": tokenized_url or public_url,
                "mobile_url": tokenized_url or public_url,
                "qr_url": self.qr_image_url(tokenized_url or public_url) if (tokenized_url or public_url) else None,
                "launchable": bool(
                    service.get("resident_display")
                    or service.get("display_owner")
                    or service.get("systemd_service")
                    or service.get("systemd_user_service")
                    or service.get("docker_container")
                    or proxy_base
                ),
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
            raw_url = str(service.get("url") or spec["local_url"])
            live_service = published_web_services.get(app_id) if isinstance(published_web_services, dict) else None
            cached_service = published_web_cache.get(app_id) if isinstance(published_web_cache, dict) else None
            if isinstance(live_service, dict) and isinstance(live_service.get("active"), bool):
                status = "running" if live_service.get("active") else "stopped"
            else:
                status = "unknown"
            public_url = self.publicize_service_url(raw_url)
            tokenized_url = self._tokenized_media_url(app_id)
            if isinstance(live_service, dict):
                public_url = str(live_service.get("url") or public_url or "").strip() or public_url
            if public_url is None and isinstance(cached_service, dict):
                cached_public = str(cached_service.get("last_url") or "").strip()
                public_url = cached_public or public_url
            version = local_build_version()
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
        extra_cookies: list[str] | None = None,
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
        for cookie in extra_cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _proxy_public_hosts(self) -> set[str]:
        hosts: set[str] = set()
        for raw in (
            self.headers.get("Host", ""),
            urlparse(self.console_origin()).hostname,
            urlparse(self.preferred_console_base()).hostname,
        ):
            hostname = urlparse(f"http://{raw}" if raw and "://" not in str(raw) else str(raw or "")).hostname
            if hostname:
                hosts.add(hostname)
        return hosts

    def _rewrite_proxied_payload(
        self,
        payload: bytes,
        headers: dict[str, str],
        app_id: str,
        target_path: str,
    ) -> tuple[bytes, dict[str, str]]:
        content_type = str(headers.get("Content-Type") or headers.get("content-type") or "")
        payload = rewrite_html_root_paths(payload, app_id, content_type)
        payload = rewrite_arr_initialize_json(payload, app_id, content_type, target_path)
        if app_id == "jellyfin":
            host = str(self.headers.get("Host") or "").split(",")[0].strip()
            public_origin = f"http://{host}" if host else ""
            payload = rewrite_jellyfin_system_info(
                payload,
                public_origin=public_origin,
                path=target_path,
                content_type=content_type,
            )
        headers["Content-Length"] = str(len(payload))
        return payload, headers

    def _shuttle_sockets(self, left, right) -> None:
        sockets = [left, right]
        try:
            while True:
                readable, _, exceptional = select.select(sockets, [], sockets, 300)
                if exceptional:
                    return
                if not readable:
                    continue
                for src in readable:
                    try:
                        data = src.recv(65536)
                    except OSError:
                        return
                    if not data:
                        return
                    dst = right if src is left else left
                    try:
                        dst.sendall(data)
                    except OSError:
                        return
        except (ValueError, OSError):
            return

    def _proxy_websocket(self, app_id: str, base: str, target_path: str, query_suffix: str) -> None:
        """Tunnel a browser WebSocket through Rocky onto the app's loopback port."""
        self.close_connection = True
        parsed = urlparse(base)
        host = parsed.hostname or "127.0.0.1"
        port = int(parsed.port or 80)
        path = target_path + query_suffix
        cookie_header = filter_browser_cookies_for_upstream(self.headers.get("Cookie", ""))
        request_bytes = build_websocket_upstream_request(
            path,
            self.headers,
            parsed.netloc,
            cookie_header=cookie_header,
        )
        upstream = None
        wrote = False
        try:
            upstream = socket.create_connection((host, port), timeout=10)
            try:
                upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            upstream.sendall(request_bytes)
            header_blob = b""
            while b"\r\n\r\n" not in header_blob:
                chunk = upstream.recv(4096)
                if not chunk:
                    break
                header_blob += chunk
                if len(header_blob) > 65536:
                    break
            if b"\r\n\r\n" not in header_blob:
                raise OSError("websocket handshake closed")
            self.wfile.write(header_blob)
            self.wfile.flush()
            wrote = True
            self._shuttle_sockets(self.connection, upstream)
        except OSError as exc:
            self.log_error("WebSocket proxy %s failed: %s", app_id, exc)
            if not wrote:
                payload, headers = upstream_unavailable_error_response(app_id, target_path)
                self.proxy_response(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    payload,
                    headers,
                )
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass

    def handle_proxy(self, request, *, method: str) -> None:

        proxy_path = request.path[len("/proxy/"):]
        app_id, _, remainder = proxy_path.partition("/")
        token_ok = self.token_authorized_proxy(request, app_id)
        if not is_public_proxy_app(app_id) and not (self.authenticated() or token_ok):
            if self._wants_html():
                self.handle_login_page(next_path="/apps")
                return
            payload = b"Authentication required.\n"
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(payload)
            return
        extra_headers: dict[str, str] = {"X-Rocky-Proxy-App": app_id}
        extra_cookies = [
            f"{LAST_PROXY_APP_COOKIE}={quote(app_id)}; Path=/; SameSite=Lax; Max-Age=43200"
        ]
        supplied_token = parse_qs(request.query).get("access_token", [""])[0]
        if supplied_token and self.validate_proxy_token(supplied_token, app_id):
            extra_cookies.append(
                f"rocky_proxy_{app_id}={supplied_token}; "
                f"Path=/proxy/{quote(app_id)}/; HttpOnly; SameSite=Lax"
            )
        public_hosts = self._proxy_public_hosts()
        base = self.proxy_base_for_app(app_id)
        if not base:
            self.send_error_page(404, "Unknown proxied application")
            return
        if is_proxy_bridge_path(remainder):
            payload = proxy_bridge_js(app_id).encode("utf-8")
            self.proxy_response(
                HTTPStatus.OK,
                payload,
                {
                    "Content-Type": "application/javascript; charset=utf-8",
                    "Cache-Control": "no-store",
                },
                extra_headers=extra_headers,
                extra_cookies=extra_cookies,
            )
            return
        forwarded_query = request.query
        if remainder == "login" or remainder.endswith("/login"):
            asset = static_asset_from_login_query(request.query)
            if asset:
                remainder = asset.lstrip("/")
                forwarded_query = ""
        target_path = "/" + remainder if remainder else "/"
        if app_id == "jellyfin":
            target_path = map_jellyfin_upstream_path(target_path)
        query_suffix = forwarded_upstream_query(
            forwarded_query,
            is_rocky_token=lambda token: self.validate_proxy_token(token, app_id),
        )
        if is_websocket_upgrade(self.headers):
            if is_transfer_proxy_app(app_id):
                self._ensure_transfer_stack_starting(app_id=app_id)
            elif app_id in MEDIA_STACK_APPS:
                self._ensure_media_app_starting(app_id)
            active_base = self.proxy_base_for_app(app_id) or base
            self._proxy_websocket(app_id, active_base, target_path, query_suffix)
            return
        body = None
        if method in {"POST", "PUT", "PATCH"}:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length)
            if app_id == "jellyseerr" and body:
                body = rewrite_jellyseerr_jellyfin_connect_body(
                    body, public_hosts=public_hosts, path=target_path
                )

        def build_upstream_request(active_base: str) -> Request:
            req = Request(active_base.rstrip("/") + target_path + query_suffix, data=body, method=method)
            for header_name, header_value in select_upstream_request_headers(self.headers, app_id=app_id):
                req.add_header(header_name, header_value)
            cookie_header = filter_browser_cookies_for_upstream(self.headers.get("Cookie", ""))
            if cookie_header:
                req.add_header("Cookie", cookie_header)
            req.add_header("Host", urlparse(active_base).netloc)
            req.add_header("Accept-Encoding", "identity")
            if is_transfer_proxy_app(app_id):
                for header_name, header_value in transfer_loopback_headers(active_base).items():
                    req.add_header(header_name, header_value)
            elif app_id not in {"jellyseerr", "jellyfin", "ragnar", "pwnagotchi"}:
                req.add_header("X-Forwarded-Host", self.headers.get("Host", ""))
                req.add_header("X-Forwarded-Proto", "http")
            if app_id not in {"jellyseerr", "jellyfin", "ragnar", "pwnagotchi"} | TRANSFER_PROXY_APP_IDS:
                req.add_header("X-Forwarded-Prefix", f"/proxy/{app_id}")
            return req

        if is_transfer_proxy_app(app_id):
            self._ensure_transfer_stack_starting(app_id=app_id)
        elif app_id in MEDIA_STACK_APPS:
            self._ensure_media_app_starting(app_id)
        proxy_timeout = 60 if app_id == "jellyseerr" and method in {"POST", "PUT", "PATCH"} else 20
        retry_seconds = proxy_retry_seconds(app_id, target_path)
        deadline = time.time() + retry_seconds
        last_url_error: BaseException | None = None
        while True:
            active_base = self.proxy_base_for_app(app_id) or base
            proxy_request = build_upstream_request(active_base)
            try:
                with PROXY_OPENER.open(proxy_request, timeout=proxy_timeout) as response:
                    payload = decode_upstream_payload(response.read(), response.headers)
                    headers, set_cookies = proxied_response_headers(
                        response.headers,
                        app_id,
                        active_base,
                        public_hosts=public_hosts,
                        keep_auth_challenge=is_transfer_proxy_app(app_id),
                    )
                    payload, headers = self._rewrite_proxied_payload(
                        payload, headers, app_id, target_path
                    )
                    self.proxy_response(
                        response.status,
                        payload,
                        headers,
                        extra_headers=extra_headers,
                        extra_cookies=extra_cookies + set_cookies,
                    )
                    return
            except HTTPError as exc:
                if (
                    exc.code in {502, 503, 504}
                    and is_media_health_endpoint(target_path)
                    and time.time() < deadline
                ):
                    try:
                        exc.read()
                    except Exception:
                        pass
                    time.sleep(0.45)
                    continue
                payload = decode_upstream_payload(exc.read(), exc.headers)
                headers, set_cookies = proxied_response_headers(
                    exc.headers,
                    app_id,
                    active_base,
                    public_hosts=public_hosts,
                    keep_auth_challenge=is_transfer_proxy_app(app_id),
                )
                location = str(headers.get("Location") or headers.get("location") or "")
                if suppress_login_redirect_for_asset(target_path, location):
                    headers = {
                        name: value
                        for name, value in headers.items()
                        if name.lower() != "location"
                    }
                    self.proxy_response(
                        HTTPStatus.NOT_FOUND,
                        b"",
                        {"Content-Type": "text/plain; charset=utf-8"},
                        extra_headers=extra_headers,
                        extra_cookies=extra_cookies,
                    )
                    return
                payload, headers = self._rewrite_proxied_payload(
                    payload, headers, app_id, target_path
                )
                self.proxy_response(
                    exc.code,
                    payload,
                    headers,
                    extra_headers=extra_headers,
                    extra_cookies=extra_cookies + set_cookies,
                )
                return
            except URLError as exc:
                last_url_error = exc
                if not is_upstream_unavailable(exc) or time.time() >= deadline:
                    break
                time.sleep(0.45)
            except (TimeoutError, OSError, http_client.HTTPException) as exc:
                last_url_error = exc
                if not is_upstream_unavailable(exc) or time.time() >= deadline:
                    break
                time.sleep(0.45)
            except Exception as exc:
                self.log_error("Proxy %s failed: %s", app_id, exc)
                self.send_html(
                    "Proxy failed",
                    "<section><h2>Rocky proxy failed</h2>"
                    f"<p>{html.escape(app_id)}</p>"
                    f"<pre>{html.escape(f'{type(exc).__name__}: {exc}')}</pre></section>",
                    status=500,
                )
                return
        if last_url_error is not None and wants_upstream_wait_page(
            method, target_path, self.headers.get("Accept", ""), app_id
        ):
            self.proxy_response(
                HTTPStatus.SERVICE_UNAVAILABLE,
                upstream_starting_page(app_id),
                {
                    "Content-Type": "text/html; charset=utf-8",
                    "Cache-Control": "no-store",
                    "Retry-After": "2",
                },
                extra_headers=extra_headers,
                extra_cookies=extra_cookies,
            )
            return
        payload, headers = upstream_unavailable_error_response(app_id, target_path)
        self.proxy_response(
            HTTPStatus.SERVICE_UNAVAILABLE,
            payload,
            headers,
            extra_headers=extra_headers,
            extra_cookies=extra_cookies,
        )
        return

    def handle_apps(self) -> None:
        try:
            payload = self.apps_payload()
        except Exception as exc:
            self.log_error("Apps payload failed: %s", exc)
            self.send_html(
                "Apps",
                "<section><p>Apps page failed to load. "
                f"{html.escape(str(exc))}</p></section>",
            )
            return

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
                app_id_attr = f' data-app-id="{safe_app_id}"'
                actions_html += (
                    f'<a href="{safe_url}" target="_blank" rel="noopener" '
                    f'class="{launch_cls}"{app_id_attr}>&#x25BA; LAUNCH</a>'
                )
            elif entry.get("launchable"):
                safe_app_id = html.escape(eid)
                actions_html += (
                    f'<button class="{launch_cls}" data-app-id="{safe_app_id}" type="button">'
                    f"&#x25BA; LAUNCH</button>"
                )
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
function queueAppStart(appId) {{
  if (!appId) return;
  fetch('/api/apps/launch', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json', 'X-Rocky-CSRF': csrfToken}},
    body: JSON.stringify({{id: appId}})
  }}).then(async (response) => {{
    const payload = await response.json().catch(() => ({{}}));
    if (feedback) {{
      if (response.ok && payload.ok !== false) {{
        feedback.textContent = payload.status === 'starting'
          ? ((payload.app_id || appId) + ' is starting. Leave the new tab open; it retries until the app is ready.')
          : ('Opened ' + (payload.app_id || appId) + '.');
      }} else {{
        feedback.textContent = 'Launch failed for ' + appId + ': ' + (payload.error || payload.detail || response.status);
      }}
    }}
  }}).catch((error) => {{
    if (feedback) feedback.textContent = 'Launch failed for ' + appId + ': ' + error;
  }});
}}
document.querySelectorAll('a.btn-launch, button.btn-launch').forEach((link) => {{
  link.addEventListener('click', (event) => {{
    queueAppStart(link.dataset.appId);
    const url = link.getAttribute('href');
    if (!url) {{
      event.preventDefault();
      return;
    }}
    // Keep this tab on the Rocky console. Modified clicks already open a new tab
    // via the browser; a plain click must not fall through to window.location.
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) {{
      return;
    }}
    event.preventDefault();
    window.open(url, '_blank', 'noopener');
  }});
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

