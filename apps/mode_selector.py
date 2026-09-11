from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

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

LOGGER = logging.getLogger("mode-selector")
WIDTH = 250
HEIGHT = 122
VISIBLE_ROWS = 3
MODE_CATALOG_PATH = Path("/opt/zero2w-manager/runtime/config/modes.json")
CURRENT_MODE_REQUEST_PATH = Path("/opt/zero2w-manager/runtime/config/current-mode.json")
RUNTIME_STATE_PATH = Path("/run/rocky/state.json")

DEFAULT_MODE_CATALOG = {
    "version": 1,
    "modes": [
        {"mode_id": "safe", "label": "Safe Mode", "description": "Minimal known-good Rocky state."},
        {"mode_id": "torrent_fortress", "label": "Torrent Fortress", "description": "qBittorrent over Proton VPN. Keeps entertainment apps running."},
        {"mode_id": "entertainment", "label": "Entertainment", "description": "Virtual media center with VPN-backed Torrentz and library apps."},
        {"mode_id": "daily_driver", "label": "Daily Driver", "description": "Normal home use with Pi-hole."},
        {"mode_id": "pihole_only", "label": "Pi-hole Only", "description": "DNS and ad-blocking utility mode."},
        {"mode_id": "print_lab", "label": "Print Lab", "description": "3D printer and Klipper workflow mode."},
    ],
}

DEFAULT_CURRENT_MODE_REQUEST = {
    "version": 1,
    "selected_mode_id": "safe",
    "previous_mode_id": "safe",
    "override_flags": {},
}


def _read_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        LOGGER.exception("Failed reading %s", path)
    return json.loads(json.dumps(fallback))


