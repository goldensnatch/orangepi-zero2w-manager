from __future__ import annotations

import gzip
import json
import re
import zlib
from urllib.parse import parse_qsl, urlencode, urlparse

from manager.runtime.media_stack import MEDIA_STACK_APPS, media_app_ids


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
_SKIP_UPSTREAM_REQUEST_HEADERS = HOP_BY_HOP_HEADERS | {
    "host",
    "cookie",
    "content-length",
    "expect",
    "accept-encoding",
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
_STATIC_ASSET_RE = re.compile(
    r"(?i)(?:^|/)[^/?]+\.(?:js|css|map|woff2?|ttf|png|jpe?g|gif|svg|ico)(?:\?|$)"
)
_ARR_URLBASE_RE = re.compile(r"""(urlBase\s*:\s*)(['"])(?:__URL_BASE__|/)?([^'"]*)\2""")


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


def static_asset_from_login_query(query: str) -> str | None:
    """Webpack sometimes treats /login?returnUrl=/ as publicPath and appends 194-hash.js."""
    for key, value in parse_qsl(str(query or ""), keep_blank_values=True):
        if key.lower() not in {"returnurl", "return_url", "returnto"}:
            continue
        candidate = urlparse(str(value or "").strip()).path or str(value or "").strip()
        if not _STATIC_ASSET_RE.search(candidate):
            continue
        if not candidate.startswith("/"):
            candidate = "/" + candidate
        return candidate
    return None


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


def is_static_asset_path(path: str) -> bool:
    return bool(_STATIC_ASSET_RE.search(str(path or "")))


def suppress_login_redirect_for_asset(path: str, location: str) -> bool:
    """Do not send browsers to /login HTML when a webpack chunk 302s."""
    if not is_static_asset_path(path):
        return False
    loc_path = (urlparse(str(location or "")).path or "").rstrip("/").lower()
    return loc_path.endswith("/login") or loc_path == "login"


def rewrite_arr_initialize_json(
    payload: bytes,
    app_id: str,
    content_type: str = "",
    path: str = "",
) -> bytes:
    """Force *arr initialize.json onto /proxy/<app> so webpack chunks stay on-prefix."""
    resource = str(path or "").split("?", 1)[0]
    kind = str(content_type or "").lower()
    looks_like_init = resource.endswith("initialize.json")
    if not looks_like_init and "json" not in kind:
        return payload
    try:
        data = json.loads(payload)
    except (TypeError, ValueError, UnicodeDecodeError):
        return payload
    if not isinstance(data, dict):
        return payload
    if not looks_like_init and "urlBase" not in data:
        return payload
    prefix = proxy_prefix(app_id)
    previous_base = str(data.get("urlBase") or "")
    api_root = str(data.get("apiRoot") or "")
    if previous_base and api_root.startswith(previous_base):
        api_root = api_root[len(previous_base):] or "/api/v1"
    if not api_root:
        api_root = "/api/v1"
    if not api_root.startswith("/"):
        api_root = "/" + api_root
    data["urlBase"] = prefix
    data["apiRoot"] = prefix + api_root
    return json.dumps(data).encode("utf-8")


def proxy_bridge_script(app_id: str) -> str:
    prefix = json.dumps(proxy_prefix(app_id))
    base = json.dumps(proxy_document_base(app_id))
    return (
        "<script>(function(p,b){"
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
        "var x=new URL(u,location.origin+b);"
        "if(x.host!==location.host)return u;"
        "if(skip(x.pathname))return u;"
        "var path=x.pathname,search=x.search;"
        "var ru=x.searchParams.get('returnUrl')||x.searchParams.get('returnurl');"
        "if((path===p+'/login'||path.slice(-6)==='/login')&&ru&&/\\.(js|css|map|woff2?|png|svg|ico)(\\?|$)/i.test(ru)){"
        "path=ru.charAt(0)==='/'?ru:'/'+ru;search='';}"
        "if(path!==p&&path.indexOf(p+'/')!==0){"
        "if(b.length>p.length+1&&path.charAt(0)==='/'&&/\\.(js|css|json|map|woff2?|png|svg|ico)(\\?|$)/i.test(path))"
        "path=b.replace(/\\/$/,'')+path;"
        "else path=p+(path.charAt(0)==='/'?path:'/'+path);}"
        "var next=path+search+x.hash;"
        "if(x.protocol==='ws:'||x.protocol==='wss:')return x.protocol+'//'+location.host+next;"
        "if(/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(u))return x.protocol+'//'+location.host+next;"
        "return next;"
        "}catch(e){return u;}}"
        "function hookHist(name){var orig=history[name];if(!orig)return;"
        "history[name]=function(s,t,u){if(u!==undefined&&u!==null&&u!==''){"
        "arguments[2]=rewrite(String(u));}return orig.apply(this,arguments);};}"
        "hookHist('pushState');hookHist('replaceState');"
        "function lock(obj,key,val){try{Object.defineProperty(obj,key,{configurable:true,enumerable:true,"
        "get:function(){return val;},set:function(){}});}catch(e){try{obj[key]=val;}catch(e2){}}}"
        "function pin(v){if(!v||typeof v!=='object')return v;lock(v,'urlBase',p);"
        "var root=v.apiRoot;if(typeof root==='string'&&root.indexOf(p)!==0){"
        "lock(v,'apiRoot',root.charAt(0)==='/'?p+root:p+'/'+root);}return v;}"
        "['Prowlarr','Radarr','Sonarr','Lidarr','Readarr','Whisparr'].forEach(function(n){"
        "var cur=window[n];"
        "try{Object.defineProperty(window,n,{configurable:true,get:function(){return cur;},"
        "set:function(v){cur=pin(v);}});}catch(e){}"
        "if(cur)pin(cur);});"
        "function hook(proto,prop){var d=Object.getOwnPropertyDescriptor(proto,prop);"
        "if(!d||!d.set)d=Object.getOwnPropertyDescriptor(HTMLElement.prototype,prop);"
        "if(!d||!d.set)return;"
        "Object.defineProperty(proto,prop,{configurable:true,enumerable:true,"
        "get:function(){return d.get.call(this);},"
        "set:function(v){d.set.call(this,rewrite(String(v)));}});} "
        "hook(HTMLScriptElement.prototype,'src');hook(HTMLLinkElement.prototype,'href');"
        "var sa=Element.prototype.setAttribute;"
        "Element.prototype.setAttribute=function(n,v){"
        "if(n&&(String(n).toLowerCase()==='src'||String(n).toLowerCase()==='href'))v=rewrite(String(v));"
        "return sa.call(this,n,v);};"
        "var f=window.fetch;"
        "if(f)window.fetch=function(i,n){"
        "if(typeof i==='string'||(typeof URL!=='undefined'&&i instanceof URL)"
        "||(typeof Request!=='undefined'&&i instanceof Request))i=rewrite(i);"
        "return f.call(this,i,n).then(function(r){"
        "var u='';try{u=typeof i==='string'?i:(i&&i.url)||'';}catch(e){}"
        "if(!/initialize\\.json/i.test(String(u)))return r;"
        "return r.clone().text().then(function(t){"
        "try{var data=JSON.parse(t);pin(data);"
        "return new Response(JSON.stringify(data),{status:r.status,statusText:r.statusText,headers:r.headers});}"
        "catch(e){return r;}});});};"
        "var o=XMLHttpRequest.prototype.open;"
        "XMLHttpRequest.prototype.open=function(m,u){arguments[1]=rewrite(u);return o.apply(this,arguments);};"
        "if(window.WebSocket){var W=window.WebSocket;window.WebSocket=function(u,pr){"
        "return pr===undefined?new W(rewrite(u)):new W(rewrite(u),pr);};window.WebSocket.prototype=W.prototype;}"
        "if(p==='/proxy/jellyseerr'){function fillHost(){var h=document.getElementById('hostname');"
        "if(!h||h.value)return;var d=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');"
        "if(d&&d.set)d.set.call(h,'jellyfin');else h.value='jellyfin';"
        "h.dispatchEvent(new Event('input',{bubbles:true}));h.dispatchEvent(new Event('change',{bubbles:true}));}"
        "var n=0,t=setInterval(function(){fillHost();if(++n>48)clearInterval(t);},250);}"
        "})(" + prefix + "," + base + ");</script>"
    )


def inject_proxy_bridge(html: str, app_id: str) -> str:
    snippet = f'<base href="{proxy_document_base(app_id)}">' + proxy_bridge_script(app_id)
    lower = html.lower()
    marker = "<head>"
    idx = lower.find(marker)
    if idx >= 0:
        insert = idx + len(marker)
        return html[:insert] + snippet + html[insert:]
    match = re.search(r"<head\s[^>]*>", html, flags=re.IGNORECASE)
    if match:
        insert = match.end()
        return html[:insert] + snippet + html[insert:]
    return snippet + html


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
    asset = static_asset_from_login_query(request_query)
    if asset:
        return f"{proxy_prefix(app_id)}{asset}"
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


def select_upstream_request_headers(headers) -> list[tuple[str, str]]:
    """Copy browser headers *arr/Jellyfin need (X-Api-Key, Authorization, …)."""
    forwarded: list[tuple[str, str]] = []
    items = headers.items() if hasattr(headers, "items") else []
    for name, value in items:
        lower = str(name or "").lower()
        if not value or lower in _SKIP_UPSTREAM_REQUEST_HEADERS:
            continue
        if lower.startswith("x-forwarded-"):
            continue
        forwarded.append((str(name), str(value)))
    return forwarded


def decode_upstream_payload(payload: bytes, headers) -> bytes:
    """Undo gzip/deflate so HTML/JSON rewriting sees real text, not binary."""
    body = payload if isinstance(payload, (bytes, bytearray)) else bytes(payload or b"")
    encoding = ""
    if headers is not None and hasattr(headers, "get"):
        encoding = str(headers.get("Content-Encoding") or headers.get("content-encoding") or "")
    first = encoding.split(",", 1)[0].strip().lower()
    try:
        if first in {"gzip", "x-gzip"} or body.startswith(b"\x1f\x8b"):
            return gzip.decompress(body)
        if first == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, EOFError, zlib.error):
        return bytes(body)
    return bytes(body)


