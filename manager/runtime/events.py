from __future__ import annotations

from .event_bus import EventBus


event_bus = EventBus(
    history_limit=200,
    queue_size=256,
)
