from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from manager.api.state_store import RuntimeStatePublisher
from manager.menu_service import MenuService
from manager.runtime.application_manager import ApplicationManager
from manager.runtime.network_transfer import (
    NetworkManager,
    NetworkTransferConfig,
    StorageManager,
    TransferManager,
)
from manager.runtime.workspace import WorkspaceRegistry


ApplicationExitCallback = Callable[[dict[str, Any]], None]


@dataclass
class RuntimeContext:
    """Shared service container for Rocky Runtime.

    RuntimeContext centralizes construction and ownership of the runtime's
    long-lived services. ManagerDaemon remains responsible for coordination
    and policy while services remain independently testable.
    """

    menu: MenuService
    state_publisher: RuntimeStatePublisher
    application_manager: ApplicationManager
    workspace_registry: WorkspaceRegistry
    network_config: NetworkTransferConfig
    network_manager: NetworkManager
    transfer_manager: TransferManager
    storage_manager: StorageManager
    state_lock: threading.RLock = field(default_factory=threading.RLock)
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("zero2w-manager")
    )

    @classmethod
    def create(
        cls,
        *,
        on_application_exit: ApplicationExitCallback,
        application_state_file: str = (
            "/opt/zero2w-manager/runtime/active_application.json"
        ),
        stop_timeout: float = 8.0,
        kill_timeout: float = 3.0,
    ) -> "RuntimeContext":
        """Construct the default production runtime context."""

        network_config = NetworkTransferConfig()
        storage_manager = StorageManager()
        transfer_manager = TransferManager(network_config)
        network_manager = NetworkManager(network_config)

        return cls(
            menu=MenuService(),
            state_publisher=RuntimeStatePublisher(),
            application_manager=ApplicationManager(
                state_file=application_state_file,
                stop_timeout=stop_timeout,
                kill_timeout=kill_timeout,
                on_exit=on_application_exit,
            ),
            workspace_registry=WorkspaceRegistry.create_default(),
            network_config=network_config,
            network_manager=network_manager,
            transfer_manager=transfer_manager,
            storage_manager=storage_manager,
        )

    def close(self) -> None:
        """Release context-owned services in safe shutdown order."""

        self.application_manager.close()
        self.menu.close()
