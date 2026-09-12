from __future__ import annotations

import gzip
import json
import re
import zlib
from urllib.parse import parse_qsl, urlencode, urlparse

from manager.runtime.media_stack import (
    MEDIA_STACK_APPS,
    TRANSFER_PROXY_APP_IDS,
    jellyfin_connect_hostname,
    media_app_ids,
    media_connect_hostname,
)


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
    "/favicon.ico",
    "/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png",
    "/robots.txt",
)
_CONSOLE_CHROME_NAMES = frozenset(
    {
        "favicon.ico",
        "apple-touch-icon.png",
        "apple-touch-icon-precomposed.png",
        "robots.txt",
    }
)
_APP_LOGIN_QUERY_KEYS = {"returnurl", "return_url", "returnto"}
_CSP_HEADERS = {"content-security-policy", "content-security-policy-report-only", "x-webkit-csp"}
_STATIC_ASSET_RE = re.compile(
    r"(?i)(?:^|/)[^/?]+\.(?:js|css|map|woff2?|ttf|png|jpe?g|gif|svg|ico)(?:\?|$)"
)
_ARR_URLBASE_RE = re.compile(r"""(urlBase\s*:\s*)(['"])(?:__URL_BASE__|/)?([^'"]*)\2""")
_JELLYSEERR_LEAK_PREFIXES = (
    "/api/v1/auth",
    "/api/v1/settings",
    "/api/v1/request",
    "/api/v1/user",
    "/api/v1/status",
    "/api/v1/discover",
    "/api/v1/issue",
    "/api/v1/override",
    "/_next/",
)
_ROOT_COOKIE_APPS = frozenset({"jellyseerr"})
_PROXY_BRIDGE_NAME = "__rocky_bridge.js"
_NEXT_DATA_RE = re.compile(
    r'(<script[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>)(.*?)(</script>)',
    re.DOTALL | re.IGNORECASE,
)
_CSP_META_RE = re.compile(
    r'(?is)<meta[^>]*http-equiv=["\']Content-Security-Policy["\'][^>]*>',
)
_ROOT_SERVICE_WORKERS = frozenset({"serviceworker.js", "service-worker.js", "sw.js"})


def public_proxy_app_ids() -> frozenset[str]:
    return frozenset(media_app_ids()) | TRANSFER_PROXY_APP_IDS


def is_public_proxy_app(app_id: str) -> bool:
    return str(app_id or "") in public_proxy_app_ids()


def proxy_app_id_from_path(path: str) -> str | None:
    parts = [part for part in str(path or "").split("/") if part]
    if len(parts) >= 2 and parts[0] == "proxy" and parts[1]:
        return parts[1]
    return None


def is_console_chrome_path(path: str) -> bool:
    """Origin-root browser chrome such as /favicon.ico — never a media proxy target."""
    resource = str(path or "").split("?", 1)[0]
    if not resource.startswith("/"):
        resource = "/" + resource
    if resource.count("/") != 1:
        return False
    name = resource.rsplit("/", 1)[-1].lower()
    if name in _CONSOLE_CHROME_NAMES:
        return True
    return name.startswith("apple-touch-icon")


def is_rocky_console_request(path: str) -> bool:
    normalized = str(path or "/") or "/"
    if normalized in {"/", "/login"}:
        return True
    if is_console_chrome_path(normalized):
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
    """Map a root-relative app request (e.g. /initialize.json) back to /proxy/<app>/.

    Console routes (`/`, `/apps`, `/login`, `/favicon.ico`) must never follow
    rocky_last_proxy_app or a /proxy/<app>/ Referer. Distinctive Jellyseerr
    prefixes still pin to jellyseerr so `/api/v1/auth*` is not sent to Jellyfin.
    """
    if str(path or "").startswith("/proxy/") or is_rocky_console_request(path):
        return None
    normalized = str(path or "")
    if _is_root_service_worker_path(normalized):
        return None
    if _is_jellyseerr_leaked_path(normalized):
        return "jellyseerr"
    referer_app = proxy_app_id_from_path(urlparse(referer or "").path)
    last_app = str(last_app_id or "").strip()
    if referer_app and is_public_proxy_app(referer_app) and referer_app != "jellyfin":
        return referer_app
    app_id = referer_app or last_app
    if app_id == "jellyfin" and normalized.startswith("/api/v1"):
        if last_app and last_app != "jellyfin" and is_public_proxy_app(last_app):
            return last_app
        return "jellyseerr"
    if not app_id or not is_public_proxy_app(app_id):
        return None
    return app_id


