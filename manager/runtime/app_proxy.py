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
) -> str | None:
    """Map a Rocky /login hit back to the entertainment app that owns it."""
    candidate = str(next_path or "").strip()
    if candidate.startswith("/proxy/"):
        return candidate
    app_id = proxy_app_id_from_path(urlparse(referer or "").path)
    if not app_id or not is_public_proxy_app(app_id):
        return None
    pairs = [
        (key, value)
        for key, value in parse_qsl(str(request_query or ""), keep_blank_values=True)
        if key != "next"
    ]
    location = f"{proxy_prefix(app_id)}/login"
    if pairs:
        location += "?" + urlencode(pairs, doseq=True)
    return location


def filter_browser_cookies_for_upstream(cookie_header: str) -> str:
    kept: list[str] = []
    for part in str(cookie_header or "").split(";"):
        item = part.strip()
        if not item:
            continue
        name = item.split("=", 1)[0].strip()
        if name == "rocky_session" or name.startswith(_ROCKY_COOKIE_PREFIXES[1]):
            continue
        kept.append(item)
    return "; ".join(kept)


def proxy_prefix(app_id: str) -> str:
    return f"/proxy/{app_id}"


def rewrite_upstream_location(location: str, app_id: str, upstream_base: str) -> str:
    value = str(location or "").strip()
    if not value:
        return value
    prefix = proxy_prefix(app_id)
    parsed = urlparse(value)
    upstream = urlparse(upstream_base)
    if not parsed.netloc:
        path, sep, rest = value.partition("?")
        if not path.startswith("/"):
            path = "/" + path
        if path == prefix or path.startswith(prefix + "/"):
            return value
        return prefix + path + (sep + rest if sep else "")
    upstream_hosts = {upstream.hostname, "127.0.0.1", "localhost"}
    if parsed.hostname not in upstream_hosts:
        return value
    if upstream.port and parsed.port not in {None, upstream.port}:
        return value
    path = parsed.path or "/"
    if path != prefix and not path.startswith(prefix + "/"):
        path = prefix + path
    rewritten = path
    if parsed.query:
        rewritten += f"?{parsed.query}"
    if parsed.fragment:
        rewritten += f"#{parsed.fragment}"
    return rewritten


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
    if "html" not in str(content_type or "").lower():
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
) -> dict[str, str]:
    rewritten: dict[str, str] = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS:
            continue
        if lower == "location":
            rewritten[name] = rewrite_upstream_location(value, app_id, upstream_base)
        elif lower == "set-cookie":
            rewritten[name] = rewrite_cookie_header(value, app_id)
        else:
            rewritten[name] = value
    return rewritten