class ModeSelectorApp(RockyButtonApp):
    name = "Modes"
    update_interval = 0.25

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.RLock()
        self.display_context = None
        self.display = None
        self.menu_items: list[dict[str, Any]] = []
        self.selected_index = 0
        self._dirty = True
        self._last_render_signature: str | None = None
        self._status_message: str | None = None
        self._status_until = 0.0
        self._current_mode_id = "safe"
        self._mode_map: dict[str, dict[str, Any]] = {}
        self._runtime_state: dict[str, Any] = {}

    def setup(self) -> None:
        self.display_context = Display()
        self.display = self.display_context.__enter__()
        self._reload_modes()
        self._render_if_needed(force=True)
        LOGGER.info("Mode selector started")

    def cleanup(self) -> None:
        if self.display_context is not None:
            try:
                self.display_context.__exit__(None, None, None)
            except Exception:
                LOGGER.exception("Mode selector display cleanup failed")
        LOGGER.info("Mode selector stopped")

    def update(self) -> None:
        now = time.monotonic()
        with self._lock:
            if self._status_until and now >= self._status_until:
                self._status_until = 0.0
                self._status_message = None
                self._dirty = True
            self._reload_runtime_state()
        self._render_if_needed()

    def on_button(self, event: ButtonEvent) -> None:
        should_render = False
        with self._lock:
            if event.button == "up" and event.action == "short_press":
                self.selected_index = (self.selected_index - 1) % len(self.menu_items)
                self._dirty = True
                should_render = True
            elif event.button == "down" and event.action == "short_press":
                self.selected_index = (self.selected_index + 1) % len(self.menu_items)
                self._dirty = True
                should_render = True
            elif event.button == "select" and event.action == "long_press":
                should_render = self._handle_select()
        if should_render:
            self._render_if_needed()

    def _reload_modes(self) -> None:
        catalog = _read_json(MODE_CATALOG_PATH, DEFAULT_MODE_CATALOG)
        request = _read_json(CURRENT_MODE_REQUEST_PATH, DEFAULT_CURRENT_MODE_REQUEST)
        modes = catalog.get("modes", []) if isinstance(catalog.get("modes"), list) else []
        items: list[dict[str, Any]] = []
        current_mode_id = str(request.get("selected_mode_id") or "safe")
        selected_index = 0
        mode_map: dict[str, dict[str, Any]] = {}
        for idx, raw_mode in enumerate(modes):
            if not isinstance(raw_mode, dict):
                continue
            mode_id = str(raw_mode.get("mode_id") or "").strip()
            if not mode_id:
                continue
            mode_map[mode_id] = raw_mode
            items.append(
                {
                    "id": mode_id,
                    "label": str(raw_mode.get("label") or mode_id),
                    "description": str(raw_mode.get("description") or ""),
                }
            )
            if mode_id == current_mode_id:
                selected_index = idx
        items.append({"id": "back", "label": "Back", "description": "Return to Rocky launcher"})
        self.menu_items = items
        self._mode_map = mode_map
        self.selected_index = min(selected_index, max(0, len(self.menu_items) - 1))
        self._current_mode_id = current_mode_id
        self._dirty = True

    def _reload_runtime_state(self) -> None:
        state = _read_json(RUNTIME_STATE_PATH, {})
        if state != self._runtime_state:
            self._runtime_state = state
            self._dirty = True

    def _handle_select(self) -> bool:
        selected = self.menu_items[self.selected_index]
        item_id = str(selected.get("id") or "")
        if item_id == "back":
            self.request_stop("mode-selector-back")
            return True

        if item_id == self._current_mode_id:
            self._status_message = "MODE ALREADY ACTIVE"
            self._status_until = time.monotonic() + 1.5
            self._dirty = True
            return True

        request = _read_json(CURRENT_MODE_REQUEST_PATH, DEFAULT_CURRENT_MODE_REQUEST)
        updated = {
            "version": int(request.get("version", 1)),
            "selected_mode_id": item_id,
            "previous_mode_id": self._current_mode_id,
            "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "requested_by": "mode_selector",
            "reason": f"on_device_mode_selector:{item_id}",
            "override_flags": request.get("override_flags", {}) if isinstance(request.get("override_flags"), dict) else {},
        }
        CURRENT_MODE_REQUEST_PATH.parent.mkdir(mode=0o775, parents=True, exist_ok=True)
        CURRENT_MODE_REQUEST_PATH.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
        self._current_mode_id = item_id
        self._status_message = f"MODE SET: {selected.get('label', item_id)}".upper()[:38]
        self._status_until = time.monotonic() + 2.0
        self._dirty = True
        return True

    def _footer_text(self) -> str:
        if self._status_message:
            return self._status_message[:38]
        selected = self.menu_items[self.selected_index]
        description = str(selected.get("description") or "")
        return (description or "UP/DN NAV | HOLD SELECT")[:38]

    def _selected_mode_details(self) -> tuple[str, str]:
        selected = self.menu_items[self.selected_index]
        mode_id = str(selected.get("id") or "")
        if mode_id == "back":
            return ("Return to launcher", "No mode change")

        mode = self._mode_map.get(mode_id, {})
        network = mode.get("network", {}) if isinstance(mode.get("network"), dict) else {}
        dns_mode = str(network.get("dns_mode") or "router_default").replace("_", " ").upper()
        vpn_required = bool(network.get("vpn_required", False))
        transfer = mode.get("transfer", {}) if isinstance(mode.get("transfer"), dict) else {}
        transfer_enabled = bool(transfer.get("enabled", False))
        admin = mode.get("admin", {}) if isinstance(mode.get("admin"), dict) else {}
        qb_webui = admin.get("qb_webui", {}) if isinstance(admin.get("qb_webui"), dict) else {}
        lan_admin = bool(network.get("allow_lan_admin", True))
        qb_lan = bool(qb_webui.get("lan", False))
        runtime_mode = self._runtime_state.get("mode", {}) if isinstance(self._runtime_state.get("mode"), dict) else {}
        services = runtime_mode.get("services", {}) if isinstance(runtime_mode.get("services"), dict) else {}

        line_one = f"VPN {'ON' if vpn_required else 'OFF'} DNS {dns_mode[:12]}"
        line_two = f"XFER {'ON' if transfer_enabled else 'OFF'} LAN {'ON' if lan_admin else 'OFF'} QB {'ON' if qb_lan else 'OFF'}"

        if mode_id == self._current_mode_id and services:
            gluetun = str(services.get("gluetun") or "off").upper()
            qbt = str(services.get("qbittorrent") or "off").upper()
            line_two = f"LIVE V:{gluetun[:4]} Q:{qbt[:4]} P:{str(services.get('pihole') or 'off').upper()[:4]}"

        return (line_one[:38], line_two[:38])

    def render_image(self) -> Image.Image:
        image = Image.new("1", (WIDTH, HEIGHT), 255)
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()
        draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=0)
        draw.text((7, 5), "MODES", font=font, fill=0)
        draw.text((WIDTH - 56, 5), self._current_mode_id.upper()[:8], font=font, fill=0)
        draw.line((6, 18, WIDTH - 7, 18), fill=0)

        y = 26
        window_start = max(0, min(self.selected_index - 1, max(0, len(self.menu_items) - VISIBLE_ROWS)))
        visible = self.menu_items[window_start: window_start + VISIBLE_ROWS]
        for offset, item in enumerate(visible):
            absolute_index = window_start + offset
            is_selected = absolute_index == self.selected_index
            is_active = str(item.get("id") or "") == self._current_mode_id
            if is_selected:
                draw.rectangle((5, y - 2, WIDTH - 6, y + 10), outline=0)
            suffix = " *" if is_active else ""
            row = f"{'>' if is_selected else ' '} {str(item.get('label') or '')}{suffix}"
            draw.text((9, y), row[:37], font=font, fill=0)
            y += 16

        detail_one, detail_two = self._selected_mode_details()
        draw.line((6, 78, WIDTH - 7, 78), fill=0)
        draw.text((8, 84), detail_one, font=font, fill=0)
        draw.text((8, 94), detail_two, font=font, fill=0)
        draw.line((6, 104, WIDTH - 7, 104), fill=0)
        draw.text((8, 109), self._footer_text(), font=font, fill=0)
        return image

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


if __name__ == "__main__":
    ModeSelectorApp().run()
