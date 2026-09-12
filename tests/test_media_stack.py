from __future__ import annotations

import json
import unittest
from pathlib import Path

from manager.api.runtime_status import DEFAULT_DOCKER_CONTAINERS
from manager.runtime.media_stack import (
    MEDIA_STACK_APPS,
    entertainment_menu_items,
    media_container_names,
    media_launch_target,
)


ROOT = Path(__file__).resolve().parents[1]


class MediaStackTests(unittest.TestCase):
    def test_covers_operator_media_apps(self) -> None:
        self.assertEqual(
            set(MEDIA_STACK_APPS),
            {"jellyfin", "jellyseerr", "prowlarr", "radarr", "sonarr", "bazarr"},
        )

    def test_launch_targets_use_loopback_not_hardcoded_lan_ip(self) -> None:
        for app_id in MEDIA_STACK_APPS:
            target = media_launch_target(app_id)
            assert target is not None
            self.assertTrue(str(target["url"]).startswith("http://127.0.0.1:"))
            self.assertNotIn("192.168.1.199", str(target["url"]))
            self.assertEqual(target["container"], MEDIA_STACK_APPS[app_id]["container"])

    def test_entertainment_menu_lists_media_apps_and_transfer(self) -> None:
        items = entertainment_menu_items()
        ids = [item["id"] for item in items]
        self.assertEqual(ids[0], "transfer-stack")
        for app_id in MEDIA_STACK_APPS:
            self.assertIn(app_id, ids)
        self.assertNotIn("stashapp", ids)

    def test_runtime_status_tracks_current_media_containers(self) -> None:
        for name in media_container_names():
            self.assertIn(name, DEFAULT_DOCKER_CONTAINERS)
        self.assertNotIn("rocky-media-stashapp", DEFAULT_DOCKER_CONTAINERS)

    def test_services_catalog_activates_entertainment_mode(self) -> None:
        catalog = json.loads((ROOT / "config" / "services.json").read_text(encoding="utf-8"))
        for app_id, spec in MEDIA_STACK_APPS.items():
            service = catalog[app_id]
            self.assertEqual(service["activate_mode"], "entertainment")
            self.assertEqual(service["docker_container"], spec["container"])
            self.assertEqual(service["url"], spec["local_url"])

    def test_proxy_tokens_round_trip_with_shared_secret(self) -> None:
        import os
        from manager.runtime import proxy_tokens

        secret = "unit-test-proxy-secret"
        previous = os.environ.get("ROCKY_WEB_PROXY_TOKEN_SECRET")
        os.environ["ROCKY_WEB_PROXY_TOKEN_SECRET"] = secret
        try:
            token = proxy_tokens.build_proxy_token("prowlarr", ttl_seconds=120)
            self.assertTrue(proxy_tokens.validate_proxy_token(token, "prowlarr"))
            self.assertFalse(proxy_tokens.validate_proxy_token(token, "radarr"))
            self.assertFalse(proxy_tokens.validate_proxy_token("not-a-token", "prowlarr"))
            torrent_token = proxy_tokens.build_proxy_token("torrentz", ttl_seconds=120)
            self.assertTrue(proxy_tokens.validate_proxy_token(torrent_token, "transfer-stack"))
            self.assertTrue(proxy_tokens.validate_proxy_token(torrent_token, "torrentz"))
        finally:
            if previous is None:
                os.environ.pop("ROCKY_WEB_PROXY_TOKEN_SECRET", None)
            else:
                os.environ["ROCKY_WEB_PROXY_TOKEN_SECRET"] = previous

    def test_safe_next_path_rejects_external_redirects(self) -> None:
        from manager.web.console import _safe_next_path

        self.assertEqual(_safe_next_path("/apps"), "/apps")
        self.assertEqual(_safe_next_path("/proxy/prowlarr/"), "/proxy/prowlarr/")
        self.assertEqual(_safe_next_path("https://example.com"), "/apps")
        self.assertEqual(_safe_next_path("//evil.example"), "/apps")
        from manager.runtime.app_proxy import rewrite_html_root_paths, rewrite_upstream_location

        self.assertEqual(
            rewrite_upstream_location("/login?returnUrl=%2F", "prowlarr", "http://127.0.0.1:9696/"),
            "/proxy/prowlarr/login?returnUrl=%2F",
        )
        self.assertEqual(
            rewrite_upstream_location("http://127.0.0.1:9696/login", "prowlarr", "http://127.0.0.1:9696/"),
            "/proxy/prowlarr/login",
        )
        self.assertEqual(
            rewrite_upstream_location(
                "http://192.168.1.216:8090/login?returnUrl=%2F",
                "prowlarr",
                "http://127.0.0.1:9696/",
                public_hosts={"192.168.1.216"},
            ),
            "/proxy/prowlarr/login?returnUrl=%2F",
        )
        self.assertEqual(
            rewrite_upstream_location(
                "http://192.168.1.216:8090/web/",
                "jellyfin",
                "http://127.0.0.1:8096/",
                public_hosts={"192.168.1.216"},
            ),
            "/proxy/jellyfin/web/",
        )
        self.assertEqual(
            rewrite_upstream_location(
                "http://192.168.1.216:8090/apps",
                "prowlarr",
                "http://127.0.0.1:9696/",
                public_hosts={"192.168.1.216"},
            ),
            "http://192.168.1.216:8090/apps",
        )
        html = rewrite_html_root_paths(
            b'<form action="/login"><a href="/Content/logo.svg"></a></form>',
            "prowlarr",
            "text/html",
        )
        self.assertIn(b'action="/proxy/prowlarr/login"', html)
        self.assertIn(b'href="/proxy/prowlarr/Content/logo.svg"', html)
        js_html = rewrite_html_root_paths(
            b'<html><head></head><script>window.location="/login?returnUrl=%2F"</script></html>',
            "radarr",
            "text/html",
        )
        self.assertIn(b'window.location="/proxy/radarr/login?returnUrl=%2F"', js_html)
        self.assertIn(b"/proxy/radarr/__rocky_bridge.js", js_html)
        self.assertIn(b'<base href="/proxy/radarr/">', js_html)
        html_urlbase = rewrite_html_root_paths(
            b'<html><head></head><script>window.Prowlarr={urlBase:""}</script></html>',
            "prowlarr",
            "text/html",
        )
        self.assertIn(b'urlBase:"/proxy/prowlarr"', html_urlbase)
        from manager.runtime.app_proxy import static_asset_from_login_query

        self.assertEqual(
            static_asset_from_login_query("returnUrl=%2F194-4b970a3e3dd59743b5ea.js"),
            "/194-4b970a3e3dd59743b5ea.js",
        )
        self.assertIsNone(static_asset_from_login_query("returnUrl=%2F"))
        json_payload = rewrite_html_root_paths(
            b'{"save_path":"/data/downloads","apiRoot":"/api/v1"}',
            "prowlarr",
            "application/json",
        )
        self.assertEqual(json_payload, b'{"save_path":"/data/downloads","apiRoot":"/api/v1"}')
        torrent_html = rewrite_html_root_paths(
            b'<html><head></head><script>fetch("/api/v2/auth/login")</script></html>',
            "transfer-stack",
            "text/html",
        )
        self.assertIn(b'fetch("/api/v2/auth/login")', torrent_html)
        self.assertIn(b"/proxy/transfer-stack/__rocky_bridge.js", torrent_html)
        from manager.runtime.app_proxy import (
            rewrite_arr_initialize_json,
            suppress_login_redirect_for_asset,
        )

        rewritten_init = json.loads(
            rewrite_arr_initialize_json(
                b'{\n "apiRoot": "/api/v1",\n "urlBase": "",\n "theme": "auto"\n}\n',
                "prowlarr",
                "application/json",
                "/initialize.json",
            )
        )
        self.assertEqual(rewritten_init["urlBase"], "/proxy/prowlarr")
        self.assertEqual(rewritten_init["apiRoot"], "/proxy/prowlarr/api/v1")
        self.assertEqual(rewritten_init["theme"], "auto")
        prefixed_init = json.loads(
            rewrite_arr_initialize_json(
                b'{"apiRoot":"/prowlarr/api/v1","urlBase":"/prowlarr"}',
                "prowlarr",
                "application/json",
                "/initialize.json",
            )
        )
        self.assertEqual(prefixed_init["urlBase"], "/proxy/prowlarr")
        self.assertEqual(prefixed_init["apiRoot"], "/proxy/prowlarr/api/v1")
        untouched_qbit = rewrite_arr_initialize_json(
            b'{"save_path":"/data/downloads","apiRoot":"/api/v1"}',
            "prowlarr",
            "application/json",
            "/api/v2/app/preferences",
        )
        self.assertEqual(
            untouched_qbit,
            b'{"save_path":"/data/downloads","apiRoot":"/api/v1"}',
        )
        self.assertTrue(
            suppress_login_redirect_for_asset(
                "/194-4b970a3e3dd59743b5ea.js",
                "/login?returnUrl=%2F194-4b970a3e3dd59743b5ea.js",
            )
        )
        self.assertTrue(
            suppress_login_redirect_for_asset(
                "/194-4b970a3e3dd59743b5ea.js",
                "/proxy/prowlarr/login?returnUrl=%2F194-4b970a3e3dd59743b5ea.js",
            )
        )
        self.assertFalse(suppress_login_redirect_for_asset("/", "/login?returnUrl=%2F"))
        dual_base = rewrite_html_root_paths(
            b'<html><head><base href="/"></head></html>',
            "prowlarr",
            "text/html",
        )
        self.assertEqual(dual_base.count(b"<base "), 1)
        self.assertIn(b'<base href="/proxy/prowlarr/">', dual_base)
        jellyfin_html = rewrite_html_root_paths(
            b'<html><head><base href="/web/"></head><link rel="stylesheet" href="main.css"></html>',
            "jellyfin",
            "text/html",
        )
        self.assertIn(b'<base href="/proxy/jellyfin/web/">', jellyfin_html)
        self.assertNotIn(b'<base href="/proxy/jellyfin/">', jellyfin_html)
        from manager.runtime.app_proxy import map_jellyfin_upstream_path

        self.assertEqual(map_jellyfin_upstream_path("/"), "/web/")
        self.assertEqual(
            map_jellyfin_upstream_path("/46967.bundle.js"),
            "/web/46967.bundle.js",
        )
        self.assertEqual(map_jellyfin_upstream_path("/manifest.json"), "/web/manifest.json")
        self.assertEqual(map_jellyfin_upstream_path("/web/main.css"), "/web/main.css")
        self.assertEqual(map_jellyfin_upstream_path("/Users/authenticatebyname"), "/Users/authenticatebyname")
        self.assertEqual(map_jellyfin_upstream_path("/System/Info/Public"), "/System/Info/Public")
        self.assertEqual(map_jellyfin_upstream_path("/Sessions"), "/Sessions")
        self.assertEqual(map_jellyfin_upstream_path("/Items/abc"), "/Items/abc")
        self.assertFalse(map_jellyfin_upstream_path("/System/Info/Public").startswith("/web/"))
        from manager.runtime.app_proxy import rewrite_jellyseerr_jellyfin_connect_body
        from manager.runtime.media_stack import jellyfin_connect_hostname

        expected_host = jellyfin_connect_hostname()
        rewritten_connect = json.loads(
            rewrite_jellyseerr_jellyfin_connect_body(
                b'{"hostname":"127.0.0.1","port":8096,"urlBase":"","username":"admin","serverType":1}',
                public_hosts={"192.168.1.213"},
            )
        )
        self.assertEqual(rewritten_connect["hostname"], expected_host)
        self.assertEqual(rewritten_connect["port"], 8096)
        self.assertEqual(rewritten_connect["urlBase"], "")
        self.assertEqual(rewritten_connect["serverType"], 2)
        proxied_connect = json.loads(
            rewrite_jellyseerr_jellyfin_connect_body(
                b'{"hostname":"192.168.1.213","port":8090,"urlBase":"/proxy/jellyfin","username":"admin"}',
                public_hosts={"192.168.1.213"},
            )
        )
        self.assertEqual(proxied_connect["hostname"], expected_host)
        self.assertEqual(proxied_connect["urlBase"], "")
        untouched_connect = json.loads(
            rewrite_jellyseerr_jellyfin_connect_body(
                b'{"hostname":"jellyfin","port":8096,"username":"admin"}',
                public_hosts={"192.168.1.213"},
            )
        )
        self.assertEqual(untouched_connect["hostname"], expected_host)
        empty_host = json.loads(
            rewrite_jellyseerr_jellyfin_connect_body(
                b'{"hostname":"","port":8090,"urlBase":"","username":"admin"}'
            )
        )
        self.assertEqual(empty_host["hostname"], expected_host)
        self.assertEqual(empty_host["port"], 8096)
        lan_host = json.loads(
            rewrite_jellyseerr_jellyfin_connect_body(
                b'{"hostname":"orangepizero2w","port":8096,"username":"admin"}',
                public_hosts={"192.168.1.213"},
            )
        )
        self.assertEqual(lan_host["hostname"], expected_host)
        self.assertEqual(lan_host["port"], 8096)
        relogin = json.loads(
            rewrite_jellyseerr_jellyfin_connect_body(b'{"username":"admin","password":"secret"}')
        )
        self.assertEqual(relogin["username"], "admin")
        self.assertNotIn("hostname", relogin)
        from manager.runtime.media_stack import media_connect_hostname

        radarr_host = media_connect_hostname("radarr")
        radarr_test = json.loads(
            rewrite_jellyseerr_jellyfin_connect_body(
                b'{"hostname":"127.0.0.1","port":7878,"apiKey":"radarr-key","useSsl":true,"baseUrl":"/proxy/radarr"}',
                public_hosts={"192.168.1.213"},
                path="/api/v1/settings/radarr/test",
            )
        )
        self.assertEqual(radarr_test["hostname"], radarr_host)
        self.assertEqual(radarr_test["port"], 7878)
        self.assertEqual(radarr_test["apiKey"], "radarr-key")
        self.assertEqual(radarr_test["baseUrl"], "")
        self.assertFalse(radarr_test["useSsl"])
        sonarr_host = media_connect_hostname("sonarr")
        sonarr_test = json.loads(
            rewrite_jellyseerr_jellyfin_connect_body(
                b'{"hostname":"192.168.1.213","port":8989,"apiKey":"sonarr-key"}',
                public_hosts={"192.168.1.213"},
            )
        )
        self.assertEqual(sonarr_test["hostname"], sonarr_host)
        self.assertEqual(sonarr_test["port"], 8989)
        proxied_radarr = json.loads(
            rewrite_jellyseerr_jellyfin_connect_body(
                b'{"hostname":"192.168.1.213","port":8090,"apiKey":"x","baseUrl":"/proxy/radarr"}',
                public_hosts={"192.168.1.213"},
                path="/api/v1/settings/radarr/test",
            )
        )
        self.assertEqual(proxied_radarr["hostname"], radarr_host)
        self.assertEqual(proxied_radarr["port"], 7878)
        untouched_unrelated = json.loads(
            rewrite_jellyseerr_jellyfin_connect_body(b'{"title":"The Radarr Movie"}')
        )
        self.assertEqual(untouched_unrelated["title"], "The Radarr Movie")
        self.assertNotIn("hostname", untouched_unrelated)
        next_html = rewrite_html_root_paths(
            b'<html><head></head><script id="__NEXT_DATA__" type="application/json">{"page":"/setup"}</script></html>',
            "jellyseerr",
            "text/html",
        )
        self.assertIn(b'"page":"/setup"', next_html)
        self.assertIn(b'"assetPrefix":"/proxy/jellyseerr"', next_html)
        self.assertIn(b"/proxy/jellyseerr/__rocky_bridge.js", next_html)
        from manager.runtime.app_proxy import proxy_bridge_js

        bridge = proxy_bridge_js("jellyseerr")
        self.assertIn("fillHost", bridge)
        self.assertIn("getElementById('apiKey')", bridge)
        self.assertIn("serviceWorker", bridge)
        self.assertIn("pushState", bridge)

    def test_entertainment_proxy_skips_rocky_login(self) -> None:
        from manager.runtime.app_proxy import (
            filter_browser_cookies_for_upstream,
            is_public_proxy_app,
            proxied_app_login_location,
            public_proxy_app_ids,
        )

        self.assertTrue(is_public_proxy_app("jellyfin"))
        self.assertTrue(is_public_proxy_app("transfer-stack"))
        self.assertTrue(is_public_proxy_app("torrentz"))
        self.assertFalse(is_public_proxy_app("pikvm"))
        self.assertEqual(
            public_proxy_app_ids(),
            frozenset(MEDIA_STACK_APPS) | {"transfer-stack", "torrentz"},
        )
        self.assertEqual(
            proxied_app_login_location(
                next_path="/proxy/jellyfin/?access_token=abc.def",
                referer="",
                request_query="next=%2Fproxy%2Fjellyfin%2F",
            ),
            "/proxy/jellyfin/?access_token=abc.def",
        )
        self.assertEqual(
            proxied_app_login_location(
                next_path="",
                referer="",
                request_query="returnUrl=%2F",
                last_app_id="prowlarr",
            ),
            "/proxy/prowlarr/login?returnUrl=%2F",
        )
        self.assertIsNone(
            proxied_app_login_location(
                next_path="",
                referer="",
                request_query="",
                last_app_id="jellyfin",
            )
        )
        self.assertIsNone(
            proxied_app_login_location(
                next_path="/",
                referer="",
                request_query="",
                last_app_id="torrentz",
            )
        )
        self.assertIsNone(
            proxied_app_login_location(
                next_path="/apps",
                referer="",
                request_query="",
                last_app_id="jellyfin",
            )
        )
        self.assertEqual(
            proxied_app_login_location(
                next_path="",
                referer="http://192.168.1.216:8090/proxy/jellyfin/web/",
                request_query="",
                last_app_id="torrentz",
            ),
            "/proxy/jellyfin/",
        )
        self.assertEqual(
            filter_browser_cookies_for_upstream(
                "rocky_session=abc; arr=xyz; rocky_proxy_prowlarr=tok"
            ),
            "arr=xyz",
        )
        from manager.runtime.app_proxy import select_upstream_request_headers

        forwarded = dict(
            select_upstream_request_headers(
                {
                    "Host": "192.168.1.213:8090",
                    "Cookie": "rocky_session=nope",
                    "X-Api-Key": "arr-secret",
                    "Authorization": "Bearer jellyfin",
                    "Accept": "application/json",
                    "X-Emby-Token": "emby-token",
                    "X-Forwarded-Prefix": "spoof",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla",
                    "Accept-Encoding": "gzip, deflate, br",
                }
            )
        )
        self.assertEqual(forwarded["X-Api-Key"], "arr-secret")
        self.assertEqual(forwarded["Authorization"], "Bearer jellyfin")
        self.assertEqual(forwarded["X-Emby-Token"], "emby-token")
        self.assertEqual(forwarded["Accept"], "application/json")
        self.assertEqual(forwarded["Content-Type"], "application/json")
        self.assertNotIn("Host", forwarded)
        self.assertNotIn("Cookie", forwarded)
        self.assertNotIn("X-Forwarded-Prefix", forwarded)
        self.assertNotIn("Accept-Encoding", forwarded)
        torrent_headers = dict(
            select_upstream_request_headers(
                {
                    "Origin": "http://192.168.1.213:8090",
                    "Referer": "http://192.168.1.213:8090/proxy/transfer-stack/",
                    "Authorization": "Basic dXNlcjpwYXNz",
                    "X-Api-Key": "unused",
                    "Accept": "application/json",
                },
                app_id="transfer-stack",
            )
        )
        self.assertNotIn("Origin", torrent_headers)
        self.assertNotIn("Referer", torrent_headers)
        self.assertNotIn("Authorization", torrent_headers)
        self.assertEqual(torrent_headers["Accept"], "application/json")
        gzipped = __import__("gzip").compress(b"<html><head></head></html>")
        from manager.runtime.app_proxy import decode_upstream_payload

        self.assertEqual(
            decode_upstream_payload(gzipped, {"Content-Encoding": "gzip"}),
            b"<html><head></head></html>",
        )
        self.assertEqual(decode_upstream_payload(gzipped, {}), b"<html><head></head></html>")
        self.assertEqual(decode_upstream_payload(b"plain", {}), b"plain")
        from manager.runtime.app_proxy import cookie_path_for_app, leaked_proxy_app_id, rewrite_cookie_header

        self.assertEqual(cookie_path_for_app("jellyseerr"), "/")
        self.assertEqual(cookie_path_for_app("prowlarr"), "/proxy/prowlarr/")
        self.assertEqual(cookie_path_for_app("transfer-stack"), "/")
        self.assertEqual(cookie_path_for_app("torrentz"), "/")
        self.assertIn("Path=/", rewrite_cookie_header("SID=abc; Path=/", "torrentz"))
        self.assertNotIn(
            "Path=/proxy/torrentz",
            rewrite_cookie_header("SID=abc; Path=/", "torrentz"),
        )
        self.assertIn("Path=/", rewrite_cookie_header("connect.sid=abc; Path=/", "jellyseerr"))
        self.assertNotIn(
            "Path=/proxy/jellyseerr",
            rewrite_cookie_header("connect.sid=abc; Path=/", "jellyseerr"),
        )
        self.assertIn("Path=/proxy/prowlarr/", rewrite_cookie_header("sid=x; Path=/", "prowlarr"))
        self.assertEqual(
            leaked_proxy_app_id(
                path="/api/v1/auth/me",
                referer="",
                last_app_id="jellyfin",
            ),
            "jellyseerr",
        )
        self.assertEqual(
            leaked_proxy_app_id(
                path="/api/v1/auth/jellyfin",
                referer="",
                last_app_id="jellyfin",
            ),
            "jellyseerr",
        )
        self.assertEqual(
            leaked_proxy_app_id(
                path="/api/v1/auth/me",
                referer="",
                last_app_id="",
            ),
            "jellyseerr",
        )
        self.assertEqual(
            leaked_proxy_app_id(
                path="/initialize.json",
                referer="http://192.168.1.216:8090/proxy/prowlarr/login",
            ),
            "prowlarr",
        )
        self.assertEqual(
            leaked_proxy_app_id(
                path="/api/v2/auth/login",
                referer="",
                last_app_id="transfer-stack",
            ),
            "transfer-stack",
        )
        self.assertEqual(
            leaked_proxy_app_id(
                path="/api/v2/auth/login",
                referer="",
                last_app_id="torrentz",
            ),
            "torrentz",
        )
        self.assertIsNone(
            leaked_proxy_app_id(
                path="/api/mode/config",
                referer="http://192.168.1.216:8090/proxy/prowlarr/",
                last_app_id="prowlarr",
            )
        )
        self.assertIsNone(
            leaked_proxy_app_id(
                path="/apps",
                referer="http://192.168.1.216:8090/proxy/prowlarr/",
                last_app_id="prowlarr",
            )
        )
        self.assertEqual(
            leaked_proxy_app_id(
                path="/api/v1/indexer",
                referer="http://192.168.1.216:8090/proxy/prowlarr/",
                last_app_id="jellyfin",
            ),
            "prowlarr",
        )
        self.assertIsNone(
            leaked_proxy_app_id(
                path="/serviceworker.js",
                referer="",
                last_app_id="jellyfin",
            )
        )
        jellyseerr_wait_referer = (
            "http://192.168.1.213:8090/proxy/jellyseerr/?access_token=abc"
        )
        for console_path in ("/favicon.ico", "/apps", "/login", "/"):
            self.assertIsNone(
                leaked_proxy_app_id(
                    path=console_path,
                    referer=jellyseerr_wait_referer,
                    last_app_id="jellyseerr",
                ),
                f"{console_path} must not proxy into jellyseerr",
            )
        source = (ROOT / "manager" / "web" / "console.py").read_text(encoding="utf-8")
        proxy_fn = source.split("def handle_proxy", 1)[1].split("def handle_apps", 1)[0]
        self.assertIn("is_public_proxy_app(app_id)", proxy_fn)
        self.assertNotIn("/login?next=", proxy_fn)
        self.assertIn("rewrite_arr_initialize_json", source)
        self.assertIn("suppress_login_redirect_for_asset", proxy_fn)
        self.assertIn("select_upstream_request_headers", proxy_fn)
        self.assertIn("decode_upstream_payload", proxy_fn)
        self.assertIn("upstream_starting_page", proxy_fn)
        self.assertIn("wants_upstream_wait_page", proxy_fn)
        self.assertIn("proxy_retry_seconds(app_id, target_path)", proxy_fn)
        self.assertIn("upstream_unavailable_error_response", proxy_fn)
        self.assertNotIn("Proxy unavailable", proxy_fn)
        self.assertIn("map_jellyfin_upstream_path", proxy_fn)
        self.assertIn("rewrite_jellyfin_system_info", source)
        self.assertIn("rewrite_jellyseerr_jellyfin_connect_body", proxy_fn)
        self.assertIn("path=target_path", proxy_fn)
        self.assertIn("is_proxy_bridge_path", proxy_fn)
        self.assertIn("proxy_bridge_js", proxy_fn)
        self.assertIn("X-Rocky-Proxy-App", proxy_fn)
        self.assertIn("is_upstream_unavailable", proxy_fn)
        self.assertIn("http_client.HTTPException", proxy_fn)
        self.assertIn("_ensure_transfer_stack_starting", proxy_fn)
        self.assertIn("_ensure_media_app_starting", proxy_fn)
        self.assertIn("MEDIA_STACK_APPS", proxy_fn)
        self.assertIn("_launch_transfer_stack", source)
        self.assertIn("media_stack_activate_mode(current)", source)
        self.assertIn("print_lab_activate_mode(current)", source)
        self.assertIn("entertainment apps stay running", source)
        self.assertIn("proxy_retry_seconds", proxy_fn)
        self.assertIn("upstream_unavailable_error_response", proxy_fn)
        before_retry, _, after_retry = proxy_fn.partition("deadline = time.time() + retry_seconds")
        self.assertIn("_ensure_media_app_starting", before_retry)
        self.assertIn("_ensure_transfer_stack_starting", before_retry)
        self.assertNotIn("_ensure_media_app_starting", after_retry)
        self.assertNotIn("_ensure_transfer_stack_starting", after_retry)
        self.assertNotIn("deadline = time.time() + 1.8", proxy_fn)
        self.assertIn("send_console_failure", source)
        self.assertNotIn('if eid != "transfer-stack" else ""', source)
        self.assertNotIn('next_path == "/apps" and last_app', source)
        self.assertIn("is_transfer_proxy_app(app_id)", proxy_fn)
        self.assertIn("transfer_loopback_headers", proxy_fn)
        self.assertIn("proxied_response_headers", proxy_fn)
        self.assertIn("keep_auth_challenge", proxy_fn)
        self.assertIn("handle_login_page", proxy_fn)
        self.assertIn(
            'if app_id not in {"jellyseerr", "jellyfin", "ragnar", "pwnagotchi"} | TRANSFER_PROXY_APP_IDS',
            proxy_fn,
        )
        self.assertIn(
            'elif app_id not in {"jellyseerr", "jellyfin", "ragnar", "pwnagotchi"}:',
            proxy_fn,
        )
        self.assertIn('X-Forwarded-Host', proxy_fn)
        self.assertIn("is_media_health_endpoint(target_path)", proxy_fn)
        self.assertIn("active_base = self.proxy_base_for_app(app_id) or base", proxy_fn)
        self.assertIn("_media_port_listening", source)
        self.assertIn('Accept-Encoding", "identity"', proxy_fn)
        self.assertNotIn('("Content-Type", "User-Agent")', proxy_fn)
        from http.client import RemoteDisconnected
        from urllib.error import URLError
        from manager.runtime.app_proxy import (
            is_connection_refused,
            is_upstream_unavailable,
            proxy_retry_seconds,
            rewrite_jellyfin_system_info,
            upstream_starting_page,
            upstream_unavailable_api_payload,
            upstream_unavailable_error_response,
            wants_upstream_wait_page,
        )
        from manager.runtime.media_stack import (
            TRANSFER_PROXY_APP_IDS,
            media_stack_activate_mode,
            print_lab_activate_mode,
            transfer_stack_activate_mode,
        )

        self.assertEqual(TRANSFER_PROXY_APP_IDS, frozenset({"transfer-stack", "torrentz"}))
        self.assertTrue(is_connection_refused(OSError(111, "Connection refused")))
        self.assertTrue(is_upstream_unavailable(OSError(111, "Connection refused")))
        self.assertTrue(is_upstream_unavailable(ConnectionResetError(104, "Connection reset")))
        self.assertTrue(is_upstream_unavailable(URLError(ConnectionResetError(104, "Connection reset"))))
        self.assertTrue(is_upstream_unavailable(TimeoutError("timed out")))
        self.assertTrue(is_upstream_unavailable(RemoteDisconnected("Remote end closed connection")))
        self.assertEqual(transfer_stack_activate_mode("safe"), "torrent_fortress")
        self.assertIsNone(transfer_stack_activate_mode("entertainment"))
        self.assertIsNone(transfer_stack_activate_mode("torrent_fortress"))
        self.assertEqual(
            transfer_stack_activate_mode("entertainment", force_torrent_fortress=True),
            "torrent_fortress",
        )
        self.assertEqual(media_stack_activate_mode("safe"), "entertainment")
        self.assertIsNone(media_stack_activate_mode("entertainment"))
        self.assertIsNone(media_stack_activate_mode("torrent_fortress"))
        self.assertIsNone(media_stack_activate_mode("print_lab"))
        self.assertIsNone(transfer_stack_activate_mode("print_lab"))
        self.assertEqual(print_lab_activate_mode("safe"), "print_lab")
        self.assertIsNone(print_lab_activate_mode("entertainment"))
        self.assertIsNone(print_lab_activate_mode("torrent_fortress"))
        self.assertIsNone(print_lab_activate_mode("print_lab"))
        from manager.runtime.app_proxy import (
            iter_set_cookie_headers,
            proxied_response_headers,
            transfer_loopback_headers,
        )

        csrf_headers = transfer_loopback_headers("http://127.0.0.1:8088")
        self.assertEqual(csrf_headers["Origin"], "http://127.0.0.1:8088")
        self.assertEqual(csrf_headers["Referer"], "http://127.0.0.1:8088/")

        class FakeHeaders:
            def items(self):
                return [
                    ("Content-Type", "application/json"),
                    ("Set-Cookie", "SID=one; Path=/"),
                    ("Set-Cookie", "other=two; Path=/"),
                ]

            def get_all(self, name):
                if str(name).lower() == "set-cookie":
                    return ["SID=one; Path=/", "other=two; Path=/"]
                return None

        self.assertEqual(len(iter_set_cookie_headers(FakeHeaders())), 2)
        _headers, cookies = proxied_response_headers(
            FakeHeaders(), "torrentz", "http://127.0.0.1:8088"
        )
        self.assertEqual(len(cookies), 2)
        self.assertTrue(any("SID=one" in cookie and "Path=/" in cookie for cookie in cookies))
        self.assertNotIn("Set-Cookie", _headers)
        self.assertNotIn("set-cookie", {name.lower() for name in _headers})
        html_accept = "text/html,application/xhtml+xml"
        json_accept = "application/json"
        self.assertTrue(wants_upstream_wait_page("GET", "/", html_accept))
        self.assertTrue(
            wants_upstream_wait_page("GET", "/web/", html_accept, app_id="jellyfin")
        )
        self.assertFalse(
            wants_upstream_wait_page(
                "GET", "/System/Info/Public", json_accept, app_id="jellyfin"
            )
        )
        self.assertFalse(
            wants_upstream_wait_page(
                "GET", "/api/v1/system/status", json_accept, app_id="jellyseerr"
            )
        )
        wait_html = upstream_starting_page("jellyseerr")
        self.assertIn(b"Starting Jellyseerr", wait_html)
        self.assertIn(b'meta http-equiv="refresh" content="2"', wait_html)
        self.assertGreater(
            proxy_retry_seconds("jellyfin", "/System/Info/Public"),
            proxy_retry_seconds("jellyfin", "/web/main.css"),
        )
        json_body = upstream_unavailable_api_payload("radarr")
        self.assertNotIn(b"<html", json_body)
        self.assertIn(b"upstream_unavailable", json_body)
        plain, plain_headers = upstream_unavailable_error_response(
            "sonarr", "/Content/styles.css"
        )
        self.assertIn("text/plain", plain_headers["Content-Type"])
        self.assertNotIn(b"<html", plain)
        mixed_accept = "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"
        self.assertFalse(
            wants_upstream_wait_page("GET", "/System/Info/Public", mixed_accept, "jellyfin")
        )
        self.assertFalse(
            wants_upstream_wait_page(
                "GET", "/System/Info/Public", "text/html,application/xhtml+xml", "jellyfin"
            )
        )
        self.assertFalse(
            wants_upstream_wait_page("GET", "/Users/authenticatebyname", mixed_accept)
        )
        self.assertFalse(wants_upstream_wait_page("GET", "/Sessions", "", "jellyfin"))
        self.assertFalse(wants_upstream_wait_page("GET", "/Items/abc", "text/html"))
        self.assertTrue(
            wants_upstream_wait_page("GET", "/web/", "text/html,application/xhtml+xml", "jellyfin")
        )
        self.assertTrue(
            wants_upstream_wait_page(
                "GET", "/web/index.html", "text/html,application/xhtml+xml", "jellyfin"
            )
        )
        self.assertFalse(
            wants_upstream_wait_page("GET", "/web/", "application/json", "jellyfin")
        )
        self.assertGreater(proxy_retry_seconds("jellyfin", "/System/Info/Public"), 1.8)
        self.assertGreaterEqual(proxy_retry_seconds("jellyfin", "/System/Info/Public"), 40)
        rewritten_info = json.loads(
            rewrite_jellyfin_system_info(
                b'{"LocalAddress":"http://[::1]:8096","WanAddress":"http://192.168.1.213:8090","Address":"http://192.168.1.213:8090","Version":"10.11.11"}',
                public_origin="http://192.168.1.213:8090",
                path="/System/Info/Public",
                content_type="application/json",
            )
        )
        self.assertEqual(rewritten_info["LocalAddress"], "http://192.168.1.213:8090/proxy/jellyfin")
        self.assertEqual(rewritten_info["WanAddress"], "http://192.168.1.213:8090/proxy/jellyfin")
        self.assertEqual(rewritten_info["Address"], "http://192.168.1.213:8090/proxy/jellyfin")
        self.assertEqual(proxy_retry_seconds("jellyfin", "/web/index.html"), 1.8)
        self.assertEqual(proxy_retry_seconds("jellyfin", "/web/main.css"), 1.8)
        self.assertEqual(proxy_retry_seconds("jellyseerr", "/"), 1.8)
        self.assertGreater(proxy_retry_seconds("jellyseerr", "/api/v1/system/status"), 1.8)
        api_payload = json.loads(upstream_unavailable_api_payload("jellyfin"))
        self.assertEqual(api_payload["error"], "upstream_unavailable")
        self.assertEqual(api_payload["app"], "jellyfin")
        self.assertNotIn("Starting Jellyfin", api_payload["message"])
        self.assertNotIn("<html", json.dumps(api_payload))

    def test_console_imports_media_stack_symbols(self) -> None:
        from manager.web import console as web_console
        from manager.runtime.media_stack import (
            MEDIA_STACK_APPS,
            MODES_KEEP_MEDIA,
            TRANSFER_PROXY_APP_IDS,
            media_app_ids,
            media_launch_target,
            media_stack_activate_mode,
            print_lab_activate_mode,
        )

        self.assertIs(web_console.MEDIA_STACK_APPS, MEDIA_STACK_APPS)
        self.assertIs(web_console.media_app_ids, media_app_ids)
        self.assertIs(web_console.media_launch_target, media_launch_target)
        self.assertIs(web_console.TRANSFER_PROXY_APP_IDS, TRANSFER_PROXY_APP_IDS)
        self.assertIs(web_console.media_stack_activate_mode, media_stack_activate_mode)
        self.assertIs(web_console.print_lab_activate_mode, print_lab_activate_mode)
        self.assertEqual(
            MODES_KEEP_MEDIA,
            frozenset({"entertainment", "torrent_fortress", "print_lab"}),
        )
        self.assertIn("jellyfin", web_console.MEDIA_STACK_APPS)
        self.assertIn("torrentz", web_console.TRANSFER_PROXY_APP_IDS)

    def test_jellyfin_proxy_api_skips_html_wait_page_and_loopback_target(self) -> None:
        from manager.runtime.app_proxy import (
            map_jellyfin_upstream_path,
            proxy_bridge_js,
            proxy_document_base,
            proxy_prefix,
        )
        from manager.runtime.launch_requests import catalog_proxy_url

        self.assertEqual(MEDIA_STACK_APPS["jellyfin"]["local_url"], "http://127.0.0.1:8096/")
        self.assertNotIn("::1", MEDIA_STACK_APPS["jellyfin"]["local_url"])
        self.assertEqual(
            catalog_proxy_url("jellyfin", {"url": "http://127.0.0.1:8096/"}),
            "http://127.0.0.1:8096/",
        )
        self.assertEqual(proxy_prefix("jellyfin"), "/proxy/jellyfin")
        self.assertEqual(proxy_document_base("jellyfin"), "/proxy/jellyfin/web/")
        self.assertEqual(map_jellyfin_upstream_path("/System/Info/Public"), "/System/Info/Public")
        bridge = proxy_bridge_js("jellyfin")
        self.assertIn('"/proxy/jellyfin"', bridge)
        self.assertIn('"/proxy/jellyfin/web/"', bridge)
        self.assertIn("path=p+(path.charAt(0)==='/'?path:'/'+path)", bridge)
        self.assertIn("hn==='::1'", bridge)
        self.assertIn("p==='/proxy/jellyfin'", bridge)
        self.assertIn("x.port)==='8096'", bridge)
        self.assertIn("function loop(){return once().then(function(r){", bridge)
        self.assertIn("if(r&&r.ok)return pinInfo(r);", bridge)
        self.assertIn("this.timeout=0", bridge)
        self.assertIn("data.Address=origin", bridge)
        console_src = (ROOT / "manager" / "web" / "console.py").read_text(encoding="utf-8")
        proxy_fn = console_src.split("def handle_proxy", 1)[1].split("def handle_apps", 1)[0]
        self.assertIn("upstream_unavailable_error_response", proxy_fn)
        self.assertIn("wants_upstream_wait_page", proxy_fn)
        self.assertIn('if app_id not in {"jellyseerr", "jellyfin", "ragnar", "pwnagotchi"}', proxy_fn)
        daemon_src = (ROOT / "manager" / "daemon.py").read_text(encoding="utf-8")
        print_lab = daemon_src.split("elif effective_mode_id == 'print_lab':", 1)[1].split("else:", 1)[0]
        self.assertNotIn("ensure_media(False)", print_lab)
        self.assertIn("ensure_media(False)", daemon_src.split("if effective_mode_id == 'safe':", 1)[1].split("elif", 1)[0])

    def test_wait_page_only_for_documents_across_all_media_apps(self) -> None:
        from manager.runtime.app_proxy import (
            proxy_retry_seconds,
            upstream_unavailable_api_payload,
            upstream_unavailable_error_response,
            wants_upstream_wait_page,
        )

        html_accept = "text/html,application/xhtml+xml"
        json_accept = "application/json"
        rest_paths = {
            "jellyfin": (
                "/System/Info/Public",
                "/Users/Me",
                "/Sessions",
                "/Items/abc",
                "/Library/MediaFolders",
            ),
            "jellyseerr": ("/api/v1/status", "/api/v1/auth/me", "/api/v1/settings/main"),
            "prowlarr": ("/api/v1/system/status", "/api/v1/indexer", "/initialize.json"),
            "radarr": ("/api/v1/system/status", "/api/v1/movie", "/initialize.json"),
            "sonarr": ("/api/v1/system/status", "/api/v1/series", "/initialize.json"),
            "bazarr": ("/api/system/status", "/api/movies", "/initialize.json"),
        }
        document_paths = {
            "jellyfin": ("/web/", "/web/index.html"),
            "jellyseerr": ("/", "/login"),
            "prowlarr": ("/", "/login"),
            "radarr": ("/", "/login"),
            "sonarr": ("/", "/login"),
            "bazarr": ("/", "/login"),
        }
        self.assertEqual(set(rest_paths), set(MEDIA_STACK_APPS))
        self.assertEqual(set(document_paths), set(MEDIA_STACK_APPS))
        for app_id in MEDIA_STACK_APPS:
            for path in rest_paths[app_id]:
                self.assertFalse(
                    wants_upstream_wait_page("GET", path, json_accept, app_id),
                    f"{app_id} {path} must not get an HTML wait page",
                )
                self.assertFalse(
                    wants_upstream_wait_page("GET", path, html_accept, app_id),
                    f"{app_id} {path} HTML accept still must not wait-page REST",
                )
            for path in document_paths[app_id]:
                self.assertTrue(
                    wants_upstream_wait_page("GET", path, html_accept, app_id),
                    f"{app_id} {path} should show the starting page",
                )
            payload = upstream_unavailable_api_payload(app_id)
            self.assertNotIn(b"<html", payload)
            self.assertNotIn(b"<!DOCTYPE", payload)
            parsed = json.loads(payload)
            self.assertEqual(parsed["error"], "upstream_unavailable")
            self.assertEqual(parsed["app"], app_id)
            css_body, css_headers = upstream_unavailable_error_response(
                app_id, "/static/app.css"
            )
            self.assertIn("text/plain", css_headers["Content-Type"])
            self.assertNotIn(b"<html", css_body)
            json_body, json_headers = upstream_unavailable_error_response(
                app_id, rest_paths[app_id][0]
            )
            self.assertIn("application/json", json_headers["Content-Type"])
            self.assertEqual(json.loads(json_body)["error"], "upstream_unavailable")
            self.assertEqual(proxy_retry_seconds(app_id, "/static/app.js"), 1.8)
            self.assertEqual(proxy_retry_seconds(app_id, "/web/main.css"), 1.8)
        self.assertGreater(
            proxy_retry_seconds("jellyfin", "/System/Info/Public"),
            proxy_retry_seconds("jellyfin", "/web/main.css"),
        )
        for app_id in ("jellyseerr", "prowlarr", "radarr", "sonarr"):
            self.assertGreater(
                proxy_retry_seconds(app_id, "/api/v1/system/status"),
                proxy_retry_seconds(app_id, "/Content/styles.css"),
            )
        source = (ROOT / "manager" / "web" / "console.py").read_text(encoding="utf-8")
        proxy_fn = source.split("def handle_proxy", 1)[1].split("def handle_apps", 1)[0]
        self.assertIn("_ensure_media_app_starting", proxy_fn)
        ensure_at, _, retry_loop = proxy_fn.partition("deadline = time.time() + retry_seconds")
        self.assertIn("_ensure_media_app_starting", ensure_at)
        self.assertNotIn("_ensure_media_app_starting", retry_loop)

    def test_console_favicon_never_proxies_into_media_apps(self) -> None:
        from manager.runtime.app_proxy import (
            is_console_chrome_path,
            is_rocky_console_request,
            leaked_proxy_app_id,
            proxy_bridge_js,
            upstream_starting_page,
            wants_upstream_wait_page,
        )

        html_accept = "text/html,application/xhtml+xml"
        wait_referer = "http://192.168.1.213:8090/proxy/jellyseerr/?access_token=tok"
        for path in ("/favicon.ico", "/apps", "/login", "/"):
            self.assertTrue(is_rocky_console_request(path), path)
            self.assertIsNone(
                leaked_proxy_app_id(
                    path=path,
                    referer=wait_referer,
                    last_app_id="jellyseerr",
                ),
                path,
            )
            self.assertIsNone(
                leaked_proxy_app_id(
                    path=path,
                    referer="http://192.168.1.213:8090/proxy/jellyfin/web/",
                    last_app_id="jellyfin",
                ),
                path,
            )
        self.assertTrue(is_console_chrome_path("/favicon.ico"))
        self.assertTrue(is_console_chrome_path("/apple-touch-icon.png"))
        self.assertFalse(is_console_chrome_path("/api/v1/auth/me"))
        self.assertEqual(
            leaked_proxy_app_id(
                path="/api/v1/auth/me",
                referer="",
                last_app_id="jellyfin",
            ),
            "jellyseerr",
        )
        self.assertTrue(
            wants_upstream_wait_page("GET", "/", html_accept, "jellyseerr")
        )
        self.assertFalse(
            wants_upstream_wait_page("GET", "/favicon.ico", "*/*", "jellyseerr")
        )
        wait_html = upstream_starting_page("jellyseerr")
        self.assertIn(b'rel="icon"', wait_html)
        self.assertIn(b'href="/favicon.ico"', wait_html)
        self.assertIn(b"Starting Jellyseerr", wait_html)
        bridge = proxy_bridge_js("jellyseerr")
        self.assertIn("'/favicon.ico'", bridge)
        console_src = (ROOT / "manager" / "web" / "console.py").read_text(encoding="utf-8")
        get_fn = console_src.split("def _handle_get", 1)[1].split("def do_POST", 1)[0]
        before_leak, _, after_leak = get_fn.partition("_leaked_proxy_request")
        self.assertIn("_serve_console_chrome", before_leak)
        self.assertNotIn("handle_proxy(leaked", before_leak)
        self.assertIn("handle_proxy(leaked", after_leak)
        self.assertIn('rel="icon" href="/favicon.ico"', console_src)
        proxy_fn = console_src.split("def handle_proxy", 1)[1].split("def handle_apps", 1)[0]
        ensure_at, _, retry_loop = proxy_fn.partition("deadline = time.time() + retry_seconds")
        self.assertIn("_ensure_media_app_starting", ensure_at)
        self.assertIn("write_launch_request", console_src.split("def _ensure_media_app_starting", 1)[1].split("def _launch_transfer_stack", 1)[0])
        self.assertNotIn("_ensure_media_app_starting", retry_loop)
        self.assertIn("upstream_unavailable_error_response", proxy_fn)
        self.assertIn("wants_upstream_wait_page", proxy_fn)

    def test_apps_launch_stays_on_console_and_opens_new_tab(self) -> None:
        source = (ROOT / "manager" / "web" / "console.py").read_text(encoding="utf-8")
        self.assertIn('target="_blank" rel="noopener"', source)
        self.assertIn("window.open(url, '_blank', 'noopener')", source)
        self.assertIn("Leave the new tab open", source)
        self.assertNotIn("noopener,noreferrer", source)
        self.assertNotIn("window.location.href = url", source)
        self.assertNotIn("startAndOpenApp", source)
        self.assertNotIn("data-open-url", source)

    def test_mode_permission_setup_skips_missing_user(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "ensure_mode_config_permissions",
            ROOT / "scripts" / "ensure_mode_config_permissions.py",
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIsNone(module.resolve_owner("definitely-not-a-real-rocky-user", None))

    def test_jellyseerr_config_volume_overlay(self) -> None:
        from tempfile import TemporaryDirectory

        from manager.runtime.media_stack import (
            JELLYSEERR_OVERLAY_NAME,
            ensure_jellyseerr_config_volume,
            media_compose_up_command,
        )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text(
                "services:\n  jellyseerr:\n    image: example/jellyseerr\n",
                encoding="utf-8",
            )
            config_dir = ensure_jellyseerr_config_volume(root)
            self.assertTrue(config_dir.is_dir())
            overlay = root / JELLYSEERR_OVERLAY_NAME
            self.assertTrue(overlay.is_file())
            overlay_text = overlay.read_text(encoding="utf-8")
            self.assertIn("./jellyseerr-config:/app/config", overlay_text)
            self.assertRegex(overlay_text, r"jellyfin:\d+\.\d+\.\d+\.\d+")
            self.assertRegex(overlay_text, r"radarr:\d+\.\d+\.\d+\.\d+")
            self.assertRegex(overlay_text, r"sonarr:\d+\.\d+\.\d+\.\d+")
            self.assertNotIn("host-gateway", overlay_text)
            command = media_compose_up_command("jellyseerr", root, force_recreate=True)
            assert command is not None
            self.assertIn(str(overlay), command)
            self.assertIn("--force-recreate", command)
            self.assertEqual(command[-1], "jellyseerr")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text(
                "services:\n  jellyseerr:\n    volumes:\n      - ./data:/app/config\n",
                encoding="utf-8",
            )
            ensure_jellyseerr_config_volume(root)
            overlay_text = (root / JELLYSEERR_OVERLAY_NAME).read_text(encoding="utf-8")
            self.assertRegex(overlay_text, r"jellyfin:\d+\.\d+\.\d+\.\d+")
            self.assertNotIn("./jellyseerr-config:/app/config", overlay_text)

        source = (ROOT / "manager" / "web" / "console.py").read_text(encoding="utf-8")
        self.assertIn("jellyseerr_needs_volume_recreate", source)
        self.assertIn("media_compose_up_command", source)
        daemon = (ROOT / "manager" / "daemon.py").read_text(encoding="utf-8")
        self.assertIn("jellyseerr_needs_volume_recreate", daemon)
        self.assertIn("media_compose_up_command", daemon)


if __name__ == "__main__":
    unittest.main()
