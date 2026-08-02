from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from manager.button_service import ButtonEvent, ButtonService
from manager.runtime import (
    ApplicationAlreadyRunning,
    ApplicationLaunchError,
    button_server,
)
from manager.runtime.context import RuntimeContext


LOGGER = logging.getLogger("zero2w-manager")
PROXY_TOKEN_SECRET_PATH = Path('/opt/zero2w-manager/runtime/config/proxy-token-secret')
PUBLIC_BASE_URL = os.environ.get('ROCKY_PUBLIC_BASE_URL', '').strip()
ROCKY_WEB_PORT = int(os.environ.get('ROCKY_WEB_PORT', '8090'))
PROXY_TOKEN_TTL_SECONDS = int(os.environ.get('ROCKY_WEB_PROXY_TOKEN_TTL_SECONDS', '900'))
MODE_CATALOG_PATH = Path('/opt/zero2w-manager/runtime/config/modes.json')
CURRENT_MODE_REQUEST_PATH = Path('/opt/zero2w-manager/runtime/config/current-mode.json')
WEB_SERVICE_CACHE_PATH = Path('/opt/zero2w-manager/runtime/config/web-service-cache.json')
DEFAULT_MODE_CATALOG = {'version': 1, 'modes': [{'mode_id': 'safe', 'label': 'Safe Mode', 'description': 'Minimal known-good Rocky state.', 'category': 'baseline', 'network': {'profile': 'lan_only', 'vpn_required': False, 'vpn_provider': 'none', 'mobile_exit_node': False, 'allow_lan_admin': True, 'allow_wireguard_admin': True, 'allow_public_admin': False, 'firewall_policy': 'strict', 'dns_mode': 'router_default', 'mac_randomization': False, 'public_network_posture': 'cautious'}, 'dns': {'provider': 'system', 'serve_lan': False, 'serve_wireguard_clients': False, 'upstream_mode': 'vpn_preferred', 'ad_blocking': False, 'safe_search': False, 'blocklists_profile': 'none'}, 'admin': {'rocky_admin': {'enabled': True, 'lan': True, 'wireguard': True, 'public': False}, 'qb_webui': {'enabled': False, 'lan': False, 'wireguard': False}, 'terminal': {'enabled': True}, 'auth_profile': 'standard'}, 'transfer': {'enabled': False, 'client': 'qbittorrent', 'vpn_enforced': False, 'kill_switch': True, 'webui_exposure': 'none', 'privacy_profile': 'strict_off', 'bittorrent': {'anonymous_mode': False, 'force_encryption': False, 'dht': False, 'pex': False, 'lsd': False, 'upnp': False, 'fixed_port': 6881, 'random_port': False, 'port_forwarding': False}}, 'portable': {'enabled': False, 'hotspot_enabled': False, 'hotspot_ssid': None, 'captive_portal': False, 'passive_collection': False, 'active_collection': False, 'storage_capture': False}, 'power': {'profile': 'normal', 'suspend_nonessential_services': True, 'reduced_polling': True, 'display_refresh_policy': 'normal', 'radios_policy': 'normal'}, 'pikvm': {'policy': 'auto', 'require_minipc_presence': True, 'auto_disable_when_disconnected': True}, 'display': {'surface': 'browser_and_epaper', 'epaper_menu_enabled': True, 'epaper_qr_behavior': 'mode_aware', 'test_path': 'zero2w_manager_menu'}, 'ui': {'warning_level': 'normal', 'reversible_to': 'safe', 'color_hint': 'blue', 'expose_advanced_toggles': False}}, {'mode_id': 'torrent_fortress', 'label': 'Torrent Fortress', 'description': 'Strongest torrent/privacy posture with Proton-enforced transfer path.', 'category': 'privacy', 'network': {'profile': 'wireguard_admin', 'vpn_required': True, 'vpn_provider': 'protonvpn', 'mobile_exit_node': False, 'allow_lan_admin': True, 'allow_wireguard_admin': True, 'allow_public_admin': False, 'firewall_policy': 'strict', 'dns_mode': 'pihole_lan_and_wg', 'mac_randomization': False, 'public_network_posture': 'cautious'}, 'dns': {'provider': 'pihole', 'serve_lan': True, 'serve_wireguard_clients': True, 'upstream_mode': 'vpn_preferred', 'ad_blocking': True, 'safe_search': False, 'blocklists_profile': 'balanced'}, 'admin': {'rocky_admin': {'enabled': True, 'lan': True, 'wireguard': True, 'public': False}, 'qb_webui': {'enabled': True, 'lan': True, 'wireguard': True}, 'terminal': {'enabled': True}, 'auth_profile': 'hardened'}, 'transfer': {'enabled': True, 'client': 'qbittorrent', 'vpn_enforced': True, 'kill_switch': True, 'webui_exposure': 'lan_and_wireguard', 'privacy_profile': 'fortress', 'bittorrent': {'anonymous_mode': True, 'force_encryption': True, 'dht': False, 'pex': False, 'lsd': False, 'upnp': False, 'fixed_port': 6881, 'random_port': False, 'port_forwarding': False}}, 'portable': {'enabled': False, 'hotspot_enabled': False, 'hotspot_ssid': None, 'captive_portal': False, 'passive_collection': False, 'active_collection': False, 'storage_capture': False}, 'power': {'profile': 'normal', 'suspend_nonessential_services': False, 'reduced_polling': False, 'display_refresh_policy': 'normal', 'radios_policy': 'normal'}, 'pikvm': {'policy': 'auto', 'require_minipc_presence': True, 'auto_disable_when_disconnected': True}, 'display': {'surface': 'browser_and_epaper', 'epaper_menu_enabled': True, 'epaper_qr_behavior': 'mode_aware', 'test_path': 'zero2w_manager_menu'}, 'ui': {'warning_level': 'caution', 'reversible_to': 'safe', 'color_hint': 'red', 'expose_advanced_toggles': False}}, {'mode_id': 'pihole_only', 'label': 'Pi-hole Only', 'description': 'Pi-hole DNS active for LAN ad-blocking. No VPN, no transfer.', 'category': 'utility', 'network': {'profile': 'lan_only', 'vpn_required': False, 'vpn_provider': 'none', 'mobile_exit_node': False, 'allow_lan_admin': True, 'allow_wireguard_admin': True, 'allow_public_admin': False, 'firewall_policy': 'strict', 'dns_mode': 'pihole_lan_only', 'mac_randomization': False, 'public_network_posture': 'cautious'}, 'dns': {'provider': 'pihole', 'serve_lan': True, 'serve_wireguard_clients': False, 'upstream_mode': 'direct', 'ad_blocking': True, 'safe_search': False, 'blocklists_profile': 'balanced'}, 'admin': {'rocky_admin': {'enabled': True, 'lan': True, 'wireguard': True, 'public': False}, 'qb_webui': {'enabled': False, 'lan': False, 'wireguard': False}, 'terminal': {'enabled': True}, 'auth_profile': 'standard'}, 'transfer': {'enabled': False, 'client': 'qbittorrent', 'vpn_enforced': False, 'kill_switch': False, 'webui_exposure': 'none', 'privacy_profile': 'off', 'bittorrent': {'anonymous_mode': False, 'force_encryption': False, 'dht': False, 'pex': False, 'lsd': False, 'upnp': False, 'fixed_port': 6881, 'random_port': False, 'port_forwarding': False}}, 'portable': {'enabled': False, 'hotspot_enabled': False, 'hotspot_ssid': None, 'captive_portal': False, 'passive_collection': False, 'active_collection': False, 'storage_capture': False}, 'power': {'profile': 'normal', 'suspend_nonessential_services': False, 'reduced_polling': False, 'display_refresh_policy': 'normal', 'radios_policy': 'normal'}, 'pikvm': {'policy': 'auto', 'require_minipc_presence': True, 'auto_disable_when_disconnected': True}, 'display': {'surface': 'browser_and_epaper', 'epaper_menu_enabled': True, 'epaper_qr_behavior': 'mode_aware', 'test_path': 'zero2w_manager_menu'}, 'ui': {'warning_level': 'normal', 'reversible_to': 'safe', 'color_hint': 'green', 'expose_advanced_toggles': False}}, {'mode_id': 'print_lab', 'label': 'Print Lab', 'description': '3D printer apps active. LAN-only, no VPN, no torrent.', 'category': 'utility', 'network': {'profile': 'lan_only', 'vpn_required': False, 'vpn_provider': 'none', 'mobile_exit_node': False, 'allow_lan_admin': True, 'allow_wireguard_admin': True, 'allow_public_admin': False, 'firewall_policy': 'relaxed', 'dns_mode': 'router_default', 'mac_randomization': False, 'public_network_posture': 'cautious'}, 'dns': {'provider': 'system', 'serve_lan': False, 'serve_wireguard_clients': False, 'upstream_mode': 'direct', 'ad_blocking': False, 'safe_search': False, 'blocklists_profile': 'none'}, 'admin': {'rocky_admin': {'enabled': True, 'lan': True, 'wireguard': True, 'public': False}, 'qb_webui': {'enabled': False, 'lan': False, 'wireguard': False}, 'terminal': {'enabled': True}, 'auth_profile': 'standard'}, 'transfer': {'enabled': False, 'client': 'qbittorrent', 'vpn_enforced': False, 'kill_switch': False, 'webui_exposure': 'none', 'privacy_profile': 'off', 'bittorrent': {'anonymous_mode': False, 'force_encryption': False, 'dht': False, 'pex': False, 'lsd': False, 'upnp': False, 'fixed_port': 6881, 'random_port': False, 'port_forwarding': False}}, 'portable': {'enabled': False, 'hotspot_enabled': False, 'hotspot_ssid': None, 'captive_portal': False, 'passive_collection': False, 'active_collection': False, 'storage_capture': False}, 'power': {'profile': 'normal', 'suspend_nonessential_services': False, 'reduced_polling': False, 'display_refresh_policy': 'normal', 'radios_policy': 'normal'}, 'pikvm': {'policy': 'auto', 'require_minipc_presence': True, 'auto_disable_when_disconnected': True}, 'display': {'surface': 'browser_and_epaper', 'epaper_menu_enabled': True, 'epaper_qr_behavior': 'mode_aware', 'test_path': 'zero2w_manager_menu'}, 'ui': {'warning_level': 'normal', 'reversible_to': 'safe', 'color_hint': 'yellow', 'expose_advanced_toggles': False}}, {'mode_id': 'daily_driver', 'label': 'Daily Driver', 'description': 'Balanced everyday mode. Pi-hole on, no torrent, LAN admin.', 'category': 'baseline', 'network': {'profile': 'lan_only', 'vpn_required': False, 'vpn_provider': 'none', 'mobile_exit_node': False, 'allow_lan_admin': True, 'allow_wireguard_admin': True, 'allow_public_admin': False, 'firewall_policy': 'standard', 'dns_mode': 'pihole_lan_only', 'mac_randomization': False, 'public_network_posture': 'cautious'}, 'dns': {'provider': 'pihole', 'serve_lan': True, 'serve_wireguard_clients': True, 'upstream_mode': 'direct', 'ad_blocking': True, 'safe_search': False, 'blocklists_profile': 'balanced'}, 'admin': {'rocky_admin': {'enabled': True, 'lan': True, 'wireguard': True, 'public': False}, 'qb_webui': {'enabled': False, 'lan': False, 'wireguard': False}, 'terminal': {'enabled': True}, 'auth_profile': 'standard'}, 'transfer': {'enabled': False, 'client': 'qbittorrent', 'vpn_enforced': False, 'kill_switch': False, 'webui_exposure': 'none', 'privacy_profile': 'off', 'bittorrent': {'anonymous_mode': False, 'force_encryption': False, 'dht': False, 'pex': False, 'lsd': False, 'upnp': False, 'fixed_port': 6881, 'random_port': False, 'port_forwarding': False}}, 'portable': {'enabled': False, 'hotspot_enabled': False, 'hotspot_ssid': None, 'captive_portal': False, 'passive_collection': False, 'active_collection': False, 'storage_capture': False}, 'power': {'profile': 'normal', 'suspend_nonessential_services': False, 'reduced_polling': False, 'display_refresh_policy': 'normal', 'radios_policy': 'normal'}, 'pikvm': {'policy': 'auto', 'require_minipc_presence': True, 'auto_disable_when_disconnected': True}, 'display': {'surface': 'browser_and_epaper', 'epaper_menu_enabled': True, 'epaper_qr_behavior': 'mode_aware', 'test_path': 'zero2w_manager_menu'}, 'ui': {'warning_level': 'normal', 'reversible_to': 'safe', 'color_hint': 'blue', 'expose_advanced_toggles': False}}]}
DEFAULT_CURRENT_MODE_REQUEST = {'version': 1, 'selected_mode_id': 'torrent_fortress', 'previous_mode_id': 'safe', 'requested_at': '2026-07-26T20:45:00Z', 'requested_by': 'operator', 'reason': 'Enable the strongest current torrent/privacy posture while preserving Rocky Admin access on LAN and WireGuard.', 'override_flags': {'mobile_exit_node': False, 'pikvm_policy': 'auto', 'epaper_menu_enabled': True, 'epaper_test_path': 'zero2w_manager_menu', 'allow_public_admin': False}}


