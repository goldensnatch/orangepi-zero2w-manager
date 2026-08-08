#!/usr/bin/env python3
"""Small Rocky control-plane service for NS-USBloader.

The service exposes a LAN/control-plane UI and JSON API that can launch
NS-USBloader CLI jobs against files rooted under Rocky's completed-transfer
folder. It intentionally does not expose arbitrary filesystem paths.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

TRANSFER_ROOT = Path(os.environ.get("ROCKY_TRANSFER_COMPLETE", "/mnt/rocky-transfer/complete")).resolve()
NS_USBLOADER_JAR = Path(os.environ.get("NS_USBLOADER_JAR", "/opt/zero2w-manager/runtime/ns-usbloader/ns-usbloader.jar")).resolve()
HOST = os.environ.get("ROCKY_NS_USBLOADER_HOST", "0.0.0.0")
PORT = int(os.environ.get("ROCKY_NS_USBLOADER_PORT", "8078"))
ALLOWED_SUFFIXES = {".nsp", ".nsz", ".xci", ".xcz", ".nro", ".bin"}
MAX_LIST_ITEMS = 500


@dataclass
class Job:
    id: str
    mode: str
    command: list[str]
    created_at: float = field(default_factory=time.time)
    status: str = "running"
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    completed_at: float | None = None


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def _safe_relative(raw: str) -> Path:
    decoded = unquote(raw or "").strip().lstrip("/")
    rel = Path(decoded)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        raise ValueError("path must be relative to the Rocky completed-transfer folder")
    return rel


def _resolve_completed(raw: str) -> Path:
    rel = _safe_relative(raw)
    target = (TRANSFER_ROOT / rel).resolve()
    if not str(target).startswith(str(TRANSFER_ROOT)):
        raise ValueError("path escapes transfer root")
    if not target.exists() or not target.is_file():
        raise ValueError(f"file does not exist: {rel.as_posix()}")
    if target.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ValueError(f"unsupported file suffix for NS-USBloader: {target.suffix}")
    return target


def _resolve_completed_selection(raw: str) -> list[Path]:
    rel = _safe_relative(raw)
    target = (TRANSFER_ROOT / rel).resolve()
    if not str(target).startswith(str(TRANSFER_ROOT)):
        raise ValueError("path escapes transfer root")
    if not target.exists():
        raise ValueError(f"selection does not exist: {rel.as_posix()}")
    if target.is_file():
        return [_resolve_completed(raw)]
    if target.is_dir():
        files = sorted(
            (child for child in target.rglob("*") if child.is_file() and child.suffix.lower() in ALLOWED_SUFFIXES),
            key=lambda child: child.as_posix().lower(),
        )
        if not files:
            raise ValueError(f"folder contains no supported package files: {rel.as_posix()}")
        return files
    raise ValueError(f"selection is not a file or folder: {rel.as_posix()}")


def _directory_has_eligible_files(path: Path) -> bool:
    try:
        return any(child.is_file() and child.suffix.lower() in ALLOWED_SUFFIXES for child in path.rglob("*"))
    except OSError:
        return False


def _lsusb_text() -> str:
    if not shutil.which("lsusb"):
        return ""
    try:
        return subprocess.run(["lsusb"], capture_output=True, text=True, check=False, timeout=5).stdout
    except Exception:
        return ""


def service_status() -> dict[str, object]:
    java_path = shutil.which("java")
    java_version = "missing"
    if java_path:
        try:
            proc = subprocess.run([java_path, "-version"], capture_output=True, text=True, check=False, timeout=5)
            java_version = (proc.stderr or proc.stdout or "").splitlines()[0] if (proc.stderr or proc.stdout) else "unknown"
        except Exception as exc:
            java_version = f"error: {exc}"
    lsusb = _lsusb_text().lower()
    return {
        "healthy": bool(java_path and NS_USBLOADER_JAR.exists() and TRANSFER_ROOT.exists()),
        "java_path": java_path,
        "java_version": java_version,
        "jar": str(NS_USBLOADER_JAR),
        "jar_exists": NS_USBLOADER_JAR.exists(),
        "jar_size": NS_USBLOADER_JAR.stat().st_size if NS_USBLOADER_JAR.exists() else 0,
        "transfer_root": str(TRANSFER_ROOT),
        "transfer_root_exists": TRANSFER_ROOT.exists(),
        "switch_usb_detected": "057e:3000" in lsusb or "nintendo" in lsusb,
        "rcm_detected": "0955:7321" in lsusb or "nvidia" in lsusb,
        "lawful_use": "Homebrew, personal media, saves, patches, and lawful personal backups only.",
    }


def list_files(rel: str = "") -> dict[str, object]:
    base_rel = _safe_relative(rel)
    base = (TRANSFER_ROOT / base_rel).resolve()
    if not str(base).startswith(str(TRANSFER_ROOT)):
        raise ValueError("path escapes transfer root")
    if not base.exists():
        raise ValueError("folder does not exist")
    if not base.is_dir():
        raise ValueError("path is not a folder")
    items: list[dict[str, object]] = []
    for child in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:MAX_LIST_ITEMS]:
        rel_child = child.relative_to(TRANSFER_ROOT).as_posix()
        stat = child.stat()
        eligible = (child.is_file() and child.suffix.lower() in ALLOWED_SUFFIXES) or (child.is_dir() and _directory_has_eligible_files(child))
        items.append({
            "name": child.name,
            "path": rel_child,
            "kind": "directory" if child.is_dir() else "file",
            "size": stat.st_size if child.is_file() else None,
            "mtime": int(stat.st_mtime),
            "eligible": eligible,
        })
    return {"root": str(TRANSFER_ROOT), "path": base_rel.as_posix(), "items": items}


def _run_job(job: Job) -> None:
    try:
        proc = subprocess.run(job.command, capture_output=True, text=True, check=False, timeout=None)
        job.returncode = proc.returncode
        job.stdout = (proc.stdout or "")[-20000:]
        job.stderr = (proc.stderr or "")[-20000:]
        job.status = "completed" if proc.returncode == 0 else "failed"
    except Exception as exc:
        job.status = "failed"
        job.stderr = str(exc)
    finally:
        job.completed_at = time.time()


def start_install_job(payload: dict[str, object]) -> Job:
    status = service_status()
    if not status["healthy"]:
        raise ValueError("NS-USBloader is not installed/healthy; run scripts/ns_usbloader_install.py on the OPi")
    mode = str(payload.get("mode") or "network").strip().lower()
    paths = payload.get("paths") or payload.get("path")
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not paths:
        raise ValueError("paths is required")
    files = []
    for selected in paths:
        files.extend(_resolve_completed_selection(str(selected)))
    java = shutil.which("java") or "java"
    cmd = [java, "-jar", str(NS_USBLOADER_JAR)]
    if mode in {"network", "awoo-network", "tfn"}:
        nsip = str(payload.get("nsip") or "").strip()
        if not nsip:
            raise ValueError("nsip is required for network mode")
        cmd += ["-n", f"nsip={nsip}"]
        hostip = str(payload.get("hostip") or "").strip()
        if hostip:
            cmd.append(f"hostip={hostip}")
    elif mode in {"usb", "awoo-usb", "tinfoil"}:
        cmd += ["-t"]
    elif mode == "goldleaf":
        version = str(payload.get("version") or "v1.2.0").strip()
        cmd += ["-g", f"ver={version}"]
    else:
        raise ValueError("mode must be network, usb, or goldleaf")
    cmd += [str(p) for p in files]
    job = Job(id=secrets.token_hex(8), mode=mode, command=cmd)
    with JOBS_LOCK:
        JOBS[job.id] = job
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return job


def job_payload(job: Job) -> dict[str, object]:
    redacted = ["java", "-jar", str(NS_USBLOADER_JAR.name)] + job.command[3:]
    return {
        "id": job.id,
        "mode": job.mode,
        "status": job.status,
        "returncode": job.returncode,
        "created_at": int(job.created_at),
        "completed_at": int(job.completed_at) if job.completed_at else None,
        "command": redacted,
        "stdout": job.stdout,
        "stderr": job.stderr,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "RockyNSUSBLoader/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"ok": False, "error": message}, status)

    def _read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ValueError("request body too large")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        request = urlparse(self.path)
        try:
            if request.path in {"/", "/index.html"}:
                self._html()
            elif request.path in {"/__health", "/api/status"}:
                self._json(service_status())
            elif request.path == "/api/files":
                query = parse_qs(request.query)
                self._json(list_files(query.get("path", [""])[0]))
            elif request.path.startswith("/api/jobs/"):
                job_id = request.path.rsplit("/", 1)[-1]
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                if not job:
                    self._error(HTTPStatus.NOT_FOUND, "job not found")
                else:
                    self._json(job_payload(job))
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:
        request = urlparse(self.path)
        try:
            payload = self._read_json()
            if request.path == "/api/install":
                job = start_install_job(payload)
                self._json({"ok": True, "job": job_payload(job)}, HTTPStatus.ACCEPTED)
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def _html(self) -> None:
        status = service_status()
        files = []
        try:
            files = list_files("")["items"]
        except Exception:
            files = []
        eligible_count = sum(1 for item in files if item.get("eligible"))
        rows = "".join(
            f"<label title='{'Contains supported package files' if item.get('eligible') else 'No .nsp/.nsz/.xci/.xcz/.nro/.bin files found here'}'><input type='checkbox' value='{html.escape(str(item['path']))}' {'disabled' if not item.get('eligible') else ''}> "
            f"{html.escape(str(item['name']))} <small>{html.escape(str(item['kind']))}{' · selectable' if item.get('eligible') else ' · no supported package files'}</small></label>"
            for item in files[:100]
        ) or "<p>No completed transfer folders/files found.</p>"
        if files and eligible_count == 0:
            rows += "<p><strong>No selectable package files found yet.</strong> NS-USBloader only accepts completed .nsp/.nsz/.xci/.xcz/.nro/.bin files. Archive parts such as .rar/.r00/.r01 must be unpacked into a lawful personal backup/package before they can be sent.</p>"
        body = f"""<!doctype html>
