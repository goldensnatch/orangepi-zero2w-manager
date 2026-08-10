from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


logger = logging.getLogger(__name__)


DEFAULT_BUTTON_SOCKET = Path(
    "/run/rocky/buttons.sock"
)


@dataclass(frozen=True)
class ButtonEvent:
    button: str
    action: str
    duration: float = 0.0
    timestamp: float = 0.0
    source: str = "orange-pi-lradc"

    def __post_init__(self) -> None:
        if self.timestamp == 0.0:
            object.__setattr__(
                self,
                "timestamp",
                time.time(),
            )

    def to_json(self) -> str:
        return json.dumps(
            asdict(self),
            separators=(",", ":"),
        )

    @classmethod
    def from_json(
        cls,
        value: str,
    ) -> "ButtonEvent":
        data = json.loads(value)

        return cls(
            button=str(data["button"]),
            action=str(data["action"]),
            duration=float(
                data.get("duration", 0.0)
            ),
            timestamp=float(
                data.get("timestamp", time.time())
            ),
            source=str(
                data.get(
                    "source",
                    "rocky-runtime",
                )
            ),
        )


class ButtonServer:
    """Broadcasts Rocky button events to application clients."""

    def __init__(
        self,
        *,
        socket_path: str | Path = DEFAULT_BUTTON_SOCKET,
    ) -> None:
        self.socket_path = Path(socket_path)

        self._lock = threading.RLock()
        self._server: socket.socket | None = None
        self._clients: set[socket.socket] = set()
        self._thread: threading.Thread | None = None
        self._running = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def start(self) -> None:
        with self._lock:
            if self._running:
                return

            self.socket_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass

            server = socket.socket(
                socket.AF_UNIX,
                socket.SOCK_STREAM,
            )

            server.bind(
                str(self.socket_path)
            )

            os.chmod(
                self.socket_path,
                0o666,
            )

            server.listen(8)
            server.settimeout(0.5)

            self._server = server
            self._running = True

            self._thread = threading.Thread(
                target=self._accept_loop,
                name="rocky-button-server",
                daemon=True,
            )
            self._thread.start()

        logger.info(
            "Rocky button server listening on %s",
            self.socket_path,
        )

    def _accept_loop(self) -> None:
        while self.running:
            server = self._server

            if server is None:
                return

            try:
                client, _address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    logger.exception(
                        "Button server accept failed"
                    )
                return

            client.setblocking(True)

            with self._lock:
                self._clients.add(client)

            logger.info(
                "Rocky button client connected; clients=%d",
                self.client_count,
            )

    def publish(
        self,
        button: str,
        action: str,
        *,
        duration: float = 0.0,
        source: str = "orange-pi-lradc",
    ) -> ButtonEvent:
        event = ButtonEvent(
            button=str(button),
            action=str(action),
            duration=max(
                0.0,
                float(duration),
            ),
            source=source,
        )

        payload = (
            event.to_json() + "\n"
        ).encode("utf-8")

        dead_clients: list[socket.socket] = []

        with self._lock:
            clients = list(self._clients)

        for client in clients:
            try:
                client.sendall(payload)
            except OSError:
                dead_clients.append(client)

        for client in dead_clients:
            self._remove_client(client)

        logger.debug(
            "Button event: button=%s action=%s "
            "duration=%.2f clients=%d",
            event.button,
            event.action,
            event.duration,
            self.client_count,
        )

        return event

    def _remove_client(
        self,
        client: socket.socket,
    ) -> None:
        with self._lock:
            self._clients.discard(client)

        try:
            client.close()
        except OSError:
            pass

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return

            self._running = False
            server = self._server
            self._server = None
            clients = list(self._clients)
            self._clients.clear()
            thread = self._thread
            self._thread = None

        if server is not None:
            try:
                server.close()
            except OSError:
                pass

        for client in clients:
            try:
                client.close()
            except OSError:
                pass

        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=1.5)

        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning(
                "Unable to remove button socket",
                exc_info=True,
            )

        logger.info(
            "Rocky button server stopped"
        )


class ButtonClient:
    """Receives Rocky button events inside an application."""

    def __init__(
        self,
        *,
        socket_path: str | Path = DEFAULT_BUTTON_SOCKET,
        reconnect_interval: float = 1.0,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.reconnect_interval = max(
            0.2,
            float(reconnect_interval),
        )

        self._callbacks: list[
            Callable[[ButtonEvent], None]
        ] = []

        self._lock = threading.RLock()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def subscribe(
        self,
        callback: Callable[[ButtonEvent], None],
    ) -> None:
        if not callable(callback):
            raise TypeError(
                "Button callback must be callable"
            )

        with self._lock:
            self._callbacks.append(callback)

    def start(self) -> None:
        with self._lock:
            if self._running:
                return

            self._running = True

            self._thread = threading.Thread(
                target=self._run,
                name="rocky-button-client",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while self.running:
            try:
                self._connect_and_read()
            except Exception:
                if self.running:
                    logger.debug(
                        "Button client disconnected",
                        exc_info=True,
                    )

            if self.running:
                time.sleep(
                    self.reconnect_interval
                )

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def _connect_and_read(self) -> None:
        client = socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )

        client.connect(
            str(self.socket_path)
        )

        with self._lock:
            self._socket = client

        buffer = ""

        try:
            while self.running:
                data = client.recv(4096)

                if not data:
                    return

                buffer += data.decode(
                    "utf-8",
                    errors="replace",
                )

                while "\n" in buffer:
                    line, buffer = buffer.split(
                        "\n",
                        1,
                    )

                    if not line.strip():
                        continue

                    try:
                        event = ButtonEvent.from_json(
                            line
                        )
                    except Exception:
                        logger.warning(
                            "Ignoring invalid button event",
                            exc_info=True,
                        )
                        continue

                    self._dispatch(event)

        finally:
            with self._lock:
                if self._socket is client:
                    self._socket = None

            try:
                client.close()
            except OSError:
                pass

    def _dispatch(
        self,
        event: ButtonEvent,
    ) -> None:
        with self._lock:
            callbacks = list(
                self._callbacks
            )

        for callback in callbacks:
            try:
                callback(event)
            except Exception:
                logger.exception(
                    "Button callback failed"
                )

    def stop(self) -> None:
        with self._lock:
            self._running = False
            client = self._socket
            self._socket = None
            thread = self._thread
            self._thread = None

        if client is not None:
            try:
                client.shutdown(
                    socket.SHUT_RDWR
                )
            except OSError:
                pass

            try:
                client.close()
            except OSError:
                pass

        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=1.5)
