#!/usr/bin/env python3
from __future__ import annotations

import argparse
import cgi
import html
import json
import mimetypes
import os
import shutil
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SWITCH_USB_ID = "11ec:a7e0"
SWITCH_USB_LABEL = "Nyx USB Disk UMS"
DEFAULT_ROOT = "/mnt/rocky-transfer/complete"


def run_cmd(command: list[str], *, timeout: int = 8) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)


def human_size(size: int | None) -> str:
    if size is None:
        return "—"
    value = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def flatten_mounts(node: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [node]
    for child in node.get("children") or []:
        if isinstance(child, dict):
            rows.extend(flatten_mounts(child))
    return rows


def safe_rel_path(raw: str) -> Path:
    raw = urllib.parse.unquote(raw or "").replace("\\", "/").strip("/")
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("unsafe path")
    return candidate


@dataclass
class SwitchMount:
    detected: bool
    usb_line: str | None
    disk: str | None
    partition: str | None
    mount_path: str | None
    label: str | None
    model: str | None
    size: str | None
    fstype: str | None
    read_only: bool
    writable: bool
    usage: dict[str, Any] | None


class SwitchTransferState:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def usb_line(self) -> str | None:
        lsusb = shutil.which("lsusb")
        if not lsusb:
            return None
        result = run_cmd([lsusb], timeout=5)
        for line in result.stdout.splitlines():
            if SWITCH_USB_ID in line.lower() or "nyx" in line.lower():
                return line.strip()
        return None

    def switch_mount(self) -> SwitchMount:
        usb = self.usb_line()
        detected = usb is not None
        disk = partition = mount_path = label = model = size = fstype = None
        read_only = False
        writable = False
        usage = None

        lsblk = shutil.which("lsblk")
        if lsblk:
            result = run_cmd([lsblk, "-J", "-o", "NAME,PATH,LABEL,MODEL,VENDOR,TRAN,MOUNTPOINTS,SIZE,FSTYPE,RM,RO,TYPE"], timeout=8)
            try:
                data = json.loads(result.stdout or "{}")
            except json.JSONDecodeError:
                data = {}
            candidates: list[dict[str, Any]] = []
            for dev in data.get("blockdevices") or []:
                if not isinstance(dev, dict):
                    continue
                text = " ".join(str(dev.get(k) or "") for k in ("path", "label", "model", "vendor", "tran")).lower()
                is_switch_disk = dev.get("tran") == "usb" and (dev.get("rm") is True) and (
                    "hekate" in text or "sd raw" in text or detected
                )
                if is_switch_disk:
                    disk = str(dev.get("path") or "") or disk
                    model = str(dev.get("model") or dev.get("vendor") or "").strip() or model
                    size = str(dev.get("size") or "") or size
                    for child in dev.get("children") or []:
                        if isinstance(child, dict) and child.get("fstype"):
                            candidates.append(child)
                    if not candidates:
                        candidates.append(dev)
            for cand in candidates:
                mps = [m for m in (cand.get("mountpoints") or []) if m]
                if not partition and cand.get("path"):
                    partition = str(cand.get("path"))
                if not label and cand.get("label"):
                    label = str(cand.get("label"))
                if not fstype and cand.get("fstype"):
                    fstype = str(cand.get("fstype"))
                if cand.get("size"):
                    size = str(cand.get("size"))
                read_only = bool(cand.get("ro"))
                if mps:
                    mount_path = str(mps[0])
                    break

        if mount_path:
            mp = Path(mount_path)
            writable = os.access(mp, os.W_OK) and not read_only
            try:
                du = shutil.disk_usage(mp)
                usage = {"total": du.total, "used": du.used, "free": du.free, "total_human": human_size(du.total), "used_human": human_size(du.used), "free_human": human_size(du.free)}
            except OSError:
                usage = None

        return SwitchMount(detected, usb, disk, partition, mount_path, label, model, size, fstype, read_only, writable, usage)

    def files(self, rel: Path = Path(".")) -> list[dict[str, Any]]:
        base = (self.root / rel).resolve()
        if self.root not in [base, *base.parents]:
            raise ValueError("outside root")
        if not base.exists() or not base.is_dir():
            raise FileNotFoundError(str(rel))
        rows: list[dict[str, Any]] = []
        for item in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            try:
                st = item.stat()
            except OSError:
                continue
            item_rel = item.relative_to(self.root).as_posix()
            is_dir = item.is_dir()
            size = directory_size(item) if is_dir else st.st_size
            child_count = sum(1 for _ in item.iterdir()) if is_dir else None
            rows.append({
                "name": item.name,
                "path": item_rel,
                "kind": "directory" if is_dir else "file",
                "size": size,
                "size_human": human_size(size),
                "child_count": child_count,
                "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                "download_url": f"/file/{urllib.parse.quote(item_rel)}" if item.is_file() else None,
            })
        return rows

    def list_directory(self, base_root: Path, rel: Path = Path(".")) -> list[dict[str, Any]]:
        base = (base_root / rel).resolve()
        if base_root not in [base, *base.parents]:
            raise ValueError("outside root")
        if not base.exists() or not base.is_dir():
            raise FileNotFoundError(str(rel))
        rows: list[dict[str, Any]] = []
        for item in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            try:
                st = item.stat()
            except OSError:
                continue
            is_dir = item.is_dir()
            size = directory_size(item) if is_dir else st.st_size
            child_count = None
            if is_dir:
                try:
                    child_count = sum(1 for _ in item.iterdir())
                except OSError:
                    child_count = None
            item_rel = item.relative_to(base_root).as_posix()
            rows.append({"name": item.name, "path": item_rel, "kind": "directory" if is_dir else "file", "size": size, "size_human": human_size(size), "child_count": child_count, "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
        return rows

    def switch_files(self, rel: Path = Path(".")) -> tuple[Path, list[dict[str, Any]]]:
        switch = self.switch_mount()
        if not switch.mount_path:
            raise ValueError("Switch SD is not mounted")
        root = Path(switch.mount_path).resolve()
        return root, self.list_directory(root, rel)

    def status(self) -> dict[str, Any]:
        root_ok = self.root.is_dir() and os.access(self.root, os.R_OK)
        try:
            files = self.files(Path("."))
        except Exception:
            files = []
        switch = self.switch_mount()
        return {
            "service": "switch-transfer",
            "capabilities": {
                "gio_unmount": shutil.which("gio") is not None,
                "umount": shutil.which("umount") is not None,
                "udisks_mount": shutil.which("udisksctl") is not None,
                "udisks_poweroff": shutil.which("udisksctl") is not None,
                "true_poweroff_available": shutil.which("udisksctl") is not None,
            },
            "root": str(self.root),
            "root_exists": self.root.exists(),
            "root_is_dir": self.root.is_dir(),
            "readable": os.access(self.root, os.R_OK),
            "healthy": root_ok,
            "file_count_root": len(files),
            "switch": switch.__dict__,
            "safety": "For homebrew, personal media, saves, patches, and lawful backups only.",
        }



def merge_copy(src: Path, dest: Path, *, replace: bool) -> dict[str, Any]:
    copied = 0
    skipped = 0
    replaced = 0
    bytes_copied = 0

    def copy_file(source: Path, target: Path) -> None:
        nonlocal copied, skipped, replaced, bytes_copied
        if target.exists():
            if not replace:
                skipped += 1
                return
            replaced += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
        try:
            bytes_copied += source.stat().st_size
        except OSError:
            pass

    if src.is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        for item in src.rglob("*"):
            rel = item.relative_to(src)
            target = dest / rel
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.is_file():
                copy_file(item, target)
    else:
        copy_file(src, dest)

    return {"copied_files": copied, "skipped_files": skipped, "replaced_files": replaced, "bytes_copied": bytes_copied, "bytes_human": human_size(bytes_copied)}

class SwitchTransferHandler(BaseHTTPRequestHandler):
    server_version = "RockySwitchTransfer/2.0"

    @property
    def state(self) -> SwitchTransferState:
        return self.server.state  # type: ignore[attr-defined]

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, payload: dict[str, Any] | list[Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8") + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: HTTPStatus, message: str, **extra: Any) -> None:
        payload = {"ok": False, "error": message, **extra}
        self.send_json(payload, status)

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self.send_html()
        elif path == "/__health" or path == "/api/status":
            self.send_json(self.state.status())
        elif path == "/api/files":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                rel = safe_rel_path(query.get("path", [""])[0])
                self.send_json({"path": rel.as_posix(), "files": self.state.files(rel)})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        elif path == "/api/switch-files":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                rel = safe_rel_path(query.get("path", [""])[0])
                root, files = self.state.switch_files(rel)
                self.send_json({"root": str(root), "path": rel.as_posix(), "files": files})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        elif path == "/api/common-targets":
            self.handle_common_targets()
        elif path.startswith("/file/"):
            self.send_file(path.removeprefix("/file/"))
        else:
            self.send_error_json(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/copy":
            self.handle_copy()
        elif path == "/api/upload":
            self.handle_upload()
        elif path == "/api/delete":
            self.handle_delete()
        elif path == "/api/mount":
            self.handle_mount()
        elif path == "/api/eject":
            self.handle_eject()
        else:
            self.send_error_json(HTTPStatus.NOT_FOUND, "not found")

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1024 * 1024:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def resolve_source(self, raw: str, *, allow_directory: bool = False) -> Path:
        rel = safe_rel_path(raw)
        src = (self.state.root / rel).resolve()
        if self.state.root not in [src, *src.parents] or not src.exists():
            raise ValueError("source path is outside transfer root or missing")
        if src.is_dir() and not allow_directory:
            raise ValueError("source is a directory; directory copy must be explicitly allowed")
        if not src.is_file() and not src.is_dir():
            raise ValueError("source is not a regular file or directory")
        return src

    def resolve_switch_target_root(self, target: str = "") -> Path:
        switch = self.state.switch_mount()
        if not switch.mount_path:
            raise ValueError("Switch USB is detected but not mounted")
        if not switch.writable:
            raise ValueError("Switch mount is not writable")
        dest_root = Path(switch.mount_path).resolve()
        rel = safe_rel_path(target or "")
        target_root = (dest_root / rel).resolve()
        if dest_root not in [target_root, *target_root.parents]:
            raise ValueError("target folder is outside Switch mount")
        target_root.mkdir(parents=True, exist_ok=True)
        return target_root

    def resolve_switch_dest(self, name: str, *, target: str = "") -> Path:
        safe = Path(name).name
        if not safe:
            raise ValueError("invalid destination name")
        target_root = self.resolve_switch_target_root(target)
        dest = (target_root / safe).resolve()
        if target_root not in [dest, *dest.parents]:
            raise ValueError("destination outside selected Switch target")
        return dest

    def copy_source_to_switch(self, src: Path, *, target: str = "", replace: bool = False) -> dict[str, Any]:
        dest = self.resolve_switch_dest(src.name, target=target)
        kind = "directory" if src.is_dir() else "file"
        size = directory_size(src) if src.is_dir() else src.stat().st_size
        result = merge_copy(src, dest, replace=replace)
        return {"name": src.name, "kind": kind, "destination": str(dest), "size": size, "size_human": human_size(size), **result}

    def handle_copy(self) -> None:
        try:
            payload = self.read_json_body()
            raw_paths = payload.get("paths")
            if raw_paths is None:
                raw_paths = [payload.get("path")]
            if not isinstance(raw_paths, list) or not raw_paths:
                raise ValueError("paths must be a non-empty list")
            target = str(payload.get("target") or "")
            replace = bool(payload.get("replace"))
            copied: list[dict[str, Any]] = []
            for raw in raw_paths:
                src = self.resolve_source(str(raw or ""), allow_directory=True)
                copied.append(self.copy_source_to_switch(src, target=target, replace=replace))
            self.send_json({"ok": True, "target": target or "/", "replace": replace, "copied": copied, "count": len(copied)})
        except Exception as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

    def handle_upload(self) -> None:
        try:
            switch = self.state.switch_mount()
            if not switch.mount_path or not switch.writable:
                raise ValueError("Switch mount is not writable")
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type", "")})
            field = form["file"] if "file" in form else None
            if field is None or not getattr(field, "filename", ""):
                raise ValueError("missing upload field named file")
            dest = self.resolve_switch_dest(Path(field.filename).name)
            if dest.exists():
                raise FileExistsError(f"destination already exists: {dest.name}")
            with dest.open("wb") as out:
                shutil.copyfileobj(field.file, out)
            self.send_json({"ok": True, "uploaded": dest.name, "destination": str(dest), "size": dest.stat().st_size})
        except Exception as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

    def handle_common_targets(self) -> None:
        try:
            switch = self.state.switch_mount()
            if not switch.mount_path:
                raise ValueError("Switch SD is not mounted")
            root = Path(switch.mount_path).resolve()
            candidates = ["", "switch", "Nintendo", "atmosphere", "bootloader", "config"]
            rows = []
            for rel in candidates:
                path = (root / rel).resolve()
                if root in [path, *path.parents] and path.exists() and path.is_dir():
                    rows.append({"path": rel, "label": "/" if not rel else "/" + rel})
            self.send_json({"ok": True, "targets": rows})
        except Exception as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

    def handle_delete(self) -> None:
        try:
            payload = self.read_json_body()
            raw_paths = payload.get("paths")
            if raw_paths is None:
                raw_paths = [payload.get("path")]
            if not isinstance(raw_paths, list) or not raw_paths:
                raise ValueError("paths must be a non-empty list")
            deleted = []
            for raw in raw_paths:
                rel = safe_rel_path(str(raw or ""))
                if rel.as_posix() in {"", "."}:
                    raise ValueError("refusing to delete the transfer root")
                target = (self.state.root / rel).resolve()
                if self.state.root not in [target, *target.parents] or not target.exists():
                    raise ValueError(f"delete target is outside transfer root or missing: {rel.as_posix()}")
                kind = "directory" if target.is_dir() else "file"
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                deleted.append({"path": rel.as_posix(), "kind": kind})
            self.send_json({"ok": True, "deleted": deleted, "count": len(deleted)})
        except Exception as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

    def handle_mount(self) -> None:
        try:
            switch = self.state.switch_mount()
            if not switch.detected:
                raise ValueError("Switch UMS device is not detected")
            if switch.mount_path:
                self.send_json({"ok": True, "message": "Switch already mounted", "switch": switch.__dict__})
                return
            if not switch.partition:
                raise ValueError("Switch partition not found")
            if not shutil.which("udisksctl"):
                raise ValueError("udisksctl is unavailable; mount must be done manually")
            result = run_cmd(["udisksctl", "mount", "-b", switch.partition], timeout=30)
            mounted = self.state.switch_mount()
            self.send_json({
                "ok": result.returncode == 0 and bool(mounted.mount_path),
                "command": f"udisksctl mount -b {switch.partition}",
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
                "switch": mounted.__dict__,
            }, HTTPStatus.OK if result.returncode == 0 else HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

    def handle_eject(self) -> None:
        switch = self.state.switch_mount()
        steps: list[dict[str, Any]] = []
        sync_cmd = shutil.which("sync") or "/usr/bin/sync"
        sync_result = run_cmd([sync_cmd], timeout=30)
        steps.append({"command": "sync", "returncode": sync_result.returncode, "stderr": sync_result.stderr.strip()})
        if not switch.detected:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Switch USB device is not detected", steps=steps)
            return
        if switch.mount_path:
            if shutil.which("gio"):
                result = run_cmd(["gio", "mount", "-u", switch.mount_path], timeout=30)
                steps.append({"command": f"gio mount -u {switch.mount_path}", "returncode": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()})
            if self.state.switch_mount().mount_path and shutil.which("umount"):
                result = run_cmd(["umount", switch.mount_path], timeout=30)
                steps.append({"command": f"umount {switch.mount_path}", "returncode": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()})
        else:
            steps.append({"command": "unmount", "skipped": True, "reason": "Switch USB is not mounted"})
        poweroff_attempted = False
        poweroff_ok = False
        if shutil.which("udisksctl") and switch.partition:
            poweroff_attempted = True
            result = run_cmd(["udisksctl", "power-off", "-b", switch.disk or switch.partition], timeout=30)
            poweroff_ok = result.returncode == 0
            steps.append({"command": f"udisksctl power-off -b {switch.disk or switch.partition}", "returncode": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()})
        else:
            steps.append({"command": "power-off", "skipped": True, "reason": "udisksctl unavailable or no block device discovered; OPi can sync/unmount but cannot USB power-off from this user service"})
        remaining = self.state.switch_mount()
        unmounted = remaining.mount_path is None
        self.send_json({
            "ok": unmounted and (poweroff_ok or not remaining.detected),
            "safe_to_disconnect": unmounted,
            "true_poweroff": poweroff_ok,
            "poweroff_attempted": poweroff_attempted,
            "message": "Synced and unmounted; true USB power-off unavailable on this OPi service" if unmounted and not poweroff_ok else "Eject complete" if poweroff_ok else "Eject incomplete",
            "steps": steps,
            "switch": remaining.__dict__,
        })

    def send_file(self, raw: str) -> None:
        try:
            src = self.resolve_source(raw)
            if not src.is_file():
                raise ValueError("download target is not a file")
        except Exception as exc:
            self.send_error_json(HTTPStatus.NOT_FOUND, str(exc))
            return
        ctype = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(src.stat().st_size))
        self.send_header("Content-Disposition", f"attachment; filename={urllib.parse.quote(src.name)}")
        self.end_headers()
        with src.open("rb") as fh:
            shutil.copyfileobj(fh, self.wfile)

    def send_html(self) -> None:
        body = HTML.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Rocky Switch Transfer</title>
<style>
:root{--bg:#060a0f;--panel:#0d1623;--cyan:#00d4ff;--purple:#7c3aed;--green:#22c55e;--red:#ef4444;--yellow:#f59e0b;--text:#e2e8f0;--muted:#94a3b8;--line:#1e293b}
*{box-sizing:border-box} body{margin:0;background:radial-gradient(circle at top right,#16213e,transparent 40%),var(--bg);color:var(--text);font-family:Inter,Segoe UI,system-ui,sans-serif} a{color:var(--cyan)}
.wrap{max-width:1180px;margin:0 auto;padding:24px}.hero{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;margin-bottom:20px}.kicker{color:var(--cyan);font-size:12px;letter-spacing:.16em;text-transform:uppercase}.title{font-size:34px;font-weight:800;margin:4px 0}.subtitle{color:var(--muted);max-width:760px}.pill{border:1px solid var(--line);background:#0b1220;border-radius:999px;padding:8px 12px;color:var(--muted);white-space:nowrap}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}.card{background:linear-gradient(180deg,rgba(15,23,42,.95),rgba(2,6,23,.92));border:1px solid var(--line);border-radius:18px;padding:16px;box-shadow:0 14px 40px rgba(0,0,0,.22)}.label{font-size:11px;color:var(--cyan);text-transform:uppercase;letter-spacing:.12em}.value{font-size:20px;font-weight:750;margin-top:5px}.muted{color:var(--muted)}.ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--yellow)}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}.btn{border:1px solid var(--cyan);background:rgba(0,212,255,.08);color:var(--text);border-radius:12px;padding:10px 12px;cursor:pointer;text-decoration:none}.btn:hover{background:rgba(0,212,255,.18)}.btn-danger{border-color:var(--red);background:rgba(239,68,68,.08)}.btn[disabled]{opacity:.45;cursor:not-allowed}.browser{margin-top:16px}.file-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}.file{border:1px solid var(--line);border-radius:16px;background:rgba(15,23,42,.76);padding:14px}.file.selected{border-color:var(--cyan);box-shadow:0 0 0 1px rgba(0,212,255,.35)}.file-name{font-weight:700;word-break:break-word}.file-meta{font-size:12px;color:var(--muted);margin:8px 0 12px}.actions{display:flex;gap:8px;flex-wrap:wrap}.notice{border-left:3px solid var(--purple);background:rgba(124,58,237,.10);padding:12px;border-radius:10px;color:var(--muted);margin:12px 0}.upload{display:none}.upload.active{display:block}input[type=file]{color:var(--muted);max-width:100%}pre{white-space:pre-wrap;color:var(--muted);background:#020617;border:1px solid var(--line);border-radius:12px;padding:12px}.crumb{color:var(--muted);margin:8px 0 14px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}.crumb button{border:1px solid var(--line);background:#0b1220;color:var(--text);border-radius:999px;padding:6px 10px;cursor:pointer}.select-row{display:flex;align-items:flex-start;gap:8px}.select-row input{margin-top:3px}.folder-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
</style>
</head>
<body><main class="wrap">
<section class="hero"><div><div class="kicker">Rocky Control Plane</div><div class="title">Switch Transfer</div><div class="subtitle">Polished LAN transfer surface rooted at completed Rocky downloads. For homebrew, personal media, saves, patches, and lawful backups only.</div></div><div class="pill" id="service-pill">loading…</div></section>
<section class="grid" id="status-grid"></section>
<div class="notice" id="notice"></div>
<section class="card"><div class="label">Switch Copy Target</div><div class="toolbar"><select id="target-select"></select><input id="target-input" placeholder="Custom target folder, e.g. switch" /><label class="muted"><input type="checkbox" id="replace-existing" /> Replace existing files</label><button class="btn" onclick="applyTargetInput()">Use Target</button></div><div class="muted" id="target-note">Target: /</div></section>
<section class="card upload" id="upload-card"><div class="label">Upload to Switch</div><form id="upload-form"><input name="file" type="file" required /> <button class="btn" type="submit">Upload</button></form></section>
<section class="toolbar"><button class="btn" onclick="loadAll()">Refresh</button><button class="btn" id="mount-btn" onclick="mountSwitch()">Mount Switch UMS</button><button class="btn" id="copy-selected-btn" onclick="copySelected()">Copy Selected to Switch</button><button class="btn" onclick="clearSelection()">Clear Selection</button><button class="btn btn-danger" id="eject-btn" onclick="ejectSwitch()">Sync + Eject Switch</button><a class="btn" href="/__health">Health JSON</a><a class="btn" href="/api/status">Status JSON</a></section>
<section class="browser card"><div class="label">Completed files</div><div class="crumb" id="crumb">/</div><div class="file-grid" id="files"></div></section>
<section class="browser card"><div class="label">Switch SD Card</div><div class="muted">Read-only browser for the mounted UMS SD card.</div><div class="crumb" id="switch-crumb">/</div><div class="file-grid" id="switch-files"></div></section>
<pre id="log"></pre>
</main><script>
let currentPath=''; let switchPath=''; let copyTarget=''; let statusCache=null; const selected=new Set();
const fmt = v => v || '—';
function esc(s){return String(s ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function j(url, opts){const r=await fetch(url, opts); const data=await r.json(); if(!r.ok) throw new Error(data.error || r.statusText); return data}
function log(x){document.getElementById('log').textContent = typeof x === 'string' ? x : JSON.stringify(x,null,2)}
function card(label,value,cls=''){return `<div class="card"><div class="label">${label}</div><div class="value ${cls}">${esc(value)}</div></div>`}
function renderStatus(s){statusCache=s; const sw=s.switch||{}; const connected=sw.detected; const mounted=!!sw.mount_path; const writable=!!sw.writable; document.getElementById('service-pill').textContent=s.healthy?'service healthy':'service degraded'; document.getElementById('status-grid').innerHTML=[card('Transfer root',s.root),card('Switch USB',connected?'detected':'not detected',connected?'ok':'bad'),card('Mount state',mounted?sw.mount_path:'not mounted',mounted?'ok':'warn'),card('Writable',writable?'yes':'no',writable?'ok':'warn'),card('Switch size',sw.size || (sw.usage&&sw.usage.total_human)),card('Switch free',sw.usage&&sw.usage.free_human)].join(''); document.getElementById('upload-card').classList.toggle('active', writable); document.getElementById('eject-btn').disabled=!connected; document.getElementById('mount-btn').disabled=!(connected && !mounted && sw.partition && s.capabilities && s.capabilities.udisks_mount); document.getElementById('eject-btn').textContent=(s.capabilities&&s.capabilities.true_poweroff_available)?'Sync + Eject Switch':'Sync + Unmount Switch'; document.getElementById('copy-selected-btn').disabled=!(writable && selected.size>0); document.getElementById('notice').textContent = connected ? (mounted ? (writable?'Switch mounted and writable. Copy/upload controls enabled.':'Switch mounted read-only or not writable. Copy/upload disabled.') : 'Nyx USB Disk UMS is detected but no filesystem is mounted yet. Copy/upload disabled until mounted.') : 'No Switch UMS device detected.';}
function parentPath(){if(!currentPath) return ''; const parts=currentPath.split('/').filter(Boolean); parts.pop(); return parts.join('/')}
function renderCrumb(){const crumb=document.getElementById('crumb'); const parts=currentPath.split('/').filter(Boolean); let html='<button onclick="openDirRaw(\'\')">Root</button>'; let acc=''; for(const part of parts){acc = acc ? acc + '/' + part : part; html += `<span>/</span><button onclick="openDirRaw('${encodeURIComponent(acc)}')">${esc(part)}</button>`} if(currentPath) html = `<button onclick="openDirRaw('${encodeURIComponent(parentPath())}')">← Back</button>` + html; crumb.innerHTML=html}
function renderFiles(rows){const box=document.getElementById('files'); renderCrumb(); if(!rows.length){box.innerHTML='<div class="muted">No completed files in this folder.</div>'; return} box.innerHTML=rows.map(f=>{const isDir=f.kind==='directory'; const enc=encodeURIComponent(f.path); const checked=selected.has(f.path)?'checked':''; const sel=selected.has(f.path)?' selected':''; const icon=isDir?'📁':'📄'; const count=isDir && f.child_count!=null ? ` · ${f.child_count} items` : ''; return `<div class="file${sel}"><div class="file-name select-row"><input type="checkbox" ${checked} onchange="toggleSelected('${enc}', this.checked)" /><span>${icon} ${esc(f.name)}</span></div><div class="file-meta">${esc(f.kind)} · ${esc(f.size_human)}${count} · ${esc(f.modified)}</div><div class="actions">${isDir?`<button class="btn" onclick="openDir('${enc}')">Open</button><button class="btn" ${statusCache&&statusCache.switch&&statusCache.switch.writable?'':'disabled'} onclick="copyFile('${enc}')">Copy Folder to Switch</button><button class="btn btn-danger" onclick="deletePath('${enc}')">Delete Folder</button>`:`<a class="btn" href="${f.download_url}">Download</a><button class="btn" ${statusCache&&statusCache.switch&&statusCache.switch.writable?'':'disabled'} onclick="copyFile('${enc}')">Copy to Switch</button><button class="btn btn-danger" onclick="deletePath('${enc}')">Delete File</button>`}</div></div>`}).join(''); if(statusCache) renderStatus(statusCache)}
function switchParentPath(){if(!switchPath) return ''; const parts=switchPath.split('/').filter(Boolean); parts.pop(); return parts.join('/')}
function renderSwitchCrumb(){const crumb=document.getElementById('switch-crumb'); const parts=switchPath.split('/').filter(Boolean); let html='<button onclick="openSwitchDirRaw(\'\')">Root</button>'; let acc=''; for(const part of parts){acc = acc ? acc + '/' + part : part; html += `<span>/</span><button onclick="openSwitchDirRaw('${encodeURIComponent(acc)}')">${esc(part)}</button>`} if(switchPath) html = `<button onclick="openSwitchDirRaw('${encodeURIComponent(switchParentPath())}')">← Back</button>` + html; crumb.innerHTML=html}
function renderSwitchFiles(rows){const box=document.getElementById('switch-files'); renderSwitchCrumb(); if(!rows.length){box.innerHTML='<div class="muted">No files shown, or Switch SD is not mounted.</div>'; return} box.innerHTML=rows.map(f=>{const isDir=f.kind==='directory'; const enc=encodeURIComponent(f.path); const icon=isDir?'📁':'📄'; const count=isDir && f.child_count!=null ? ` · ${f.child_count} items` : ''; return `<div class="file"><div class="file-name">${icon} ${esc(f.name)}</div><div class="file-meta">${esc(f.kind)} · ${esc(f.size_human)}${count} · ${esc(f.modified)}</div><div class="actions">${isDir?`<button class="btn" onclick="openSwitchDir('${enc}')">Open</button><button class="btn" onclick="setCopyTarget(decodeURIComponent('${enc}'))">Use as Target</button>`:''}</div></div>`}).join('')}
function setCopyTarget(t){copyTarget=(t||'').replace(/^\/+|\/+$/g,''); document.getElementById('target-input').value=copyTarget; document.getElementById('target-note').textContent='Target: /' + copyTarget}
function applyTargetInput(){setCopyTarget(document.getElementById('target-input').value)}
async function loadTargets(){try{const data=await j('/api/common-targets'); const sel=document.getElementById('target-select'); sel.innerHTML=data.targets.map(t=>`<option value="${esc(t.path)}">${esc(t.label)}</option>`).join(''); sel.onchange=()=>setCopyTarget(sel.value); if(!copyTarget && data.targets.length) setCopyTarget(data.targets[0].path)}catch(e){document.getElementById('target-select').innerHTML='<option value="">/</option>';}}
async function loadAll(){try{await loadTargets(); const s=await j('/api/status'); renderStatus(s); const files=await j('/api/files?path='+encodeURIComponent(currentPath)); renderFiles(files.files); try{const sf=await j('/api/switch-files?path='+encodeURIComponent(switchPath)); renderSwitchFiles(sf.files)}catch(err){document.getElementById('switch-files').innerHTML='<div class="muted">'+esc(err.message)+'</div>'; renderSwitchCrumb()} log('Ready')}catch(e){log(e.message)}}
function openDir(p){currentPath=decodeURIComponent(p); selected.clear(); loadAll()}
function openDirRaw(p){currentPath=decodeURIComponent(p); selected.clear(); loadAll()}
function openSwitchDir(p){switchPath=decodeURIComponent(p); loadAll()}
function openSwitchDirRaw(p){switchPath=decodeURIComponent(p); loadAll()}
function toggleSelected(p,on){const path=decodeURIComponent(p); if(on) selected.add(path); else selected.delete(path); if(statusCache) renderStatus(statusCache);}
function clearSelection(){selected.clear(); loadAll()}
async function copyFile(p){try{log(await j('/api/copy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:decodeURIComponent(p), target:copyTarget, replace:document.getElementById('replace-existing').checked})}))}catch(e){log(e.message)}}
async function copySelected(){try{if(!selected.size) throw new Error('No files or folders selected'); log(await j('/api/copy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:Array.from(selected), target:copyTarget, replace:document.getElementById('replace-existing').checked})})); selected.clear(); await loadAll()}catch(e){log(e.message)}}
async function deletePath(p){const path=decodeURIComponent(p); if(!confirm('Delete from Rocky completed files?\n\n'+path)) return; try{log(await j('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})})); selected.delete(path); await loadAll()}catch(e){log(e.message)}}
async function mountSwitch(){try{log(await j('/api/mount',{method:'POST'})); await loadAll()}catch(e){log(e.message)}}
async function ejectSwitch(){if(!confirm('Sync and eject the detected Switch USB storage?')) return; try{log(await j('/api/eject',{method:'POST'})); await loadAll()}catch(e){log(e.message)}}
document.getElementById('upload-form').addEventListener('submit', async e=>{e.preventDefault(); try{const fd=new FormData(e.target); const r=await fetch('/api/upload',{method:'POST',body:fd}); log(await r.json()); await loadAll()}catch(err){log(err.message)}});
loadAll(); setInterval(loadAll, 15000);
</script></body></html>'''


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve Rocky completed transfers and Switch UMS controls over LAN.")
    parser.add_argument("--root", default=os.environ.get("SWITCH_TRANSFER_ROOT", DEFAULT_ROOT))
    parser.add_argument("--host", default=os.environ.get("SWITCH_TRANSFER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SWITCH_TRANSFER_PORT", "8077")))
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        raise SystemExit(f"Switch Transfer root does not exist or is not a directory: {root}")

    server = ThreadingHTTPServer((args.host, args.port), SwitchTransferHandler)
    server.state = SwitchTransferState(root)  # type: ignore[attr-defined]
    print(f"Switch Transfer serving {root} on http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
