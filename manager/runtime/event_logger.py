from __future__ import annotations

import logging

from .event_bus import Event, EventBus


logger = logging.getLogger(__name__)


class RuntimeEventLogger:
    """Logs Event Bus traffic for development and diagnostics."""

    def __init__(
        self,
        bus: EventBus,
        *,
        pattern: str = "*",
    ) -> None:
        self.bus = bus
        self.pattern = pattern
        self.subscription_id: str | None = None

    def start(self) -> None:
        if self.subscription_id is not None:
            return

        self.subscription_id = self.bus.subscribe(
            self.pattern,
            self._handle_event,
        )

    def stop(self) -> None:
        if self.subscription_id is None:
            return

        self.bus.unsubscribe(
            self.subscription_id
        )

        self.subscription_id = None

    @staticmethod
    def _handle_event(
        event: Event,
    ) -> None:
        logger.info(
            "EVENT %s source=%s data=%s",
            event.name,
            event.source,
            event.data,
        )
