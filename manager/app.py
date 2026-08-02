#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys

from .display_service import show_idle
from .process_manager import ProcessManager


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Orange Pi Zero 2W application manager"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("mode")

    subparsers.add_parser("stop")
    subparsers.add_parser("status")
    subparsers.add_parser("idle-screen")

    args = parser.parse_args()
    manager = ProcessManager()

    try:
        if args.command == "start":
            result = manager.start(args.mode)
        elif args.command == "stop":
            result = manager.stop()
            show_idle()
        elif args.command == "status":
            result = manager.status()
        elif args.command == "idle-screen":
            show_idle()
            result = manager.status()
        else:
            return 2

        print(json.dumps(result, indent=2))
        return 0

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
