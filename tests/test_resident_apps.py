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

    def test_console_requests_resident_launch(self) -> None:
        source = (ROOT / "manager" / "web" / "console.py").read_text(encoding="utf-8")
        self.assertIn("write_launch_request", source)
        self.assertIn("catalog_proxy_url", source)
        self.assertIn("button.btn-launch", source)
        daemon = (ROOT / "manager" / "daemon.py").read_text(encoding="utf-8")
        self.assertIn("_apply_console_launch_request", daemon)
        self.assertIn("application_manager.active_service_id", daemon)
        self.assertIn("consume_launch_request", daemon)
        self.assertIn("self._start_mode_reconcile_thread()", daemon)


if __name__ == "__main__":
    unittest.main()
