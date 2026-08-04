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
from zoneinfo import ZoneInfo

from manager.button_service import ButtonEvent, ButtonService
from manager.runtime import (
    ApplicationAlreadyRunning,
    ApplicationLaunchError,
    button_server,
)
from manager.runtime.context import RuntimeContext
from manager.system_probe import network_header_token, network_links_snapshot


LOGGER = logging.getLogger("zero2w-manager")
RESIDENT_DISPLAY_SETTLE_SECONDS = 1.35
PROXY_TOKEN_SECRET_PATH = Path('/opt/zero2w-manager/runtime/config/proxy-token-secret')
PUBLIC_BASE_URL = os.environ.get('ROCKY_PUBLIC_BASE_URL', '').strip()
ROCKY_WEB_PORT = int(os.environ.get('ROCKY_WEB_PORT', '8090'))
PROXY_TOKEN_TTL_SECONDS = int(os.environ.get('ROCKY_WEB_PROXY_TOKEN_TTL_SECONDS', '900'))
MODE_CATALOG_PATH = Path('/opt/zero2w-manager/runtime/config/modes.json')
CURRENT_MODE_REQUEST_PATH = Path('/opt/zero2w-manager/runtime/config/current-mode.json')
WEB_SERVICE_CACHE_PATH = Path('/opt/zero2w-manager/runtime/config/web-service-cache.json')
ISP_PREP_REQUEST_PATH = Path('/run/rocky/isp-prep-request.json')
HARDWARE_PREP_STATE_PATH = Path('/run/rocky/hardware-prep-state.json')
MANAGED_PROCESS_STATE_PATH = Path('/opt/zero2w-manager/runtime/state.json')
DISPLAY_TIMEZONE = ZoneInfo("America/Chicago")
CURRENT_MODE_MENU_ITEM_ID = "__current_mode__"
DEFAULT_MODE_CATALOG = {
    "version": 1,
    "modes": [
        {
            "mode_id": "safe",
            "label": "Safe Mode",
            "description": "Minimal known-good Rocky state.",
            "category": "baseline",
            "network": {
                "profile": "lan_only",
                "vpn_required": False,
                "vpn_provider": "none",
                "mobile_exit_node": False,
                "allow_lan_admin": True,
                "allow_wireguard_admin": True,
                "allow_public_admin": False,
                "firewall_policy": "strict",
                "dns_mode": "router_default",
                "mac_randomization": False,
                "public_network_posture": "cautious",
            },
            "dns": {
                "provider": "system",
                "serve_lan": False,
                "serve_wireguard_clients": False,
                "upstream_mode": "vpn_preferred",
                "ad_blocking": False,
                "safe_search": False,
                "blocklists_profile": "none",
            },
            "admin": {
                "rocky_admin": {
                    "enabled": True,
                    "lan": True,
                    "wireguard": True,
                    "public": False,
                },
                "qb_webui": {
                    "enabled": False,
                    "lan": False,
                    "wireguard": False,
                },
                "terminal": {"enabled": True},
                "auth_profile": "standard",
            },
            "transfer": {
                "enabled": False,
                "client": "qbittorrent",
                "vpn_enforced": False,
                "kill_switch": True,
                "webui_exposure": "none",
                "privacy_profile": "strict_off",
                "bittorrent": {
                    "anonymous_mode": False,
                    "force_encryption": False,
                    "dht": False,
                    "pex": False,
                    "lsd": False,
                    "upnp": False,
                    "fixed_port": 6881,
                    "random_port": False,
                    "port_forwarding": False,
                },
            },
            "portable": {
                "enabled": False,
                "hotspot_enabled": False,
                "hotspot_ssid": None,
                "captive_portal": False,
                "passive_collection": False,
                "active_collection": False,
                "storage_capture": False,
            },
            "power": {
                "profile": "normal",
                "suspend_nonessential_services": True,
                "reduced_polling": True,
                "display_refresh_policy": "normal",
                "radios_policy": "normal",
            },
            "pikvm": {
                "policy": "auto",
                "require_minipc_presence": True,
                "auto_disable_when_disconnected": True,
            },
            "display": {
                "surface": "browser_and_epaper",
                "epaper_menu_enabled": True,
                "epaper_qr_behavior": "mode_aware",
                "test_path": "zero2w_manager_menu",
            },
            "ui": {
                "warning_level": "normal",
                "reversible_to": "safe",
                "color_hint": "blue",
                "expose_advanced_toggles": False,
            },
        },
        {
            "mode_id": "torrent_fortress",
            "label": "Torrent Fortress",
            "description": "Strongest torrent/privacy posture with Proton-enforced transfer path.",
            "category": "privacy",
            "network": {
                "profile": "wireguard_admin",
                "vpn_required": True,
                "vpn_provider": "protonvpn",
                "mobile_exit_node": False,
                "allow_lan_admin": True,
                "allow_wireguard_admin": True,
                "allow_public_admin": False,
                "firewall_policy": "strict",
                "dns_mode": "pihole_lan_and_wg",
                "mac_randomization": False,
                "public_network_posture": "cautious",
            },
            "dns": {
                "provider": "pihole",
                "serve_lan": True,
                "serve_wireguard_clients": True,
                "upstream_mode": "vpn_preferred",
                "ad_blocking": True,
                "safe_search": False,
                "blocklists_profile": "balanced",
            },
            "admin": {
                "rocky_admin": {
                    "enabled": True,
                    "lan": True,
                    "wireguard": True,
                    "public": False,
                },
                "qb_webui": {
                    "enabled": True,
                    "lan": True,
                    "wireguard": True,
                },
                "terminal": {"enabled": True},
                "auth_profile": "hardened",
            },
            "transfer": {
                "enabled": True,
                "client": "qbittorrent",
                "vpn_enforced": True,
                "kill_switch": True,
                "webui_exposure": "lan_and_wireguard",
                "privacy_profile": "fortress",
                "bittorrent": {
                    "anonymous_mode": True,
                    "force_encryption": True,
                    "dht": False,
                    "pex": False,
                    "lsd": False,
                    "upnp": False,
                    "fixed_port": 6881,
                    "random_port": False,
                    "port_forwarding": False,
                },
            },
            "portable": {
                "enabled": False,
                "hotspot_enabled": False,
                "hotspot_ssid": None,
                "captive_portal": False,
                "passive_collection": False,
                "active_collection": False,
                "storage_capture": False,
            },
            "power": {
                "profile": "normal",
                "suspend_nonessential_services": False,
                "reduced_polling": False,
                "display_refresh_policy": "normal",
                "radios_policy": "normal",
            },
            "pikvm": {
                "policy": "auto",
                "require_minipc_presence": True,
                "auto_disable_when_disconnected": True,
            },
            "display": {
                "surface": "browser_and_epaper",
                "epaper_menu_enabled": True,
                "epaper_qr_behavior": "mode_aware",
                "test_path": "zero2w_manager_menu",
            },
            "ui": {
                "warning_level": "caution",
                "reversible_to": "safe",
                "color_hint": "red",
                "expose_advanced_toggles": False,
            },
        },
        {
            "mode_id": "daily_driver",
            "label": "Daily Driver",
            "description": "Normal home use with Pi-hole and admin access, transfer disabled.",
            "category": "baseline",
            "network": {
                "profile": "lan_only",
                "vpn_required": False,
                "vpn_provider": "none",
                "mobile_exit_node": False,
                "allow_lan_admin": True,
                "allow_wireguard_admin": True,
                "allow_public_admin": False,
                "firewall_policy": "strict",
                "dns_mode": "pihole_lan",
                "mac_randomization": False,
                "public_network_posture": "normal",
            },
            "dns": {
                "provider": "pihole",
                "serve_lan": True,
                "serve_wireguard_clients": False,
                "upstream_mode": "direct",
                "ad_blocking": True,
                "safe_search": False,
                "blocklists_profile": "balanced",
            },
            "admin": {
                "rocky_admin": {"enabled": True, "lan": True, "wireguard": True, "public": False},
                "qb_webui": {"enabled": False, "lan": False, "wireguard": False},
                "terminal": {"enabled": True},
                "auth_profile": "standard",
            },
            "transfer": {
                "enabled": False,
                "client": "qbittorrent",
                "vpn_enforced": False,
                "kill_switch": True,
                "webui_exposure": "none",
                "privacy_profile": "strict_off",
            },
            "pikvm": {"policy": "auto"},
            "display": {
                "surface": "browser_and_epaper",
                "epaper_menu_enabled": True,
                "epaper_qr_behavior": "mode_aware",
                "test_path": "zero2w_manager_menu",
            },
            "ui": {
                "warning_level": "normal",
                "reversible_to": "safe",
                "color_hint": "teal",
                "expose_advanced_toggles": False,
            },
        },
        {
            "mode_id": "pihole_only",
            "label": "Pi-hole Only",
            "description": "DNS and ad-blocking utility mode without transfer services.",
            "category": "utility",
            "network": {
                "profile": "lan_only",
                "vpn_required": False,
                "vpn_provider": "none",
                "mobile_exit_node": False,
                "allow_lan_admin": True,
                "allow_wireguard_admin": True,
                "allow_public_admin": False,
                "firewall_policy": "strict",
                "dns_mode": "pihole_lan",
                "mac_randomization": False,
                "public_network_posture": "normal",
            },
            "dns": {
                "provider": "pihole",
                "serve_lan": True,
                "serve_wireguard_clients": False,
                "upstream_mode": "direct",
                "ad_blocking": True,
                "safe_search": False,
                "blocklists_profile": "balanced",
            },
            "admin": {
                "rocky_admin": {"enabled": True, "lan": True, "wireguard": True, "public": False},
                "qb_webui": {"enabled": False, "lan": False, "wireguard": False},
                "terminal": {"enabled": True},
                "auth_profile": "standard",
            },
            "transfer": {
                "enabled": False,
                "client": "qbittorrent",
                "vpn_enforced": False,
                "kill_switch": True,
                "webui_exposure": "none",
                "privacy_profile": "strict_off",
            },
            "pikvm": {"policy": "auto"},
            "display": {
                "surface": "browser_and_epaper",
                "epaper_menu_enabled": True,
                "epaper_qr_behavior": "mode_aware",
                "test_path": "zero2w_manager_menu",
            },
            "ui": {
                "warning_level": "normal",
                "reversible_to": "safe",
                "color_hint": "green",
                "expose_advanced_toggles": False,
            },
        },
        {
            "mode_id": "print_lab",
            "label": "Print Lab",
            "description": "3D printer and Klipper workflow mode with transfer services disabled.",
            "category": "utility",
            "network": {
                "profile": "lan_only",
                "vpn_required": False,
                "vpn_provider": "none",
                "mobile_exit_node": False,
                "allow_lan_admin": True,
                "allow_wireguard_admin": True,
                "allow_public_admin": False,
                "firewall_policy": "strict",
                "dns_mode": "router_default",
                "mac_randomization": False,
                "public_network_posture": "normal",
            },
            "dns": {
                "provider": "pihole",
                "serve_lan": True,
                "serve_wireguard_clients": False,
                "upstream_mode": "direct",
                "ad_blocking": True,
                "safe_search": False,
                "blocklists_profile": "balanced",
            },
            "admin": {
                "rocky_admin": {"enabled": True, "lan": True, "wireguard": True, "public": False},
                "qb_webui": {"enabled": False, "lan": False, "wireguard": False},
                "terminal": {"enabled": True},
                "auth_profile": "standard",
            },
            "transfer": {
                "enabled": False,
                "client": "qbittorrent",
                "vpn_enforced": False,
                "kill_switch": True,
                "webui_exposure": "none",
                "privacy_profile": "strict_off",
            },
            "pikvm": {"policy": "auto"},
            "display": {
                "surface": "browser_and_epaper",
                "epaper_menu_enabled": True,
                "epaper_qr_behavior": "mode_aware",
                "test_path": "zero2w_manager_menu",
            },
            "ui": {
                "warning_level": "normal",
                "reversible_to": "safe",
                "color_hint": "orange",
                "expose_advanced_toggles": False,
            },
        },
    ],
}
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
        self._active_display_ready_at = 0.0
        self._suppressed_resident_exit_until: dict[str, float] = {}
        self._menu_transition_in_progress = False

        # Used to prevent the application exit callback from drawing
        # the menu while a deliberate long-press stop is still running.
        self._manual_stop_in_progress = False
        self._config_poll_interval_seconds = 1.0
        self._last_published_config_signature: str | None = None
        self._last_config_reconcile_at = 0.0
        self._last_mode_signature: str | None = None
        self._last_menu_chrome_refresh_at = 0.0

        self._clear_isp_prep_request()
        self._clear_hardware_prep_state()
        self._publish_reconciled_infrastructure_state(force_refresh=True)

    def _mark_active_display_settling(
        self,
        settle_seconds: float = RESIDENT_DISPLAY_SETTLE_SECONDS,
    ) -> None:
        self._active_display_ready_at = (
            time.monotonic() + max(0.0, float(settle_seconds))
        )

    def _clear_active_display_settling(self) -> None:
        self._active_display_ready_at = 0.0

    def _suppress_resident_exit_callback(
        self,
        app_id: str | None,
        *,
        seconds: float = 4.0,
    ) -> None:
        if not app_id:
            return
        self._suppressed_resident_exit_until[str(app_id)] = (
            time.monotonic() + max(0.5, float(seconds))
        )

    def _resident_exit_callback_suppressed(
        self,
        app_id: str | None,
    ) -> bool:
        if not app_id:
            return False

        key = str(app_id)
        deadline = self._suppressed_resident_exit_until.get(key)
        if deadline is None:
            return False

        now = time.monotonic()
        if now < deadline:
            return True

        self._suppressed_resident_exit_until.pop(key, None)
        return False

    def _active_display_is_settling(self) -> bool:
        return (
            not self.menu_visible
            and bool(self.active_app_id)
            and time.monotonic() < self._active_display_ready_at
        )

    def _active_display_accepts_controls(self) -> bool:
        return self._active_resident_display_service() is not None

    def _active_resident_display_service(
        self,
        *,
        allow_settling: bool = False,
    ) -> dict[str, Any] | None:
        if self.menu_visible or not self.active_app_id:
            return None
        if not allow_settling and self._active_display_is_settling():
            return None

        try:
            service = self._service_configuration(self.active_app_id)
        except Exception:
            return None

        if not self._is_resident_display_app(service):
            return None

        return service

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

        pihole_health = self._docker_container_health(PIHOLE)
        if pihole_health == 'stopped':
            self.log.info('%s: starting %s', effective_mode_id, PIHOLE)
            self._docker_ensure(PIHOLE, running=True)

        if effective_mode_id == 'safe':
            # Safe mode keeps the VPN tunnel up while leaving transfer disabled.
            qbt_health = self._docker_container_health(QBT)
            if qbt_health != 'stopped':
                self.log.info('safe mode: stopping %s', QBT)
                self._docker_ensure(QBT, running=False)
            gluetun_health = self._docker_container_health(GLUETUN)
            if gluetun_health == 'stopped':
                self.log.info('safe mode: starting %s', GLUETUN)
                self._docker_ensure(GLUETUN, running=True)

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
            qbt_health = self._docker_container_health(QBT)
            if qbt_health != 'stopped':
                self.log.info('%s: stopping %s', effective_mode_id, QBT)
                self._docker_ensure(QBT, running=False)
            gluetun_health = self._docker_container_health(GLUETUN)
            if gluetun_health != 'stopped':
                self.log.info('%s: stopping %s', effective_mode_id, GLUETUN)
                self._docker_ensure(GLUETUN, running=False)

        elif effective_mode_id == 'print_lab':
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

        if self.menu_visible and (
            now - self._last_menu_chrome_refresh_at >= 5.0
        ):
            self._last_menu_chrome_refresh_at = now
            try:
                if hasattr(self.menu, "footer_override"):
                    self.menu.footer_override = self._mode_footer_text()
                self.menu.render()
            except Exception:
                self.log.exception("Failed to refresh launcher header")

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

    def _mode_abbreviation(
        self,
        mode_payload: dict[str, Any] | None = None,
    ) -> str:
        payload = mode_payload or self._resolve_mode_payload()
        mode = payload.get("mode", {}) if isinstance(payload, dict) else {}
        live = mode.get("live", {}) if isinstance(mode.get("live"), dict) else {}
        label = str(
            live.get("label")
            or mode.get("desired", {}).get("mode_id", "safe")
        ).strip()
        tokens = [segment for segment in label.replace("-", " ").split() if segment]
        if len(tokens) >= 2:
            return "".join(token[0].upper() for token in tokens[:3])[:3]
        compact = "".join(ch for ch in label.upper() if ch.isalnum())
        return compact[:4] or "MODE"

    def _battery_header_text(self) -> str:
        try:
            power_root = Path("/sys/class/power_supply")
            if not power_root.is_dir():
                return "USB"
            for device in sorted(power_root.iterdir()):
                capacity_path = device / "capacity"
                if not capacity_path.is_file():
                    continue
                capacity = capacity_path.read_text(encoding="utf-8").strip()
                if capacity:
                    return f"B{capacity[:3]}"
        except Exception:
            self.log.debug("Battery header lookup failed", exc_info=True)
        return "USB"

    def _menu_header_payload(self) -> dict[str, str]:
        mode_payload = self._resolve_mode_payload()
        stamp = time.time()
        local_dt = time.strftime("%H:%M", time.localtime(stamp))
        chicago_dt = time.strftime("%H:%M", time.localtime(stamp))
        try:
            from datetime import datetime
            chicago_now = datetime.now(DISPLAY_TIMEZONE)
            local_dt = chicago_now.strftime("%H:%M")
            chicago_dt = chicago_now.strftime("%m-%d")
        except Exception:
            chicago_dt = time.strftime("%m-%d", time.localtime(stamp))
        show_date = int(time.time() // 6) % 2 == 1
        clock_text = chicago_dt if show_date else local_dt
        network_text = network_header_token(network_links_snapshot())
        return {
            "title": f"ROCKY {self._mode_abbreviation(mode_payload)}"[:18],
            "meta": f"{self._battery_header_text()} {network_text} {clock_text}"[:16],
        }

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

    def _run_systemctl(
        self,
        action: str,
        units: list[str],
        *,
        timeout: int = 30,
    ) -> bool:
        filtered = [str(unit).strip() for unit in units if str(unit).strip()]
        if not filtered:
            return True

        try:
            result = subprocess.run(
                ["systemctl", action, *filtered],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
            if result.returncode == 0:
                return True

            self.log.warning(
                "systemctl %s failed for %s: %s",
                action,
                ", ".join(filtered),
                result.stderr.strip() or result.stdout.strip() or result.returncode,
            )
            return False
        except Exception:
            self.log.exception(
                "systemctl %s failed for %s",
                action,
                ", ".join(filtered),
            )
            return False

    def _await_systemd_service_state(
        self,
        unit: str,
        expected: str | list[str] | tuple[str, ...] | set[str],
        *,
        timeout_seconds: float = 8.0,
    ) -> bool:
        unit = str(unit).strip()
        if not unit:
            return False

        deadline = time.monotonic() + max(0.5, float(timeout_seconds))
        if isinstance(expected, str):
            expected_states = {expected.strip().lower()}
        else:
            expected_states = {
                str(value).strip().lower()
                for value in expected
                if str(value).strip()
            }
        if not expected_states:
            return False

        while time.monotonic() < deadline:
            status = self._mode_service_status(unit).strip().lower()
            if status in expected_states:
                return True
            if "inactive" in expected_states and status in {"inactive", "failed", "unknown"}:
                return True
            time.sleep(0.15)

        status = self._mode_service_status(unit).strip().lower()
        if status in expected_states:
            return True
        if "inactive" in expected_states and status in {"inactive", "failed", "unknown"}:
            return True
        self.log.warning(
            "Timed out waiting for %s to become %s (last=%s)",
            unit,
            ",".join(sorted(expected_states)),
            status,
        )
        return False

    def _load_web_service_cache(self) -> dict[str, Any]:
        return self._read_json_file(WEB_SERVICE_CACHE_PATH, {})

    def _save_web_service_cache(self, payload: dict[str, Any]) -> None:
        try:
            WEB_SERVICE_CACHE_PATH.parent.mkdir(mode=0o775, parents=True, exist_ok=True)
            WEB_SERVICE_CACHE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        except Exception:
            self.log.exception('Failed to persist web service cache')

    def _build_web_services_state(
        self,
        effective_mode: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        live: dict[str, Any] = {}
        cache = self._load_web_service_cache()
        cache_changed = False
        transfer_state = self.transfer_manager.state_snapshot()
        mode_id = str((effective_mode or {}).get('mode_id') or '').strip().lower()
        network_policy = (
            effective_mode.get('network', {})
            if isinstance((effective_mode or {}).get('network'), dict)
            else {}
        )
        allow_public_admin = bool(network_policy.get('allow_public_admin', False))
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
            if (
                mode_id == 'torrent_fortress'
                and not allow_public_admin
                and str(app_id) in {'pihole', 'pikvm'}
            ):
                tokenized_proxy_url = ''
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

        downloader = (
            transfer_state.get("downloader", {})
            if isinstance(transfer_state, dict)
            else {}
        )
        web_ui_url = downloader.get("web_ui_url") if isinstance(downloader, dict) else None
        if isinstance(web_ui_url, str) and web_ui_url:
            public_url = self._publicize_service_url(web_ui_url)
            tokenized_proxy_url = self._tokenized_proxy_url("transfer-stack")
            live["transfer-stack"] = {
                "available": True,
                "active": bool(transfer_state.get("healthy")),
                "url": public_url,
                "tokenized_proxy_url": tokenized_proxy_url,
            }
            cached = cache.get("transfer-stack", {}) if isinstance(cache.get("transfer-stack"), dict) else {}
            new_cached = {
                "last_url": public_url,
                "last_tokenized_proxy_url": tokenized_proxy_url,
                "last_seen_at": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            }
            if cached != new_cached:
                cache["transfer-stack"] = new_cached
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

        service_state = self._build_web_services_state(effective_mode)
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
        if self._is_systemd_display_service(service):
            return False
        service_type = str(service.get('type') or '').strip()
        if service_type != 'background_service':
            return False
        app_id = service.get('id')
        if app_id and self._resolve_qr_target(str(app_id)):
            return True
        if not service.get('url'):
            return False
        return bool(service.get('systemd_service') or service_type == 'background_service')

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

    def _request_mode_change(
        self,
        selected_mode_id: str,
        *,
        reason: str,
        requested_by: str = "rocky_menu",
    ) -> bool:
        target_mode = str(selected_mode_id).strip()
        if not target_mode:
            return False

        existing = self._load_current_mode_request()
        current_mode = str(existing.get("selected_mode_id") or "safe")
        if current_mode == target_mode:
            return False

        updated = {
            "version": int(existing.get("version", 1)),
            "selected_mode_id": target_mode,
            "previous_mode_id": current_mode,
            "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "requested_by": requested_by,
            "reason": reason,
            "override_flags": existing.get("override_flags", {}) if isinstance(existing.get("override_flags"), dict) else {},
        }

        CURRENT_MODE_REQUEST_PATH.parent.mkdir(
            mode=0o775,
            parents=True,
            exist_ok=True,
        )
        CURRENT_MODE_REQUEST_PATH.write_text(
            json.dumps(updated, indent=2) + "\n",
            encoding="utf-8",
        )
        self._last_mode_signature = None
        return True

    def _apply_service_activation_mode(
        self,
        service: dict[str, Any],
    ) -> None:
        target_mode = str(service.get("activate_mode") or "").strip()
        if not target_mode:
            return

        if self._request_mode_change(
            target_mode,
            reason=f"launcher_open:{str(service.get('id') or target_mode)}",
        ):
            self.log.info(
                "Requested mode change to %s for %s launch",
                target_mode,
                str(service.get("id") or "service"),
            )
            self._reconcile_mode_actions(target_mode)
            self._publish_runtime_state()

    def _selection_footer_text(self, selected: dict[str, Any] | None = None) -> str:
        selected = selected if isinstance(selected, dict) else self.menu.selected
        if not isinstance(selected, dict):
            return self._mode_footer_text()
        selected_id = selected.get('id')
        if not selected_id:
            return self._mode_footer_text()
        if str(selected_id) == CURRENT_MODE_MENU_ITEM_ID:
            return f"{self._mode_footer_text()} | HOLD OPEN"[:76]
        try:
            service = self._service_configuration(str(selected_id))
        except Exception:
            return self._mode_footer_text()

        description = str(service.get('description') or selected.get('description') or '').strip()
        status_hint = self._service_status_hint(service)
        action_hint = self._selection_action_hint(service)

        segments = []
        if description:
            segments.append(description[:38])
        if status_hint or action_hint:
            segments.append(f"{status_hint} {action_hint}".strip())

        footer = " | ".join(segment for segment in segments if segment)
        if not footer:
            footer = self._mode_footer_text()
        return footer[:76]

    def _load_isp_prep_request(self) -> dict[str, Any] | None:
        try:
            if not ISP_PREP_REQUEST_PATH.is_file():
                return None
            payload = json.loads(
                ISP_PREP_REQUEST_PATH.read_text(encoding='utf-8')
            )
            if isinstance(payload, dict):
                return payload
        except Exception:
            self.log.exception(
                "Failed to load ISP prep request"
            )
        return None

    def _clear_isp_prep_request(self) -> None:
        try:
            ISP_PREP_REQUEST_PATH.unlink()
        except FileNotFoundError:
            return
        except Exception:
            self.log.exception(
                "Failed to clear ISP prep request"
            )

    def _clear_hardware_prep_state(self) -> None:
        try:
            HARDWARE_PREP_STATE_PATH.unlink()
        except FileNotFoundError:
            return
        except Exception:
            self.log.exception(
                "Failed to clear hardware prep state"
            )

    def _consume_isp_prep_request(self) -> dict[str, Any] | None:
        request = self._load_isp_prep_request()
        if not isinstance(request, dict):
            return None

        action = str(request.get("action") or "").strip()
        app_id = str(request.get("app_id") or "").strip()

        if action != "enter_isp_prep" or app_id != str(self.active_app_id or ""):
            return None

        self._clear_isp_prep_request()
        return request

    def _selection_action_hint(self, service: dict[str, Any]) -> str:
        if self._service_supports_qr_preview(service):
            return "HOLD=QR"
        if self._is_resident_display_app(service):
            if self._service_is_running(service):
                return "HOLD=OPEN XL=STOP"
            return "HOLD=OPEN"
        if self._is_systemd_display_service(service):
            if service.get("display_switch_target"):
                return "HOLD=OPEN TAP=SWAP"
            return "HOLD=OPEN"
        if service.get("type") == "background_service":
            return "HOLD=STATUS"
        return "UP/DN BROWSE HOLD OPEN"

    def _service_status_hint(self, service: dict[str, Any]) -> str:
        service_type = str(service.get("type") or "application")
        app_id = str(service.get("id") or "")

        if self._is_resident_display_app(service):
            if self._service_is_foreground(service):
                return "LIVE"
            if self._service_is_running(service):
                return "READY"

        if self._is_systemd_display_service(service):
            if self._service_is_foreground(service):
                return "LIVE"

            unit = str(service.get("systemd_service") or "")
            status = self._mode_service_status(unit) if unit else "unknown"
            if status == "active":
                return "READY"
            if not service.get("configured", True):
                return "MISSING"
            return status.upper()[:12]

        if service_type == "background_service":
            if self._resolve_qr_target(app_id):
                return "QR READY"

            unit = service.get("systemd_service")
            container = service.get("docker_container")

            if unit:
                status = self._mode_service_status(str(unit))
                return "RUNNING" if status == "active" else status.upper()[:12]

            if container:
                status = self._docker_container_health(str(container))
                return status.upper()[:12]

            return "BACKGROUND"

        if self._service_is_foreground(service):
            return "LIVE"

        if not service.get("configured", True):
            return "MISSING"

        if service.get("network_owner"):
            return "NET APP"

        return "APP"

    def _service_is_configured(
        self,
        service: dict[str, Any] | None,
    ) -> bool:
        if not isinstance(service, dict):
            return False
        explicit = service.get("configured")
        if explicit is not None:
            return bool(explicit)
        return bool(
            service.get("command") is not None
            or service.get("systemd_service")
            or service.get("docker_container")
            or service.get("installed_path")
            or (
                str(service.get("type") or "").strip() == "background_service"
                and service.get("url")
            )
        )

    def _menu_item_label(self, item: dict[str, Any], _is_selected: bool, _index: int) -> str:
        if str(item.get("id") or "") == CURRENT_MODE_MENU_ITEM_ID:
            payload = self._resolve_mode_payload()
            mode = payload.get("mode", {}) if isinstance(payload, dict) else {}
            live = mode.get("live", {}) if isinstance(mode.get("live"), dict) else {}
            label = str(live.get("label") or live.get("mode_id") or "Mode").strip()
            healthy = bool(live.get("healthy", True))
            compact_name = label.upper().replace(" MODE", "")[:24]
            return f"{compact_name} [{'OK' if healthy else 'WARN'}]"

        try:
            service = self._service_configuration(str(item.get("id") or ""))
        except Exception:
            service = dict(item)

        name = str(item.get("name") or service.get("name") or item.get("id") or "App")
        status_hint = self._service_status_hint(service)

        if status_hint:
            compact_name = name[:24]
            return f"{compact_name} [{status_hint[:8]}]"

        return name

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

    def _is_systemd_display_service(
        self,
        service: dict[str, Any] | None,
    ) -> bool:
        return bool(
            isinstance(service, dict)
            and service.get("display_owner")
            and service.get("systemd_service")
            and not service.get("command")
        )

    def _is_resident_display_app(
        self,
        service: dict[str, Any] | None,
    ) -> bool:
        return bool(
            isinstance(service, dict)
            and service.get("display_owner")
            and service.get("resident_display")
        )

    def _load_managed_process_state(self) -> dict[str, Any]:
        try:
            if MANAGED_PROCESS_STATE_PATH.is_file():
                data = json.loads(
                    MANAGED_PROCESS_STATE_PATH.read_text(encoding='utf-8')
                )
                if isinstance(data, dict):
                    return data
        except Exception:
            self.log.exception("Failed to read managed process state")
        return {}

    def _save_managed_process_state(
        self,
        state: dict[str, Any],
    ) -> None:
        try:
            MANAGED_PROCESS_STATE_PATH.parent.mkdir(
                mode=0o775,
                parents=True,
                exist_ok=True,
            )
            temporary = MANAGED_PROCESS_STATE_PATH.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(state, indent=2) + "\n",
                encoding='utf-8',
            )
            temporary.replace(MANAGED_PROCESS_STATE_PATH)
        except Exception:
            self.log.exception("Failed to write managed process state")

    def _managed_process_matches(
        self,
        app_id: str,
    ) -> tuple[int | None, bool]:
        state = self._load_managed_process_state()
        if str(state.get("active_mode") or "") != app_id:
            return None, False

        pid = state.get("active_pid")
        if not isinstance(pid, int) or pid <= 1:
            return None, False

        try:
            os.kill(pid, 0)
            return pid, True
        except OSError:
            return pid, False

    def _signal_service_process_group(
        self,
        service: dict[str, Any],
        sig: signal.Signals,
    ) -> bool:
        app_id = str(service.get("id") or "")
        pid, running = self._managed_process_matches(app_id)
        if not pid or not running:
            return False

        try:
            os.killpg(pid, sig)
            return True
        except Exception:
            self.log.exception(
                "Failed to send %s to %s process group %s",
                sig.name,
                app_id,
                pid,
            )
            return False

    def _managed_resident_pause_strategy(
        self,
        service: dict[str, Any],
    ) -> str:
        strategy = str(service.get("pause_strategy") or "").strip().lower()
        if strategy in {"terminate", "stop"}:
            return "terminate"
        return "signal"

    def _terminate_managed_resident_display(
        self,
        service: dict[str, Any],
        *,
        clear_state: bool = True,
    ) -> bool:
        app_id = str(service.get("id") or "")
        pid, running = self._managed_process_matches(app_id)
        if not pid or not running:
            if clear_state:
                state = self._load_managed_process_state()
                if str(state.get("active_mode") or "") == app_id:
                    state.update(
                        {
                            "active_mode": "idle",
                            "active_pid": None,
                            "status": "stopped",
                            "last_error": None,
                        }
                    )
                    self._save_managed_process_state(state)
            return True

        try:
            os.killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                _pid, still_running = self._managed_process_matches(app_id)
                if not still_running:
                    break
                time.sleep(0.2)
            _pid, still_running = self._managed_process_matches(app_id)
            if still_running:
                os.killpg(pid, signal.SIGKILL)
        except Exception:
            self.log.exception(
                "Failed to terminate resident display process for %s",
                app_id,
            )
            return False

        if clear_state:
            state = self._load_managed_process_state()
            if str(state.get("active_mode") or "") == app_id:
                state.update(
                    {
                        "active_mode": "idle",
                        "active_pid": None,
                        "status": "stopped",
                        "last_error": None,
                    }
                )
                self._save_managed_process_state(state)
        return True

    def _kill_systemd_unit_signal(
        self,
        unit: str,
        sig: str,
    ) -> bool:
        if not unit:
            return False
        try:
            result = subprocess.run(
                ["systemctl", "kill", "--signal", sig, "--kill-whom=all", unit],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if result.returncode == 0:
                return True
            self.log.warning(
                "systemctl kill --signal %s failed for %s: %s",
                sig,
                unit,
                result.stderr.strip() or result.stdout.strip() or result.returncode,
            )
            return False
        except Exception:
            self.log.exception(
                "systemctl kill --signal %s failed for %s",
                sig,
                unit,
            )
            return False

    def _service_is_running(
        self,
        service: dict[str, Any] | None,
    ) -> bool:
        if not isinstance(service, dict):
            return False

        if self._is_systemd_display_service(service):
            unit = str(service.get("systemd_service") or "").strip()
            return bool(unit) and self._mode_service_status(unit) == "active"

        app_id = str(service.get("id") or "")
        _pid, running = self._managed_process_matches(app_id)
        return running

    def _service_is_foreground(
        self,
        service: dict[str, Any] | None,
    ) -> bool:
        if not isinstance(service, dict):
            return False
        app_id = str(service.get("id") or "")
        return (
            not self.menu_visible
            and bool(app_id)
            and self.active_app_id == app_id
            and self._service_is_running(service)
        )

    def _pause_resident_display_service(
        self,
        service: dict[str, Any],
    ) -> bool:
        if self._is_systemd_display_service(service):
            freeze_units = service.get("freeze_services")
            if isinstance(freeze_units, list) and freeze_units:
                if self._run_systemctl(
                    "freeze",
                    [str(unit) for unit in freeze_units],
                    timeout=15,
                ):
                    return True
                self.log.warning(
                    "Freeze unavailable for %s, falling back to pause strategy",
                    str(service.get("id") or service.get("systemd_service") or "service"),
                )

            pause_units = service.get("pause_services")
            if isinstance(pause_units, list) and pause_units:
                pause_timeout = float(service.get("pause_timeout_seconds") or 8.0)
                if not self._run_systemctl(
                    "stop",
                    [str(unit) for unit in pause_units],
                    timeout=max(10, int(pause_timeout) + 2),
                ):
                    return False
                primary = str(service.get("systemd_service") or "").strip()
                return self._await_systemd_service_state(
                    primary,
                    "inactive",
                    timeout_seconds=pause_timeout,
                )

            unit = str(service.get("systemd_service") or "").strip()
            return self._kill_systemd_unit_signal(unit, "STOP")

        if self._managed_resident_pause_strategy(service) == "terminate":
            return self._terminate_managed_resident_display(service)

        return self._signal_service_process_group(service, signal.SIGSTOP)

    def _resume_resident_display_service(
        self,
        service: dict[str, Any],
    ) -> bool:
        if self._is_systemd_display_service(service):
            thaw_units = service.get("thaw_services")
            if isinstance(thaw_units, list) and thaw_units:
                if self._run_systemctl(
                    "thaw",
                    [str(unit) for unit in thaw_units],
                    timeout=15,
                ):
                    return True
                self.log.warning(
                    "Thaw unavailable for %s, falling back to resume strategy",
                    str(service.get("id") or service.get("systemd_service") or "service"),
                )

            resume_units = service.get("resume_services")
            if isinstance(resume_units, list) and resume_units:
                resume_timeout = float(service.get("resume_timeout_seconds") or 8.0)
                resume_states = service.get("resume_accept_states")
                if not isinstance(resume_states, list) or not resume_states:
                    resume_states = ["active"]
                if not self._run_systemctl(
                    "start",
                    [str(unit) for unit in resume_units],
                    timeout=max(10, int(resume_timeout) + 2),
                ):
                    return False
                primary = str(service.get("systemd_service") or "").strip()
                return self._await_systemd_service_state(
                    primary,
                    resume_states,
                    timeout_seconds=resume_timeout,
                )

            unit = str(service.get("systemd_service") or "").strip()
            return self._kill_systemd_unit_signal(unit, "CONT")

        return self._signal_service_process_group(service, signal.SIGCONT)

    def _foreground_systemd_app_running(self) -> bool:
        if self.menu_visible or not self.active_app_id:
            return False

        try:
            service = self._service_configuration(self.active_app_id)
        except Exception:
            return False

        if not self._is_systemd_display_service(service):
            return False

        unit = str(service.get("systemd_service") or "").strip()
        if not unit:
            return False

        return self._mode_service_status(unit) == "active"

    def _app_is_running(self) -> bool:
        if self.menu_visible:
            return False
        return (
            self.application_manager.is_running
            or self._foreground_systemd_app_running()
        )

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

    def _companion_display_target(
        self,
        app_id: str | None,
    ) -> str | None:
        if not app_id:
            return None
        try:
            service = self._service_configuration(app_id)
        except Exception:
            return None

        target = service.get("display_switch_target")
        if not isinstance(target, str) or not target.strip():
            return None
        return target.strip()

    def _reclaim_display_surface(self) -> None:
        try:
            self.menu.prepare_for_app()
        except Exception:
            self.log.exception(
                "Failed to reclaim display surface"
            )
        finally:
            try:
                self.menu.close()
            except Exception:
                self.log.exception(
                    "Failed to close menu display after reclaim"
                )

    def _should_short_press_swap_active_display(self) -> bool:
        service = self._active_resident_display_service()
        if not service:
            return False

        return bool(
            service.get("display_owner")
            and self._companion_display_target(self.active_app_id)
        )

    def _handle_resident_display_button(
        self,
        event: ButtonEvent,
    ) -> bool:
        service = self._active_resident_display_service()
        if not service:
            return False

        if event.held_seconds < ButtonService.LONG_PRESS_SECONDS:
            return self._toggle_companion_display()

        return self._park_active_display_to_menu()

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
        *,
        force_full: bool = False,
    ) -> None:
        with self._state_lock:
            self._menu_transition_in_progress = True

        should_reclaim_display = (
            not self.menu_visible
            or bool(self.active_app_id)
            or self._active_resident_display_service(allow_settling=True) is not None
        )

        if should_reclaim_display:
            self._quiesce_all_resident_displays()
            self._reclaim_display_surface()
            time.sleep(0.2)

        force_full = force_full or should_reclaim_display

        try:
            with self._state_lock:
                if not self.running:
                    return

                self._clear_hardware_prep_state()

                try:
                    if hasattr(self.menu, "item_formatter"):
                        self.menu.item_formatter = self._menu_item_label
                    if hasattr(self.menu, "header_provider"):
                        self.menu.header_provider = self._menu_header_payload
                    if hasattr(self.menu, "footer_override"):
                        self.menu.footer_override = self._selection_footer_text(self.menu.selected if self.menu.items else None)
                    self.menu.render(
                        message,
                        force_full=force_full,
                    )
                except Exception:
                    self.menu_visible = False
                    self.log.exception(
                        "Failed to display manager menu"
                    )
                    return

                self.menu_visible = True
                self.active_app_id = None
                self._clear_active_display_settling()

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
        finally:
            with self._state_lock:
                self._menu_transition_in_progress = False

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
        """KEY_1 tap moves up; hold selects; extra hold stops."""

        if self._menu_transition_in_progress:
            self.log.info("Ignoring navigation during menu transition")
            return

        if (
            not self.menu_visible
            and self._active_display_is_settling()
            and event.held_seconds < ButtonService.VERY_LONG_PRESS_SECONDS
        ):
            self.log.info(
                "Ignoring navigation while %s settles onto the display",
                self.active_app_id,
            )
            return

        if self._handle_resident_display_button(event):
            return

        if self._app_is_running() or not self.menu_visible:
            if (
                not self.menu_visible
                and event.held_seconds < ButtonService.LONG_PRESS_SECONDS
                and self._should_short_press_swap_active_display()
                and self._toggle_companion_display()
            ):
                return

            if (
                not self.menu_visible
                and event.held_seconds >= ButtonService.LONG_PRESS_SECONDS
                and self._park_active_display_to_menu()
            ):
                return

            self.log.info(
                "Forwarding Up/Select button to active application"
            )

            if event.held_seconds >= ButtonService.VERY_LONG_PRESS_SECONDS:
                if not self.menu_visible and self.active_app_id:
                    try:
                        active_service = self._service_configuration(self.active_app_id)
                    except Exception:
                        active_service = None

                    if self._is_resident_display_app(active_service):
                        if self._park_active_display_to_menu():
                            return

                prep_request = self._consume_isp_prep_request()
                button_server.publish(
                    "select",
                    "very_long_press",
                    duration=event.held_seconds,
                )
                if prep_request:
                    self._enter_isp_prep_mode(
                        prep_request,
                        held_seconds=event.held_seconds,
                    )
                    return
                if self._app_is_running():
                    self.log.info(
                        "Stopping active application via very long Up/Select hold"
                    )
                    result = self._stop_active_application()
                    self.log.info(
                        "Stop return code: %s",
                        result,
                    )
                    time.sleep(0.75)
                    self.show_menu(
                        "Application stopped"
                    )
                return

            if event.held_seconds >= ButtonService.LONG_PRESS_SECONDS:
                button_server.publish(
                    "select",
                    "long_press",
                    duration=event.held_seconds,
                )
                return

            button_server.publish(
                "up",
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
                    "action": "up_or_select",
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

            if event.held_seconds >= ButtonService.VERY_LONG_PRESS_SECONDS:
                if self.menu_visible and self._stop_selected_menu_item():
                    return
                self.show_menu(
                    "No application running"
                )
                return

            if event.held_seconds >= ButtonService.LONG_PRESS_SECONDS:
                self._activate_selected_menu_item()
                return

            selected = self.menu.previous(
                render=False,
            )

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
            if self.active_app_id:
                try:
                    service = self._service_configuration(self.active_app_id)
                except Exception:
                    service = None

                if self._is_systemd_display_service(service):
                    return 0 if self._stop_systemd_display_service(service) else 1

            return self.application_manager.stop()
        finally:
            with self._state_lock:
                self._manual_stop_in_progress = False

    def _enter_isp_prep_mode(
        self,
        request: dict[str, Any],
        *,
        held_seconds: float,
    ) -> None:
        active_app_id = str(self.active_app_id or request.get("app_id") or "application")
        requested_by = str(request.get("requested_by") or "unknown")

        self.log.info(
            "Entering ISP prep mode from %s via %.2fs hold",
            active_app_id,
            held_seconds,
        )

        result = self._stop_active_application()

        self.log.info(
            "ISP prep stop return code: %s",
            result,
        )

        time.sleep(0.75)

        with self._state_lock:
            self.menu_visible = False
            self.active_app_id = None
            self._clear_active_display_settling()

        payload = {
            "entered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "requested_by": requested_by,
            "source_application": active_app_id,
            "hold_seconds": round(float(held_seconds), 3),
            "notes": request.get("notes", {}),
        }

        try:
            HARDWARE_PREP_STATE_PATH.parent.mkdir(mode=0o775, parents=True, exist_ok=True)
            HARDWARE_PREP_STATE_PATH.write_text(
                json.dumps(payload, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            self.log.exception(
                "Failed to persist hardware prep state"
            )

        self.state_publisher.set_foreground_application(
            None,
            transition="hardware_prep_entered",
            publish=False,
        )
        self.state_publisher.set_launcher_selection(
            None,
            publish=False,
        )
        self._publish_runtime_state(
            {
                "runtime": {
                    "status": "running",
                    "mode": "hardware_prep",
                },
                "foreground_application": None,
                "launcher": {
                    "active": False,
                    "selected_application": None,
                },
                "display": {
                    "connected": False,
                    "mode": "released_for_hardware_prep",
                },
                "application": {
                    "active_id": None,
                    "active_pid": None,
                    "status": "hardware_prep",
                    "last_id": active_app_id,
                },
                "hardware_prep": {
                    "active": True,
                    "action": "isp_boot",
                    **payload,
                },
            }
        )

    def _stop_systemd_display_service(
        self,
        service: dict[str, Any],
    ) -> bool:
        stop_units = service.get("stop_services")
        if not isinstance(stop_units, list) or not stop_units:
            primary = str(service.get("systemd_service") or "").strip()
            stop_units = [primary] if primary else []

        return self._run_systemctl(
            "stop",
            [str(unit) for unit in stop_units],
        )

    def _stop_resident_display_service(
        self,
        service: dict[str, Any],
    ) -> bool:
        if self._is_systemd_display_service(service):
            return self._stop_systemd_display_service(service)

        if self._managed_resident_pause_strategy(service) == "terminate":
            return self._terminate_managed_resident_display(service)

        # Resume first so TERM/KILL are not left pending against a stopped task.
        self._resume_resident_display_service(service)
        app_id = str(service.get("id") or "")
        pid, running = self._managed_process_matches(app_id)
        if not pid or not running:
            return False

        try:
            os.killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                _pid, still_running = self._managed_process_matches(app_id)
                if not still_running:
                    break
                time.sleep(0.25)
            _pid, still_running = self._managed_process_matches(app_id)
            if still_running:
                os.killpg(pid, signal.SIGKILL)
            state = self._load_managed_process_state()
            state.update(
                {
                    "active_mode": "idle",
                    "active_pid": None,
                    "status": "stopped",
                    "last_error": None,
                }
            )
            self._save_managed_process_state(state)
            return True
        except Exception:
            self.log.exception(
                "Failed to stop resident display process for %s",
                app_id,
            )
            return False

    def _park_active_display_to_menu(self) -> bool:
        service = self._active_resident_display_service()
        if not service:
            return False

        app_id = str(service.get("id") or self.active_app_id or "")
        app_name = str(service.get("name") or self.active_app_id)
        self._suppress_resident_exit_callback(app_id)
        if not self._pause_resident_display_service(service):
            self.show_menu(f"{app_name}: PAUSE FAILED")
            return True

        self._quiesce_all_resident_displays(
            except_app_id=app_id,
        )

        time.sleep(0.2)
        self.show_menu(
            f"{app_name}: MENU",
            force_full=False,
        )
        return True

    def _quiesce_other_resident_displays(
        self,
        target_app_id: str,
    ) -> None:
        for app_id in self.menu.services:
            if app_id == target_app_id:
                continue
            try:
                service = self._service_configuration(str(app_id))
            except Exception:
                continue

            if not self._is_resident_display_app(service):
                continue
            if not self._service_is_running(service):
                continue

            self.log.info(
                "Quiescing competing resident display %s before foregrounding %s",
                app_id,
                target_app_id,
            )
            self._pause_resident_display_service(service)

    def _quiesce_all_resident_displays(
        self,
        *,
        except_app_id: str | None = None,
        suppress_exit_callbacks: bool = True,
    ) -> None:
        for app_id in self.menu.services:
            if except_app_id and str(app_id) == str(except_app_id):
                continue
            try:
                service = self._service_configuration(str(app_id))
            except Exception:
                continue

            if not self._is_resident_display_app(service):
                continue
            if not self._service_is_running(service):
                continue

            if suppress_exit_callbacks:
                self._suppress_resident_exit_callback(str(app_id))

            self.log.info(
                "Quiescing resident display %s for launcher ownership",
                app_id,
            )
            self._pause_resident_display_service(service)

    def _resume_service_to_foreground(
        self,
        service: dict[str, Any],
        *,
        show_transition: bool = True,
    ) -> bool:
        app_id = str(service.get("id") or "")
        app_name = str(service.get("name") or app_id or "App")

        if show_transition:
            self.menu.render(f"Opening {app_name}...")

        self._quiesce_other_resident_displays(app_id)

        if self.menu_visible:
            self.hide_menu()

        if self._service_is_running(service):
            resumed = self._resume_resident_display_service(service)
            if not resumed and self._is_systemd_display_service(service):
                resumed = self._run_systemctl(
                    "start",
                    [str(service.get("systemd_service") or "").strip()],
                )
            if not resumed:
                self.show_menu(f"{app_name}: RESUME FAILED")
                return False

            with self._state_lock:
                self.active_app_id = app_id
                self._mark_active_display_settling()

            owner = "systemd" if self._is_systemd_display_service(service) else "process"
            self.state_publisher.set_foreground_application(
                app_id,
                transition="application_resumed",
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
                        "active_pid": None if owner == "systemd" else self._managed_process_matches(app_id)[0],
                        "status": "running",
                        "name": app_name,
                        "owner": owner,
                    },
                    "display": {
                        "connected": True,
                        "mode": "application_owned",
                    },
                }
            )
            return True

        self._activate_service(
            service,
            show_transition=show_transition,
        )
        return True

    def _handle_background_service(
        self,
        service: dict[str, Any],
    ) -> None:
        """Show the status of an always-running background service."""

        app_name = str(
            service.get("name")
            or service.get("id")
            or "Service"
        )
        unit = service.get("systemd_service")
        container = service.get("docker_container")
        url = service.get("url")
        status = "unknown"
        is_running = False

        if unit:
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
            is_running = result.returncode == 0 and status == "active"
        elif container:
            status = self._docker_container_health(str(container))
            is_running = status in {"healthy", "running"}
        else:
            self.show_menu(
                f"{app_name}: SERVICE NOT SET"
            )
            return

        qr_target = self._resolve_qr_target(str(service.get('id') or app_name.lower()))
        if is_running:
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
            str(unit or container or app_name),
            status,
        )
        self.show_menu(message)

    def _activate_selected_menu_item(self) -> None:
        selected = self.menu.selected
        app_id = str(selected["id"])
        app_name = str(selected["name"])

        if app_id == CURRENT_MODE_MENU_ITEM_ID:
            self._activate_service(
                self._mode_selector_service()
            )
            return

        self.log.info(
            "Launching selected menu item: %s",
            app_id,
        )

        service = self._service_configuration(
            app_id
        )
        if not self._service_is_configured(service):
            self.show_menu(
                f"{app_name}: NOT INSTALLED"
            )
            return
        self._apply_service_activation_mode(service)

        if self._is_resident_display_app(service):
            self._resume_service_to_foreground(service)
            return

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

        self._activate_service(service)

    def _mode_selector_service(self) -> dict[str, Any]:
        return {
            "id": "mode_selector",
            "name": "Modes",
            "description": "Select the active Rocky operating mode",
            "type": "application",
            "command": [
                "/opt/zero2w-manager/venv/bin/python3",
                "-m",
                "apps.mode_selector",
            ],
            "environment": {
                "PYTHONPATH": "/opt/zero2w-manager",
                "PYTHONUNBUFFERED": "1",
            },
            "display_owner": True,
            "background_allowed": False,
            "network_owner": False,
            "working_directory": "/opt/zero2w-manager",
        }

    def _stop_selected_menu_item(self) -> bool:
        selected = self.menu.selected
        app_id = str(selected.get("id") or "")
        app_name = str(selected.get("name") or app_id or "App")
        if not app_id:
            return False

        try:
            service = self._service_configuration(app_id)
        except Exception:
            self.show_menu(f"{app_name}: UNKNOWN")
            return True

        if not self._is_resident_display_app(service):
            return False

        if not self._service_is_running(service):
            self.show_menu(f"{app_name}: NOT RUNNING")
            return True

        if self._stop_resident_display_service(service):
            self.show_menu(f"{app_name}: STOPPED")
        else:
            self.show_menu(f"{app_name}: STOP FAILED")
        return True

    def _activate_systemd_display_service(
        self,
        service: dict[str, Any],
        *,
        show_transition: bool = True,
    ) -> None:
        app_id = str(service.get("id") or "service")
        app_name = str(service.get("name") or app_id)
        start_units = service.get("start_services")

        if not isinstance(start_units, list) or not start_units:
            primary = str(service.get("systemd_service") or "").strip()
            start_units = [primary] if primary else []

        if show_transition:
            self.menu.render(
                f"Starting {app_name}..."
            )

        self._quiesce_other_resident_displays(app_id)

        if self.menu_visible:
            self.hide_menu()

        if not self._run_systemctl(
            "start",
            [str(unit) for unit in start_units],
        ):
            self.show_menu(
                f"{app_name}: START FAILED"
            )
            return

        with self._state_lock:
            self.active_app_id = app_id
            self._mark_active_display_settling()

        self.log.info(
            "Activated systemd display service: %s",
            app_id,
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
                    "active_pid": None,
                    "status": "running",
                    "name": app_name,
                    "owner": "systemd",
                },
                "display": {
                    "connected": True,
                    "mode": "application_owned",
                },
            }
        )

    def _toggle_companion_display(self) -> bool:
        source_id = self.active_app_id
        target_id = self._companion_display_target(source_id)
        if not source_id or not target_id:
            return False

        self.log.info(
            "Toggling companion display from %s to %s",
            source_id,
            target_id,
        )

        try:
            source_service = self._service_configuration(source_id)
        except Exception:
            source_service = None

        if source_service and self._is_resident_display_app(source_service):
            self._suppress_resident_exit_callback(source_id)
            if not self._pause_resident_display_service(source_service):
                self.show_menu(f"Switch failed: {source_id}")
                return True
            self._reclaim_display_surface()
            time.sleep(0.2)
        else:
            self._stop_active_application()
            self._reclaim_display_surface()
            time.sleep(0.75)

        try:
            target_service = self._service_configuration(target_id)
        except Exception:
            self.show_menu(
                f"Switch target missing: {target_id}"
            )
            return True

        try:
            if self._is_resident_display_app(target_service):
                self._resume_service_to_foreground(
                    target_service,
                    show_transition=False,
                )
            else:
                self._activate_service(
                    target_service,
                    show_transition=False,
                )
        except Exception:
            self.log.exception(
                "Companion display toggle failed: %s -> %s",
                source_id,
                target_id,
            )
            self.show_menu(
                f"Switch failed: {target_id}"
            )
        return True

    def _activate_service(
        self,
        service: dict[str, Any],
        *,
        show_transition: bool = True,
    ) -> None:
        if self._is_systemd_display_service(service):
            self._activate_systemd_display_service(
                service,
                show_transition=show_transition,
            )
            return

        app_id = str(service.get("id") or "")
        app_name = str(service.get("name") or app_id or "App")

        if show_transition:
            self.menu.render(
                f"Starting {app_name}..."
            )
        if self.menu_visible:
            self.hide_menu()

        process = self.application_manager.launch(
            service
        )

        with self._state_lock:
            self.active_app_id = app_id
            self._mark_active_display_settling()

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

    def handle_select(
        self,
        event: ButtonEvent,
    ) -> None:
        """KEY_ENTER tap moves down; hold selects; extra hold stops."""

        if self._menu_transition_in_progress:
            self.log.info("Ignoring select during menu transition")
            return

        if (
            not self.menu_visible
            and self._active_display_is_settling()
            and event.held_seconds < ButtonService.VERY_LONG_PRESS_SECONDS
        ):
            self.log.info(
                "Ignoring select while %s settles onto the display",
                self.active_app_id,
            )
            return

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
                    "action": "down_or_select",
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
            is_very_long_press = (
                event.held_seconds
                >= ButtonService.VERY_LONG_PRESS_SECONDS
            )

            if self._handle_resident_display_button(event):
                return

            if (
                not self.menu_visible
                and not is_long_press
                and self._should_short_press_swap_active_display()
                and self._toggle_companion_display()
            ):
                return

            if (
                not self.menu_visible
                and is_long_press
                and not is_very_long_press
                and self._park_active_display_to_menu()
            ):
                return

            if (
                self._app_is_running()
                and not is_long_press
            ):
                self.log.info(
                    "Forwarding Down button to active application"
                )

                button_server.publish(
                    "down",
                    "short_press",
                    duration=event.held_seconds,
                )
                return

            if (
                self._app_is_running()
                and is_long_press
                and not is_very_long_press
            ):
                self.log.info(
                    "Forwarding Select hold to active application"
                )

                button_server.publish(
                    "select",
                    "long_press",
                    duration=event.held_seconds,
                )
                return

            # Extra-long hold remains the recovery path.
            if is_very_long_press:
                if self.menu_visible and self._stop_selected_menu_item():
                    return
                if (
                    not self.menu_visible
                    or self._app_is_running()
                ):
                    if (
                        not self.menu_visible
                        and self.active_app_id
                    ):
                        try:
                            active_service = self._service_configuration(self.active_app_id)
                        except Exception:
                            active_service = None

                        if self._is_resident_display_app(active_service):
                            if self._park_active_display_to_menu():
                                return

                    prep_request = self._consume_isp_prep_request()
                    button_server.publish(
                        "select",
                        "very_long_press",
                        duration=event.held_seconds,
                    )

                    if prep_request:
                        self._enter_isp_prep_mode(
                            prep_request,
                            held_seconds=event.held_seconds,
                        )
                        return

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

            if not self.menu_visible:
                self.log.info(
                    "Ignoring short Down press while "
                    "application is running"
                )
                return

            if is_long_press:
                self._activate_selected_menu_item()
                return

            selected = self.menu.next(
                render=False,
            )

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

        if self._resident_exit_callback_suppressed(str(app_id)):
            self.log.info(
                "Suppressing launcher restoration for intentional resident-display transition of %s",
                app_id,
            )
            return

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