def load_proxy_token_secret() -> str:
    env_secret = os.environ.get('ROCKY_WEB_PROXY_TOKEN_SECRET')
    if env_secret:
        return env_secret
    try:
        if PROXY_TOKEN_SECRET_PATH.is_file():
            return PROXY_TOKEN_SECRET_PATH.read_text(encoding='utf-8').strip()
        PROXY_TOKEN_SECRET_PATH.parent.mkdir(mode=0o775, parents=True, exist_ok=True)
        import secrets
        generated = secrets.token_urlsafe(32)
        PROXY_TOKEN_SECRET_PATH.write_text(generated + '\n', encoding='utf-8')
        return generated
    except OSError:
        return 'rocky-fallback-secret'


class ManagerDaemon:
    def __init__(self) -> None:
        self.log = LOGGER
        self.running = True

        self.context = RuntimeContext.create(
            on_application_exit=self._handle_application_exit,
        )

        # Compatibility aliases keep this first refactor behavior-neutral.
        # They can be removed gradually as components consume RuntimeContext
        # directly in later milestones.
        self.menu = self.context.menu
        self.state_publisher = self.context.state_publisher
        self.application_manager = self.context.application_manager
        self.network_manager = self.context.network_manager
        self.transfer_manager = self.context.transfer_manager
        self.storage_manager = self.context.storage_manager
        self._state_lock = self.context.state_lock

        self.menu_visible = True
        self.active_app_id: str | None = None

        # Used to prevent the application exit callback from drawing
        # the menu while a deliberate long-press stop is still running.
        self._manual_stop_in_progress = False
        self._config_poll_interval_seconds = 1.0
        self._last_published_config_signature: str | None = None
        self._last_config_reconcile_at = 0.0
        self._last_mode_signature: str | None = None

        self._publish_reconciled_infrastructure_state(force_refresh=True)

    def _infrastructure_state(
        self,
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        if force_refresh:
            self.context.network_config.reload()

        self.storage_manager.ensure_internal_destination(
            subdirectories=self.transfer_manager.managed_subdirectories(),
        )

        transfer_state = self.transfer_manager.state_snapshot()
        destination_id = str(
            transfer_state.get("active_destination_id", "internal")
        )

        return {
            "network": self.network_manager.state_snapshot(),
            "transfer": transfer_state,
            "storage": self.storage_manager.state_snapshot(
                selected_destination_id=destination_id,
            ),
        }

    def _publish_reconciled_infrastructure_state(
        self,
        *,
        force_refresh: bool = False,
    ) -> bool:
        changed = False

        if force_refresh:
            self.context.network_config.reload()
            changed = True
        elif self.context.network_config.reload_if_changed():
            changed = True

        infrastructure_state = self._infrastructure_state()
        signature = json.dumps(infrastructure_state, sort_keys=True)

        if signature != self._last_published_config_signature:
            self._last_published_config_signature = signature
            self._publish_runtime_state(infrastructure_state)
            changed = True

        return changed

    def _docker_container_health(self, name: str) -> str:
        """Return 'healthy', 'running', 'stopped', or 'unknown'."""
        try:
            import subprocess as _sp
            r = _sp.run(
                ['docker', 'inspect', '--format', '{{.State.Status}}', name],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if r.returncode != 0:
                return 'stopped'
            state = r.stdout.strip()
            if state != 'running':
                return 'stopped'
            h = _sp.run(
                ['docker', 'inspect', '--format', '{{.State.Health.Status}}', name],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if h.returncode == 0 and h.stdout.strip() == 'healthy':
                return 'healthy'
            return 'running'
        except Exception:
            return 'unknown'

    def _docker_ensure(self, name: str, running: bool) -> bool:
        """Start or stop a Docker container. Returns True on success."""
        import subprocess as _sp
        action = 'start' if running else 'stop'
        try:
            r = _sp.run(
                ['docker', action, name],
                capture_output=True, text=True, timeout=30, check=False,
            )
            return r.returncode == 0
        except Exception:
            self.log.exception('docker %s %s failed', action, name)
            return False

    def _reconcile_mode_actions(self, effective_mode_id: str) -> None:
        """Enforce the actual running state of transfer containers for the given mode."""
        import time as _time
        GLUETUN = 'rocky-transfer-gluetun'
        QBT = 'rocky-transfer-qbittorrent'
        PIHOLE = 'rocky-pihole'

        if effective_mode_id == 'safe':
            # Stop qBittorrent first so it does not leak on VPN teardown
            qbt_health = self._docker_container_health(QBT)
            if qbt_health != 'stopped':
                self.log.info('safe mode: stopping %s', QBT)
                self._docker_ensure(QBT, running=False)
            gluetun_health = self._docker_container_health(GLUETUN)
            if gluetun_health != 'stopped':
                self.log.info('safe mode: stopping %s', GLUETUN)
                self._docker_ensure(GLUETUN, running=False)
            pihole_health = self._docker_container_health(PIHOLE)
            if pihole_health != 'stopped':
                self.log.info('safe mode: stopping %s', PIHOLE)
                self._docker_ensure(PIHOLE, running=False)

        elif effective_mode_id == 'torrent_fortress':
            # Start gluetun first, wait for healthy, then start qBittorrent
            gluetun_health = self._docker_container_health(GLUETUN)
            if gluetun_health == 'stopped':
                self.log.info('torrent_fortress: starting %s', GLUETUN)
                self._docker_ensure(GLUETUN, running=True)
                for _ in range(15):
                    _time.sleep(2)
                    gluetun_health = self._docker_container_health(GLUETUN)
                    if gluetun_health in ('healthy', 'running'):
                        break
                self.log.info('torrent_fortress: %s health=%s', GLUETUN, gluetun_health)

            if gluetun_health in ('healthy', 'running'):
                qbt_health = self._docker_container_health(QBT)
                if qbt_health == 'stopped':
                    self.log.info('torrent_fortress: starting %s', QBT)
                    self._docker_ensure(QBT, running=True)
            else:
                self.log.warning(
                    'torrent_fortress: skipping %s start - gluetun not healthy (%s)',
                    QBT, gluetun_health,
                )

        elif effective_mode_id in ('pihole_only', 'daily_driver'):
            pihole_health = self._docker_container_health(PIHOLE)
            if pihole_health == 'stopped':
                self.log.info('%s: starting %s', effective_mode_id, PIHOLE)
                self._docker_ensure(PIHOLE, running=True)
            qbt_health = self._docker_container_health(QBT)
            if qbt_health != 'stopped':
                self.log.info('%s: stopping %s', effective_mode_id, QBT)
                self._docker_ensure(QBT, running=False)
            gluetun_health = self._docker_container_health(GLUETUN)
            if gluetun_health != 'stopped':
                self.log.info('%s: stopping %s', effective_mode_id, GLUETUN)
                self._docker_ensure(GLUETUN, running=False)

        elif effective_mode_id == 'print_lab':
            pihole_health = self._docker_container_health(PIHOLE)
            if pihole_health != 'stopped':
                self.log.info('print_lab: stopping %s', PIHOLE)
                self._docker_ensure(PIHOLE, running=False)
            qbt_health = self._docker_container_health(QBT)
            if qbt_health != 'stopped':
                self.log.info('print_lab: stopping %s', QBT)
                self._docker_ensure(QBT, running=False)
            gluetun_health = self._docker_container_health(GLUETUN)
            if gluetun_health != 'stopped':
                self.log.info('print_lab: stopping %s', GLUETUN)
                self._docker_ensure(GLUETUN, running=False)
            for svc in ('klipper', 'moonraker', 'nginx'):
                r = __import__('subprocess').run(['systemctl', 'is-active', svc], capture_output=True, text=True)
                if r.stdout.strip() != 'active':
                    self.log.info('print_lab: starting %s', svc)
                    __import__('subprocess').run(['systemctl', 'start', svc], check=False)
        else:
            self.log.debug('_reconcile_mode_actions: no action for mode %s', effective_mode_id)

    def _button_loop_should_continue(self) -> bool:
        if not self.running:
            return False

        now = time.monotonic()
        if (
            now - self._last_config_reconcile_at
            >= self._config_poll_interval_seconds
        ):
            self._last_config_reconcile_at = now
            try:
                mode_payload = self._resolve_mode_payload()
                mode_signature = json.dumps(mode_payload.get("mode", {}), sort_keys=True)
                if mode_signature != self._last_mode_signature:
                    self._last_mode_signature = mode_signature
                    self._publish_runtime_state()
                    self._reconcile_mode_actions(
                        str(mode_payload.get("mode", {}).get("live", {}).get("mode_id", "safe"))
                    )
                self._publish_reconciled_infrastructure_state()
            except Exception:
                self.log.exception(
                    "Failed to reconcile persisted network/transfer config"
                )

        return self.running

    def _publish_runtime_state(
        self,
        values: dict[str, Any] | None = None,
    ) -> None:
        """Publish runtime state without disrupting device operation."""

        try:
            payload = {}
            if values:
                payload.update(values)
            mode_payload = self._resolve_mode_payload()
            if isinstance(mode_payload, dict):
                payload["mode"] = mode_payload.get("mode", {})
                if "web_services" in mode_payload:
                    payload["web_services"] = mode_payload.get("web_services", {})
                if "web_service_cache" in mode_payload:
                    payload["web_service_cache"] = mode_payload.get("web_service_cache", {})
                if hasattr(self.menu, "footer_override"):
                    self.menu.footer_override = self._mode_footer_text(mode_payload)
            if payload:
                self.state_publisher.update(payload)
            else:
                self.state_publisher.publish()
        except Exception:
            self.log.exception(
                "Failed to publish Rocky runtime state"
            )

    def _default_mode_catalog(self) -> dict[str, Any]:
        return json.loads(json.dumps(DEFAULT_MODE_CATALOG))

    def _default_current_mode_request(self) -> dict[str, Any]:
        return json.loads(json.dumps(DEFAULT_CURRENT_MODE_REQUEST))

    def _read_json_file(self, path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    return data
        except Exception:
            self.log.exception("Failed to read JSON config: %s", path)
        return json.loads(json.dumps(fallback))

    def _load_mode_catalog(self) -> dict[str, Any]:
        return self._read_json_file(MODE_CATALOG_PATH, self._default_mode_catalog())

    def _load_current_mode_request(self) -> dict[str, Any]:
        return self._read_json_file(CURRENT_MODE_REQUEST_PATH, self._default_current_mode_request())

    def _mode_service_status(self, unit: str) -> str:
        try:
            result = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, check=False, timeout=5)
            return result.stdout.strip() or result.stderr.strip() or 'unknown'
        except Exception:
            return 'unknown'

    def _load_web_service_cache(self) -> dict[str, Any]:
        return self._read_json_file(WEB_SERVICE_CACHE_PATH, {})

    def _save_web_service_cache(self, payload: dict[str, Any]) -> None:
        try:
            WEB_SERVICE_CACHE_PATH.parent.mkdir(mode=0o775, parents=True, exist_ok=True)
            WEB_SERVICE_CACHE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        except Exception:
            self.log.exception('Failed to persist web service cache')

    def _build_web_services_state(self) -> dict[str, Any]:
        live: dict[str, Any] = {}
        cache = self._load_web_service_cache()
        cache_changed = False
        for app_id, service in self.menu.services.items():
            if not isinstance(service, dict):
                continue
            raw_url = service.get('url')
            if not isinstance(raw_url, str) or not raw_url:
                continue
            unit = service.get('systemd_service')
            active = False
            if unit:
                active = self._mode_service_status(str(unit)) == 'active'
            elif self.active_app_id == app_id:
                active = True
            public_url = self._publicize_service_url(raw_url)
            tokenized_proxy_url = self._tokenized_proxy_url(str(app_id))
            entry = {
                'available': True,
                'active': bool(active),
                'url': public_url,
                'tokenized_proxy_url': tokenized_proxy_url,
            }
            live[str(app_id)] = entry
            if active:
                cached = cache.get(str(app_id), {}) if isinstance(cache.get(str(app_id)), dict) else {}
                new_cached = {
                    'last_url': public_url,
                    'last_tokenized_proxy_url': tokenized_proxy_url,
                    'last_seen_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                }
                if cached != new_cached:
                    cache[str(app_id)] = new_cached
                    cache_changed = True
        if cache_changed:
            self._save_web_service_cache(cache)
        return {'web_services': live, 'web_service_cache': cache}

    def _apply_mode_overrides(self, mode: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        effective = json.loads(json.dumps(mode))
        overrides = request.get('override_flags', {})
        if not isinstance(overrides, dict):
            return effective
        if 'mobile_exit_node' in overrides:
            effective.setdefault('network', {})['mobile_exit_node'] = bool(overrides['mobile_exit_node'])
        if 'allow_public_admin' in overrides:
            effective.setdefault('network', {})['allow_public_admin'] = bool(overrides['allow_public_admin'])
        if 'pikvm_policy' in overrides:
            effective.setdefault('pikvm', {})['policy'] = str(overrides['pikvm_policy'])
        if 'epaper_menu_enabled' in overrides:
            effective.setdefault('display', {})['epaper_menu_enabled'] = bool(overrides['epaper_menu_enabled'])
        if 'epaper_test_path' in overrides:
            effective.setdefault('display', {})['test_path'] = str(overrides['epaper_test_path'])
        return effective

    def _resolve_mode_payload(self) -> dict[str, Any]:
        catalog = self._load_mode_catalog()
        request = self._load_current_mode_request()
        modes = catalog.get('modes', []) if isinstance(catalog.get('modes'), list) else []
        mode_map = {str(mode.get('mode_id')): mode for mode in modes if isinstance(mode, dict) and mode.get('mode_id')}
        selected_mode_id = str(request.get('selected_mode_id') or 'safe')
        base_mode = mode_map.get(selected_mode_id) or mode_map.get('safe')
        if not isinstance(base_mode, dict):
            base_mode = self._default_mode_catalog()['modes'][0]
        effective_mode = self._apply_mode_overrides(base_mode, request)
        effective_mode_id = str(effective_mode.get('mode_id', selected_mode_id))
        effective_label = str(effective_mode.get('label', effective_mode_id.replace('_', ' ').title()))

        transfer_state = self.transfer_manager.state_snapshot()
        gluetun_ok = bool(transfer_state.get('healthy'))
        qb_enabled = bool(effective_mode.get('transfer', {}).get('enabled'))
        qbt_status = 'running' if qb_enabled and gluetun_ok else ('disabled' if not qb_enabled else 'degraded')
        warnings: list[str] = []
        healthy = True
        if effective_mode_id == 'torrent_fortress' and not gluetun_ok:
            warnings.append('transfer_vpn_not_healthy')
            healthy = False
        if effective_mode_id == 'safe' and transfer_state.get('healthy'):
            warnings.append('transfer_stack_active_under_safe_mode')

        service_state = self._build_web_services_state()
        return {
            'mode': {
                'desired': {
                    'mode_id': effective_mode_id,
                    'requested_at': str(request.get('requested_at') or ''),
                    'requested_by': str(request.get('requested_by') or 'operator'),
                },
                'live': {
                    'mode_id': effective_mode_id,
                    'label': effective_label,
                    'healthy': healthy,
                    'reconciled_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                    'warnings': warnings,
                },
                'display': effective_mode.get('display', {}),
                'services': {
                    'wireguard_admin': self._mode_service_status('wg-quick@wg0'),
                    'gluetun': 'healthy' if gluetun_ok else 'degraded',
                    'qbittorrent': qbt_status,
                    'pihole': self._mode_service_status('pihole-FTL'),
                    'pikvm': str(effective_mode.get('pikvm', {}).get('policy', 'auto')),
                },
                'policies': {
                    'lan_admin': bool(effective_mode.get('network', {}).get('allow_lan_admin', True)),
                    'wireguard_admin': bool(effective_mode.get('network', {}).get('allow_wireguard_admin', True)),
                    'qb_webui_lan': bool(effective_mode.get('admin', {}).get('qb_webui', {}).get('lan', False)),
                    'qb_webui_wireguard': bool(effective_mode.get('admin', {}).get('qb_webui', {}).get('wireguard', False)),
                    'torrent_privacy_profile': str(effective_mode.get('transfer', {}).get('privacy_profile', 'strict_off')),
                    'dns_mode': str(effective_mode.get('network', {}).get('dns_mode', 'router_default')),
                },
            },
            'web_services': service_state.get('web_services', {}),
            'web_service_cache': service_state.get('web_service_cache', {}),
        }

    def _mode_footer_text(self, mode_payload: dict[str, Any] | None = None) -> str:
        payload = mode_payload or self._resolve_mode_payload()
        mode = payload.get('mode', {}) if isinstance(payload, dict) else {}
        live = mode.get('live', {}) if isinstance(mode.get('live'), dict) else {}
        services = mode.get('services', {}) if isinstance(mode.get('services'), dict) else {}
        label = str(live.get('label', mode.get('desired', {}).get('mode_id', 'MODE'))).upper()
        short_label = label.replace(' MODE', '')[:12]
        vpn_text = 'VPN OK' if services.get('gluetun') == 'healthy' else 'VPN OFF'
        qbt_text = 'QBT ON' if services.get('qbittorrent') == 'running' else 'QBT OFF'
        pikvm_text = f"PIKVM {str(services.get('pikvm', 'AUTO')).upper()}"
        return f"{short_label} | {vpn_text} | {qbt_text} | {pikvm_text}"[:38]
    def _service_supports_qr_preview(self, service: dict[str, Any] | None) -> bool:
        if not isinstance(service, dict):
            return False
        app_id = service.get('id')
        if app_id and self._resolve_qr_target(str(app_id)):
            return True
        if not service.get('url'):
            return False
        return bool(service.get('systemd_service') or service.get('type') == 'background_service')

    def _resolve_qr_target(self, app_id: str) -> str | None:
        try:
            state_path = Path('/run/rocky/state.json')
            if state_path.is_file():
                state = json.loads(state_path.read_text(encoding='utf-8'))
                if isinstance(state, dict):
                    web_services = state.get('web_services', {})
                    cache = state.get('web_service_cache', {})
                    live = web_services.get(app_id) if isinstance(web_services, dict) else None
                    if isinstance(live, dict):
                        tokenized = live.get('tokenized_proxy_url')
                        if isinstance(tokenized, str) and tokenized:
                            return tokenized
                    cached = cache.get(app_id) if isinstance(cache, dict) else None
                    if isinstance(cached, dict):
                        tokenized = cached.get('last_tokenized_proxy_url')
                        if isinstance(tokenized, str) and tokenized:
                            return tokenized
        except Exception:
            return None
        return None

    def _selection_footer_text(self, selected: dict[str, Any] | None = None) -> str:
        base = self._mode_footer_text()
        selected = selected if isinstance(selected, dict) else self.menu.selected
        if not isinstance(selected, dict):
            return base
        selected_id = selected.get('id')
        if not selected_id:
            return base
        try:
            service = self._service_configuration(str(selected_id))
        except Exception:
            return base
        if not self._service_supports_qr_preview(service):
            return base
        if self._resolve_qr_target(str(selected_id)):
            return f"{base[:26]} | SELECT=QR"[:38]
        unit = service.get('systemd_service')
        status = self._mode_service_status(str(unit)) if unit else 'unknown'
        if status == 'active':
            return f"{base[:26]} | SELECT=QR"[:38]
        return base

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

    def _app_is_running(self) -> bool:
        return self.application_manager.is_running

    def _service_configuration(
        self,
        app_id: str,
    ) -> dict[str, Any]:
        """Return the complete service configuration for an app."""

        configured = self.menu.services.get(app_id)

        if not isinstance(configured, dict):
            raise ApplicationLaunchError(
                f"Unknown application: {app_id}"
            )

        service = dict(configured)
        service["id"] = app_id

        return service

    def _primary_lan_ip(self) -> str:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            value = probe.getsockname()[0]
            probe.close()
            return value
        except Exception:
            return socket.gethostname()

    def _publicize_service_url(self, raw_url: str) -> str:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(raw_url)
        host = parsed.hostname or self._primary_lan_ip()
        if host in {"127.0.0.1", "localhost"}:
            host = self._primary_lan_ip()
        netloc = host
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urlunparse((parsed.scheme or "http", netloc, parsed.path or "/", "", parsed.query, parsed.fragment))

    def _build_proxy_token(self, app_id: str, *, ttl_seconds: int = PROXY_TOKEN_TTL_SECONDS) -> str:
        issued_at = int(time.time())
        expires_at = issued_at + max(60, int(ttl_seconds))
        payload = json.dumps({"app": app_id, "exp": expires_at}, separators=(",", ":")).encode('utf-8')
        payload_b64 = base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')
        secret = load_proxy_token_secret().encode('utf-8')
        signature = hmac.new(secret, payload_b64.encode('utf-8'), hashlib.sha256).digest()
        signature_b64 = base64.urlsafe_b64encode(signature).decode('ascii').rstrip('=')
        return f"{payload_b64}.{signature_b64}"

    def _preferred_console_base(self) -> str:
        configured = PUBLIC_BASE_URL.rstrip('/')
        if configured:
            parsed = urlparse(configured if '://' in configured else f"http://{configured}")
            if parsed.netloc:
                base_path = parsed.path.rstrip('/')
                return urlunparse((parsed.scheme or 'http', parsed.netloc, base_path, '', '', ''))
        return f"http://{self._primary_lan_ip()}:{ROCKY_WEB_PORT}"

    def _tokenized_proxy_url(self, app_id: str) -> str:
        token = self._build_proxy_token(app_id)
        return self._preferred_console_base().rstrip('/') + f"/proxy/{quote(app_id)}/?access_token={quote(token)}"

    def show_menu(
        self,
        message: str | None = None,
    ) -> None:
        with self._state_lock:
            if not self.running:
                return

            try:
                if hasattr(self.menu, "footer_override"):
                    self.menu.footer_override = self._selection_footer_text(self.menu.selected if self.menu.items else None)
                self.menu.render(message)
            except Exception:
                self.menu_visible = False
                self.log.exception(
                    "Failed to display manager menu"
                )
                return

            self.menu_visible = True
            self.active_app_id = None

            selected = self.menu.selected
            selected_id = (
                str(selected.get("id"))
                if isinstance(selected, dict)
                and selected.get("id")
                else None
            )

            self.state_publisher.set_foreground_application(
                None,
                transition="launcher_activated",
                publish=False,
            )
            self.state_publisher.set_launcher_selection(
                selected_id,
                publish=False,
            )
            self._publish_runtime_state(
                {
                    "runtime": {
                        "status": "running",
                        "mode": "launcher",
                    },
                    "foreground_application": None,
                    "launcher": {
                        "active": True,
                        "selected_application": selected_id,
                    },
                    "display": {
                        "connected": True,
                        "mode": "launcher",
                    },
                    "application": {
                        "active_id": None,
                        "active_pid": None,
                        "status": "idle",
                    },
                }
            )

    def hide_menu(self) -> None:
        with self._state_lock:
            self.menu_visible = False

            self._publish_runtime_state(
                {
                    "runtime": {
                        "mode": "application",
                    },
                    "launcher": {
                        "active": False,
                    },
                    "display": {
                        "connected": True,
                        "mode": "application_owned",
                    },
                }
            )

            # Remove residual launcher pixels before handing the
            # panel to the application.
            try:
                self.menu.prepare_for_app()
            except Exception:
                self.log.exception(
                    "Failed to clear display before "
                    "application launch"
                )

            # Release GPIO/SPI resources so the application can
            # take ownership of the e-paper display.
            self.menu.close()

    def handle_navigation(
        self,
        event: ButtonEvent,
    ) -> None:
        """KEY_1 moves to the next launcher item."""

        if self._app_is_running() or not self.menu_visible:
            self.log.info(
                "Forwarding Navigate button to active application"
            )

            button_server.publish(
                "navigate",
                "short_press",
                duration=event.held_seconds,
            )
            return

        self.log.info(
            "Navigation button: %s held %.2fs",
            event.name,
            event.held_seconds,
        )

        self.state_publisher.set_buttons(
            last_event=True,
            publish=False,
        )
        self._publish_runtime_state(
            {
                "input": {
                    "provider": "lradc",
                    "action": "navigate",
                    "name": event.name,
                    "held_seconds": event.held_seconds,
                }
            }
        )

        try:
            if not self.menu_visible:
                self.log.info(
                    "Ignoring navigation while "
                    "application is running"
                )
                return

            selected = self.menu.next()

            self.log.info(
                "Selected menu item: %s",
                selected.get("id"),
            )

            selected_id = selected.get("id")
            self.state_publisher.set_launcher_selection(
                str(selected_id) if selected_id else None
            )
            if hasattr(self.menu, "footer_override"):
                self.menu.footer_override = self._selection_footer_text(selected)
            self.menu.render()

        except Exception:
            self.log.exception(
                "Navigation handling failed"
            )

    def _stop_active_application(self) -> int | None:
        """Stop the current app without callback/menu races."""

        with self._state_lock:
            self._manual_stop_in_progress = True

        try:
            return self.application_manager.stop()
        finally:
            with self._state_lock:
                self._manual_stop_in_progress = False

    def _handle_background_service(
        self,
        service: dict[str, Any],
    ) -> None:
        """Show the status of an always-running systemd service."""

        app_name = str(
            service.get("name")
            or service.get("id")
            or "Service"
        )
        unit = service.get("systemd_service")
        url = service.get("url")

        if not unit:
            self.show_menu(
                f"{app_name}: SERVICE NOT SET"
            )
            return

        result = subprocess.run(
            [
                "systemctl",
                "is-active",
                str(unit),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )

        status = (
            result.stdout.strip()
            or result.stderr.strip()
            or "unknown"
        )
        qr_target = self._resolve_qr_target(str(service.get('id') or app_name.lower()))
        if result.returncode == 0 and status == "active":
            if qr_target:
                public_url = self._publicize_service_url(str(url)) if url else qr_target
                self.log.info(
                    "Rendering web QR for %s at %s (public %s)",
                    app_name,
                    qr_target,
                    public_url,
                )
                self.menu.render_web_qr(app_name, qr_target, caption=qr_target)
                return
            else:
                message = f"{app_name}: RUNNING"
        else:
            if qr_target:
                self.log.info(
                    "Rendering cached QR for %s at %s while service status is %s",
                    app_name,
                    qr_target,
                    status,
                )
                self.menu.render_web_qr(app_name, qr_target, caption=qr_target)
                return
            message = f"{app_name}: {status.upper()}"

        self.log.info(
            "Background service %s status: %s",
            unit,
            status,
        )
        self.show_menu(message)

    def handle_select(
        self,
        event: ButtonEvent,
    ) -> None:
        """KEY_ENTER tap launches; hold stops and returns home."""

        self.log.info(
            "Select button: %s held %.2fs",
            event.name,
            event.held_seconds,
        )

        self.state_publisher.set_buttons(
            last_event=True,
            publish=False,
        )
        self._publish_runtime_state(
            {
                "input": {
                    "provider": "lradc",
                    "action": "select",
                    "name": event.name,
                    "held_seconds": event.held_seconds,
                }
            }
        )

        try:
            is_long_press = (
                event.held_seconds
                >= ButtonService.LONG_PRESS_SECONDS
            )

            if (
                self._app_is_running()
                and not is_long_press
            ):
                self.log.info(
                    "Forwarding Select button to active application"
                )

                button_server.publish(
                    "select",
                    "short_press",
                    duration=event.held_seconds,
                )
                return

            # Long Enter press is always Home/Stop.
            if is_long_press:
                if (
                    not self.menu_visible
                    or self._app_is_running()
                ):
                    button_server.publish(
                        "select",
                        "long_press",
                        duration=event.held_seconds,
                    )

                    self.log.info(
                        "Stopping active application"
                    )

                    result = self._stop_active_application()

                    self.log.info(
                        "Stop return code: %s",
                        result,
                    )

                    # Allow the application to release SPI/GPIO.
                    time.sleep(0.75)
                    self.show_menu(
                        "Application stopped"
                    )
                else:
                    self.show_menu(
                        "No application running"
                    )

                return

            # Ignore short Enter presses while an app is open.
            if not self.menu_visible:
                self.log.info(
                    "Ignoring short Enter press while "
                    "application is running"
                )
                return

            selected = self.menu.selected
            app_id = str(selected["id"])
            app_name = str(selected["name"])

            self.log.info(
                "Launching selected menu item: %s",
                app_id,
            )

            if not selected.get("configured", False):
                self.show_menu(
                    f"{app_name}: NOT INSTALLED"
                )
                return

            service = self._service_configuration(
                app_id
            )

            if self._service_supports_qr_preview(service):
                self._handle_background_service(service)
                return

            if (
                service.get("type")
                == "background_service"
            ):
                self._handle_background_service(
                    service
                )
                return

            self.menu.render(
                f"Starting {app_name}..."
            )
            self.hide_menu()

            process = self.application_manager.launch(
                service
            )

            with self._state_lock:
                self.active_app_id = app_id

            self.log.info(
                "Started %s with PID %s",
                app_id,
                process.pid,
            )

            self.state_publisher.set_foreground_application(
                app_id,
                transition="application_started",
                publish=False,
            )
            self._publish_runtime_state(
                {
                    "runtime": {
                        "status": "running",
                        "mode": "application",
                    },
                    "application": {
                        "active_id": app_id,
                        "active_pid": process.pid,
                        "status": "running",
                        "name": app_name,
                    },
                    "display": {
                        "connected": True,
                        "mode": "application_owned",
                    },
                }
            )

        except ApplicationAlreadyRunning:
            self.log.warning(
                "Launch ignored because another "
                "application is running"
            )

            if self.menu_visible:
                self.show_menu(
                    "Application already running"
                )

        except ApplicationLaunchError as error:
            self.log.error(
                "Application launch failed: %s",
                error,
            )

            time.sleep(0.5)
            self.show_menu(
                f"Failed: {error}"
            )

        except Exception:
            self.log.exception(
                "Select handling failed"
            )

            time.sleep(0.5)
            self.show_menu(
                "Application launch failed"
            )

    def _handle_application_exit(
        self,
        event: dict[str, Any],
    ) -> None:
        """Handle an application ending outside button dispatch.

        This callback runs from ApplicationManager's process monitor
        thread, so all shared state is protected by _state_lock.
        """

        service = event.get("service") or {}
        app_id = (
            service.get("id")
            or self.active_app_id
            or "Application"
        )

        return_code = event.get("return_code")
        stopped_by_manager = bool(
            event.get("stopped_by_manager")
        )

        self.log.info(
            "%s exited with code %s "
            "(stopped_by_manager=%s)",
            app_id,
            return_code,
            stopped_by_manager,
        )

        with self._state_lock:
            if not self.running:
                return

            # The long-press handler will restore the menu itself.
            if self._manual_stop_in_progress:
                self.log.debug(
                    "Menu restoration deferred to "
                    "manual stop handler"
                )
                return

            menu_was_hidden = not self.menu_visible

        if not menu_was_hidden:
            return

        # Allow the exiting process to release GPIO/SPI.
        time.sleep(0.5)

        if stopped_by_manager:
            message = f"{app_id} stopped"
            transition = "application_stopped"
            application_status = "stopped"
        elif return_code == 0:
            message = f"{app_id} exited"
            transition = "application_exited"
            application_status = "exited"
        else:
            message = f"{app_id} crashed: {return_code}"
            transition = "application_crashed"
            application_status = "crashed"

        self.state_publisher.set_foreground_application(
            None,
            transition=transition,
            publish=False,
        )
        self._publish_runtime_state(
            {
                "application": {
                    "active_id": None,
                    "active_pid": None,
                    "status": application_status,
                    "last_id": str(app_id),
                    "last_return_code": return_code,
                },
                "display": {
                    "mode": "returning_to_launcher",
                },
            }
        )

        self.show_menu(message)

    def shutdown(self, *_args: object) -> None:
        if not self.running:
            return

        self.log.info(
            "Manager daemon shutting down"
        )

        self._publish_runtime_state(
            {
                "runtime": {
                    "status": "stopping",
                },
                "last_transition": {
                    "type": "runtime_stopping",
                    "application": self.active_app_id,
                },
            }
        )

        self.running = False

    def _start_mode_reconcile_thread(self) -> None:
        """Reconcile mode state every 2 seconds."""
        import threading
        import time
        def loop():
            while self.running:
                try:
                    time.sleep(2)
                    if not self.running:
                        break
                    payload = self._resolve_mode_payload()
                    sig = json.dumps(payload.get("mode", {}), sort_keys=True)
                    if sig != self._last_mode_signature:
                        self._last_mode_signature = sig
                        self.log.info("Mode changed: reconciling")
                        mode_id = str(payload.get("mode", {}).get("live", {}).get("mode_id", "safe"))
                        self._reconcile_mode_actions(mode_id)
                        self._publish_runtime_state()
                except Exception:
                    self.log.exception("Mode reconcile thread error")
        thread = threading.Thread(target=loop, daemon=True, name="mode-reconcile")
        thread.start()

    def run(self) -> None:
        self.install_signal_handlers()

        self.log.info(
            "Starting Zero2W Manager daemon"
        )

        button_server.start()

        self._publish_runtime_state(
            {
                "runtime": {
                    "status": "running",
                    "mode": "starting",
                },
                "buttons": {
                    "service_active": True,
                    "socket_path": "/run/rocky/buttons.sock",
                },
                "input": {
                    "providers": [
                        "lradc",
                    ],
                    "last_event": None,
                },
                "last_transition": {
                    "type": "runtime_started",
                    "application": None,
                },
            }
        )

        self.show_menu()

        buttons = ButtonService(
            on_navigate=self.handle_navigation,
            on_select=self.handle_select,
            should_continue=self._button_loop_should_continue,
        )

        while self.running:
            try:
                buttons.run()

            except RuntimeError as error:
                if not self.running:
                    break

                self.log.error("%s", error)
                time.sleep(5)

            except OSError as error:
                if not self.running:
                    break

                self.log.error(
                    "Button device error: %s",
                    error,
                )
                time.sleep(2)

            except Exception:
                if not self.running:
                    break

                self.log.exception(
                    "Unexpected button service failure"
                )
                time.sleep(2)

        # Stop an active foreground app before releasing runtime
        # resources during daemon shutdown.
        if self.application_manager.is_running:
            self.log.info(
                "Stopping application during daemon shutdown"
            )

            try:
                self._stop_active_application()
            except Exception:
                self.log.exception(
                    "Failed to stop application "
                    "during shutdown"
                )

        button_server.stop()
        self.context.close()

        self._publish_runtime_state(
            {
                "runtime": {
                    "status": "stopped",
                    "mode": "offline",
                },
                "foreground_application": None,
                "launcher": {
                    "active": False,
                },
                "application": {
                    "active_id": None,
                    "active_pid": None,
                    "status": "stopped",
                },
                "display": {
                    "mode": "closed",
                },
                "buttons": {
                    "service_active": False,
                    "connected_clients": 0,
                },
                "last_transition": {
                    "type": "runtime_stopped",
                    "application": None,
                },
            }
        )

        self.log.info(
            "Manager daemon stopped"
        )


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s %(levelname)s "
            "%(name)s: %(message)s"
        ),
    )


def main() -> int:
    configure_logging()
    ManagerDaemon().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
