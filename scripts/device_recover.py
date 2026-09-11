#!/usr/bin/env python3
"""Recover a Zero 2W that is stuck at 100% CPU without needing a git checkout.

/opt/zero2w-manager is often a copied tree, not a git clone. `git fetch` then
prints: fatal: not a git repository. This script is stdlib-only so it works
when curled into /tmp on a board that still has the old Rocky tree.

Run as root on the device:

  curl -fsSL -o /tmp/device_recover.py \\
    https://raw.githubusercontent.com/goldensnatch/orangepi-zero2w-manager/cursor/entertainment-media-apps-e885/scripts/device_recover.py
  sudo python3 /tmp/device_recover.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path


DEFAULT_REPO = os.environ.get("ROCKY_UPDATE_REPO", "goldensnatch/orangepi-zero2w-manager")
DEFAULT_REF = os.environ.get(
    "ROCKY_UPDATE_REF",
    "cursor/entertainment-media-apps-e885",
)
DEFAULT_ROOT = Path(os.environ.get("ROCKY_ROOT", "/opt/zero2w-manager"))
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
RADIO_UNITS = ("pwnagotchi.service", "bettercap.service")
PRINT_UNITS = ("klipper.service", "moonraker.service")
MEDIA_CONTAINERS = (
    "rocky-media-jellyfin",
    "rocky-media-jellyseerr",
    "rocky-media-prowlarr",
    "rocky-media-radarr",
    "rocky-media-sonarr",
    "rocky-media-bazarr",
)
TRANSFER_CONTAINERS = ("rocky-transfer-qbittorrent",)
CURRENT_MODE_PATH = Path("/opt/zero2w-manager/runtime/config/current-mode.json")


def archive_url(repo: str, ref: str) -> str:
    return f"https://github.com/{repo}/archive/refs/heads/{ref}.tar.gz"


def run(command: list[str], timeout: int = 45) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return subprocess.CompletedProcess(command, 124, stdout, stderr or "timeout")


def loadavg_1() -> float:
    try:
        return float(os.getloadavg()[0])
    except OSError:
        return 0.0


def top_cpu_snapshot(limit: int = 8) -> str:
    result = run(["ps", "-eo", "pid,pcpu,pmem,comm,args", "--sort=-pcpu"], timeout=5)
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    if not lines:
        return "ps failed: " + (result.stderr or result.stdout or f"exit {result.returncode}").strip()
    return "\n".join(lines[: max(1, limit) + 1])


def write_safe_mode(path: Path, reason: str) -> None:
    existing: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}
    payload = {
        "version": int(existing.get("version", 1) or 1),
        "selected_mode_id": "safe",
        "previous_mode_id": str(existing.get("selected_mode_id") or "safe"),
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requested_by": "device_recover",
        "reason": reason,
        "override_flags": existing.get("override_flags", {})
        if isinstance(existing.get("override_flags"), dict)
        else {},
    }
    path.parent.mkdir(mode=0o775, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def print_command(result: subprocess.CompletedProcess[str]) -> None:
    summary = " ".join(result.args if isinstance(result.args, list) else [str(result.args)])
    if result.returncode == 0:
        print(summary)
        return
    detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
    print(f"warning: {summary}: {detail}")


def quiesce(*, keep_mode: bool, keep_print_lab: bool, keep_transfer: bool) -> None:
    print(f"before loadavg={loadavg_1():.2f}")
    print(top_cpu_snapshot())
    if not keep_mode:
        write_safe_mode(CURRENT_MODE_PATH, "device_recover")
        print(f"pinned {CURRENT_MODE_PATH} to safe mode")
    print_command(run(["systemctl", "stop", *RADIO_UNITS], timeout=45))
    print_command(run(["docker", "stop", "--time", "5", *MEDIA_CONTAINERS], timeout=90))
    if not keep_transfer:
        print_command(run(["docker", "stop", "--time", "5", *TRANSFER_CONTAINERS], timeout=45))
    if not keep_print_lab:
        print_command(run(["systemctl", "stop", *PRINT_UNITS], timeout=45))
    pkill = run(["pkill", "-f", "Ragnar.py"], timeout=10)
    if pkill.returncode in {0, 1}:
        print("pkill -f Ragnar.py")
    else:
        print_command(pkill)
    print(f"after loadavg={loadavg_1():.2f}")
    print(top_cpu_snapshot())


def download_archive(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    return destination


def extract_archive(archive: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        try:
            tar.extractall(dest_dir, filter="data")
        except TypeError:
            tar.extractall(dest_dir)
    children = [path for path in dest_dir.iterdir() if path.is_dir()]
    if not children:
        raise RuntimeError(f"tarball {archive} did not contain a directory")
    return children[0]


def install_tree(source: Path, dest: Path, *, ref: str) -> list[str]:
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
    (dest / "ROCKY_BUILD_VERSION").write_text(f"{ref}\n", encoding="utf-8")
    copied.append("ROCKY_BUILD_VERSION")
    return copied


def restart_rocky() -> None:
    for unit in ("rocky-web.service", "zero2w-manager.service"):
        result = run(["systemctl", "restart", unit], timeout=45)
        if result.returncode == 0:
            print(f"restart {unit}: ok")
        else:
            print_command(result)


def cmd_quiesce(args: argparse.Namespace) -> int:
    quiesce(
        keep_mode=bool(args.keep_mode),
        keep_print_lab=bool(args.keep_print_lab),
        keep_transfer=bool(args.keep_transfer),
    )
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    dest = Path(args.root).resolve()
    ref = str(args.ref)
    repo = str(args.repo)
    with tempfile.TemporaryDirectory(prefix="rocky-update-") as tmp:
        tmpdir = Path(tmp)
        if args.from_dir:
            source = Path(args.from_dir).resolve()
            if not source.is_dir():
                print(f"from-dir is not a directory: {source}", file=sys.stderr)
                return 2
        else:
            archive = Path(args.tarball).resolve() if args.tarball else tmpdir / "rocky.tar.gz"
            if args.tarball:
                if not archive.is_file():
                    print(f"tarball not found: {archive}", file=sys.stderr)
                    return 2
            else:
                url = archive_url(repo, ref)
                print(f"downloading {url}")
                download_archive(url, archive)
            print(f"extracting {archive}")
            source = extract_archive(archive, tmpdir / "src")
        print(f"installing {source} -> {dest}")
        copied = install_tree(source, dest, ref=ref)
        print("updated: " + ", ".join(copied))
    if not args.skip_restart:
        restart_rocky()
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    quiesce_rc = cmd_quiesce(args)
    if quiesce_rc != 0:
        return quiesce_rc
    update_rc = cmd_update(args)
    if update_rc != 0:
        return update_rc
    # Pin safe mode again after restart so entertainment cannot start Jellyseerr.
    return cmd_quiesce(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="Install root (default /opt/zero2w-manager)")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--ref", default=DEFAULT_REF)
    parser.add_argument("--tarball", default="", help="Existing .tar.gz instead of GitHub download")
    parser.add_argument("--from-dir", default="", help="Existing unpacked tree instead of a tarball")
    parser.add_argument("--skip-restart", action="store_true")
    parser.add_argument("--keep-mode", action="store_true", help="Do not pin current-mode.json to safe")
    parser.add_argument("--keep-print-lab", action="store_true")
    parser.add_argument("--keep-transfer", action="store_true")
    parser.add_argument(
        "--all",
        dest="flag_all",
        action="store_true",
        help="Stop leftover work, install this branch, and restart Rocky (default)",
    )
    parser.add_argument(
        "--quiesce",
        dest="flag_quiesce",
        action="store_true",
        help="Only stop leftover radio/media work and pin safe mode",
    )
    parser.add_argument(
        "--update",
        dest="flag_update",
        action="store_true",
        help="Only install this branch from GitHub",
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=("quiesce", "update", "all"),
        default="all",
        help="quiesce = stop leftover work; update = tarball install; all = both (default)",
    )
    return parser


def resolve_action(args: argparse.Namespace) -> str:
    flags = [
        name
        for name, enabled in (
            ("quiesce", bool(getattr(args, "flag_quiesce", False))),
            ("update", bool(getattr(args, "flag_update", False))),
            ("all", bool(getattr(args, "flag_all", False))),
        )
        if enabled
    ]
    if len(flags) > 1:
        raise SystemExit("Specify only one of --quiesce, --update, or --all")
    if flags:
        return flags[0]
    return str(args.action or "all")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    action = resolve_action(args)
    if os.geteuid() != 0 and action in {"quiesce", "all"} and not os.environ.get("ROCKY_RECOVER_ALLOW_USER"):
        print("Run as root so systemctl/docker stop can work.", file=sys.stderr)
        return 1
    if action == "quiesce":
        return cmd_quiesce(args)
    if action == "update":
        return cmd_update(args)
    return cmd_all(args)


if __name__ == "__main__":
    raise SystemExit(main())