def is_connection_refused(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", None)
    errno = getattr(reason, "errno", None) or getattr(exc, "errno", None)
    if errno in {111, 61}:
        return True
    text = str(exc).lower()
    return "connection refused" in text or "[errno 111]" in text


def wants_upstream_wait_page(method: str, path: str, accept: str = "") -> bool:
    if str(method or "GET").upper() != "GET":
        return False
    normalized = str(path or "/")
    if "/api/" in normalized or normalized.endswith(".json"):
        return False
    if is_static_asset_path(normalized):
        return False
    accept_l = str(accept or "").lower()
    if "application/json" in accept_l and "text/html" not in accept_l:
        return False
    return True


def proxied_app_display_name(app_id: str) -> str:
    spec = MEDIA_STACK_APPS.get(str(app_id or ""))
    if spec:
        return str(spec["name"])
    if app_id == "transfer-stack":
        return "Torrentz"
    label = re.sub(r"[^A-Za-z0-9 _.-]", "", str(app_id or "app"))
    return label or "app"


def upstream_starting_page(app_id: str) -> bytes:
    name = proxied_app_display_name(app_id)
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta http-equiv=\"refresh\" content=\"2\">"
        f"<title>Starting {name}</title>"
        "<style>body{font-family:system-ui,sans-serif;background:#111;color:#eee;"
        "display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}"
        "main{text-align:center;max-width:28rem;padding:1.5rem}p{color:#aaa;line-height:1.45}</style>"
        f"</head><body><main><h1>Starting {name}</h1>"
        "<p>The container is up. Waiting for the app to accept connections. "
        "This page retries automatically.</p></main></body></html>"
    ).encode("utf-8")


