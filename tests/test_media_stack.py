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
