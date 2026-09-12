#!/usr/bin/env python3
"""Ensure Rocky mode config files are writable by the web service user."""
from __future__ import annotations

import argparse
import json
import os
import pwd
import grp
from pathlib import Path

DEFAULT_CONFIG_DIR = Path("/opt/zero2w-manager/runtime/config")
DEFAULT_CURRENT_MODE = DEFAULT_CONFIG_DIR / "current-mode.json"
DEFAULT_PROXY_SECRET = DEFAULT_CONFIG_DIR / "proxy-token-secret"
DEFAULT_OWNER = "rocky-web"


def resolve_owner(owner: str, group: str | None) -> tuple[int, int] | None:
    try:
        user = pwd.getpwnam(owner)
    except KeyError:
        return None
    gid = user.pw_gid
    if group:
        try:
            gid = grp.getgrnam(group).gr_gid
        except KeyError:
            pass
    return user.pw_uid, gid


def ensure_file(path: Path) -> None:
    if path.exists():
        return
    path.write_text(
        json.dumps({"version": 1, "selected_mode_id": "safe", "override_flags": {}}, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default=os.environ.get("ROCKY_MODE_CONFIG_OWNER", DEFAULT_OWNER))
    parser.add_argument("--group", default=os.environ.get("ROCKY_MODE_CONFIG_GROUP"))
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--current-mode", type=Path, default=DEFAULT_CURRENT_MODE)
    parser.add_argument("--proxy-secret", type=Path, default=DEFAULT_PROXY_SECRET)
    args = parser.parse_args()

    try:
        args.config_dir.mkdir(mode=0o775, parents=True, exist_ok=True)
        ensure_file(args.current_mode)
        owner = resolve_owner(args.owner, args.group)
        if owner is not None:
            uid, gid = owner
            os.chown(args.config_dir, uid, gid)
            args.config_dir.chmod(0o775)
            os.chown(args.current_mode, uid, gid)
            args.current_mode.chmod(0o664)
            if args.proxy_secret.exists():
                os.chown(args.proxy_secret, uid, gid)
                args.proxy_secret.chmod(0o640)
        else:
            print(f"skipping mode-config chown: user {args.owner!r} does not exist")
    except OSError as exc:
        print(f"mode-config permissions setup skipped: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
