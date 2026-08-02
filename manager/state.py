from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


STATE_FILE = Path("/opt/zero2w-manager/runtime/state.json")


class StateStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        if not STATE_FILE.exists():
            self.save({
                "active_mode": "idle",
                "active_pid": None,
                "status": "stopped",
                "last_error": None,
            })

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                return {
                    "active_mode": "idle",
                    "active_pid": None,
                    "status": "error",
                    "last_error": "State file could not be read",
                }

    def save(self, state: dict[str, Any]) -> None:
        with self._lock:
            temporary = STATE_FILE.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, indent=2) + "\n")
            temporary.replace(STATE_FILE)

    def update(self, **changes: Any) -> dict[str, Any]:
        with self._lock:
            state = self.load()
            state.update(changes)
            self.save(state)
            return state
