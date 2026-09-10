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
        self.assertIn(b"window.__rockyPrefix", js_html)
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

    def test_entertainment_proxy_skips_rocky_login(self) -> None:
        from manager.runtime.app_proxy import (
            filter_browser_cookies_for_upstream,
            is_public_proxy_app,
            proxied_app_login_location,
            public_proxy_app_ids,
        )

        self.assertTrue(is_public_proxy_app("jellyfin"))
        self.assertTrue(is_public_proxy_app("transfer-stack"))
        self.assertFalse(is_public_proxy_app("pikvm"))
        self.assertEqual(
            public_proxy_app_ids(),
            frozenset(MEDIA_STACK_APPS) | {"transfer-stack"},
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
        self.assertEqual(
            proxied_app_login_location(
                next_path="",
                referer="",
                request_query="",
                last_app_id="jellyfin",
            ),
            "/proxy/jellyfin/",
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
        gzipped = __import__("gzip").compress(b"<html><head></head></html>")
        from manager.runtime.app_proxy import decode_upstream_payload

        self.assertEqual(
            decode_upstream_payload(gzipped, {"Content-Encoding": "gzip"}),
            b"<html><head></head></html>",
        )
        self.assertEqual(decode_upstream_payload(gzipped, {}), b"<html><head></head></html>")
        self.assertEqual(decode_upstream_payload(b"plain", {}), b"plain")
        from manager.runtime.app_proxy import leaked_proxy_app_id

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
        self.assertIn('Accept-Encoding", "identity"', proxy_fn)
        self.assertNotIn('("Content-Type", "User-Agent")', proxy_fn)
        from manager.runtime.app_proxy import (
            is_connection_refused,
            upstream_starting_page,
            wants_upstream_wait_page,
        )

        self.assertTrue(is_connection_refused(OSError(111, "Connection refused")))
        self.assertTrue(
            wants_upstream_wait_page("GET", "/", "text/html,application/xhtml+xml")
        )
        self.assertFalse(
            wants_upstream_wait_page("GET", "/api/v1/system/status", "application/json")
        )
        wait_html = upstream_starting_page("jellyseerr")
        self.assertIn(b"Starting Jellyseerr", wait_html)
        self.assertIn(b'meta http-equiv="refresh" content="2"', wait_html)

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


if __name__ == "__main__":
    unittest.main()
