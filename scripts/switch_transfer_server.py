#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class SwitchTransferHandler(SimpleHTTPRequestHandler):
    server_version = "RockySwitchTransfer/1.0"

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        if self.path.split("?", 1)[0] == "/__health":
            self._send_health()
            return
        super().do_GET()

    def _send_health(self) -> None:
        root = Path(self.directory)
        payload: dict[str, Any] = {
            "service": "switch-transfer",
            "root": str(root),
            "root_exists": root.exists(),
            "root_is_dir": root.is_dir(),
            "readable": os.access(root, os.R_OK),
        }
        body = json.dumps(payload, sort_keys=True).encode("utf-8") + b"\n"
        status = HTTPStatus.OK if payload["root_is_dir"] and payload["readable"] else HTTPStatus.SERVICE_UNAVAILABLE
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve Rocky completed transfers over LAN for Switch/CyberFoil pulls.")
    parser.add_argument("--root", default=os.environ.get("SWITCH_TRANSFER_ROOT", "/mnt/rocky-transfer/complete"))
    parser.add_argument("--host", default=os.environ.get("SWITCH_TRANSFER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SWITCH_TRANSFER_PORT", "8077")))
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        raise SystemExit(f"Switch Transfer root does not exist or is not a directory: {root}")

    handler = lambda *a, **kw: SwitchTransferHandler(*a, directory=str(root), **kw)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Switch Transfer serving {root} on http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
