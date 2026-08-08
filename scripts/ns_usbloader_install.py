#!/usr/bin/env python3
"""Install/update NS-USBloader for Rocky Switch Transfer control plane.

This script downloads the latest upstream ns-usbloader jar into Rocky's runtime
area and installs Linux udev rules needed for non-root Nintendo Switch USB
access when run with sufficient privileges.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

GITHUB_LATEST = "https://api.github.com/repos/developersu/ns-usbloader/releases/latest"
DEFAULT_INSTALL_DIR = Path("/opt/zero2w-manager/runtime/ns-usbloader")
DEFAULT_JAR = DEFAULT_INSTALL_DIR / "ns-usbloader.jar"
UDEV_RULES = {
    "99-NS.rules": 'SUBSYSTEM=="usb", ATTRS{idVendor}=="057e", ATTRS{idProduct}=="3000", MODE="0666", TAG+="uaccess"\n',
    "99-NS-RCM.rules": 'SUBSYSTEM=="usb", ATTRS{idVendor}=="0955", ATTRS{idProduct}=="7321", MODE="0666", TAG+="uaccess"\n',
}


def run(cmd: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def java_major() -> int | None:
    java = shutil.which("java")
    if not java:
        return None
    result = run([java, "-version"])
    text = (result.stderr or result.stdout or "").strip()
    match = re.search(r'version "(\d+)', text)
    if not match:
        return None
    return int(match.group(1))


def select_release_asset(release: dict[str, object]) -> tuple[str, str]:
    assets = release.get("assets", [])
    candidates: list[tuple[str, str]] = []
    for asset in assets if isinstance(assets, list) else []:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if not name.endswith(".jar") or not url:
            continue
        lower = name.lower()
        if "legacy" in lower or "m1" in lower:
            continue
        candidates.append((name, url))
    if not candidates:
        raise RuntimeError("No suitable non-legacy NS-USBloader jar found in latest GitHub release")
    candidates.sort()
    return candidates[-1]


def download_latest(dest: Path) -> tuple[str, Path]:
    with urllib.request.urlopen(GITHUB_LATEST, timeout=30) as response:
        release = json.loads(response.read().decode("utf-8"))
    tag = str(release.get("tag_name") or "latest")
    asset_name, asset_url = select_release_asset(release)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="ns-usbloader-", suffix=".jar", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        print(f"Downloading {asset_name} ({tag})")
        urllib.request.urlretrieve(asset_url, tmp_path)
        if tmp_path.stat().st_size < 1_000_000:
            raise RuntimeError(f"Downloaded jar is unexpectedly small: {tmp_path.stat().st_size} bytes")
        shutil.move(str(tmp_path), str(dest))
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    version_file = dest.parent / "VERSION"
    version_file.write_text(f"{tag}\n{asset_name}\n{asset_url}\n", encoding="utf-8")
    return tag, dest


def install_udev_rules() -> bool:
    target = Path("/etc/udev/rules.d")
    if os.geteuid() != 0:
        print("Skipping udev rule install: rerun with sudo to write /etc/udev/rules.d")
        return False
    for name, content in UDEV_RULES.items():
        (target / name).write_text(content, encoding="utf-8")
        print(f"Installed /etc/udev/rules.d/{name}")
    run(["udevadm", "control", "--reload-rules"])
    run(["udevadm", "trigger"])
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Install/update NS-USBloader for Rocky")
    parser.add_argument("--dest", type=Path, default=DEFAULT_JAR, help="Jar destination")
    parser.add_argument("--skip-udev", action="store_true", help="Do not install udev rules")
    parser.add_argument("--check-java-only", action="store_true", help="Only verify Java availability")
    args = parser.parse_args()

    major = java_major()
    if major is None or major < 17:
        print("Java 17+ is required for NS-USBloader.", file=sys.stderr)
        print("OPi/Linux: sudo apt update && sudo apt install -y openjdk-17-jre-headless", file=sys.stderr)
        if args.check_java_only:
            return 1
    else:
        print(f"Java OK: major version {major}")
        if args.check_java_only:
            return 0

    tag, dest = download_latest(args.dest)
    print(f"Installed NS-USBloader {tag} -> {dest}")

    if not args.skip_udev:
        install_udev_rules()

    print("Done. Restart/replug Switch USB after udev rule changes if needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
