from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(RuntimeError):
    """Raised when a requested path is outside the allowed console workspace."""


@dataclass(frozen=True)
class WorkspaceRoot:
    id: str
    name: str
    path: Path
    read_only: bool = False


class WorkspaceRegistry:
    def __init__(self, workspaces: list[WorkspaceRoot]) -> None:
        self._workspaces = {workspace.id: workspace for workspace in workspaces}

    @classmethod
    def create_default(cls) -> "WorkspaceRegistry":
        return cls([
            WorkspaceRoot("project", "Project", Path("/opt/zero2w-manager"), False),
            WorkspaceRoot("runtime", "Runtime", Path("/opt/zero2w-manager/runtime"), False),
            WorkspaceRoot("config", "Config", Path("/opt/zero2w-manager/config"), False),
            WorkspaceRoot("logs", "Logs", Path("/var/log"), True),
        ])

    def list(self) -> list[WorkspaceRoot]:
        return list(self._workspaces.values())

    def get(self, root_name: str) -> WorkspaceRoot:
        try:
            return self._workspaces[root_name]
        except KeyError as error:
            raise WorkspaceError(f"unknown workspace root: {root_name}") from error

    def resolve(self, root_name: str, relative_path: str, *, for_write: bool = False) -> Path:
        workspace = self.get(root_name)
        if for_write and workspace.read_only:
            raise WorkspaceError(f"workspace is read-only: {root_name}")

        root = workspace.path.resolve()
        relative = Path(str(relative_path).lstrip("/"))
        candidate = (root / relative).resolve()

        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise WorkspaceError("path escapes workspace root") from error

        return candidate
