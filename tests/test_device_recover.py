from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")


class DeviceRecoverTests(unittest.TestCase):
    def test_overload_thresholds(self) -> None:
        from manager.runtime.overload import (
            should_shed_workloads,
            should_skip_docker_probe,
        )

        self.assertFalse(should_skip_docker_probe(0.4))
        self.assertTrue(should_skip_docker_probe(1.9))
        self.assertFalse(should_shed_workloads(1.9))
        self.assertTrue(should_shed_workloads(3.1))

    def test_desired_mode_signature_ignores_unrelated_fields(self) -> None:
        from manager.runtime.device_quiesce import desired_mode_signature

        first = desired_mode_signature(
            {"selected_mode_id": "entertainment", "requested_at": "t1", "live": "ignored"}
        )
        second = desired_mode_signature(
            {"selected_mode_id": "entertainment", "requested_at": "t1", "healthy": False}
        )
        switched = desired_mode_signature(
            {"selected_mode_id": "safe", "requested_at": "t2"}
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, switched)

    def test_quiesce_stops_radio_media_and_pins_safe_mode(self) -> None:
        from manager.runtime.device_quiesce import RADIO_UNITS, quiesce_device
        from manager.runtime.media_stack import media_container_names

        runner = RecordingRunner()
        with tempfile.TemporaryDirectory() as tmp:
            mode_path = Path(tmp) / "current-mode.json"
            mode_path.write_text(
                json.dumps({"selected_mode_id": "entertainment", "override_flags": {"allow_public_admin": False}}),
                encoding="utf-8",
            )
            report = quiesce_device(
                write_mode=True,
                mode_path=mode_path,
                reason="test",
                runner=runner,
            )
            payload = json.loads(mode_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["selected_mode_id"], "safe")
            self.assertEqual(payload["previous_mode_id"], "entertainment")
            self.assertEqual(report.mode_written, "safe")
            joined = [" ".join(command) for command in runner.commands]
            self.assertTrue(any("systemctl stop" in line and RADIO_UNITS[0] in line for line in joined))
            self.assertTrue(any("docker stop" in line and "rocky-media-jellyseerr" in line for line in joined))
            self.assertTrue(any("pkill -f Ragnar.py" in line for line in joined))
            for name in media_container_names():
                self.assertTrue(any(name in line for line in joined), name)

    def test_install_tree_skips_runtime_and_writes_version(self) -> None:
        from manager.runtime.device_update import COPY_ENTRIES, install_tree

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src"
            dest = Path(tmp) / "dest"
            (source / "manager").mkdir(parents=True)
            (source / "manager" / "hello.py").write_text("print('ok')\n", encoding="utf-8")
            (source / "manager" / "runtime").mkdir(parents=True)
            (source / "manager" / "runtime" / "device_quiesce.py").write_text("QUIESCE = True\n", encoding="utf-8")
            (source / "runtime" / "secret").mkdir(parents=True)
            (source / "runtime" / "secret" / "keep-me").write_text("nope\n", encoding="utf-8")
            (dest / "runtime" / "config").mkdir(parents=True)
            (dest / "runtime" / "config" / "keep.json").write_text("{}\n", encoding="utf-8")
            copied = install_tree(source, dest, ref="test-ref")
            self.assertIn("manager", copied)
            self.assertIn("ROCKY_BUILD_VERSION", copied)
            self.assertEqual((dest / "manager" / "hello.py").read_text(encoding="utf-8"), "print('ok')\n")
            self.assertEqual(
                (dest / "manager" / "runtime" / "device_quiesce.py").read_text(encoding="utf-8"),
                "QUIESCE = True\n",
            )
            self.assertEqual((dest / "ROCKY_BUILD_VERSION").read_text(encoding="utf-8").strip(), "test-ref")
            self.assertTrue((dest / "runtime" / "config" / "keep.json").is_file())
            self.assertFalse((dest / "runtime" / "secret" / "keep-me").exists())
            self.assertTrue(set(copied) <= set(COPY_ENTRIES) | {"ROCKY_BUILD_VERSION"})

    def test_local_build_version_skips_git_without_checkout(self) -> None:
        from manager.web.console import local_build_version

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ROCKY_BUILD_VERSION", None)
                with patch("manager.web.console.subprocess.run") as mocked:
                    self.assertEqual(local_build_version(root), "rocky@local")
                    mocked.assert_not_called()
            (root / "ROCKY_BUILD_VERSION").write_text("cursor/fix\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ROCKY_BUILD_VERSION", None)
                self.assertEqual(local_build_version(root), "rocky@cursor/fix")

    def test_archive_url_uses_branch_tarball_not_git(self) -> None:
        from manager.runtime.device_update import archive_url

        url = archive_url("goldensnatch/orangepi-zero2w-manager", "cursor/entertainment-media-apps-e885")
        self.assertIn("archive/refs/heads/cursor/entertainment-media-apps-e885.tar.gz", url)
        self.assertNotIn(".git", url)

    def test_recover_script_is_stdlib_bootstrap(self) -> None:
        source = (ROOT / "scripts" / "device_recover.py").read_text(encoding="utf-8")
        self.assertNotIn("from manager.", source)
        self.assertIn("fatal: not a git repository", source)
        self.assertIn("archive/refs/heads/", source)
        self.assertIn('dest="flag_all"', source)

    def test_recover_script_accepts_all_flag(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "device_recover_cli",
            ROOT / "scripts" / "device_recover.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        parser = module.build_parser()
        self.assertEqual(module.resolve_action(parser.parse_args([])), "all")
        self.assertEqual(module.resolve_action(parser.parse_args(["--all"])), "all")
        self.assertEqual(module.resolve_action(parser.parse_args(["all"])), "all")
        self.assertEqual(module.resolve_action(parser.parse_args(["--quiesce"])), "quiesce")
        self.assertEqual(module.resolve_action(parser.parse_args(["update"])), "update")
        self.assertEqual(module.resolve_action(parser.parse_args(["--update"])), "update")
        with self.assertRaises(SystemExit):
            parser.parse_args(["--not-a-real-flag"])

    def test_apps_page_does_not_block_on_docker(self) -> None:
        source = (ROOT / "manager" / "web" / "console.py").read_text(encoding="utf-8")
        apps_fn = source.split("def apps_payload", 1)[1].split("def proxy_response", 1)[0]
        self.assertIn("published_console_state()", apps_fn)
        self.assertIn("MEDIA_STACK_APPS.items()", apps_fn)
        self.assertIn("from manager.runtime.media_stack import", source)
        self.assertNotIn("runtime_status_payload(detail=\"full\")", apps_fn)
        self.assertNotIn("docker inspect", apps_fn)
        self.assertNotIn("service_reported_version", apps_fn)
        self.assertNotIn("docker_container_version", apps_fn)
        self.assertIn("def handle_one_request", source)
        self.assertIn("ERR_EMPTY_RESPONSE", source)

    def test_safe_mode_does_not_start_pihole_or_gluetun(self) -> None:
        daemon = (ROOT / "manager" / "daemon.py").read_text(encoding="utf-8")
        safe_fn = daemon.split("if effective_mode_id == 'safe':", 1)[1].split("elif effective_mode_id == 'torrent_fortress':", 1)[0]
        self.assertIn("ensure_media(False)", safe_fn)
        self.assertNotIn("starting %s", safe_fn)
        self.assertNotIn("rocky-pihole", safe_fn)
        self.assertIn("Do not start Pi-hole or Gluetun", safe_fn)

    def test_recover_all_does_not_docker_stop_after_restart(self) -> None:
        source = (ROOT / "scripts" / "device_recover.py").read_text(encoding="utf-8")
        all_fn = source.split("def cmd_all", 1)[1].split("def build_parser", 1)[0]
        self.assertIn("wait_for_console()", all_fn)
        self.assertIn("restore_web_permissions", all_fn)
        self.assertNotIn("return cmd_quiesce(args)", all_fn)
        self.assertIn("Use http://<device-ip>:8090/apps", source)

    def test_button_loop_does_not_docker_inspect_every_tick(self) -> None:
        daemon = (ROOT / "manager" / "daemon.py").read_text(encoding="utf-8")
        button_loop = daemon.split("def _button_loop_should_continue", 1)[1].split("def _publish_runtime_state", 1)[0]
        self.assertIn("desired_mode_signature", button_loop)
        self.assertNotIn("_resolve_mode_payload()", button_loop)
        self.assertNotIn("_docker_container_health", button_loop)


if __name__ == "__main__":
    unittest.main()
