from __future__ import annotations

import copy
import json
import os
try:
    import pwd
except ImportError:  # pragma: no cover - Windows dev verification
    pwd = None
try:
    import grp
except ImportError:  # pragma: no cover - Windows dev verification
    grp = None
import shutil
import subprocess
from configparser import ConfigParser
from pathlib import Path
from typing import Any


DEFAULT_NETWORK_TRANSFER_CONFIG_PATH = Path(
    "/opt/zero2w-manager/runtime/config/network-transfer.json"
)
DEFAULT_TRANSFER_STACK_ROOT = Path("/opt/zero2w-manager/runtime/transfer-stack")
DEFAULT_INTERNAL_TRANSFER_ROOT = Path("/mnt/rocky-transfer")
DEFAULT_USB_TRANSFER_ROOT = Path("/media/rocky")


def _default_config() -> dict[str, Any]:
    return {
        "privacy_relay": {
            "mode": "off",
            "tor": {
                "enabled": False,
                "listen_host": "127.0.0.1",
                "socks_port": 9050,
            },
        },
        "vpn": {
            "provider": "none",
            "policy": "optional",
            "kill_switch": False,
            "allow_lan": True,
            "openvpn": {
                "profile_path": None,
            },
            "gluetun": {
                "enabled": False,
                "stack_name": "rocky-transfer",
                "network_name": "rocky_transfer_net",
            },
        },
        "transfer": {
            "engine": "qbittorrent-nox",
            "destination_id": "internal",
            "auto_use_usb": False,
            "require_vpn": True,
            "incomplete_subdir": "incomplete",
            "complete_subdir": "complete",
            "watch_subdir": "watch",
            "metadata_subdir": "metadata",
        },
        "remote_access": {
            "mode": "disabled",
            "cloudflared": {
                "enabled": False,
                "tunnel_name": None,
            },
        },
    }