<html><head><meta charset='utf-8'><title>Rocky NS-USBloader</title>
<style>
body{{font-family:system-ui,sans-serif;background:#060a0f;color:#e2e8f0;margin:2rem;}}
.card{{border:1px solid #1f2937;background:#0b1220;border-radius:14px;padding:1rem;margin:1rem 0;}}
.ok{{color:#22c55e}} .bad{{color:#ef4444}} small{{color:#94a3b8}} input,button{{font:inherit;margin:.25rem;}}
button{{background:#00d4ff;color:#001018;border:0;border-radius:8px;padding:.5rem .75rem;font-weight:700;}}
.files label{{display:block;padding:.35rem;border-bottom:1px solid #172033;}}
pre{{white-space:pre-wrap;background:#020617;padding:1rem;border-radius:10px;}}
</style></head><body>
<h1>NS-USBloader for Switch Transfer</h1>
<p><strong>Boundary:</strong> homebrew, personal media, saves, patches, and lawful personal backups only.</p>
<div class='card'>
<h2>Status</h2>
<p class='{ 'ok' if status['healthy'] else 'bad' }'>{'Healthy' if status['healthy'] else 'Needs install/check'}</p>
<ul>
<li>Jar: {html.escape(str(status['jar']))} ({'present' if status['jar_exists'] else 'missing'})</li>
<li>Java: {html.escape(str(status['java_version']))}</li>
<li>Switch USB: {status['switch_usb_detected']}</li>
<li>Transfer root: {html.escape(str(status['transfer_root']))}</li>
</ul></div>
<div class='card'><h2>Launch install job</h2>
<p>Network mode requires Awoo-compatible network install listening on the Switch IP. USB mode requires the Switch installer USB screen and udev access.</p>
<label>Mode <select id='mode'><option value='network'>Awoo Network</option><option value='usb'>Awoo USB</option><option value='goldleaf'>Goldleaf USB</option></select></label>
<label>Switch IP <input id='nsip' placeholder='192.168.1.x'></label>
<div class='files'>{rows}</div>
<button onclick='startJob()'>Start NS-USBloader</button>
<pre id='out'></pre></div>
<script>
async function startJob(){{
  const paths=[...document.querySelectorAll('input[type=checkbox]:checked')].map(x=>x.value);
  const payload={{mode:document.getElementById('mode').value, nsip:document.getElementById('nsip').value, paths}};
  const r=await fetch('/api/install',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});
  const j=await r.json(); document.getElementById('out').textContent=JSON.stringify(j,null,2);
  if(j.job) poll(j.job.id);
}}
async function poll(id){{
  const r=await fetch('/api/jobs/'+id); const j=await r.json(); document.getElementById('out').textContent=JSON.stringify(j,null,2);
  if(j.status==='running') setTimeout(()=>poll(id),2000);
}}
</script></body></html>"""
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Rocky NS-USBloader control service")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Rocky NS-USBloader control listening on http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
