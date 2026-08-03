from __future__ import annotations

import argparse
import shutil
import subprocess
import sys


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=True,
    )


def nmcli_available() -> bool:
    return shutil.which("nmcli") is not None


def connection_exists(name: str) -> bool:
    result = run(["nmcli", "-t", "-f", "NAME", "connection", "show"], check=False)
    if result.returncode != 0:
        return False
    return name in {line.strip() for line in result.stdout.splitlines() if line.strip()}


def ensure_networkmanager_fallback(
    *,
    ssid: str,
    password: str,
    connection_name: str,
    priority: int,
    route_metric: int,
    hidden: bool,
    activate: bool,
) -> None:
    if not connection_exists(connection_name):
        run(
            [
                "nmcli",
                "connection",
                "add",
                "type",
                "wifi",
                "ifname",
                "*",
                "con-name",
                connection_name,
                "ssid",
                ssid,
            ]
        )

    run(
        [
            "nmcli",
            "connection",
            "modify",
            connection_name,
            "wifi-sec.key-mgmt",
            "wpa-psk",
            "wifi-sec.psk",
            password,
            "connection.autoconnect",
            "yes",
            "connection.autoconnect-priority",
            str(priority),
            "ipv4.method",
            "auto",
            "ipv4.route-metric",
            str(route_metric),
            "ipv6.method",
            "ignore",
            "wifi.hidden",
            "yes" if hidden else "no",
        ]
    )

    if activate:
        run(["nmcli", "connection", "up", connection_name], check=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Configure a lower-priority Wi-Fi fallback connection behind Ethernet."
    )
    parser.add_argument("--ssid", required=True, help="Wi-Fi SSID to use as fallback")
    parser.add_argument("--password", required=True, help="Wi-Fi passphrase")
    parser.add_argument(
        "--connection-name",
        default="rocky-wifi-fallback",
        help="NetworkManager connection profile name",
    )
    parser.add_argument(
        "--priority",
        type=int,
        default=-50,
        help="Autoconnect priority lower than Ethernet so wired stays preferred",
    )
    parser.add_argument(
        "--route-metric",
        type=int,
        default=600,
        help="Higher route metric keeps Ethernet preferred when both links are up",
    )
    parser.add_argument(
        "--hidden",
        action="store_true",
        help="Mark the Wi-Fi network as hidden",
    )
    parser.add_argument(
        "--activate",
        action="store_true",
        help="Bring the fallback connection up immediately after configuring it",
    )
    args = parser.parse_args()

    if not nmcli_available():
        print(
            "[FAIL] NetworkManager/nmcli was not found on this Orange Pi.\n"
            "This helper currently supports NetworkManager-managed fallback setup only.",
            file=sys.stderr,
        )
        return 1

    ensure_networkmanager_fallback(
        ssid=args.ssid,
        password=args.password,
        connection_name=args.connection_name,
        priority=args.priority,
        route_metric=args.route_metric,
        hidden=args.hidden,
        activate=args.activate,
    )

    print("[OK] Wi-Fi fallback profile configured.")
    print(f"[OK] Connection name: {args.connection_name}")
    print(f"[OK] SSID: {args.ssid}")
    print(f"[OK] Autoconnect priority: {args.priority}")
    print(f"[OK] Route metric: {args.route_metric}")
    if args.activate:
        print("[OK] Activation requested.")
    else:
        print("[WARN] Profile was configured but not brought up. Re-run with --activate if needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