class NetworkTransferConfig:
    """Loads future Rocky network/transfer operator intent."""

    def __init__(
        self,
        path: Path = DEFAULT_NETWORK_TRANSFER_CONFIG_PATH,
    ) -> None:
        self.path = Path(path)
        self._config = _default_config()
        self._last_loaded_mtime_ns: int | None = None
        self.reload()

    def reload(self) -> dict[str, Any]:
        config = _default_config()
        self._last_loaded_mtime_ns = None

        if self.path.is_file():
            try:
                raw = json.loads(
                    self.path.read_text(encoding="utf-8")
                )
                self._last_loaded_mtime_ns = self.path.stat().st_mtime_ns
            except (OSError, json.JSONDecodeError):
                raw = None
                self._last_loaded_mtime_ns = None

            if isinstance(raw, dict):
                self._merge(config, raw)

        self._config = config
        return self.snapshot()

    def reload_if_changed(self) -> bool:
        current_mtime_ns: int | None = None

        if self.path.is_file():
            try:
                current_mtime_ns = self.path.stat().st_mtime_ns
            except OSError:
                current_mtime_ns = None

        if current_mtime_ns == self._last_loaded_mtime_ns:
            return False

        self.reload()
        return True

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._config)

    def update_network(self, values: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise ValueError("network config payload must be an object")

        allowed = {
            "privacy_relay",
            "vpn",
            "remote_access",
        }
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ValueError(f"unknown network config field(s): {', '.join(unknown)}")

        config = self.snapshot()
        self._merge(config, {key: values[key] for key in allowed if key in values})
        self._config = config
        self._write()
        return self.snapshot()

    def update_transfer(self, values: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise ValueError("transfer config payload must be an object")

        allowed = {"transfer"}
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ValueError(f"unknown transfer config field(s): {', '.join(unknown)}")

        config = self.snapshot()
        self._merge(config, {key: values[key] for key in allowed if key in values})
        self._config = config
        self._write()
        return self.snapshot()

    def _write(self) -> None:
        self.path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        payload = json.dumps(self._config, indent=2, sort_keys=True) + "\n"
        temporary = self.path.with_name(f'.{self.path.name}.tmp')
        temporary.write_text(payload, encoding='utf-8')
        os.replace(temporary, self.path)

    @classmethod
    def _merge(
        cls,
        target: dict[str, Any],
        values: dict[str, Any],
    ) -> None:
        for key, value in values.items():
            current = target.get(key)
            if isinstance(current, dict) and isinstance(value, dict):
                cls._merge(current, value)
            else:
                target[key] = value


class NetworkManager:
    def __init__(self, config: NetworkTransferConfig) -> None:
        self.config = config

    def config_snapshot(self) -> dict[str, Any]:
        return self.config.snapshot()["vpn"] | {
            "privacy_relay": self.config.snapshot()["privacy_relay"],
            "remote_access": self.config.snapshot()["remote_access"],
        }

    def state_snapshot(self) -> dict[str, Any]:
        config = self.config.snapshot()
        vpn = config["vpn"]
        relay = config["privacy_relay"]
        remote = config["remote_access"]

        provider = str(vpn.get("provider", "none"))
        relay_mode = str(relay.get("mode", "off"))
        remote_mode = str(remote.get("mode", "disabled"))

        if relay_mode == "tor_socks5":
            tor_enabled = True
        else:
            tor_enabled = bool(relay.get("tor", {}).get("enabled", False))

        connected = False
        endpoint = None
        last_error = None

        if provider == "openvpn":
            connected, endpoint, last_error = self._openvpn_status()
        elif provider == "gluetun":
            connected, endpoint, last_error = self._gluetun_status(vpn.get("gluetun", {}))

        transport_mode = "direct"
        if provider != "none":
            transport_mode = "vpn"
        elif relay_mode != "off":
            transport_mode = "privacy_relay"

        return {
            "transport_mode": transport_mode,
            "vpn": {
                "provider": provider,
                "policy": str(vpn.get("policy", "optional")),
                "connected": connected,
                "kill_switch": bool(vpn.get("kill_switch", False)),
                "allow_lan": bool(vpn.get("allow_lan", True)),
                "endpoint": endpoint,
                "last_error": last_error,
            },
            "privacy_relay": {
                "mode": relay_mode,
                "tor": {
                    "enabled": tor_enabled,
                    "socks_host": str(
                        relay.get("tor", {}).get("listen_host", "127.0.0.1")
                    ),
                    "socks_port": int(
                        relay.get("tor", {}).get("socks_port", 9050)
                    ),
                    "healthy": False,
                    "last_error": None,
                },
            },
            "remote_access": {
                "mode": remote_mode,
                "healthy": False,
                "url": None,
                "last_error": None,
            },
        }

    def _openvpn_status(self) -> tuple[bool, str | None, str | None]:
        service_state = self._command_output(["systemctl", "is-active", "openvpn.service"])
        tunnel_interface = self._first_tunnel_interface()

        if service_state == "active" and tunnel_interface:
            return True, tunnel_interface, None
        if service_state == "active":
            return False, None, "openvpn active but no tunnel interface detected"
        return False, None, f"openvpn service state: {service_state or 'unknown'}"

    def _gluetun_status(self, gluetun_config: dict[str, Any]) -> tuple[bool, str | None, str | None]:
        stack_name = str(gluetun_config.get("stack_name", "rocky-transfer"))
        network_name = str(gluetun_config.get("network_name", "rocky_transfer_net"))
        containers = self._command_output([
            "docker", "ps", "--format", "{{.Names}} {{.Image}} {{.Status}}"
        ])
        networks = self._command_output([
            "docker", "network", "ls", "--format", "{{.Name}}"
        ])

        matched_line = None
        for line in containers.splitlines():
            lowered = line.lower()
            if "gluetun" in lowered or stack_name.lower() in lowered:
                matched_line = line.strip()
                break

        if matched_line and network_name in networks.splitlines():
            return True, matched_line.split()[0], None
        if matched_line:
            return False, matched_line.split()[0], f"docker network missing: {network_name}"
        return False, None, f"gluetun container not found for stack {stack_name}"

    def _first_tunnel_interface(self) -> str | None:
        output = self._command_output(["ip", "-brief", "addr"])
        for line in output.splitlines():
            parts = line.split()
            if not parts:
                continue
            name = parts[0]
            if name.startswith(("tun", "tap", "wg")):
                return name
        return None

    def _command_output(self, command: list[str]) -> str:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return ""

        return (result.stdout or result.stderr or "").strip()


class TransferManager:
    def __init__(
        self,
        config: NetworkTransferConfig,
        *,
        internal_root: Path = DEFAULT_INTERNAL_TRANSFER_ROOT,
        stack_root: Path = DEFAULT_TRANSFER_STACK_ROOT,
    ) -> None:
        self.config = config
        self.internal_root = Path(internal_root)
        self.stack_root = Path(stack_root)

    def config_snapshot(self) -> dict[str, Any]:
        return self.config.snapshot()["transfer"]

    def managed_subdirectories(self) -> tuple[str, str, str, str]:
        config = self.config.snapshot()["transfer"]
        return (
            str(config.get("incomplete_subdir", "incomplete")),
            str(config.get("complete_subdir", "complete")),
            str(config.get("watch_subdir", "watch")),
            str(config.get("metadata_subdir", "metadata")),
        )

    def state_snapshot(self) -> dict[str, Any]:
        root_config = self.config.snapshot()
        config = root_config["transfer"]
        vpn = root_config.get("vpn", {})
        complete_path = self.internal_root / str(
            config.get("complete_subdir", "complete")
        )
        incomplete_path = self.internal_root / str(
            config.get("incomplete_subdir", "incomplete")
        )

        engine = str(config.get("engine", "qbittorrent-nox"))
        destination_ready = self.internal_root.is_dir() and complete_path.is_dir() and incomplete_path.is_dir()
        stack_status = self._stack_status(engine=engine, vpn=vpn)
        downloader_status = self._downloader_status(engine=engine)

        return {
            "engine": engine,
            "healthy": bool(destination_ready and stack_status["engine_present"]),
            "vpn_required": bool(config.get("require_vpn", True)),
            "active_destination_id": str(
                config.get("destination_id", "internal")
            ),
            "destination_ready": destination_ready,
            "download_path": str(complete_path),
            "incomplete_path": str(incomplete_path),
            "last_error": stack_status["last_error"],
            "stack": stack_status,
            "downloader": downloader_status,
        }

    def _stack_status(self, *, engine: str, vpn: dict[str, Any]) -> dict[str, Any]:
        provider = str(vpn.get("provider", "none"))
        network_name = str(vpn.get("gluetun", {}).get("network_name", "rocky_transfer_net"))
        stack_name = str(vpn.get("gluetun", {}).get("stack_name", "rocky-transfer"))

        containers_output = self._command_output([
            "docker", "ps", "-a", "--format", "{{.Names}} {{.Image}} {{.Status}}"
        ])
        networks_output = self._command_output([
            "docker", "network", "ls", "--format", "{{.Name}}"
        ])

        containers = [line.strip() for line in containers_output.splitlines() if line.strip()]
        networks = {line.strip() for line in networks_output.splitlines() if line.strip()}

        gluetun_container = None
        engine_container = None
        engine_tokens = {
            "qbittorrent-nox": ["qbittorrent", "qbittorrent-nox"],
            "transmission": ["transmission"],
        }.get(engine, [engine.lower()])

        expected_gluetun_name = f"{stack_name}-gluetun".lower()
        for line in containers:
            lowered = line.lower()
            name = line.split()[0].lower() if line.split() else ""
            if gluetun_container is None and ("gluetun" in lowered or name == expected_gluetun_name):
                gluetun_container = line
            if engine_container is None and any(token in lowered for token in engine_tokens):
                engine_container = line

        gluetun_present = gluetun_container is not None
        engine_present = engine_container is not None
        network_present = network_name in networks

        last_error = None
        if provider == "gluetun":
            if not gluetun_present:
                last_error = f"gluetun container not found for stack {stack_name}"
            elif not network_present:
                last_error = f"docker network missing: {network_name}"
            elif not engine_present:
                last_error = f"transfer engine container not found for engine {engine}"
        elif not engine_present:
            last_error = f"transfer engine container not found for engine {engine}"

        return {
            "provider": provider,
            "gluetun_container": gluetun_container.split()[0] if gluetun_container else None,
            "gluetun_present": gluetun_present,
            "docker_network": network_name,
            "docker_network_present": network_present,
            "engine_container": engine_container.split()[0] if engine_container else None,
            "engine_present": engine_present,
            "last_error": last_error,
        }

    def _downloader_status(self, *, engine: str) -> dict[str, Any]:
        if engine == "qbittorrent-nox":
            return self._qbittorrent_status()
        return {
            "kind": engine,
            "web_ui_url": None,
            "api_reachable": None,
            "auth_required": None,
            "listen_port": None,
            "webui_port": None,
            "port_forwarding_enabled": None,
            "recent_warning": None,
        }

    def _qbittorrent_status(self) -> dict[str, Any]:
        conf_path = self.stack_root / "qbittorrent-config/qBittorrent/qBittorrent.conf"
        log_path = self.stack_root / "qbittorrent-config/qBittorrent/logs/qbittorrent.log"

        listen_port = None
        webui_port = None
        port_forwarding_enabled = None
        recent_warning = None
        web_ui_url = "http://192.168.1.199:8088"
        api_reachable = False
        auth_required = True

        if conf_path.is_file():
            parser = ConfigParser(strict=False)
            parser.optionxform = str
            try:
                parser.read(conf_path, encoding="utf-8")
                prefs = parser["Preferences"] if parser.has_section("Preferences") else {}
                network = parser["Network"] if parser.has_section("Network") else {}
                listen_port = prefs.get("Connection\\PortRangeMin") or prefs.get("Session\\Port")
                webui_port = prefs.get("WebUI\\Port") or "8080"
                pf_value = network.get("PortForwardingEnabled") or prefs.get("BitTorrent\\Session\\PortForwardingEnabled") or prefs.get("Session\\PortForwardingEnabled") or prefs.get("PortForwardingEnabled")
                if pf_value is not None:
                    port_forwarding_enabled = str(pf_value).strip().lower() == "true"
            except Exception:
                pass

        if log_path.is_file():
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                for line in reversed(lines[-120:]):
                    if "Invalid Host header" in line or "WebUI:" in line:
                        recent_warning = line.strip()
                        break
            except Exception:
                pass

        status_line = self._command_output(["docker", "inspect", "rocky-transfer-qbittorrent", "--format", "{{.State.Status}}"])
        if status_line == "running":
            api_reachable = True

        return {
            "kind": "qbittorrent",
            "web_ui_url": web_ui_url,
            "api_reachable": api_reachable,
            "auth_required": auth_required,
            "listen_port": int(listen_port) if str(listen_port).isdigit() else listen_port,
            "webui_port": int(webui_port) if str(webui_port).isdigit() else webui_port,
            "port_forwarding_enabled": port_forwarding_enabled,
            "recent_warning": recent_warning,
        }

    def _command_output(self, command: list[str]) -> str:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return ""

        return (result.stdout or result.stderr or "").strip()


class StorageManager:
    def __init__(
        self,
        *,
        internal_root: Path = DEFAULT_INTERNAL_TRANSFER_ROOT,
        usb_root: Path = DEFAULT_USB_TRANSFER_ROOT,
    ) -> None:
        self.internal_root = Path(internal_root)
        self.usb_root = Path(usb_root)

    def ensure_internal_destination(
        self,
        *,
        subdirectories: tuple[str, str, str, str],
    ) -> None:
        self.internal_root.mkdir(mode=0o775, parents=True, exist_ok=True)
        self._set_default_ownership(self.internal_root)
        self.internal_root.chmod(0o775)

        for name in subdirectories:
            destination = self.internal_root / name
            destination.mkdir(mode=0o775, parents=True, exist_ok=True)
            self._set_default_ownership(destination)
            destination.chmod(0o775)

    def state_snapshot(
        self,
        *,
        selected_destination_id: str = "internal",
    ) -> dict[str, Any]:
        destinations: list[dict[str, Any]] = [
            self._describe_destination(
                destination_id="internal",
                kind="internal",
                label="Internal",
                path=self.internal_root,
                selected=selected_destination_id == "internal",
            )
        ]

        if self.usb_root.is_dir():
            for child in sorted(self.usb_root.iterdir(), key=lambda p: p.name.lower()):
                if not child.is_dir():
                    continue
                destination_id = f"usb:{child.name}"
                transfer_path = child / "transfer"
                destinations.append(
                    self._describe_destination(
                        destination_id=destination_id,
                        kind="usb",
                        label=child.name,
                        path=transfer_path,
                        selected=selected_destination_id == destination_id,
                    )
                )

        return {
            "destinations": destinations,
        }

    def _set_default_ownership(self, path: Path) -> None:
        if pwd is None or grp is None:
            return
        try:
            uid = pwd.getpwnam('orangepi').pw_uid
            gid = grp.getgrnam('orangepi').gr_gid
        except KeyError:
            return

        try:
            os.chown(path, uid, gid)
        except PermissionError:
            return

    def _describe_destination(
        self,
        *,
        destination_id: str,
        kind: str,
        label: str,
        path: Path,
        selected: bool,
    ) -> dict[str, Any]:
        present = path.exists()
        writable = False
        available_bytes = None

        if present:
            writable = path.is_dir() and os_access_write(path)
            try:
                available_bytes = shutil.disk_usage(path).free
            except OSError:
                available_bytes = None

        return {
            "id": destination_id,
            "kind": kind,
            "label": label,
            "path": str(path),
            "present": present,
            "writable": writable,
            "available_bytes": available_bytes,
            "selected": selected,
        }


def os_access_write(path: Path) -> bool:
    try:
        return path.is_dir() and path.exists() and os.access(path, os.W_OK)
    except OSError:
        return False
