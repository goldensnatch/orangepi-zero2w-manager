from __future__ import annotations

import json
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
    "/api/runtime",
    "/api/security",
    "/api/mode",
    "/api/network",
    "/api/transfer",
    "/api/storage",
    "/api/audit",
    "/api/apps",
)
_APP_LOGIN_QUERY_KEYS = {"returnurl", "return_url", "returnto"}
_CSP_HEADERS = {"content-security-policy", "content-security-policy-report-only", "x-webkit-csp"}


def public_proxy_app_ids() -> frozenset[str]:
    return frozenset(media_app_ids()) | {"transfer-stack"}


def is_public_proxy_app(app_id: str) -> bool:
    return str(app_id or "") in public_proxy_app_ids()


def proxy_app_id_from_path(path: str) -> str | None:
    parts = [part for part in str(path or "").split("/") if part]
    if len(parts) >= 2 and parts[0] == "proxy" and parts[1]:
        return parts[1]
    return None


def is_rocky_console_request(path: str) -> bool:
    normalized = str(path or "/") or "/"
    if normalized in {"/", "/login"}:
        return True
    return _is_rocky_console_path(normalized)


def leaked_proxy_app_id(
    *,
    path: str,
    referer: str = "",
    last_app_id: str = "",
) -> str | None:
    """Map a root-relative app request (e.g. /initialize.json) back to /proxy/<app>/."""
    if str(path or "").startswith("/proxy/") or is_rocky_console_request(path):
        return None
    app_id = proxy_app_id_from_path(urlparse(referer or "").path) or str(last_app_id or "").strip()
    if not app_id or not is_public_proxy_app(app_id):
        return None
    return app_id


def proxy_bridge_script(app_id: str) -> str:
    prefix = json.dumps(proxy_prefix(app_id))
    return (
        "<script>(function(p){"
        "if(window.__rockyPrefix)return;window.__rockyPrefix=p;"
        "function skip(path){"
        "var s=['/apps','/logs','/files','/view','/download','/api/runtime','/api/security',"
        "'/api/mode','/api/network','/api/transfer','/api/storage','/api/audit','/api/apps'];"
        "for(var i=0;i<s.length;i++){if(path===s[i]||path.indexOf(s[i]+'/')===0)return true;}"
        "return false;}"
        "function rewrite(u){"
        "if(typeof Request!=='undefined'&&u instanceof Request)return new Request(rewrite(u.url),u);"
        "if(typeof URL!=='undefined'&&u instanceof URL)return rewrite(u.href);"
        "if(typeof u!=='string')return u;"
        "try{"
        "var x=new URL(u,location.href);"
        "if(x.host!==location.host)return u;"
        "if(x.pathname===p||x.pathname.indexOf(p+'/')===0)return u;"
        "if(skip(x.pathname))return u;"
        "var next=p+x.pathname+x.search+x.hash;"
        "if(x.protocol==='ws:'||x.protocol==='wss:')return x.protocol+'//'+location.host+next;"
        "if(/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(u))return x.protocol+'//'+location.host+next;"
        "return next;"
        "}catch(e){return u;}}"
        "var f=window.fetch;"
        "if(f)window.fetch=function(i,n){if(typeof i==='string'||(typeof URL!=='undefined'&&i instanceof URL)"
        "||(typeof Request!=='undefined'&&i instanceof Request))i=rewrite(i);"
        "return f.call(this,i,n);};"
        "var o=XMLHttpRequest.prototype.open;"
        "XMLHttpRequest.prototype.open=function(m,u){arguments[1]=rewrite(u);return o.apply(this,arguments);};"
        "if(window.WebSocket){var W=window.WebSocket;window.WebSocket=function(u,pr){"
        "return pr===undefined?new W(rewrite(u)):new W(rewrite(u),pr);};window.WebSocket.prototype=W.prototype;}"
        "})(" + prefix + ");</script>"
    )


def inject_proxy_bridge(html: str, app_id: str) -> str:
    script = proxy_bridge_script(app_id)
    lower = html.lower()
    marker = "<head>"
    idx = lower.find(marker)
    if idx >= 0:
        insert = idx + len(marker)
        return html[:insert] + script + html[insert:]
    match = re.search(r"<head\s[^>]*>", html, flags=re.IGNORECASE)
    if match:
        insert = match.end()
        return html[:insert] + script + html[insert:]
    return script + html


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
    if "html" not in kind:
        return payload
    prefix = proxy_prefix(app_id)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = payload.decode("latin-1")
    text = _ROOT_ATTR_RE.sub(rf"\g<attr>{prefix}/\g<path>", text)
    text = _URL_FUNC_RE.sub(rf"url({prefix}/", text)
    text = _QUOTED_ROOT_RE.sub(rf"\g<quote>{prefix}/\g<path>\g<quote>", text)
    text = inject_proxy_bridge(text, app_id)
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
        if lower in HOP_BY_HOP_HEADERS or lower in _CSP_HEADERS:
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
