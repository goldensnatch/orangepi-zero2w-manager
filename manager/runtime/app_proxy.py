from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse

from manager.runtime.media_stack import media_app_ids


HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "content-encoding",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "www-authenticate",
}

_ROOT_ATTR_RE = re.compile(
    r'(?P<attr>(?:href|src|action)\s*=\s*["\'])/(?P<path>(?!proxy/)[^"\']*)',
    re.IGNORECASE,
)
_URL_FUNC_RE = re.compile(r"url\(/((?!proxy/))", re.IGNORECASE)
_QUOTED_ROOT_RE = re.compile(
    r'(?P<quote>["\'])/(?!/)(?!proxy/)(?P<path>[^"\']*)(?P=quote)'
)
_ROCKY_COOKIE_PREFIXES = ("rocky_session", "rocky_proxy_")
LAST_PROXY_APP_COOKIE = "rocky_last_proxy_app"
_CONSOLE_PATH_PREFIXES = (
    "/apps",
    "/logs",
    "/files",
    "/view",
    "/download",
    "/api/",
)
_APP_LOGIN_QUERY_KEYS = {"returnurl", "return_url", "returnto"}


def public_proxy_app_ids() -> frozenset[str]:
    return frozenset(media_app_ids()) | {"transfer-stack"}


def is_public_proxy_app(app_id: str) -> bool:
    return str(app_id or "") in public_proxy_app_ids()


def proxy_app_id_from_path(path: str) -> str | None:
    parts = [part for part in str(path or "").split("/") if part]
    if len(parts) >= 2 and parts[0] == "proxy" and parts[1]:
        return parts[1]
    return None


def proxied_app_login_location(
    *,
    next_path: str = "",
    referer: str = "",
    request_query: str = "",
    last_app_id: str = "",
) -> str | None:
    """Map a Rocky /login hit back to the entertainment app that owns it."""
    candidate = str(next_path or "").strip()
    if candidate.startswith("/proxy/"):
        return candidate
    if candidate == "/apps":
        return None
    referer_app = proxy_app_id_from_path(urlparse(referer or "").path)
    pairs = [
        (key, value)
        for key, value in parse_qsl(str(request_query or ""), keep_blank_values=True)
        if key != "next"
    ]
    looks_like_app_login = any(key.lower() in _APP_LOGIN_QUERY_KEYS for key, _ in pairs)
    app_id = referer_app or str(last_app_id or "").strip()
    if not app_id or not is_public_proxy_app(app_id):
        return None
    if not referer_app and not looks_like_app_login and not last_app_id:
        return None
    if looks_like_app_login or pairs:
        location = f"{proxy_prefix(app_id)}/login"
        if pairs:
            location += "?" + urlencode(pairs, doseq=True)
        return location
    return f"{proxy_prefix(app_id)}/"


def filter_browser_cookies_for_upstream(cookie_header: str) -> str:
    kept: list[str] = []
    for part in str(cookie_header or "").split(";"):
        item = part.strip()
        if not item:
            continue
        name = item.split("=", 1)[0].strip()
        if name in {"rocky_session", LAST_PROXY_APP_COOKIE} or name.startswith("rocky_proxy_"):
            continue
        kept.append(item)
    return "; ".join(kept)


def proxy_prefix(app_id: str) -> str:
    return f"/proxy/{app_id}"


def _is_rocky_console_path(path: str) -> bool:
    normalized = str(path or "/") or "/"
    if normalized == "/":
        return False
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in _CONSOLE_PATH_PREFIXES
    )


def _join_proxy_path(app_id: str, path: str, query: str = "", fragment: str = "") -> str:
    prefix = proxy_prefix(app_id)
    if not path.startswith("/"):
        path = "/" + path
    if path != prefix and not path.startswith(prefix + "/"):
        path = prefix + path
    rewritten = path
    if query:
        rewritten += f"?{query}"
    if fragment:
        rewritten += f"#{fragment}"
    return rewritten


def rewrite_upstream_location(
    location: str,
    app_id: str,
    upstream_base: str,
    public_hosts: set[str] | None = None,
) -> str:
    value = str(location or "").strip()
    if not value:
        return value
    prefix = proxy_prefix(app_id)
    parsed = urlparse(value)
    upstream = urlparse(upstream_base)
    public_hosts = {str(host) for host in (public_hosts or set()) if host}
    if not parsed.netloc:
        path, sep, rest = value.partition("?")
        if not path.startswith("/"):
            path = "/" + path
        if path == prefix or path.startswith(prefix + "/"):
            return value
        return prefix + path + (sep + rest if sep else "")
    upstream_hosts = {upstream.hostname, "127.0.0.1", "localhost"}
    if parsed.hostname in upstream_hosts:
        if upstream.port and parsed.port not in {None, upstream.port}:
            return value
        return _join_proxy_path(app_id, parsed.path or "/", parsed.query, parsed.fragment)
    if parsed.hostname in public_hosts:
        path = parsed.path or "/"
        if path == prefix or path.startswith(prefix + "/"):
            return _join_proxy_path(app_id, path, parsed.query, parsed.fragment)
        if _is_rocky_console_path(path):
            return value
        return _join_proxy_path(app_id, path, parsed.query, parsed.fragment)
    return value


def rewrite_cookie_header(value: str, app_id: str) -> str:
    prefix = proxy_prefix(app_id)
    parts: list[str] = []
    replaced_path = False
    for part in str(value or "").split(";"):
        item = part.strip()
        if item.lower().startswith("path="):
            parts.append(f"Path={prefix}/")
            replaced_path = True
        else:
            parts.append(item)
    if not replaced_path:
        parts.append(f"Path={prefix}/")
    return "; ".join(p for p in parts if p)


def rewrite_html_root_paths(payload: bytes, app_id: str, content_type: str) -> bytes:
    kind = str(content_type or "").lower()
    if not any(token in kind for token in ("html", "javascript", "json")):
        return payload
    prefix = proxy_prefix(app_id)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = payload.decode("latin-1")
    text = _ROOT_ATTR_RE.sub(rf"\g<attr>{prefix}/\g<path>", text)
    text = _URL_FUNC_RE.sub(rf"url({prefix}/", text)
    text = _QUOTED_ROOT_RE.sub(rf"\g<quote>{prefix}/\g<path>\g<quote>", text)
    return text.encode("utf-8")


def rewrite_upstream_headers(
    headers: dict[str, str],
    app_id: str,
    upstream_base: str,
    public_hosts: set[str] | None = None,
) -> dict[str, str]:
    rewritten: dict[str, str] = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS:
            continue
        if lower == "location":
            rewritten[name] = rewrite_upstream_location(
                value,
                app_id,
                upstream_base,
                public_hosts=public_hosts,
            )
        elif lower == "set-cookie":
            rewritten[name] = rewrite_cookie_header(value, app_id)
        else:
            rewritten[name] = value
    return rewritten