def _is_jellyseerr_leaked_path(path: str) -> bool:
    normalized = str(path or "")
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in _JELLYSEERR_LEAK_PREFIXES
    )


def _is_root_service_worker_path(path: str) -> bool:
    name = str(path or "").split("?", 1)[0].rsplit("/", 1)[-1].lower()
    return name in _ROOT_SERVICE_WORKERS


def is_proxy_bridge_path(remainder: str) -> bool:
    return str(remainder or "").split("?", 1)[0].rstrip("/") == _PROXY_BRIDGE_NAME


def is_static_asset_path(path: str) -> bool:
    return bool(_STATIC_ASSET_RE.search(str(path or "")))


def suppress_login_redirect_for_asset(path: str, location: str) -> bool:
    """Do not send browsers to /login HTML when a webpack chunk 302s."""
    if not is_static_asset_path(path):
        return False
    loc_path = (urlparse(str(location or "")).path or "").rstrip("/").lower()
    return loc_path.endswith("/login") or loc_path == "login"


def rewrite_jellyfin_system_info(
    payload: bytes,
    *,
    public_origin: str,
    path: str = "",
    content_type: str = "",
) -> bytes:
    """Pin Jellyfin discovery addresses onto /proxy/jellyfin, not Rocky :8090."""
    lowered = _upstream_path_only(path).lower()
    if "/system/info" not in lowered:
        return payload
    origin = str(public_origin or "").rstrip("/")
    if not origin:
        return payload
    try:
        data = json.loads(payload)
    except (TypeError, ValueError, UnicodeDecodeError):
        return payload
    if not isinstance(data, dict):
        return payload
    version = data.get("Version") or data.get("version")
    if not version:
        return payload
    data["Version"] = version
    advertised = origin + proxy_prefix("jellyfin")
    for key in ("LocalAddress", "WanAddress", "Address"):
        if data.get(key):
            data[key] = advertised
    return json.dumps(data).encode("utf-8")


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


def proxy_bridge_js(app_id: str) -> str:
    prefix = json.dumps(proxy_prefix(app_id))
    base = json.dumps(proxy_document_base(app_id))
    jf_host = json.dumps(jellyfin_connect_hostname())
    return (
        "(function(p,b,jfHost){"
        "if(window.__rockyPrefix)return;window.__rockyPrefix=p;"
        "try{if(navigator.serviceWorker){"
        "navigator.serviceWorker.getRegistrations().then(function(rs){rs.forEach(function(r){r.unregister();});});"
        "navigator.serviceWorker.register=function(){return Promise.resolve({unregister:function(){return Promise.resolve(true);}});};"
        "}}catch(e){}"
        "function skip(path){"
        "var s=['/apps','/logs','/files','/view','/download','/api/runtime','/api/security',"
        "'/api/mode','/api/network','/api/transfer','/api/storage','/api/audit','/api/apps',"
        "'/favicon.ico','/apple-touch-icon.png','/apple-touch-icon-precomposed.png','/robots.txt'];"
        "for(var i=0;i<s.length;i++){if(path===s[i]||path.indexOf(s[i]+'/')===0)return true;}"
        "return false;}"
        "function rewrite(u){"
        "if(typeof Request!=='undefined'&&u instanceof Request)return new Request(rewrite(u.url),u);"
        "if(typeof URL!=='undefined'&&u instanceof URL)return rewrite(u.href);"
        "if(typeof u!=='string')return u;"
        "try{"
        "var x=new URL(u,location.origin+b);"
        "if(x.host!==location.host){"
        "var hn=x.hostname;"
        "var loop=hn==='127.0.0.1'||hn==='localhost'||hn==='::1';"
        "if(!(p==='/proxy/jellyfin'&&loop&&String(x.port)==='8096'))return u;"
        "x=new URL(x.pathname+x.search+x.hash,location.origin);}"
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
        "try{if(window.__NEXT_DATA__&&typeof window.__NEXT_DATA__==='object'){"
        "window.__NEXT_DATA__.assetPrefix=p;}}catch(e){}"
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
        "function hookAxios(v){if(!v||!v.interceptors||!v.interceptors.request)return v;"
        "try{v.interceptors.request.use(function(c){if(c&&c.url)c.url=rewrite(String(c.url));return c;});}catch(e){}"
        "return v;}"
        "var ax=window.axios;"
        "try{Object.defineProperty(window,'axios',{configurable:true,get:function(){return ax;},"
        "set:function(v){ax=hookAxios(v);}});}catch(e){}"
        "if(ax)hookAxios(ax);"
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
        "if(p==='/proxy/jellyseerr'){function setv(el,val){if(!el)return;"
        "var d=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');"
        "if(d&&d.set)d.set.call(el,val);else el.value=val;"
        "el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));}"
        "function fillHost(){if(document.getElementById('apiKey'))return;"
        "var port=document.getElementById('port');var pv=(port&&port.value||'').trim();"
        "if(pv==='7878'||pv==='8989'||pv==='9696'||pv==='6767')return;"
        "var h=document.getElementById('hostname');if(h){var cur=(h.value||'').trim().toLowerCase();"
        "if(!cur||cur==='localhost'||cur==='127.0.0.1'||cur==='0.0.0.0'||cur==='jellyfin'||cur==='host.docker.internal'||cur.indexOf('proxy')>=0)setv(h,jfHost);}"
        "if(port){if(!pv||pv==='8090'||pv==='80')setv(port,'8096');}"
        "var ub=document.getElementById('urlBase');if(ub&&/proxy/i.test(ub.value||''))setv(ub,'');}"
        "var n=0,t=setInterval(function(){fillHost();if(++n>48)clearInterval(t);},250);fillHost();}"
        "})(" + prefix + "," + base + "," + jf_host + ");"
    )


