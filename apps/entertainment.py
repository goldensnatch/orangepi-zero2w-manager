from __future__ import annotations

import io
import json
import logging
import textwrap
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote
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

LOGGER = logging.getLogger("entertainment")
WIDTH = 250
HEIGHT = 122
VISIBLE_ROWS = 4
RUNTIME_STATE_PATH = Path("/run/rocky/state.json")

MENU_ITEMS = [
    {"id": "transfer-stack", "label": "Torrentz", "caption": "qBittorrent via TorrentFortress"},
    {"id": "radarr", "label": "Radarr", "caption": "Movie automation"},
    {"id": "sonarr", "label": "Sonarr", "caption": "Series automation"},
    {"id": "bazarr", "label": "Bazarr", "caption": "Subtitle automation"},
    {"id": "stashapp", "label": "StashApp", "caption": "Personal media browser"},
]


class EntertainmentApp(RockyButtonApp):
    name = "Entertainment"
    update_interval = 0.25

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.RLock()
        self.display_context = None
        self.display = None
        self.selected_index = 0
        self.page = "menu"
        self.page_service_id = "transfer-stack"
        self._dirty = True
        self._last_render_signature: str | None = None
        self._last_state: dict[str, Any] = {}

    def setup(self) -> None:
        LOGGER.info("Entertainment app started")
        self.display_context = Display()
        self.display = self.display_context.__enter__()
        self._dirty = True
        self._render(force=True)

    def cleanup(self) -> None:
        LOGGER.info("Entertainment app stopped")
        if self.display_context is not None:
            try:
                self.display_context.__exit__(None, None, None)
            except Exception:
                LOGGER.exception("Failed to sleep display")

    def update(self) -> None:
        snapshot = self._load_state()
        if snapshot != self._last_state:
            self._last_state = snapshot
            self._dirty = True
        if self._dirty:
            self._render()
        time.sleep(self.update_interval)

    def on_button(self, event: ButtonEvent) -> None:
        with self._lock:
            if self.page == "menu":
                if event.button == "up" and event.action == "short_press":
                    self.selected_index = (self.selected_index - 1) % len(MENU_ITEMS)
                    self._dirty = True
                elif event.button == "down" and event.action == "short_press":
                    self.selected_index = (self.selected_index + 1) % len(MENU_ITEMS)
                    self._dirty = True
                elif event.button == "select" and event.action == "long_press":
                    self.page_service_id = str(MENU_ITEMS[self.selected_index]["id"])
                    self.page = "detail"
                    self._dirty = True
            else:
                if event.button in {"up", "down"} and event.action == "short_press":
                    self.page = "menu"
                    self._dirty = True
                elif event.button == "select" and event.action == "long_press":
                    self._dirty = True

    def _load_state(self) -> dict[str, Any]:
        try:
            if RUNTIME_STATE_PATH.is_file():
                payload = json.loads(RUNTIME_STATE_PATH.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    return payload
        except Exception:
            LOGGER.exception("Failed reading runtime state")
        return {}

    def _service_live_entry(self, service_id: str) -> dict[str, Any]:
        published = self._last_state.get("published", {}) if isinstance(self._last_state, dict) else {}
        state = published.get("state", {}) if isinstance(published, dict) else {}
        web_services = state.get("web_services", {}) if isinstance(state, dict) else {}
        entry = web_services.get(service_id) if isinstance(web_services, dict) else None
        return entry if isinstance(entry, dict) else {}

    def _service_cached_entry(self, service_id: str) -> dict[str, Any]:
        published = self._last_state.get("published", {}) if isinstance(self._last_state, dict) else {}
        state = published.get("state", {}) if isinstance(published, dict) else {}
        web_cache = state.get("web_service_cache", {}) if isinstance(state, dict) else {}
        entry = web_cache.get(service_id) if isinstance(web_cache, dict) else None
        return entry if isinstance(entry, dict) else {}

    def _service_target_url(self, service_id: str) -> str | None:
        live = self._service_live_entry(service_id)
        cached = self._service_cached_entry(service_id)
        for key in ("tokenized_proxy_url", "url"):
            value = live.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("last_tokenized_proxy_url", "last_url"):
            value = cached.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _mode_label(self) -> str:
        published = self._last_state.get("published", {}) if isinstance(self._last_state, dict) else {}
        state = published.get("state", {}) if isinstance(published, dict) else {}
        mode = state.get("mode", {}) if isinstance(state, dict) else {}
        live = mode.get("live", {}) if isinstance(mode, dict) else {}
        label = live.get("label")
        if isinstance(label, str) and label.strip():
            return label.strip()
        return "Entertainment"

    def _service_health_text(self, service_id: str) -> str:
        live = self._service_live_entry(service_id)
        if isinstance(live.get("active"), bool):
            return "RUNNING" if live.get("active") else "OFFLINE"

        published = self._last_state.get("published", {}) if isinstance(self._last_state, dict) else {}
        state = published.get("state", {}) if isinstance(published, dict) else {}
        mode = state.get("mode", {}) if isinstance(state, dict) else {}
        services = mode.get("services", {}) if isinstance(mode, dict) else {}

        key = "qbittorrent" if service_id == "transfer-stack" else service_id
        value = services.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()[:10]
        return "UNKNOWN"

    def _signature(self, image: Image.Image) -> str:
        return str(hash(image.tobytes()))

    def _render(self, force: bool = False) -> None:
        image = self._render_menu() if self.page == "menu" else self._render_detail(self.page_service_id)
        signature = self._signature(image)
        if not force and signature == self._last_render_signature:
            self._dirty = False
            return
        if self.display is not None:
            self.display.show(image)
        self._last_render_signature = signature
        self._dirty = False

    def _base_image(self) -> tuple[Image.Image, ImageDraw.ImageDraw, ImageFont.ImageFont]:
        image = Image.new("1", (WIDTH, HEIGHT), 255)
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()
        draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=0)
        draw.text((7, 5), "ENTERTAINMENT", font=font, fill=0)
        draw.text((164, 5), self._mode_label()[:13], font=font, fill=0)
        draw.line((6, 18, WIDTH - 7, 18), fill=0)
        return image, draw, font

    def _render_menu(self) -> Image.Image:
        image, draw, font = self._base_image()
        window_start = max(0, min(self.selected_index - 1, max(0, len(MENU_ITEMS) - VISIBLE_ROWS)))
        visible = MENU_ITEMS[window_start:window_start + VISIBLE_ROWS]
        y = 24
        for offset, item in enumerate(visible):
            absolute_index = window_start + offset
            selected = absolute_index == self.selected_index
            if selected:
                draw.rectangle((5, y - 2, WIDTH - 6, y + 12), outline=0)
            prefix = ">" if selected else " "
            status = self._service_health_text(str(item["id"]))
            label = f"{prefix} {item['label']} [{status[:7]}]"
            draw.text((9, y), label[:37], font=font, fill=0)
            y += 16

        footer = "SELECT=QR  UP/DN=BROWSE"
        draw.line((6, 94, WIDTH - 7, 94), fill=0)
        draw.text((8, 101), footer[:38], font=font, fill=0)
        draw.text((8, 111), "Media center via Rocky proxy"[:38], font=font, fill=0)
        return image

    def _render_detail(self, service_id: str) -> Image.Image:
        image, draw, font = self._base_image()
        item = next((entry for entry in MENU_ITEMS if str(entry["id"]) == service_id), None)
        title = str(item["label"] if item else service_id)
        caption = str(item["caption"] if item else service_id)
        url = self._service_target_url(service_id)
        draw.text((7, 22), title[:18], font=font, fill=0)
        status = self._service_health_text(service_id)
        draw.text((150, 22), status[:12], font=font, fill=0)
        draw.line((6, 32, WIDTH - 7, 32), fill=0)
        if url:
            qr_url = "https://api.qrserver.com/v1/create-qr-code/?size=180x180&data=" + quote(url, safe="")
            try:
                with urlopen(qr_url, timeout=10) as response:
                    qr = Image.open(io.BytesIO(response.read())).convert("1")
                    qr = qr.resize((78, 78))
                    image.paste(qr, (8, 38))
            except Exception:
                draw.rectangle((8, 38, 86, 116), outline=0)
                draw.text((22, 73), "QR FAIL", font=font, fill=0)
            wrapped = textwrap.wrap(caption, width=18)
            y = 42
            draw.text((96, y), "Scan for", font=font, fill=0)
            y += 14
            for line in wrapped[:3]:
                draw.text((96, y), line[:22], font=font, fill=0)
                y += 12
            draw.text((96, 94), "UP/DN=BACK", font=font, fill=0)
            draw.text((96, 106), "SEL=REFRESH", font=font, fill=0)
        else:
            draw.text((8, 52), "QR unavailable", font=font, fill=0)
            draw.text((8, 66), "Service has not", font=font, fill=0)
            draw.text((8, 78), "published a URL yet.", font=font, fill=0)
            draw.text((8, 104), "UP/DN=BACK", font=font, fill=0)
        return image


if __name__ == "__main__":
    EntertainmentApp().run()