def proxy_prefix(app_id: str) -> str:
    return f"/proxy/{app_id}"


def proxy_document_base(app_id: str) -> str:
    prefix = proxy_prefix(app_id)
    if str(app_id) == "jellyfin":
        return f"{prefix}/web/"
    return f"{prefix}/"


def map_jellyfin_upstream_path(path: str) -> str:
    """Jellyfin serves the SPA under /web/; keep API routes at the server root."""
    normalized = str(path or "/") or "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if normalized in {"/", "/web"}:
        return "/web/"
    if normalized.startswith("/web/"):
        return normalized
    name = normalized.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if name in {"manifest.json", "serviceworker.js", "service-worker.js"} or is_static_asset_path(normalized):
        return "/web" + normalized
    return normalized


def rewrite_jellyseerr_jellyfin_connect_body(
    payload: bytes,
    public_hosts: set[str] | None = None,
) -> bytes:
    """Jellyseerr talks to Jellyfin from Docker; localhost/proxy URLs cannot reach it."""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError, UnicodeDecodeError):
        return payload
    if not isinstance(data, dict):
        return payload
    host_key = "hostname" if "hostname" in data else ("ip" if "ip" in data else None)
    if host_key is None:
        return payload
    if not any(key in data for key in ("username", "apiKey", "serverType", "urlBase", "useSsl")):
        return payload
    raw = str(data.get(host_key) or "").strip()
    if not raw:
        return payload
    candidate = raw if "://" in raw else f"http://{raw}"
    parsed = urlparse(candidate)
    host_only = str(parsed.hostname or raw.split("/")[0].split(":")[0]).strip().lower()
    url_base = str(data.get("urlBase") or "")
    public = {str(host).lower() for host in (public_hosts or set()) if host}
    jellyfin = MEDIA_STACK_APPS["jellyfin"]
    use_internal = host_only in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host_only in public
    if "proxy/jellyfin" in raw.lower() or "proxy/jellyfin" in url_base.lower():
        use_internal = True
    if not use_internal:
        return payload
    data[host_key] = str(jellyfin["compose_service"])
    data["port"] = int(jellyfin["port"])
    data["urlBase"] = ""
    data["useSsl"] = False
    return json.dumps(data).encode("utf-8")


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
    if app_id != "jellyseerr":
        text = _QUOTED_ROOT_RE.sub(rf"\g<quote>{prefix}/\g<path>\g<quote>", text)
        text = _ARR_URLBASE_RE.sub(rf'\1\2{prefix}\2', text)
        text = text.replace("__URL_BASE__", prefix)
    text = re.sub(r"(?is)<base\b[^>]*>", "", text)
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
