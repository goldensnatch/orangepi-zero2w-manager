from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from zero2w_epaper import Display

from manager.runtime import ButtonEvent
from manager.sdk import RockyButtonApp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

LOGGER = logging.getLogger("mode-selector")
WIDTH = 250
HEIGHT = 122
MODE_CATALOG_PATH = Path("/opt/zero2w-manager/runtime/config/modes.json")
CURRENT_MODE_REQUEST_PATH = Path("/opt/zero2w-manager/runtime/config/current-mode.json")
RUNTIME_STATE_PATH = Path("/run/rocky/state.json")
VISIBLE_ROWS = 4


class ModeSelectorApp(RockyButtonApp):
    name = "Rocky Mode Selector"
    update_interval = 0.25

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.RLock()
        self.display_context = None
        self.display = None
        self.mode_catalog: list[dict] = []
        self.mode_ids: list[str] = []
        self.selected_index = 0
        self.current_requested_mode_id = "safe"
        self.current_live_mode_id = "safe"
        self.current_live_label = "SAFE"
        self.current_live_healthy = True
        self.current_live_warnings: list[str] = []
        self.status_message: str | None = None
        self.status_until = 0.0
        self._dirty = True
        self._last_state_poll = 0.0

    def setup(self) -> None:
        self.reload_catalog_and_state(force_select_current=True)
        self.display_context = Display()
        self.display = self.display_context.__enter__()
        self._render_if_needed(force=True)
        LOGGER.info("Mode selector started with %d modes", len(self.mode_ids))

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
            if self.status_until and now >= self.status_until:
                self.status_until = 0.0
                self.status_message = None
                self._dirty = True
            if now - self._last_state_poll >= 1.0:
                self._last_state_poll = now
                previous_requested = self.current_requested_mode_id
                previous_live = self.current_live_mode_id
                previous_healthy = self.current_live_healthy
                self.reload_catalog_and_state(force_select_current=False)
                if (
                    previous_requested != self.current_requested_mode_id
                    or previous_live != self.current_live_mode_id
                    or previous_healthy != self.current_live_healthy
                ):
                    self._dirty = True
        self._render_if_needed()

    def on_button(self, event: ButtonEvent) -> None:
        with self._lock:
            if event.button == "navigate" and event.action == "short_press":
                if self.mode_ids:
                    self.selected_index = (self.selected_index + 1) % len(self.mode_ids)
                    self.status_message = None
                    self.status_until = 0.0
                    self._dirty = True
                    LOGGER.info("Selected mode row %d -> %s", self.selected_index, self.selected_mode_id())
                return

            if event.button == "select" and event.action == "short_press":
                selected_mode_id = self.selected_mode_id()
                if selected_mode_id:
                    self.apply_selected_mode(selected_mode_id)
                return

            if event.button == "select" and event.action == "long_press":
                LOGGER.info("Long select received; Rocky Runtime should return home")
                return

            LOGGER.info("Ignored button event: %s %s", event.button, event.action)

    def selected_mode_id(self) -> str | None:
        if not self.mode_ids:
            return None
        return self.mode_ids[self.selected_index]

    def reload_catalog_and_state(self, *, force_select_current: bool) -> None:
        catalog = self.load_mode_catalog()
        self.mode_catalog = list(catalog.get("modes", [])) if isinstance(catalog.get("modes"), list) else []
        self.mode_ids = [str(entry.get("mode_id")) for entry in self.mode_catalog if entry.get("mode_id")]
        current_request = self.load_current_mode_request()
        self.current_requested_mode_id = str(current_request.get("selected_mode_id") or "safe")
        live_state = self.load_runtime_mode_state()
        self.current_live_mode_id = str(live_state.get("mode_id") or self.current_requested_mode_id or "safe")
        self.current_live_label = str(live_state.get("label") or self.current_live_mode_id).upper().replace(" MODE", "")
        self.current_live_healthy = bool(live_state.get("healthy", True))
        warnings = live_state.get("warnings")
        self.current_live_warnings = warnings if isinstance(warnings, list) else []
        if force_select_current and self.current_requested_mode_id in self.mode_ids:
            self.selected_index = self.mode_ids.index(self.current_requested_mode_id)
        elif self.selected_index >= len(self.mode_ids):
            self.selected_index = 0

    def load_mode_catalog(self) -> dict:
        try:
            if MODE_CATALOG_PATH.is_file():
                data = json.loads(MODE_CATALOG_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            LOGGER.exception("Failed to load mode catalog")
        return {"version": 1, "modes": []}

    def load_current_mode_request(self) -> dict:
        try:
            if CURRENT_MODE_REQUEST_PATH.is_file():
                data = json.loads(CURRENT_MODE_REQUEST_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            LOGGER.exception("Failed to load current mode request")
        return {"version": 1, "selected_mode_id": "safe", "override_flags": {}}

    def load_runtime_mode_state(self) -> dict:
        try:
            if RUNTIME_STATE_PATH.is_file():
                data = json.loads(RUNTIME_STATE_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    mode = data.get("mode")
                    if isinstance(mode, dict):
                        live = mode.get("live")
                        if isinstance(live, dict):
                            return live
        except Exception:
            LOGGER.exception("Failed to load runtime mode state")
        return {"mode_id": self.current_requested_mode_id, "label": self.current_requested_mode_id, "healthy": True, "warnings": []}

    def apply_selected_mode(self, selected_mode_id: str) -> None:
        existing = self.load_current_mode_request()
        updated = {
            "version": int(existing.get("version", 1)),
            "selected_mode_id": selected_mode_id,
            "previous_mode_id": str(existing.get("selected_mode_id") or "safe"),
            "requested_at": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            "requested_by": "mode-selector-app",
            "reason": "device_mode_switch",
            "override_flags": existing.get("override_flags", {}) if isinstance(existing.get("override_flags"), dict) else {},
        }
        try:
            CURRENT_MODE_REQUEST_PATH.parent.mkdir(mode=0o775, parents=True, exist_ok=True)
            CURRENT_MODE_REQUEST_PATH.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
            self.current_requested_mode_id = selected_mode_id
            self.status_message = f"APPLIED {self.mode_label_for(selected_mode_id).upper()}"
            self.status_until = time.monotonic() + 2.5
            self._dirty = True
            LOGGER.info("Requested mode switch to %s", selected_mode_id)
        except Exception:
            LOGGER.exception("Failed to write current mode request")
            self.status_message = "APPLY FAILED"
            self.status_until = time.monotonic() + 3.0
            self._dirty = True

    def mode_label_for(self, mode_id: str) -> str:
        for entry in self.mode_catalog:
            if str(entry.get("mode_id")) == mode_id:
                return str(entry.get("label") or mode_id)
        return mode_id

    def footer_text(self) -> str:
        if self.status_message:
            return self.status_message[:38]
        warning_text = "WARN" if self.current_live_warnings else "OK"
        live = self.current_live_label[:10]
        return f"LIVE {live} {warning_text} | SEL=APPLY"[:38]

    def render_image(self) -> Image.Image:
        image = Image.new("1", (WIDTH, HEIGHT), 255)
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()
        draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=0)
        draw.text((7, 5), "MODE SELECT", font=font, fill=0)
        draw.line((6, 18, WIDTH - 7, 18), fill=0)
        header = f"CUR:{self.current_requested_mode_id.upper()[:12]}"
        live = f"LIVE:{self.current_live_mode_id.upper()[:12]}"
        draw.text((8, 24), header[:18], font=font, fill=0)
        draw.text((120, 24), live[:18], font=font, fill=0)
        if not self.mode_ids:
            draw.text((8, 48), "No modes found", font=font, fill=0)
            draw.text((8, 101), self.footer_text(), font=font, fill=0)
            return image
        window_start = max(0, min(self.selected_index - 1, max(0, len(self.mode_ids) - VISIBLE_ROWS)))
        visible = self.mode_ids[window_start: window_start + VISIBLE_ROWS]
        y = 40
        for offset, mode_id in enumerate(visible):
            absolute_index = window_start + offset
            is_selected = absolute_index == self.selected_index
            label = self.mode_label_for(mode_id).upper().replace(' MODE', '')
            suffix = ''
            if mode_id == self.current_live_mode_id:
                suffix = ' *'
            elif mode_id == self.current_requested_mode_id:
                suffix = ' >'
            row = f"{'>' if is_selected else ' '} {label[:26]}{suffix}"
            if is_selected:
                draw.rectangle((5, y - 2, WIDTH - 6, y + 10), outline=0)
            draw.text((9, y), row[:37], font=font, fill=0)
            y += 13
        draw.line((6, 94, WIDTH - 7, 94), fill=0)
        draw.text((8, 101), self.footer_text(), font=font, fill=0)
        return image

    def _render_if_needed(self, *, force: bool = False) -> None:
        with self._lock:
            if not force and not self._dirty:
                return
            image = self.render_image()
            self._dirty = False
        if self.display is not None:
            self.display.show(image)


if __name__ == '__main__':
    ModeSelectorApp().run()
