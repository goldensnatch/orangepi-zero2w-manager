from __future__ import annotations

import hashlib
import io
import json
import logging
import socket
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFont
from zero2w_epaper import Display

try:
    from manager.runtime import ButtonEvent
except Exception:
    from manager.button_service import ButtonEvent

from manager.sdk import RockyButtonApp


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

LOGGER = logging.getLogger("torrentz")
WIDTH = 250
HEIGHT = 122
VISIBLE_ROWS = 4
STATE_PATH = Path("/run/rocky/state.json")


def detect_public_host() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def publicize_service_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    hostname = parsed.hostname or detect_public_host()
    if hostname in {"127.0.0.1", "localhost"}:
        hostname = detect_public_host()
    netloc = hostname
    if parsed.port:
        netloc = f"{hostname}:{parsed.port}"
    return urlunparse((parsed.scheme or "http", netloc, parsed.path or "/", "", parsed.query, ""))


class TorrentzApp(RockyButtonApp):
    name = "Torrentz"
    update_interval = 0.2

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.RLock()
        self.display_context = None
        self.display = None
        self.menu_items = [
            {"id": "qbt", "label": "qBittorrent", "kind": "detail"},
            {"id": "vpn", "label": "VPN Tunnel", "kind": "detail"},
            {"id": "transfer", "label": "Transfer State", "kind": "detail"},
            {"id": "storage", "label": "Storage", "kind": "detail"},
            {"id": "mode", "label": "Mode Policy", "kind": "detail"},
            {"id": "back", "label": "Back", "kind": "action"},
        ]
        self.selected_index = 0
        self.page = "menu"
        self.status_message: str | None = None
        self.status_until = 0.0
        self._dirty = True
        self._last_render_signature: str | None = None
        self._last_state_poll = 0.0
        self._state_snapshot: dict[str, Any] = {}
        self._qr_target: str | None = None
        self._qr_image: Image.Image | None = None
        self._qr_caption: str = ""

    def setup(self) -> None:
        self.display_context = Display()
        self.display = self.display_context.__enter__()
        self._poll_state(force=True)
        self._refresh_qr_cache()
        self._render_if_needed(force=True)
        LOGGER.info("Torrentz started")

    def cleanup(self) -> None:
        if self.display_context is not None:
            try:
                self.display_context.__exit__(None, None, None)
            except Exception:
                LOGGER.exception("Torrentz display cleanup failed")
        LOGGER.info("Torrentz stopped")

    def update(self) -> None:
        now = time.monotonic()
        with self._lock:
            if self.status_until and now >= self.status_until:
                self.status_until = 0.0
                self.status_message = None
                self._dirty = True

            if now - self._last_state_poll >= 2.0:
                self._poll_state(force=False)

        self._render_if_needed()

    def on_button(self, event: ButtonEvent) -> None:
        should_render = False
        with self._lock:
            if event.button == "up" and event.action == "short_press":
                should_render = self._handle_up()
            elif event.button == "down" and event.action == "short_press":
                should_render = self._handle_down()
            elif event.button == "select" and event.action == "long_press":
                should_render = self._handle_select()
            else:
                LOGGER.info("Ignored button event: %s %s", event.button, event.action)

        if should_render:
            self._render_if_needed()

    def _handle_up(self) -> bool:
        if self.page == "menu":
            self.selected_index = (self.selected_index - 1) % len(self.menu_items)
            self._clear_status()
            self._dirty = True
            return True

        self.page = "menu"
        self._clear_status()
        self._dirty = True
        return True

    def _handle_down(self) -> bool:
        if self.page == "menu":
            self.selected_index = (self.selected_index + 1) % len(self.menu_items)
            self._clear_status()
            self._dirty = True
            return True

        self.page = "menu"
        self._clear_status()
        self._dirty = True
        return True

    def _handle_select(self) -> bool:
        if self.page == "menu":
            selected_id = self.menu_items[self.selected_index]["id"]
            if selected_id == "back":
                self.request_stop("torrentz-back")
            else:
                self._poll_state(force=True)
                self.page = selected_id
            self._clear_status()
            self._dirty = True
            return True

        if self.page in {"qbt", "vpn", "transfer", "storage", "mode"}:
            self._poll_state(force=True)
            self.status_message = "STATUS REFRESHED"
            self.status_until = time.monotonic() + 1.5
            self._dirty = True
            return True

        return False

    def _clear_status(self) -> None:
        self.status_message = None
        self.status_until = 0.0

    def _poll_state(self, *, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_state_poll < 2.0:
            return
        self._last_state_poll = now

        snapshot: dict[str, Any] = {}
        try:
            if STATE_PATH.is_file():
                snapshot = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            LOGGER.exception("Failed to load runtime state")
            snapshot = {}

        state_changed = snapshot != self._state_snapshot
        if state_changed:
            self._state_snapshot = snapshot
        self._refresh_qr_cache()
        if state_changed:
            self._dirty = True

    def _mode_snapshot(self) -> dict[str, Any]:
        mode = self._state_snapshot.get("mode", {})
        return mode if isinstance(mode, dict) else {}

    def _transfer_snapshot(self) -> dict[str, Any]:
        transfer = self._state_snapshot.get("transfer", {})
        return transfer if isinstance(transfer, dict) else {}

    def _storage_snapshot(self) -> dict[str, Any]:
        storage = self._state_snapshot.get("storage", {})
        return storage if isinstance(storage, dict) else {}

    def _web_service_snapshot(self) -> dict[str, Any]:
        services = self._state_snapshot.get("web_services", {})
        return services if isinstance(services, dict) else {}

    def _qbt_proxy_url(self) -> str:
        live = self._web_service_snapshot().get("transfer-stack", {})
        if isinstance(live, dict):
            public = str(live.get("url") or live.get("public_url") or "").strip()
            if public:
                return public
            tokenized = str(live.get("tokenized_proxy_url") or "").strip()
            if tokenized:
                return tokenized
        return publicize_service_url("http://127.0.0.1:8088/")

    def _refresh_qr_cache(self) -> None:
        target = self._qbt_proxy_url()
        caption = publicize_service_url(target)
        if target == self._qr_target and self._qr_image is not None and caption == self._qr_caption:
            return
        self._qr_target = target
        self._qr_caption = caption
        self._qr_image = self._build_qr_image(target)

    @staticmethod
    def _build_qr_image(target: str) -> Image.Image | None:
        qr_url = "https://api.qrserver.com/v1/create-qr-code/?size=180x180&data=" + quote(target, safe="")
        try:
            with urlopen(qr_url, timeout=8) as response:
                qr = Image.open(io.BytesIO(response.read())).convert("1")
                return qr.resize((76, 76))
        except Exception:
            LOGGER.exception("Failed to fetch qBittorrent QR")
            return None

    def _mode_line(self) -> str:
        live = self._mode_snapshot().get("live", {})
        if isinstance(live, dict):
            return str(live.get("label") or live.get("mode_id") or "Unknown")
        return "Unknown"

    def _service_health(self) -> tuple[str, str]:
        services = self._mode_snapshot().get("services", {})
        if not isinstance(services, dict):
            return ("UNKNOWN", "UNKNOWN")
        vpn = "OK" if str(services.get("gluetun", "")).lower() == "healthy" else "OFF"
        qbt = "ON" if str(services.get("qbittorrent", "")).lower() == "running" else "OFF"
        return (vpn, qbt)

    def _storage_line(self) -> str:
        transfer = self._transfer_snapshot()
        return str(transfer.get("active_destination_id") or "internal").upper()

    def footer_text(self) -> str:
        if self.status_message:
            return self.status_message[:38]
        if self.page == "menu":
            return "UP/DN NAV | HOLD OPEN"[:38]
        return "UP/DN BACK | HOLD REFRESH"[:38]

    def render_image(self) -> Image.Image:
        image = Image.new("1", (WIDTH, HEIGHT), 255)
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()
        draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=0)
        draw.text((7, 5), "TORRENTZ", font=font, fill=0)
        draw.line((6, 18, WIDTH - 7, 18), fill=0)

        if self.page == "menu":
            self._draw_menu(draw, font)
        elif self.page == "qbt":
            self._draw_qbt_qr_card(image, draw, font)
            return image
        elif self.page == "vpn":
            mode = self._mode_snapshot()
            services = mode.get("services", {}) if isinstance(mode.get("services"), dict) else {}
            policies = mode.get("policies", {}) if isinstance(mode.get("policies"), dict) else {}
            self._draw_lines(draw, font, "VPN TUNNEL", [
                f"Tunnel: {str(services.get('gluetun', 'unknown')).upper()}",
                f"WG Admin: {str(services.get('wireguard_admin', 'unknown')).upper()}",
                f"Privacy: {str(policies.get('torrent_privacy_profile', 'unknown')).upper()}",
                f"DNS: {str(policies.get('dns_mode', 'unknown')).upper()}",
                "",
                "Hold refresh status.",
            ])
        elif self.page == "transfer":
            transfer = self._transfer_snapshot()
            self._draw_lines(draw, font, "TRANSFER", [
                f"Healthy: {str(transfer.get('healthy', False)).upper()}",
                f"Engine: {str(transfer.get('engine', 'unknown')).upper()}",
                f"VPN Req: {str(transfer.get('vpn_required', False)).upper()}",
                f"Dest OK: {str(transfer.get('destination_ready', False)).upper()}",
                f"Dest: {str(transfer.get('active_destination_id', 'internal')).upper()}",
                "Hold refresh status.",
            ])
        elif self.page == "storage":
            storage = self._storage_snapshot()
            destinations = storage.get("destinations", []) if isinstance(storage.get("destinations"), list) else []
            selected = next((item for item in destinations if isinstance(item, dict) and item.get("selected")), None)
            selected = selected or (destinations[0] if destinations and isinstance(destinations[0], dict) else {})
            self._draw_lines(draw, font, "STORAGE", [
                f"Label: {str(selected.get('label', 'Internal')).upper()}",
                f"Path: {str(selected.get('path', '/mnt/rocky-seed'))}",
                f"Present: {str(selected.get('present', False)).upper()}",
                f"Writable: {str(selected.get('writable', False)).upper()}",
                f"Free: {self._format_bytes(selected.get('available_bytes'))}",
                "Hold refresh status.",
            ])
        elif self.page == "mode":
            mode = self._mode_snapshot()
            live = mode.get("live", {}) if isinstance(mode.get("live"), dict) else {}
            policies = mode.get("policies", {}) if isinstance(mode.get("policies"), dict) else {}
            self._draw_lines(draw, font, "MODE POLICY", [
                f"Mode: {str(live.get('mode_id', 'safe')).upper()}",
                f"Healthy: {str(live.get('healthy', False)).upper()}",
                f"QB LAN: {str(policies.get('qb_webui_lan', False)).upper()}",
                f"QB WG: {str(policies.get('qb_webui_wireguard', False)).upper()}",
                f"LAN Adm: {str(policies.get('lan_admin', False)).upper()}",
                f"PIKVM: {str(self._mode_snapshot().get('services', {}).get('pikvm', 'auto')).upper()}",
            ])
        draw.line((6, 94, WIDTH - 7, 94), fill=0)
        draw.text((8, 101), self.footer_text(), font=font, fill=0)
        return image

    def _draw_menu(self, draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont) -> None:
        count_text = f"{self.selected_index + 1}/{len(self.menu_items)}"
        vpn, qbt = self._service_health()
        draw.text((WIDTH - 48, 5), count_text[:7], font=font, fill=0)
        draw.text((68, 5), f"VPN {vpn} QBT {qbt}"[:20], font=font, fill=0)
        y = 26
        window_start = max(0, min(self.selected_index - 1, max(0, len(self.menu_items) - VISIBLE_ROWS)))
        visible = self.menu_items[window_start: window_start + VISIBLE_ROWS]
        for offset, item in enumerate(visible):
            absolute_index = window_start + offset
            is_selected = absolute_index == self.selected_index
            if is_selected:
                draw.rectangle((5, y - 2, WIDTH - 6, y + 10), outline=0)
            row = f"{'>' if is_selected else ' '} {item['label']}"
            detail = self._menu_row_detail(item["id"])
            text = f"{row[:22]} {detail}".rstrip()
            draw.text((9, y), text[:37], font=font, fill=0)
            y += 16

    def _menu_row_detail(self, item_id: str) -> str:
        mode = self._mode_snapshot()
        services = mode.get("services", {}) if isinstance(mode.get("services"), dict) else {}
        transfer = self._transfer_snapshot()
        if item_id == "qbt":
            return "ON" if str(services.get("qbittorrent", "")).lower() == "running" else "OFF"
        if item_id == "vpn":
            return "OK" if str(services.get("gluetun", "")).lower() == "healthy" else "OFF"
        if item_id == "transfer":
            return "READY" if bool(transfer.get("destination_ready")) else "WAIT"
        if item_id == "storage":
            return self._storage_line()[:8]
        if item_id == "mode":
            live = mode.get("live", {}) if isinstance(mode.get("live"), dict) else {}
            return str(live.get("mode_id", "safe")).upper()[:8]
        return ""

    def _draw_qbt_qr_card(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        font: ImageFont.ImageFont,
    ) -> None:
        draw.text((7, 5), "QBITTORRENT QR", font=font, fill=0)
        draw.line((6, 18, WIDTH - 7, 18), fill=0)
        qr = self._qr_image
        if qr is not None:
            image.paste(qr, (10, 28))
            draw.rectangle((8, 26, 88, 106), outline=0)
            mode_text = self._mode_line()[:15]
            draw.text((102, 28), f"Mode: {mode_text}", font=font, fill=0)
            draw.text((102, 41), "Scan for Web UI", font=font, fill=0)
            draw.text((102, 54), "Stable browser URL", font=font, fill=0)
            draw.text((102, 67), "for this session.", font=font, fill=0)
            self._draw_wrapped_text(draw, font, self._qr_caption, 102, 82, 20, 3)
            return

        self._draw_lines(draw, font, "QBITTORRENT", [
            f"Mode: {self._mode_line()}",
            "QR fetch failed.",
            "",
            "URL fallback:",
            self._qr_caption or self._qbt_proxy_url(),
            "Hold refresh status.",
        ])

    def _draw_lines(
        self,
        draw: ImageDraw.ImageDraw,
        font: ImageFont.ImageFont,
        title: str,
        lines: list[str],
    ) -> None:
        draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=0)
        draw.text((7, 5), title[:34], font=font, fill=0)
        draw.line((6, 18, WIDTH - 7, 18), fill=0)
        y = 26
        for line in lines[:6]:
            draw.text((8, y), line[:38], font=font, fill=0)
            y += 11

    @staticmethod
    def _draw_wrapped_text(
        draw: ImageDraw.ImageDraw,
        font: ImageFont.ImageFont,
        text: str,
        x: int,
        y: int,
        width_chars: int,
        max_lines: int,
    ) -> None:
        remaining = text.strip()
        line_y = y
        for _ in range(max_lines):
            if not remaining:
                break
            line = remaining[:width_chars]
            if len(remaining) > width_chars:
                split_at = line.rfind("/")
                if split_at <= 0:
                    split_at = line.rfind(".")
                if split_at <= 0:
                    split_at = width_chars
                line = remaining[:split_at]
                remaining = remaining[split_at:].lstrip("/")
                if split_at != width_chars:
                    line = line + "/"
            else:
                remaining = ""
            draw.text((x, line_y), line[:width_chars], font=font, fill=0)
            line_y += 11

    def _render_if_needed(self, *, force: bool = False) -> None:
        with self._lock:
            if not force and not self._dirty:
                return
            image = self.render_image()
            signature = hashlib.sha1(image.tobytes()).hexdigest()
            if not force and self._last_render_signature == signature:
                self._dirty = False
                return
            self._dirty = False
        if self.display is not None:
            self.display.show(image)
            with self._lock:
                self._last_render_signature = signature

    @staticmethod
    def _format_bytes(value: Any) -> str:
        try:
            size = int(value)
        except (TypeError, ValueError):
            return "UNKNOWN"
        units = ["B", "KB", "MB", "GB", "TB"]
        amount = float(size)
        unit = units[0]
        for unit in units:
            if amount < 1024.0 or unit == units[-1]:
                break
            amount /= 1024.0
        if unit == "B":
            return f"{int(amount)}B"
        return f"{amount:.1f}{unit}"


if __name__ == "__main__":
    TorrentzApp().run()