def proxy_bridge_script(app_id: str) -> str:
    return f'<script src="{proxy_prefix(app_id)}/{_PROXY_BRIDGE_NAME}"></script>'


def rewrite_jellyseerr_next_data(text: str) -> str:
    prefix = proxy_prefix("jellyseerr")

    def repl(match) -> str:
        try:
            data = json.loads(match.group(2))
        except (TypeError, ValueError):
            return match.group(0)
        if not isinstance(data, dict):
            return match.group(0)
        data["assetPrefix"] = prefix
        return match.group(1) + json.dumps(data, separators=(",", ":")) + match.group(3)

    return _NEXT_DATA_RE.sub(repl, text, count=1)


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
    # Visiting /apps, /, or /login must show Rocky login. A leftover
    # rocky_last_proxy_app cookie must not bounce the console into Torrentz.
    if candidate and is_rocky_console_request(candidate):
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
    if not referer_app and not looks_like_app_login:
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


def select_upstream_request_headers(headers, *, app_id: str = "") -> list[tuple[str, str]]:
    """Copy browser headers *arr/Jellyfin need (X-Api-Key, Authorization, …)."""
    skip_origin = str(app_id or "") in TRANSFER_PROXY_APP_IDS
    forwarded: list[tuple[str, str]] = []
    items = headers.items() if hasattr(headers, "items") else []
    for name, value in items:
        lower = str(name or "").lower()
        if not value or lower in _SKIP_UPSTREAM_REQUEST_HEADERS:
            continue
        if lower.startswith("x-forwarded-"):
            continue
        if skip_origin and lower in {"origin", "referer", "authorization"}:
            continue
        forwarded.append((str(name), str(value)))
    return forwarded


def transfer_loopback_headers(upstream_base: str) -> dict[str, str]:
    """Make qBittorrent CSRF see the same origin as Host (127.0.0.1:8088).

    Browser Origin/Referer are the Rocky LAN URL. qBittorrent then logs
    'Referer header & Target origin mismatch' and /api/v2/sync/maindata
    returns 403, so the WebUI looks empty after login.
    """
    parsed = urlparse(str(upstream_base or "http://127.0.0.1:8088"))
    origin = f"{parsed.scheme or 'http'}://{parsed.netloc or '127.0.0.1:8088'}"
    return {"Origin": origin, "Referer": origin.rstrip("/") + "/"}


def proxied_response_headers(
    message,
    app_id: str,
    upstream_base: str,
    public_hosts: set[str] | None = None,
    *,
    keep_auth_challenge: bool = False,
) -> tuple[dict[str, str], list[str]]:
    header_map = {
        str(name): str(value)
        for name, value in (message.items() if hasattr(message, "items") else [])
        if str(name).lower() != "set-cookie"
    }
    headers = rewrite_upstream_headers(
        header_map,
        app_id,
        upstream_base,
        public_hosts=public_hosts,
        keep_auth_challenge=keep_auth_challenge,
    )
    cookies = [rewrite_cookie_header(value, app_id) for value in iter_set_cookie_headers(message)]
    return headers, cookies


