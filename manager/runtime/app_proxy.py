from __future__ import annotations

import re
from urllib.parse import urlparse


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
