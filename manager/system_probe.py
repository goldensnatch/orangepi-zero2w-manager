from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


NETWORK_ROOT = Path("/sys/class/net")
WIRELESS_STATUS_PATH = Path("/proc/net/wireless")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _run_command(command: list[str], *, timeout: float = 3.0) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except Exception:
        return ""
    return (result.stdout or "").strip()


def _ipv4_address(interface: str) -> str | None:
    output = _run_command(
        ["ip", "-o", "-4", "addr", "show", "dev", interface],
        timeout=2.0,
    )
    if not output:
        return None

    for token in output.split():
        if "/" in token and token.count(".") == 3:
            return token.split("/", 1)[0]

    return None


def _wireless_metrics() -> dict[str, dict[str, float | int | None]]:
    metrics: dict[str, dict[str, float | int | None]] = {}

    try:
        lines = WIRELESS_STATUS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return metrics

    for raw_line in lines[2:]:
        if ":" not in raw_line:
            continue
        name_part, values_part = raw_line.split(":", 1)
        interface = name_part.strip()
        fields = values_part.split()
        if len(fields) < 3:
            continue

        try:
            quality = float(fields[0].rstrip("."))
        except ValueError:
            quality = 0.0

        try:
            signal_dbm = int(float(fields[2].rstrip(".")))
        except ValueError:
            signal_dbm = None

        signal_percent = max(0, min(100, int(round((quality / 70.0) * 100))))
        metrics[interface] = {
            "quality": quality,
            "signal_dbm": signal_dbm,
            "signal_percent": signal_percent,
        }

    return metrics


def network_links_snapshot(
    preferred_interfaces: tuple[str, ...] = ("eth0", "wlan0"),
) -> dict[str, Any]:
    wireless = _wireless_metrics()
    interfaces: list[dict[str, Any]] = []

    for interface in preferred_interfaces:
        interface_path = NETWORK_ROOT / interface
        if not interface_path.exists():
            continue

        operstate = _read_text(interface_path / "operstate") or "unknown"
        carrier_raw = _read_text(interface_path / "carrier")
        carrier = None
        if carrier_raw is not None:
            try:
                carrier = int(carrier_raw)
            except ValueError:
                carrier = None

        address = _read_text(interface_path / "address")
        ipv4 = _ipv4_address(interface)
        is_wireless = (interface_path / "wireless").exists() or interface.startswith("wl")
        radio = wireless.get(interface, {})
        active = bool(ipv4) and (carrier == 1 or operstate == "up" or is_wireless)

        interfaces.append(
            {
                "name": interface,
                "kind": "wifi" if is_wireless else "ethernet",
                "operstate": operstate,
                "carrier": carrier,
                "mac": address,
                "ipv4": ipv4,
                "active": active,
                "signal_percent": radio.get("signal_percent"),
                "signal_dbm": radio.get("signal_dbm"),
            }
        )

    primary = None
    for preferred_kind in ("ethernet", "wifi"):
        for interface in interfaces:
            if interface["kind"] == preferred_kind and interface["active"]:
                primary = interface["name"]
                break
        if primary:
            break

    return {
        "primary": primary,
        "interfaces": interfaces,
    }


def network_header_token(snapshot: dict[str, Any] | None = None) -> str:
    snapshot = snapshot or network_links_snapshot()
    interfaces = snapshot.get("interfaces", []) if isinstance(snapshot, dict) else []
    primary_name = snapshot.get("primary") if isinstance(snapshot, dict) else None

    ethernet = next((item for item in interfaces if item.get("kind") == "ethernet"), None)
    wifi = next((item for item in interfaces if item.get("kind") == "wifi"), None)

    wifi_percent = None
    if isinstance(wifi, dict):
        raw_percent = wifi.get("signal_percent")
        if isinstance(raw_percent, int | float):
            wifi_percent = int(raw_percent)

    wifi_bars = None
    if wifi_percent is not None:
        wifi_bars = max(0, min(9, int(round(wifi_percent / 11.11))))

    if primary_name and ethernet and primary_name == ethernet.get("name"):
        if wifi and wifi.get("active") and wifi_bars is not None:
            return f"E+W{wifi_bars}"
        return "E"

    if primary_name and wifi and primary_name == wifi.get("name"):
        return f"W{wifi_bars}" if wifi_bars is not None else "W"

    if ethernet and ethernet.get("carrier") == 1:
        return "E"

    if wifi and wifi.get("active"):
        return f"W{wifi_bars}" if wifi_bars is not None else "W"

    return "NET0"


def process_snapshot() -> dict[str, Any]:
    output = _run_command(
        [
            "ps",
            "-eo",
            "pid=,ppid=,comm=,pcpu=,pmem=,etimes=,args=",
            "--sort=-pcpu",
        ],
        timeout=5.0,
    )

    items: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        parts = line.split(None, 6)
        if len(parts) < 7:
            continue

        pid_raw, ppid_raw, command, cpu_raw, mem_raw, elapsed_raw, args = parts
        try:
            pid = int(pid_raw)
            ppid = int(ppid_raw)
            cpu = float(cpu_raw)
            memory = float(mem_raw)
            elapsed_seconds = int(float(elapsed_raw))
        except ValueError:
            continue

        items.append(
            {
                "pid": pid,
                "ppid": ppid,
                "command": command,
                "cpu_percent": cpu,
                "memory_percent": memory,
                "elapsed_seconds": elapsed_seconds,
                "args": args,
            }
        )

    return {
        "count": len(items),
        "items": items,
    }
