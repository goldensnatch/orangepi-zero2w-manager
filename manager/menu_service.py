from __future__ import annotations

import io
import textwrap
import threading
import time
from typing import Any
from urllib.parse import quote
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFont

from manager.runtime import (
    DisplayManager,
    ServiceCatalog,
)


class MenuService:
    WIDTH = 250
    HEIGHT = 122
    VISIBLE_ROWS = 4

    def __init__(self) -> None:
        self.catalog = ServiceCatalog()
        self.services = self.catalog.load()
        self.items = self._build_menu()
        self.selected_index = 0

        self._lock = threading.RLock()

        self.display_manager = DisplayManager(
            partial_limit=15,
        )
        self.footer_override: str | None = None

    def _build_menu(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []

        for service in self.catalog.menu_services():
            service_id = str(service["id"])

            items.append(
                {
                    "id": service_id,
                    "name": service.get(
                        "name",
                        service_id.title(),
                    ),
                    "description": service.get(
                        "description",
                        "",
                    ),
                    "configured": bool(
                        service.get(
                            "configured",
                            service.get("command") is not None,
                        )
                    ),
                    "order": int(
                        service.get(
                            "menu_order",
                            999,
                        )
                    ),
                    "source": (
                        "plugin"
                        if service.get("manifest_path")
                        else "central"
                    ),
                }
            )

        return items

    @property
    def selected(self) -> dict[str, Any]:
        if not self.items:
            raise RuntimeError(
                "No menu applications configured"
            )

        return self.items[self.selected_index]

    def advance_selection(
        self,
    ) -> dict[str, Any]:
        with self._lock:
            if not self.items:
                raise RuntimeError(
                    "No menu applications configured"
                )

            self.selected_index = (
                self.selected_index + 1
            ) % len(self.items)

            return self.selected

    def next(
        self,
    ) -> dict[str, Any]:
        selected = self.advance_selection()
        self.render()
        return selected

    def previous(
        self,
    ) -> dict[str, Any]:
        with self._lock:
            if not self.items:
                raise RuntimeError(
                    "No menu applications configured"
                )

            self.selected_index = (
                self.selected_index - 1
            ) % len(self.items)

            selected = self.selected

        self.render()
        return selected

    def reload(
        self,
        *,
        preserve_selection: bool = True,
    ) -> list[dict[str, Any]]:
        with self._lock:
            selected_id = None

            if preserve_selection and self.items:
                selected_id = self.selected["id"]

            self.services = self.catalog.reload()
            self.items = self._build_menu()
            self.selected_index = 0

            if selected_id is not None:
                for index, item in enumerate(self.items):
                    if item["id"] == selected_id:
                        self.selected_index = index
                        break

            return list(self.items)

    def _make_image(
        self,
        message: str | None = None,
    ) -> Image.Image:
        image = Image.new(
            "1",
            (self.WIDTH, self.HEIGHT),
            255,
        )

        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()

        draw.rectangle(
            (
                0,
                0,
                self.WIDTH - 1,
                self.HEIGHT - 1,
            ),
            outline=0,
        )

        draw.text(
            (7, 5),
            "ROCKY APPS",
            font=font,
            fill=0,
        )

        draw.line(
            (
                6,
                18,
                self.WIDTH - 7,
                18,
            ),
            fill=0,
        )

        if not self.items:
            draw.text(
                (8, 36),
                "No applications configured",
                font=font,
                fill=0,
            )
            return image

        window_start = max(
            0,
            min(
                self.selected_index - 1,
                max(
                    0,
                    len(self.items)
                    - self.VISIBLE_ROWS,
                ),
            ),
        )

        visible = self.items[
            window_start:
            window_start + self.VISIBLE_ROWS
        ]

        y = 26

        for offset, item in enumerate(visible):
            absolute_index = (
                window_start + offset
            )

            is_selected = (
                absolute_index
                == self.selected_index
            )

            marker = (
                ">"
                if is_selected
                else " "
            )

            installed = (
                ""
                if item["configured"]
                else " [--]"
            )

            label = (
                f"{marker} "
                f"{item['name']}"
                f"{installed}"
            )

            if is_selected:
                draw.rectangle(
                    (
                        5,
                        y - 2,
                        self.WIDTH - 6,
                        y + 10,
                    ),
                    outline=0,
                )

            draw.text(
                (9, y),
                label[:37],
                font=font,
                fill=0,
            )

            y += 16

        footer = (
            message
            or self.footer_override
            or self.selected["description"]
        )

        wrapped = textwrap.wrap(
            footer,
            width=38,
        )

        footer_text = (
            wrapped[0]
            if wrapped
            else ""
        )

        draw.line(
            (
                6,
                94,
                self.WIDTH - 7,
                94,
            ),
            fill=0,
        )

        draw.text(
            (8, 101),
            footer_text[:38],
            font=font,
            fill=0,
        )

        return image

    def render(
        self,
        message: str | None = None,
        force_full: bool = False,
    ) -> None:
        image = self._make_image(message)

        with self._lock:
            self.display_manager.show(
                image,
                force_full=force_full,
            )

    def render_web_qr(
        self,
        title: str,
        url: str,
        caption: str | None = None,
    ) -> None:
        qr_url = "https://api.qrserver.com/v1/create-qr-code/?size=180x180&data=" + quote(url, safe="")
        image = Image.new("1", (self.WIDTH, self.HEIGHT), 255)
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()
        draw.rectangle((0, 0, self.WIDTH - 1, self.HEIGHT - 1), outline=0)
        draw.text((7, 5), title[:28], font=font, fill=0)
        draw.line((6, 18, self.WIDTH - 7, 18), fill=0)
        try:
            with urlopen(qr_url, timeout=10) as response:
                qr = Image.open(io.BytesIO(response.read())).convert("1")
                qr = qr.resize((88, 88))
                image.paste(qr, (8, 24))
        except Exception:
            draw.rectangle((8, 24, 96, 112), outline=0)
            draw.text((18, 63), "QR FAILED", font=font, fill=0)
        draw.text((106, 28), "Scan for", font=font, fill=0)
        wrapped = textwrap.wrap(caption or url, width=20)
        y = 42
        for line in wrapped[:4]:
            draw.text((106, y), line[:20], font=font, fill=0)
            y += 12
        with self._lock:
            self.display_manager.show(image, force_full=True)

    def prepare_for_app(self) -> None:
        """Clear and release the panel before launching an app."""

        with self._lock:
            self.display_manager.clear(
                force_full=True,
            )

            time.sleep(0.2)

            self.display_manager.release()

    def close(self) -> None:
        with self._lock:
            self.display_manager.close()
