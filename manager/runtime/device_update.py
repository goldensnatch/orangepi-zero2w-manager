"""Install Rocky from a GitHub branch tarball. Does not require a .git directory."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


DEFAULT_REPO = os.environ.get("ROCKY_UPDATE_REPO", "goldensnatch/orangepi-zero2w-manager")
DEFAULT_REF = os.environ.get(
    "ROCKY_UPDATE_REF",
    "cursor/entertainment-media-apps-e885",
)
COPY_ENTRIES = (
    "manager",
    "apps",
    "bin",
    "config",
    "content",
    "docs",
    "scripts",
    "systemd",
    "tests",
    "tools",
    "ROCKY_ARCHITECTURE.md",
)
SKIP_DIR_NAMES = {"venv", ".git", "__pycache__"}


def archive_url(repo: str | None = None, ref: str | None = None) -> str:
    selected_repo = repo or DEFAULT_REPO
    selected_ref = ref or DEFAULT_REF
    return f"https://github.com/{selected_repo}/archive/refs/heads/{selected_ref}.tar.gz"


def install_tree(source: Path, dest: Path, *, ref: str | None = None) -> list[str]:
    copied: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    for name in COPY_ENTRIES:
        src = source / name
        if not src.exists():
            continue
        target = dest / name
        if src.is_dir():
            shutil.copytree(
                src,
                target,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(*SKIP_DIR_NAMES, "*.pyc"),
            )
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        copied.append(name)
    version = str(ref or os.environ.get("ROCKY_UPDATE_REF", DEFAULT_REF)).strip()
    (dest / "ROCKY_BUILD_VERSION").write_text(f"{version}\n", encoding="utf-8")
    copied.append("ROCKY_BUILD_VERSION")
    return copied
