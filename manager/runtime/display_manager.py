from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)


class DisplayManager:
    """Owns the e-paper display and controls its refresh lifecycle.

    OPi.GPIO and zero2w_epaper are imported lazily so browser-console imports do
    not crash on development machines or in code paths that never touch display
    hardware.
    """

    WIDTH = 250
    HEIGHT = 122

    def __init__(self, *, partial_limit: int = 15, gpio_cleanup_delay: float = 0.25) -> None:
        self.partial_limit = max(1, int(partial_limit))
        self.gpio_cleanup_delay = max(0.0, float(gpio_cleanup_delay))
        self._lock = threading.RLock()
        self._display_context: Optional[object] = None
        self._display: Optional[object] = None
        self._partial_ready = False
        self._partial_count = 0
        self._last_image: Optional[Image.Image] = None

    @property
    def is_open(self) -> bool:
        return self._display is not None

    @property
    def partial_count(self) -> int:
        return self._partial_count

    def _normalize_image(self, image: Image.Image) -> Image.Image:
        image = image.convert("1")
        if image.size == (self.HEIGHT, self.WIDTH):
            image = image.rotate(90, expand=True)
        if image.size != (self.WIDTH, self.HEIGHT):
            raise ValueError(f"Unexpected display image size {image.size}; expected {(self.WIDTH, self.HEIGHT)}")
        return image

    def _cleanup_gpio(self) -> None:
        try:
            import OPi.GPIO as GPIO  # type: ignore
            GPIO.cleanup()
        except Exception:
            logger.debug("GPIO cleanup skipped/failed", exc_info=True)
        if self.gpio_cleanup_delay:
            time.sleep(self.gpio_cleanup_delay)

    def open(self) -> object:
        with self._lock:
            if self._display is not None:
                return self._display
            self._cleanup_gpio()
            from zero2w_epaper import Display  # type: ignore
            context = Display()
            enter = getattr(context, "__enter__", None)
            try:
                display = enter() if callable(enter) else context
                initialize = getattr(display, "initialize", None)
                if not callable(enter) and callable(initialize):
                    initialize(clear=False)
            except Exception:
                self._display_context = None
                self._display = None
                self._cleanup_gpio()
                raise
            self._display_context = context
            self._display = display
            self.reset_refresh_state()
            logger.info("E-paper display opened")
            return display

    def reset_refresh_state(self) -> None:
        with self._lock:
            self._partial_ready = False
            self._partial_count = 0
            self._last_image = None
            display = self._display
            reset_partial = getattr(display, "reset_partial", None) if display is not None else None
            if callable(reset_partial):
                reset_partial()

    def show(self, image: Image.Image, *, force_full: bool = False, skip_duplicate: bool = True) -> str:
        image = self._normalize_image(image)
        with self._lock:
            if skip_duplicate and not force_full and self._last_image is not None and image.tobytes() == self._last_image.tobytes():
                return "skipped"
            display = self.open()
            needs_full = force_full or not self._partial_ready or self._partial_count >= self.partial_limit
            if needs_full:
                display.show(image)
                reset_partial = getattr(display, "reset_partial", None)
                if callable(reset_partial):
                    reset_partial()
                self._partial_ready = True
                self._partial_count = 0
                mode = "full"
            else:
                show_partial = getattr(display, "show_partial", None)
                if not callable(show_partial):
                    raise RuntimeError("Display wrapper does not expose show_partial()")
                show_partial(image)
                self._partial_count += 1
                mode = "partial"
            self._last_image = image.copy()
            return mode

    def clear(self, *, force_full: bool = True) -> str:
        return self.show(Image.new("1", (self.WIDTH, self.HEIGHT), 255), force_full=force_full, skip_duplicate=False)

    def release(self, *, clear: bool = False) -> None:
        with self._lock:
            display = self._display
            context = self._display_context
            try:
                if clear and display is not None:
                    self.clear(force_full=True)
                exit_method = getattr(context, "__exit__", None)
                if callable(exit_method):
                    exit_method(None, None, None)
                else:
                    close = getattr(display, "close", None)
                    if callable(close):
                        close()
            finally:
                self._display = None
                self._display_context = None
                self.reset_refresh_state()
                self._cleanup_gpio()
