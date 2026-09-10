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
        html = rewrite_html_root_paths(
            b'<form action="/login"><a href="/Content/logo.svg"></a></form>',
            "prowlarr",
            "text/html",
        )
        self.assertIn(b'action="/proxy/prowlarr/login"', html)
        self.assertIn(b'href="/proxy/prowlarr/Content/logo.svg"', html)

    def test_apps_launch_stays_on_console_and_opens_new_tab(self) -> None:
        source = (ROOT / "manager" / "web" / "console.py").read_text(encoding="utf-8")
        self.assertIn('target="_blank" rel="noopener noreferrer"', source)
        self.assertIn("window.open(url, '_blank', 'noopener,noreferrer')", source)
        self.assertIn("event.preventDefault();", source)
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
