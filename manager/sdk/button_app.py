from __future__ import annotations

import logging
from typing import Any

from manager.runtime.button_protocol import (
    ButtonClient,
    ButtonEvent,
)

from .base_app import RockyApp


logger = logging.getLogger(__name__)


class RockyButtonApp(RockyApp):
    """Rocky SDK application with universal hardware buttons.

    Applications override on_button() to handle button events.

    Available events:

        navigate / short_press
        select   / short_press
        select   / long_press

    Long Select remains controlled by Rocky Runtime and normally stops
    the foreground application after the event is broadcast.
    """

    def __init__(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)

        self.button_client = ButtonClient()
        self.button_client.subscribe(
            self._dispatch_button
        )

    def _dispatch_button(
        self,
        event: ButtonEvent,
    ) -> None:
        try:
            self.on_button(event)
        except Exception:
            logger.exception(
                "Application button handler failed: "
                "button=%s action=%s",
                event.button,
                event.action,
            )

    def on_button(
        self,
        event: ButtonEvent,
    ) -> None:
        """Handle a Rocky hardware-button event.

        Override this method in the application.
        """

    def run(self) -> None:
        self.button_client.start()

        logger.info(
            "Rocky universal button client started"
        )

        try:
            super().run()
        finally:
            self.button_client.stop()

            logger.info(
                "Rocky universal button client stopped"
            )
