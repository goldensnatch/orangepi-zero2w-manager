from __future__ import annotations

import logging
import select
import time
from dataclasses import dataclass
from typing import Callable

from evdev import InputDevice, ecodes, list_devices


LOGGER = logging.getLogger(__name__)


@dataclass
class ButtonEvent:
    code: int
    name: str
    pressed: bool
    held_seconds: float


class ButtonService:
    DEVICE_NAME_FRAGMENT = "lradc"

    NAVIGATION_CODE = 2    # KEY_1
    SELECT_CODE = 28       # KEY_ENTER

    LONG_PRESS_SECONDS = 0.9
    VERY_LONG_PRESS_SECONDS = 3.6
    DEBOUNCE_SECONDS = 0.08

    def __init__(
        self,
        on_navigate: Callable[[ButtonEvent], None],
        on_select: Callable[[ButtonEvent], None],
        should_continue: Callable[[], bool],
    ) -> None:
        self.on_navigate = on_navigate
        self.on_select = on_select
        self.should_continue = should_continue

        self.press_times: dict[int, float] = {}
        self.last_release: dict[int, float] = {}

    def _event_timestamp(self, event) -> float:
        sec = getattr(event, "sec", None)
        usec = getattr(event, "usec", None)
        if sec is None or usec is None:
            stamp = getattr(event, "timestamp", None)
            if callable(stamp):
                try:
                    return float(stamp())
                except Exception:
                    return time.monotonic()
            return time.monotonic()
        return float(sec) + (float(usec) / 1_000_000.0)

    def find_device(self) -> InputDevice:
        for path in list_devices():
            device = InputDevice(path)

            if self.DEVICE_NAME_FRAGMENT in device.name.lower():
                LOGGER.info(
                    "Using button device %s: %s",
                    path,
                    device.name,
                )
                return device

            device.close()

        raise RuntimeError("LRADC button device not found")

    def handle_event(self, event) -> None:
        if event.type != ecodes.EV_KEY:
            return

        code = int(event.code)
        value = int(event.value)

        if code == ecodes.KEY_ENTER:
            name = "KEY_ENTER"
        elif code == ecodes.KEY_1:
            name = "KEY_1"
        else:
            LOGGER.debug(
                "Ignoring unknown key: code=%d value=%d",
                code,
                value,
            )
            return

        LOGGER.info(
            "Raw button event: code=%d name=%s value=%d",
            code,
            name,
            value,
        )

        event_time = self._event_timestamp(event)

        # value 1 = press, 0 = release, 2 = autorepeat
        if value == 1:
            self.press_times[code] = event_time
            return

        if value == 2:
            return

        if value != 0:
            return

        now = event_time
        previous_release = self.last_release.get(code, 0.0)

        if now - previous_release < self.DEBOUNCE_SECONDS:
            return

        self.last_release[code] = now
        started = self.press_times.pop(code, now)
        held = max(0.0, now - started)

        button_event = ButtonEvent(
            code=code,
            name=name,
            pressed=False,
            held_seconds=held,
        )

        if code == self.NAVIGATION_CODE:
            LOGGER.info("Dispatching NAVIGATE")
            self.on_navigate(button_event)

        elif code == self.SELECT_CODE:
            LOGGER.info("Dispatching SELECT")
            self.on_select(button_event)

    def run(self) -> None:
        device = self.find_device()

        LOGGER.info(
            "Listening for buttons on %s (%s)",
            device.path,
            device.name,
        )
        LOGGER.info(
            "Button mapping: KEY_1=Up/Select, KEY_ENTER=Down/Select"
        )

        try:
            while self.should_continue():
                readable, _, _ = select.select(
                    [device.fd],
                    [],
                    [],
                    0.25,
                )

                if not readable:
                    continue

                for event in device.read():
                    self.handle_event(event)

        finally:
            LOGGER.info("Closing button device")
            device.close()
