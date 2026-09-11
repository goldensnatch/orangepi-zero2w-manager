from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ResidentAppTests(unittest.TestCase):
    def test_runtime_exports_daemon_symbols(self) -> None:
        from manager.runtime import (
            ApplicationAlreadyRunning,
            ApplicationLaunchError,
            button_server,
        )
        from manager.runtime.button_protocol import ButtonServer
        from manager.runtime.application_manager import (
            ApplicationAlreadyRunning as ManagerAlreadyRunning,
            ApplicationLaunchError as ManagerLaunchError,
        )

        self.assertIs(ApplicationAlreadyRunning, ManagerAlreadyRunning)
        self.assertIs(ApplicationLaunchError, ManagerLaunchError)
        self.assertIsInstance(button_server, ButtonServer)

    def test_daemon_imports_without_evdev(self) -> None:
        import manager.button_service as buttons
        import manager.daemon as daemon

        self.assertTrue(hasattr(daemon, "ManagerDaemon"))
        self.assertTrue(hasattr(daemon, "button_server"))
        # This environment may lack evdev; the daemon must still import.
        if buttons.list_devices is None:
            service = buttons.ButtonService(lambda _e: None, lambda _e: None, lambda: False)
            with self.assertRaises(RuntimeError):
                service.find_device()

    def test_ragnar_has_web_url_and_pwnagotchi_is_launchable(self) -> None:
        catalog = json.loads((ROOT / "config" / "services.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog["ragnar"]["url"], "http://127.0.0.1:8000")
        self.assertTrue(catalog["ragnar"]["resident_display"])
        self.assertTrue(catalog["pwnagotchi"]["resident_display"])
        self.assertEqual(catalog["pwnagotchi"]["systemd_service"], "pwnagotchi.service")
        self.assertIn("bettercap.service", catalog["pwnagotchi"]["start_services"])

        from manager.runtime.launch_requests import RAGNAR_LOCAL_URL, catalog_proxy_url

        self.assertEqual(catalog_proxy_url("ragnar", catalog["ragnar"]), RAGNAR_LOCAL_URL)
        self.assertEqual(catalog_proxy_url("ragnar", {}), RAGNAR_LOCAL_URL)

    def test_launch_request_round_trip(self) -> None:
        from manager.runtime.launch_requests import consume_launch_request, write_launch_request

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "launch-request.json"
            write_launch_request("ragnar", source="console", path=path)
            payload = consume_launch_request(path)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["app_id"], "ragnar")
            self.assertEqual(payload["source"], "console")
            self.assertFalse(path.is_file())
            self.assertIsNone(consume_launch_request(path))

    def test_pwnagotchi_web_url_from_config(self) -> None:
        from manager.runtime.launch_requests import pwnagotchi_web_url

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text("[ui.web]\nenabled = true\nport = 9898\n", encoding="utf-8")
            self.assertEqual(pwnagotchi_web_url((config,)), "http://127.0.0.1:9898")
            config.write_text("[ui.web]\nenabled = false\nport = 9898\n", encoding="utf-8")
            self.assertIsNone(pwnagotchi_web_url((config,)))

    def test_mode_reconcile_signature_ignores_live_churn(self) -> None:
        from manager.daemon import mode_reconcile_signature

        first = mode_reconcile_signature(
            {
                "mode": {
                    "desired": {"mode_id": "entertainment", "requested_at": "t1"},
                    "live": {"mode_id": "entertainment", "reconciled_at": "2026-01-01T00:00:00Z", "healthy": True},
                    "services": {"jellyfin": "running"},
                }
            }
        )
        second = mode_reconcile_signature(
            {
                "mode": {
                    "desired": {"mode_id": "entertainment", "requested_at": "t1"},
                    "live": {"mode_id": "entertainment", "reconciled_at": "2026-01-01T00:00:01Z", "healthy": False},
                    "services": {"jellyfin": "stopped"},
                }
            }
        )
        switched = mode_reconcile_signature(
            {
                "mode": {
                    "desired": {"mode_id": "print_lab", "requested_at": "t2"},
                    "live": {"mode_id": "print_lab", "reconciled_at": "2026-01-01T00:00:02Z"},
                }
            }
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, switched)

    def test_pwnagotchi_launch_does_not_double_start_units(self) -> None:
        from manager.runtime.launch_requests import catalog_start_units, needs_daemon_launch

        catalog = json.loads((ROOT / "config" / "services.json").read_text(encoding="utf-8"))
        printer = catalog["3d_printer"]
        self.assertTrue(needs_daemon_launch(printer))
        self.assertEqual(catalog_start_units(printer), ["klipper", "moonraker", "nginx"])
        self.assertTrue(needs_daemon_launch(catalog["pikvm"]))
        self.assertFalse(
            needs_daemon_launch(
                {
                    "name": "Switch Transfer",
                    "systemd_user_service": "rocky-switch-transfer.service",
                    "url": "http://127.0.0.1:8077/",
                }
            )
        )
        source = (ROOT / "manager" / "web" / "console.py").read_text(encoding="utf-8")
        launch_fn = source.split("def handle_app_launch", 1)[1].split("def apps_payload", 1)[0]
        self.assertIn("needs_daemon_launch(service)", launch_fn)
        self.assertNotIn('["systemctl", "start", unit]', launch_fn)
        daemon = (ROOT / "manager" / "daemon.py").read_text(encoding="utf-8")
        self.assertIn("catalog_start_units(service)", daemon)
        source = (ROOT / "manager" / "web" / "console.py").read_text(encoding="utf-8")
        self.assertIn("write_launch_request", source)
        self.assertIn("catalog_proxy_url", source)
        self.assertIn("button.btn-launch", source)
        daemon = (ROOT / "manager" / "daemon.py").read_text(encoding="utf-8")
        self.assertIn("_apply_console_launch_request", daemon)
        self.assertIn("application_manager.active_service_id", daemon)
        self.assertIn("consume_launch_request", daemon)
        self.assertIn("self._start_mode_reconcile_thread()", daemon)
        self.assertIn("self._quiesce_all_resident_displays()", daemon)
        apply_fn = daemon.split("def _apply_console_launch_request", 1)[1].split("def _start_mode_reconcile_thread", 1)[0]
        self.assertIn("_resume_service_to_foreground", apply_fn)
        self.assertIn("_prepare_exclusive_workload", apply_fn)
        self.assertIn("should_shed_workloads", apply_fn)
        self.assertIn("TRANSFER_PROXY_APP_IDS", apply_fn)
        self.assertIn("_start_transfer_stack_from_console", apply_fn)
        self.assertNotIn(
            "self._run_systemctl(\"start\", units)\n            if self._is_resident_display_app",
            apply_fn,
        )
        daemon = (ROOT / "manager" / "daemon.py").read_text(encoding="utf-8")
        self.assertIn("_start_transfer_stack_from_console", daemon)
        self.assertIn("write_selected_mode", daemon)
        self.assertIn("Aborting %s reconcile", daemon)
        apply_fn = daemon.split("def _apply_console_launch_request", 1)[1].split("def _start_mode_reconcile_thread", 1)[0]
        self.assertIn("MEDIA_STACK_APPS", apply_fn)
        self.assertIn("_start_media_container", apply_fn)
        self.assertIn("MODES_KEEP_MEDIA", apply_fn)
        self.assertIn("TRANSFER_MODES_KEEP_QBITTORRENT", daemon)
        fortress_fn = daemon.split("elif effective_mode_id == 'torrent_fortress':", 1)[1].split(
            "elif effective_mode_id == 'entertainment':", 1
        )[0]
        self.assertNotIn("ensure_media(False)", fortress_fn)
        self.assertNotIn("ensure_media(True)", fortress_fn)
        self.assertIn("Additive with entertainment", fortress_fn)
        entertainment_fn = daemon.split("elif effective_mode_id == 'entertainment':", 1)[1].split(
            "elif effective_mode_id in ('pihole_only', 'daily_driver'):", 1
        )[0]
        self.assertIn("ensure_media(True)", entertainment_fn)
        self.assertNotIn("ensure_media(False)", entertainment_fn)
        self.assertNotIn("entertainment: stopping %s", entertainment_fn)
        print_lab_fn = daemon.split("elif effective_mode_id == 'print_lab':", 1)[1].split(
            "else:", 1
        )[0]
        self.assertNotIn("ensure_media(False)", print_lab_fn)
        self.assertNotIn("print_lab: stopping %s", print_lab_fn)
        self.assertIn("Additive with entertainment and fortress", print_lab_fn)
        self.assertIn("ADDITIVE_STACK_MODES", daemon)
        self.assertIn("Keeping %s; %s is additive with %s", daemon)
        self.assertIn("desired_mode_signature(request)", daemon)
        self.assertIn("_lightweight_mode_footer_text", daemon)
        self.assertIn("self._enforce_mode_exclusions()", daemon)
        self.assertIn("self._shed_overload_workloads", daemon)
        button_loop = daemon.split("def _button_loop_should_continue", 1)[1].split("def _publish_runtime_state", 1)[0]
        self.assertNotIn("self._resolve_mode_payload()", button_loop)
        self.assertIn("desired_mode_signature(request)", button_loop)


if __name__ == "__main__":
    unittest.main()