def iter_set_cookie_headers(message) -> list[str]:
    """Keep every Set-Cookie. Dict-collapsing headers drops qBittorrent's SID."""
    if message is None:
        return []
    if hasattr(message, "get_all"):
        values = message.get_all("Set-Cookie") or message.get_all("set-cookie") or []
        cookies = [str(value) for value in values if value]
        if cookies:
            return cookies
    return [
        str(value)
        for name, value in (message.items() if hasattr(message, "items") else [])
        if str(name).lower() == "set-cookie" and value
    ]


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


_UNAVAILABLE_ERRNOS = {32, 61, 101, 103, 104, 110, 111, 113}
_UNAVAILABLE_EXC_NAMES = {
    "timeout",
    "timeouterror",
    "remotedisconnected",
    "incompleteread",
    "badstatusline",
    "connectionreseterror",
    "connectionabortederror",
    "brokenpipeerror",
}
_UNAVAILABLE_TEXT = (
    "connection reset",
    "connection aborted",
    "connection refused",
    "timed out",
    "timeout",
    "remotedisconnected",
    "incomplete read",
    "bad status line",
    "network is unreachable",
    "temporarily unavailable",
    "broken pipe",
    "[errno 111]",
    "[errno 104]",
)


def is_connection_refused(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", None)
    errno = getattr(reason, "errno", None) or getattr(exc, "errno", None)
    if errno in {111, 61}:
        return True
    text = str(exc).lower()
    return "connection refused" in text or "[errno 111]" in text


def is_upstream_unavailable(exc: BaseException) -> bool:
    """True when qBittorrent/media is down, resetting, or still binding the port.

    urllib often wraps these as URLError, but getresponse() can raise
    RemoteDisconnected / TimeoutError unwrapped — that used to become a
    stock HTTP 500 "Rocky console request failed" page.
    """
    if is_connection_refused(exc):
        return True
    chain: list[BaseException] = [exc]
    reason = getattr(exc, "reason", None)
    if isinstance(reason, BaseException):
        chain.append(reason)
    for item in chain:
        errno = getattr(item, "errno", None)
        if errno in _UNAVAILABLE_ERRNOS:
            return True
        if type(item).__name__.lower() in _UNAVAILABLE_EXC_NAMES:
            return True
    text = str(exc).lower()
    return any(needle in text for needle in _UNAVAILABLE_TEXT)


_JELLYFIN_REST_ROOTS = frozenset(
    {
        "activitylog",
        "albums",
        "artists",
        "audio",
        "auth",
        "branding",
        "channels",
        "clientlog",
        "collections",
        "connect",
        "devices",
        "displaypreferences",
        "environment",
        "genres",
        "images",
        "items",
        "library",
        "livetv",
        "localization",
        "lyrics",
        "mediainfo",
        "mediasegments",
        "movies",
        "musicalbums",
        "musicgenres",
        "notifications",
        "packages",
        "persons",
        "playback",
        "playbackinfo",
        "playingitems",
        "playlists",
        "playstate",
        "plugins",
        "quickconnect",
        "remoteimage",
        "scheduledtasks",
        "search",
        "sessions",
        "shows",
        "startup",
        "studios",
        "subtitle",
        "subtitles",
        "syncplay",
        "system",
        "trailers",
        "trickplay",
        "universalaudio",
        "useritems",
        "userlibrary",
        "users",
        "userviews",
        "videos",
        "webhooks",
        "years",
    }
)
_DEFAULT_PROXY_RETRY_SECONDS = 1.8
_HEALTH_PROXY_RETRY_SECONDS = 15.0
_DOCUMENT_WAIT_PATHS = frozenset({"/", "/index.html", "/login"})


def _upstream_path_only(path: str) -> str:
    normalized = str(path or "/") or "/"
    path_only = normalized.split("?", 1)[0]
    if not path_only.startswith("/"):
        path_only = "/" + path_only
    return path_only


def accept_is_json_ish(accept: str) -> bool:
    accept_l = str(accept or "").lower()
    if "text/html" in accept_l:
        return False
    return (
        "application/json" in accept_l
        or "text/json" in accept_l
        or "+json" in accept_l
    )


def is_jellyfin_spa_path(path: str) -> bool:
    lowered = _upstream_path_only(path).lower()
    return lowered == "/web" or lowered.startswith("/web/")


def is_jellyfin_rest_path(path: str) -> bool:
    """Jellyfin REST lives at the server root; the SPA is under /web/."""
    if is_jellyfin_spa_path(path):
        return False
    lowered = _upstream_path_only(path).lower()
    root = lowered.strip("/").split("/", 1)[0]
    return bool(root) and root in _JELLYFIN_REST_ROOTS


def is_jellyfin_public_system_info_path(path: str) -> bool:
    return _upstream_path_only(path).lower().rstrip("/") == "/system/info/public"


def is_media_status_health_path(path: str) -> bool:
    return _upstream_path_only(path).lower().rstrip("/") == "/api/v1/system/status"


def is_media_health_endpoint(path: str) -> bool:
    """Jellyfin public info and *arr/Jellyseerr system status — not CSS/JS."""
    return is_jellyfin_public_system_info_path(path) or is_media_status_health_path(path)


def is_media_document_path(path: str) -> bool:
    """HTML GET of `/`, `/web/`, or `/login` — not REST under any media app."""
    lowered = _upstream_path_only(path).lower()
    if is_jellyfin_spa_path(lowered):
        return True
    normalized = lowered.rstrip("/") or "/"
    return normalized in _DOCUMENT_WAIT_PATHS or lowered in _DOCUMENT_WAIT_PATHS


def proxy_retry_seconds(app_id: str, path: str) -> float:
    """Retry connection-refused briefly; health probes get a longer window."""
    if str(app_id) in MEDIA_STACK_APPS and is_media_health_endpoint(path):
        return _HEALTH_PROXY_RETRY_SECONDS
    return _DEFAULT_PROXY_RETRY_SECONDS


def wants_upstream_wait_page(
    method: str,
    path: str,
    accept: str = "",
    app_id: str = "",
) -> bool:
    """HTML wait page is for document navigations, never JSON/REST API calls."""
    if str(method or "GET").upper() != "GET":
        return False
    path_only = _upstream_path_only(path)
    lowered = path_only.lower()
    if "/api/" in lowered or lowered.endswith(".json"):
        return False
    if is_static_asset_path(path_only):
        return False
    if accept_is_json_ish(accept):
        return False
    if is_jellyfin_rest_path(path_only):
        return False
    if str(app_id or "") in MEDIA_STACK_APPS and not is_media_document_path(path_only):
        return False
    return True


def upstream_unavailable_api_payload(app_id: str) -> bytes:
    name = proxied_app_display_name(app_id)
    return json.dumps(
        {
            "error": "upstream_unavailable",
            "app": str(app_id or ""),
            "message": f"{name} is not reachable",
        },
        separators=(",", ":"),
    ).encode("utf-8")


def upstream_unavailable_error_response(app_id: str, path: str) -> tuple[bytes, dict[str, str]]:
    """JSON for REST/XHR, plain 503 for static assets — never an HTML wait page."""
    if is_static_asset_path(path):
        payload: bytes = b"upstream unavailable\n"
        content_type = "text/plain; charset=utf-8"
    else:
        payload = upstream_unavailable_api_payload(app_id)
        content_type = "application/json; charset=utf-8"
    return payload, {
        "Content-Type": content_type,
        "Cache-Control": "no-store",
        "Retry-After": "2",
    }


def proxied_app_display_name(app_id: str) -> str:
    spec = MEDIA_STACK_APPS.get(str(app_id or ""))
    if spec:
        return str(spec["name"])
    if app_id in TRANSFER_PROXY_APP_IDS:
        return "Torrentz"
    label = re.sub(r"[^A-Za-z0-9 _.-]", "", str(app_id or "app"))
    return label or "app"


def upstream_starting_page(app_id: str) -> bytes:
    name = proxied_app_display_name(app_id)
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta http-equiv=\"refresh\" content=\"2\">"
        "<link rel=\"icon\" href=\"/favicon.ico\">"
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


_ARR_APP_BY_PORT = {
    7878: "radarr",
    8989: "sonarr",
    9696: "prowlarr",
    6767: "bazarr",
}
_ARR_APP_IDS = ("radarr", "sonarr", "prowlarr", "bazarr")


def _jellyseerr_media_target(data: dict, path: str = "") -> str | None:
    lowered = str(path or "").lower()
    for app_id in (*_ARR_APP_IDS, "jellyfin"):
        if re.search(rf"(?:^|/){re.escape(app_id)}(?:/|$)", lowered):
            return app_id
    haystack = " ".join(
        str(data.get(key) or "")
        for key in ("hostname", "ip", "baseUrl", "urlBase")
    ).lower()
    for app_id in _ARR_APP_IDS:
        if app_id in haystack or f"/proxy/{app_id}" in haystack:
            return app_id
    if "jellyfin" in haystack or "/proxy/jellyfin" in haystack:
        return "jellyfin"
    try:
        port = int(data.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if port in _ARR_APP_BY_PORT:
        return _ARR_APP_BY_PORT[port]
    return None


def _pin_jellyseerr_upstream(data: dict, app_id: str) -> dict:
    spec = MEDIA_STACK_APPS[app_id]
    host = media_connect_hostname(app_id)
    data["hostname"] = host
    data["ip"] = host
    data["port"] = int(spec["port"])
    data["useSsl"] = False
    data["urlBase"] = ""
    if app_id != "jellyfin":
        data["baseUrl"] = ""
    else:
        data["serverType"] = 2  # Jellyseerr MediaServerType.JELLYFIN
    return data


def rewrite_jellyseerr_jellyfin_connect_body(
    payload: bytes,
    public_hosts: set[str] | None = None,
    path: str = "",
) -> bytes:
    """Jellyseerr talks to Jellyfin/*arr from Docker; the browser URL cannot reach them."""
    del public_hosts
    try:
        data = json.loads(payload)
    except (TypeError, ValueError, UnicodeDecodeError):
        return payload
    if not isinstance(data, dict):
        return payload
    jellyfin_login = any(key in data for key in ("username", "password", "email"))
    if jellyfin_login:
        if not any(key in data for key in ("hostname", "port", "useSsl", "urlBase", "ip", "serverType")):
            return payload
        _pin_jellyseerr_upstream(data, "jellyfin")
        return json.dumps(data).encode("utf-8")
    target = _jellyseerr_media_target(data, path)
    if target not in _ARR_APP_IDS:
        return payload
    if "apiKey" not in data and "hostname" not in data and "port" not in data:
        return payload
    _pin_jellyseerr_upstream(data, target)
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


def cookie_path_for_app(app_id: str) -> str:
    """Jellyseerr and qBittorrent call /api/v2 at the origin root as well as /proxy/<app>/."""
    if str(app_id) in _ROOT_COOKIE_APPS or str(app_id) in TRANSFER_PROXY_APP_IDS:
        return "/"
    return f"{proxy_prefix(app_id)}/"


def rewrite_cookie_header(value: str, app_id: str) -> str:
    path = cookie_path_for_app(app_id)
    parts: list[str] = []
    replaced_path = False
    for part in str(value or "").split(";"):
        item = part.strip()
        if item.lower().startswith("path="):
            parts.append(f"Path={path}")
            replaced_path = True
        else:
            parts.append(item)
    if not replaced_path:
        parts.append(f"Path={path}")
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
    text = _CSP_META_RE.sub("", text)
    if app_id not in {"jellyseerr"} | TRANSFER_PROXY_APP_IDS:
        text = _QUOTED_ROOT_RE.sub(rf"\g<quote>{prefix}/\g<path>\g<quote>", text)
        text = _ARR_URLBASE_RE.sub(rf'\1\2{prefix}\2', text)
        text = text.replace("__URL_BASE__", prefix)
    elif app_id == "jellyseerr":
        text = rewrite_jellyseerr_next_data(text)
    text = re.sub(r"(?is)<base\b[^>]*>", "", text)
    text = inject_proxy_bridge(text, app_id)
    return text.encode("utf-8")


def rewrite_upstream_headers(
    headers: dict[str, str],
    app_id: str,
    upstream_base: str,
    public_hosts: set[str] | None = None,
    *,
    keep_auth_challenge: bool = False,
) -> dict[str, str]:
    rewritten: dict[str, str] = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS or lower in _CSP_HEADERS:
            if not (keep_auth_challenge and lower == "www-authenticate"):
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
