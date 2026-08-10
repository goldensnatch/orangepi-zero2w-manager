from __future__ import annotations

import copy
import fnmatch
import logging
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable


logger = logging.getLogger(__name__)


EventCallback = Callable[["Event"], None]


@dataclass(frozen=True)
class Event:
    """A message published through the Rocky Event Bus."""

    name: str
    data: dict[str, Any] = field(default_factory=dict)
    source: str | None = None
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(
        default_factory=lambda: uuid.uuid4().hex
    )

    def copy(self) -> "Event":
        return Event(
            name=self.name,
            data=copy.deepcopy(self.data),
            source=self.source,
            timestamp=self.timestamp,
            event_id=self.event_id,
        )


@dataclass
class Subscription:
    """Registered Event Bus listener."""

    subscription_id: str
    pattern: str
    callback: EventCallback
    once: bool = False
    enabled: bool = True


class EventBusError(RuntimeError):
    """Base Event Bus failure."""


class InvalidEventName(EventBusError):
    """Raised when an event name is invalid."""


class EventBus:
    """Thread-safe publish/subscribe service for Rocky Runtime."""

    def __init__(
        self,
        *,
        history_limit: int = 100,
        queue_size: int = 256,
        worker_name: str = "rocky-event-bus",
    ) -> None:
        self._lock = threading.RLock()

        self._subscriptions: dict[
            str,
            Subscription,
        ] = {}

        self._history: deque[Event] = deque(
            maxlen=max(0, int(history_limit))
        )

        self._queue: queue.Queue[Event | None] = (
            queue.Queue(
                maxsize=max(1, int(queue_size))
            )
        )

        self._worker_name = worker_name
        self._worker: threading.Thread | None = None
        self._running = False

    @staticmethod
    def _validate_event_name(
        event_name: str,
    ) -> str:
        if not isinstance(event_name, str):
            raise InvalidEventName(
                "Event name must be a string"
            )

        normalized = event_name.strip()

        if not normalized:
            raise InvalidEventName(
                "Event name cannot be empty"
            )

        if " " in normalized:
            raise InvalidEventName(
                "Event names cannot contain spaces"
            )

        return normalized

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def subscription_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)

    def start(self) -> None:
        """Start the asynchronous delivery worker."""

        with self._lock:
            if self._running:
                return

            self._running = True

            self._worker = threading.Thread(
                target=self._worker_loop,
                name=self._worker_name,
                daemon=True,
            )

            self._worker.start()

        logger.info("Rocky Event Bus started")

    def stop(
        self,
        *,
        timeout: float = 2.0,
        drain: bool = False,
    ) -> None:
        """Stop asynchronous delivery.

        When drain is True, queued events are delivered before shutdown.
        """

        with self._lock:
            if not self._running:
                return

            self._running = False
            worker = self._worker

        if drain:
            try:
                self._queue.join()
            except Exception:
                logger.exception(
                    "Unable to drain Event Bus queue"
                )

        try:
            self._queue.put_nowait(None)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                pass

            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass

        if (
            worker is not None
            and worker.is_alive()
            and worker is not threading.current_thread()
        ):
            worker.join(
                timeout=max(0.0, float(timeout))
            )

        with self._lock:
            self._worker = None

        logger.info("Rocky Event Bus stopped")

    def subscribe(
        self,
        pattern: str,
        callback: EventCallback,
        *,
        once: bool = False,
    ) -> str:
        """Subscribe to an event name or wildcard pattern.

        Examples:
            app.started
            app.*
            battery.*
            *
        """

        normalized_pattern = (
            self._validate_event_name(pattern)
        )

        if not callable(callback):
            raise TypeError(
                "Event callback must be callable"
            )

        subscription_id = uuid.uuid4().hex

        subscription = Subscription(
            subscription_id=subscription_id,
            pattern=normalized_pattern,
            callback=callback,
            once=bool(once),
        )

        with self._lock:
            self._subscriptions[
                subscription_id
            ] = subscription

        logger.debug(
            "Subscribed %s to %s",
            subscription_id,
            normalized_pattern,
        )

        return subscription_id

    def subscribe_once(
        self,
        pattern: str,
        callback: EventCallback,
    ) -> str:
        return self.subscribe(
            pattern,
            callback,
            once=True,
        )

    def unsubscribe(
        self,
        subscription_id: str,
    ) -> bool:
        with self._lock:
            removed = self._subscriptions.pop(
                subscription_id,
                None,
            )

        return removed is not None

    def unsubscribe_callback(
        self,
        callback: EventCallback,
    ) -> int:
        removed = 0

        with self._lock:
            subscription_ids = [
                subscription_id
                for subscription_id, subscription
                in self._subscriptions.items()
                if subscription.callback is callback
            ]

            for subscription_id in subscription_ids:
                self._subscriptions.pop(
                    subscription_id,
                    None,
                )
                removed += 1

        return removed

    def clear_subscriptions(self) -> None:
        with self._lock:
            self._subscriptions.clear()

    def publish(
        self,
        name: str,
        data: dict[str, Any] | None = None,
        *,
        source: str | None = None,
        asynchronous: bool = False,
    ) -> Event:
        """Publish an event.

        Synchronous publishing delivers callbacks immediately in the
        calling thread.

        Asynchronous publishing places the event on the Event Bus queue.
        """

        event_name = self._validate_event_name(
            name
        )

        if data is None:
            payload: dict[str, Any] = {}
        elif isinstance(data, dict):
            payload = copy.deepcopy(data)
        else:
            raise TypeError(
                "Event data must be a dictionary or None"
            )

        event = Event(
            name=event_name,
            data=payload,
            source=source,
        )

        self._remember(event)

        if asynchronous:
            self.start()

            try:
                self._queue.put_nowait(event)
            except queue.Full:
                logger.error(
                    "Event Bus queue full; dropping event %s",
                    event.name,
                )
                raise EventBusError(
                    "Event Bus queue is full"
                )

            return event

        self._deliver(event)
        return event

    def emit(
        self,
        name: str,
        **data: Any,
    ) -> Event:
        """Convenience alias for synchronous publishing."""

        return self.publish(
            name,
            data,
        )

    def emit_async(
        self,
        name: str,
        **data: Any,
    ) -> Event:
        """Convenience alias for queued publishing."""

        return self.publish(
            name,
            data,
            asynchronous=True,
        )

    def _remember(
        self,
        event: Event,
    ) -> None:
        with self._lock:
            self._history.append(
                event.copy()
            )

    def _matching_subscriptions(
        self,
        event_name: str,
    ) -> list[Subscription]:
        with self._lock:
            return [
                copy.copy(subscription)
                for subscription
                in self._subscriptions.values()
                if subscription.enabled
                and fnmatch.fnmatchcase(
                    event_name,
                    subscription.pattern,
                )
            ]

    def _deliver(
        self,
        event: Event,
    ) -> None:
        subscriptions = (
            self._matching_subscriptions(
                event.name
            )
        )

        one_time_ids: list[str] = []

        for subscription in subscriptions:
            try:
                subscription.callback(
                    event.copy()
                )
            except Exception:
                logger.exception(
                    "Event subscriber failed: "
                    "pattern=%s event=%s",
                    subscription.pattern,
                    event.name,
                )
            finally:
                if subscription.once:
                    one_time_ids.append(
                        subscription.subscription_id
                    )

        for subscription_id in one_time_ids:
            self.unsubscribe(
                subscription_id
            )

    def _worker_loop(self) -> None:
        while True:
            event = self._queue.get()

            try:
                if event is None:
                    return

                self._deliver(event)

            except Exception:
                logger.exception(
                    "Unexpected Event Bus worker failure"
                )

            finally:
                self._queue.task_done()

    def wait_until_idle(
        self,
        timeout: float | None = None,
    ) -> bool:
        """Wait for queued asynchronous events to finish."""

        if timeout is None:
            self._queue.join()
            return True

        deadline = (
            time.monotonic()
            + max(0.0, float(timeout))
        )

        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True

            time.sleep(0.01)

        return self._queue.unfinished_tasks == 0

    def history(
        self,
        *,
        pattern: str | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        with self._lock:
            events = [
                event.copy()
                for event in self._history
            ]

        if pattern is not None:
            normalized_pattern = (
                self._validate_event_name(pattern)
            )

            events = [
                event
                for event in events
                if fnmatch.fnmatchcase(
                    event.name,
                    normalized_pattern,
                )
            ]

        if limit is not None:
            count = max(0, int(limit))

            if count == 0:
                return []

            events = events[-count:]

        return events

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()

    def close(self) -> None:
        self.stop()
        self.clear_subscriptions()

    def __enter__(self) -> "EventBus":
        self.start()
        return self

    def __exit__(
        self,
        _exc_type: Any,
        _exc_value: Any,
        _traceback: Any,
    ) -> None:
        self.close()
