from __future__ import annotations

import hashlib
import json
import logging
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

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

LOGGER = logging.getLogger("print-lab")
WIDTH = 250
HEIGHT = 122
VISIBLE_ROWS = 4
MAINSAIL_URL = "http://127.0.0.1:84/"
ISP_PREP_REQUEST_PATH = Path("/run/rocky/isp-prep-request.json")


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


def service_status(unit: str) -> str:
    result = subprocess.run(
        ["systemctl", "is-active", unit],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    return (result.stdout.strip() or result.stderr.strip() or "unknown").lower()


class PrintLabApp(RockyButtonApp):
    name = "Print Lab"
    update_interval = 0.2

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.RLock()
        self.display_context = None
        self.display = None
        self.menu_items = [
            {"id": "mainsail", "label": "Mainsail", "kind": "detail"},
            {"id": "klipper_status", "label": "Klipper Status", "kind": "detail"},
            {"id": "isp_pins", "label": "ISP Pins", "kind": "detail"},
            {"id": "isp_prep", "label": "ISP Boot Prep", "kind": "action"},
            {"id": "back", "label": "Back", "kind": "action"},
        ]
        self.selected_index = 0
        self.page = "menu"
        self.status_message: str | None = None
        self.status_until = 0.0
        self._dirty = True
        self._last_render_signature: str | None = None
        self._last_status_poll = 0.0
        self._service_snapshot: dict[str, str] = {}
        self._isp_prep_armed = False

    def setup(self) -> None:
        self._clear_isp_prep_request()
        self.display_context = Display()
        self.display = self.display_context.__enter__()
        self._poll_service_snapshot(force=True)
        self._render_if_needed(force=True)
        LOGGER.info("Print Lab started")

    def cleanup(self) -> None:
        self._clear_isp_prep_request()
        if self.display_context is not None:
            try:
                self.display_context.__exit__(None, None, None)
            except Exception:
                LOGGER.exception("Print Lab display cleanup failed")
        LOGGER.info("Print Lab stopped")

    def update(self) -> None:
        now = time.monotonic()
        with self._lock:
            if self.status_until and now >= self.status_until:
                self.status_until = 0.0
                self.status_message = None
                self._dirty = True

            if now - self._last_status_poll >= 2.0:
                self._poll_service_snapshot(force=False)

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

        if self.page == "isp_prep_armed":
            self._clear_isp_prep_request()
            self._isp_prep_armed = False

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

        if self.page == "isp_prep_armed":
            self._clear_isp_prep_request()
            self._isp_prep_armed = False

        self.page = "menu"
        self._clear_status()
        self._dirty = True
        return True

    def _handle_select(self) -> bool:
        if self.page == "menu":
            selected_id = self.menu_items[self.selected_index]["id"]
            if selected_id == "mainsail":
                self.page = "mainsail"
            elif selected_id == "klipper_status":
                self._poll_service_snapshot(force=True)
                self.page = "klipper_status"
            elif selected_id == "isp_pins":
                self.page = "isp_pins"
            elif selected_id == "isp_prep":
                self.page = "isp_prep_confirm"
            elif selected_id == "back":
                self.request_stop("print-lab-back")
            self._clear_status()
            self._dirty = True
            return True

        if self.page == "mainsail":
            self.page = "menu"
            self._dirty = True
            return True

        if self.page == "klipper_status":
            self._poll_service_snapshot(force=True)
            self.status_message = "STATUS REFRESHED"
            self.status_until = time.monotonic() + 1.5
            self._dirty = True
            return True

        if self.page == "isp_pins":
            self.page = "menu"
            self._dirty = True
            return True

        if self.page == "isp_prep_confirm":
            self._write_isp_prep_request()
            self._isp_prep_armed = True
            self.page = "isp_prep_armed"
            self.status_message = "ARMED FOR ISP PREP"
            self.status_until = time.monotonic() + 2.0
            self._dirty = True
            return True

        if self.page == "isp_prep_armed":
            self.status_message = "HOLD LONGER FOR PREP"
            self.status_until = time.monotonic() + 1.5
            self._dirty = True
            return True

        return False

    def _clear_status(self) -> None:
        self.status_message = None
        self.status_until = 0.0

    def _poll_service_snapshot(self, *, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_status_poll < 2.0:
            return
        self._last_status_poll = now
        snapshot = {
            "klipper": service_status("klipper"),
            "moonraker": service_status("moonraker"),
        }
        if snapshot != self._service_snapshot:
            self._service_snapshot = snapshot
            self._dirty = True

    def _write_isp_prep_request(self) -> None:
        payload = {
            "action": "enter_isp_prep",
            "app_id": "3d_printer",
            "requested_by": "print-lab-app",
            "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "notes": {
                "purpose": "Keep SSH reachable while releasing the e-paper/header for ISP access",
                "pins": ["MOSI", "MISO", "SCK", "RESET", "VCC", "GND"],
            },
        }
        ISP_PREP_REQUEST_PATH.parent.mkdir(mode=0o775, parents=True, exist_ok=True)
        ISP_PREP_REQUEST_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        LOGGER.info("Wrote ISP prep request")

    def _clear_isp_prep_request(self) -> None:
        try:
            ISP_PREP_REQUEST_PATH.unlink()
        except FileNotFoundError:
            return
        except Exception:
            LOGGER.exception("Failed to clear ISP prep request")

    def footer_text(self) -> str:
        if self.status_message:
            return self.status_message[:38]
        if self.page == "menu":
            return "UP/DN NAV | HOLD OPEN"[:38]
        if self.page == "klipper_status":
            return "UP/DN BACK | HOLD REFRESH"[:38]
        if self.page == "isp_prep_confirm":
            return "UP/DN CANCEL | HOLD ARM"[:38]
        if self.page == "isp_prep_armed":
            return "UP/DN CANCEL | XL HOLD PREP"[:38]
        return "UP/DN BACK"[:38]

    def render_image(self) -> Image.Image:
        image = Image.new("1", (WIDTH, HEIGHT), 255)
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()
        draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=0)
        draw.text((7, 5), "PRINT LAB", font=font, fill=0)
        draw.line((6, 18, WIDTH - 7, 18), fill=0)

        if self.page == "menu":
            self._draw_menu(draw, font)
        elif self.page == "mainsail":
            self._draw_lines(draw, font, "MAINSAIL", [
                "Browser control:",
                publicize_service_url(MAINSAIL_URL),
                "",
                "Open from LAN client.",
                "UP/DN returns.",
            ])
        elif self.page == "klipper_status":
            self._draw_lines(draw, font, "KLIPPER STATUS", [
                f"Klipper: {self._service_snapshot.get('klipper', 'unknown').upper()}",
                f"Moonraker: {self._service_snapshot.get('moonraker', 'unknown').upper()}",
                "",
                publicize_service_url(MAINSAIL_URL),
                "",
                "HOLD refresh status.",
            ])
        elif self.page == "isp_pins":
            self._draw_lines(draw, font, "ISP PINS", [
                "MOSI   MISO   SCK",
                "RESET  VCC    GND",
                "",
                "Use VCC only if",
                "your programmer needs it.",
                "",
                "UP/DN returns.",
            ])
        elif self.page == "isp_prep_confirm":
            self._draw_lines(draw, font, "ISP PREP", [
                "Prep keeps SSH up.",
                "Display will release.",
                "Safe to remove eHAT",
                "after prep is entered.",
                "",
                "HOLD again to arm.",
            ])
        elif self.page == "isp_prep_armed":
            self._draw_lines(draw, font, "PREP ARMED", [
                "Extra-long hold enters",
                "hardware prep mode.",
                "Launcher will not",
                "reclaim the display.",
                "",
                "Then unplug eHAT.",
            ])

        draw.line((6, 94, WIDTH - 7, 94), fill=0)
        draw.text((8, 101), self.footer_text(), font=font, fill=0)
        return image

    def _draw_menu(self, draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont) -> None:
        count_text = f"{self.selected_index + 1}/{len(self.menu_items)}"
        draw.text((WIDTH - 40, 5), count_text[:6], font=font, fill=0)
        y = 26
        window_start = max(0, min(self.selected_index - 1, max(0, len(self.menu_items) - VISIBLE_ROWS)))
        visible = self.menu_items[window_start: window_start + VISIBLE_ROWS]
        for offset, item in enumerate(visible):
            absolute_index = window_start + offset
            is_selected = absolute_index == self.selected_index
            if is_selected:
                draw.rectangle((5, y - 2, WIDTH - 6, y + 10), outline=0)
            row = f"{'>' if is_selected else ' '} {item['label']}"
            draw.text((9, y), row[:37], font=font, fill=0)
            y += 16

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
    PrintLabApp().run()
