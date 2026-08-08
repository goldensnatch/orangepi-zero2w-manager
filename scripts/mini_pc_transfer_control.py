#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import shlex
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
CONFIG_PATH = Path(os.environ.get("MINI_PC_TRANSFER_CONFIG", "/opt/zero2w-manager/runtime/config/mini-pc-transfer.json"))
DEFAULT_KEY = Path(os.environ.get("MINI_PC_TRANSFER_KEY", "/home/orangepi/.ssh/rocky_mini_pc_ed25519"))
HOST = os.environ.get("MINI_PC_TRANSFER_HOST", "0.0.0.0")
PORT = int(os.environ.get("MINI_PC_TRANSFER_PORT", "8079"))
MAX_LIST_ITEMS = 500

DEFAULT_CONFIG = {
    "host": "192.168.1.141",
    "user": "hayden",
    "port": 22,
    "destination": "H:/Rocky-Export",
    "ssh_key": str(DEFAULT_KEY),
}

@dataclass
class Job:
    id: str
    paths: list[str]
    command: list[str]
    created_at: float = field(default_factory=time.time)
    status: str = "running"
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    completed_at: float | None = None

JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def run(cmd: list[str], *, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)


def ensure_key() -> None:
    key = DEFAULT_KEY
    if key.exists() and key.with_suffix(key.suffix + ".pub").exists():
        return
    key.parent.mkdir(parents=True, exist_ok=True)
    run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-C", "rocky-mini-pc-transfer"], timeout=30)


def load_config() -> dict[str, object]:
    config = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            loaded = json.loads(CONFIG_PATH.read_text())
            if isinstance(loaded, dict):
                config.update(loaded)
    except Exception:
        pass
    return config


def save_config(config: dict[str, object]) -> None:
    current = load_config()
    for key in ["host", "user", "destination", "ssh_key"]:
        if key in config:
            current[key] = str(config[key]).strip()
    if "port" in config:
        current["port"] = int(config["port"])
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(current, indent=2) + "\n")


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


def safe_rel_path(raw: str) -> Path:
    decoded = unquote(raw or "").replace("\\", "/").strip().lstrip("/")
    rel = Path(decoded)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        raise ValueError("path must stay inside Rocky completed-transfer folder")
    return rel


def resolve_source(raw: str) -> Path:
    rel = safe_rel_path(raw)
    target = (TRANSFER_ROOT / rel).resolve()
    if TRANSFER_ROOT not in [target, *target.parents]:
        raise ValueError("path escapes transfer root")
    if not target.exists():
        raise ValueError(f"source does not exist: {rel.as_posix()}")
    return target


def list_files(rel_raw: str = "") -> dict[str, object]:
    rel = safe_rel_path(rel_raw)
    base = (TRANSFER_ROOT / rel).resolve()
    if TRANSFER_ROOT not in [base, *base.parents]:
        raise ValueError("path escapes transfer root")
    if not base.is_dir():
        raise ValueError("path is not a folder")
    items: list[dict[str, object]] = []
    for child in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:MAX_LIST_ITEMS]:
        st = child.stat()
        size = directory_size(child) if child.is_dir() else st.st_size
        items.append({
            "name": child.name,
            "path": child.relative_to(TRANSFER_ROOT).as_posix(),
            "kind": "directory" if child.is_dir() else "file",
            "size": size,
            "size_human": human_size(size),
            "mtime": int(st.st_mtime),
        })
    return {"root": str(TRANSFER_ROOT), "path": rel.as_posix(), "items": items}


def windows_dest_for_scp(dest: str) -> str:
    cleaned = dest.strip().replace("\\", "/")
    if len(cleaned) >= 2 and cleaned[1] == ":":
        return "/" + cleaned
    return cleaned


def status_payload() -> dict[str, object]:
    ensure_key()
    config = load_config()
    key = Path(str(config.get("ssh_key") or DEFAULT_KEY)).expanduser()
    pub = key.with_suffix(key.suffix + ".pub")
    ssh = shutil.which("ssh")
    scp = shutil.which("scp")
    reachable = False
    auth_ok = False
    auth_error = "not checked"
    if ssh and key.exists():
        host = str(config.get("host") or "")
        user = str(config.get("user") or "")
        port = str(config.get("port") or 22)
        if host and user:
            probe = run([ssh, "-i", str(key), "-p", port, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", f"{user}@{host}", "echo", "ROCKY_SSH_OK"], timeout=8)
            reachable = probe.returncode == 0 or "Permission denied" in (probe.stderr + probe.stdout) or "publickey" in (probe.stderr + probe.stdout).lower()
            auth_ok = probe.returncode == 0 and "ROCKY_SSH_OK" in probe.stdout
            auth_error = (probe.stderr or probe.stdout or "").strip()[-500:]
    return {
        "healthy": bool(TRANSFER_ROOT.exists() and ssh and scp and key.exists()),
        "transfer_root": str(TRANSFER_ROOT),
        "transfer_root_exists": TRANSFER_ROOT.exists(),
        "config": config,
        "ssh": ssh,
        "scp": scp,
        "key_exists": key.exists(),
        "public_key": pub.read_text().strip() if pub.exists() else "",
        "reachable_or_auth_prompted": reachable,
        "auth_ok": auth_ok,
        "auth_error": auth_error,
        "safety": "Transfers only move files you select from Rocky's completed-transfer folder.",
    }


def make_transfer_command(paths: list[str]) -> list[str]:
    cfg = load_config()
    key = str(cfg.get("ssh_key") or DEFAULT_KEY)
    port = str(cfg.get("port") or 22)
    user = str(cfg.get("user") or "").strip()
    host = str(cfg.get("host") or "").strip()
    dest = windows_dest_for_scp(str(cfg.get("destination") or "").strip()).rstrip("/") + "/"
    if not user or not host or not dest:
        raise ValueError("mini-PC host/user/destination config is incomplete")
    sources = [str(resolve_source(p)) for p in paths]
    return ["scp", "-r", "-p", "-i", key, "-P", port, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new", *sources, f"{user}@{host}:{dest}"]


def run_job(job: Job) -> None:
    try:
        proc = subprocess.run(job.command, capture_output=True, text=True, check=False, timeout=None)
        job.returncode = proc.returncode
        job.stdout = (proc.stdout or "")[-40000:]
        job.stderr = (proc.stderr or "")[-40000:]
        job.status = "completed" if proc.returncode == 0 else "failed"
    except Exception as exc:
        job.status = "failed"
        job.stderr = str(exc)
    finally:
        job.completed_at = time.time()


def start_transfer(payload: dict[str, object]) -> Job:
    raw_paths = payload.get("paths") or payload.get("path")
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValueError("paths is required")
    paths = [str(p) for p in raw_paths]
    cmd = make_transfer_command(paths)
    job = Job(id=secrets.token_hex(8), paths=paths, command=cmd)
    with JOBS_LOCK:
        JOBS[job.id] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job


def job_payload(job: Job) -> dict[str, object]:
    cmd = list(job.command)
    if "-i" in cmd:
        idx = cmd.index("-i")
        if idx + 1 < len(cmd):
            cmd[idx + 1] = "[ssh-key]"
    return {
        "id": job.id,
        "paths": job.paths,
        "status": job.status,
        "returncode": job.returncode,
        "created_at": int(job.created_at),
        "completed_at": int(job.completed_at) if job.completed_at else None,
        "command": cmd,
        "stdout": job.stdout,
        "stderr": job.stderr,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "RockyMiniPCTransfer/1.0"

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
        req = urlparse(self.path)
        try:
            if req.path in {"/", "/index.html"}:
                self._html()
            elif req.path in {"/__health", "/api/status"}:
                self._json(status_payload())
            elif req.path == "/api/files":
                q = parse_qs(req.query)
                self._json(list_files(q.get("path", [""])[0]))
            elif req.path == "/api/jobs":
                with JOBS_LOCK:
                    jobs = [job_payload(j) for j in JOBS.values()]
                self._json({"jobs": sorted(jobs, key=lambda x: x["created_at"], reverse=True)[:20]})
            elif req.path.startswith("/api/jobs/"):
                job_id = req.path.rsplit("/", 1)[-1]
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
        req = urlparse(self.path)
        try:
            payload = self._read_json()
            if req.path == "/api/config":
                save_config(payload)
                self._json({"ok": True, "status": status_payload()})
            elif req.path == "/api/transfer":
                job = start_transfer(payload)
                self._json({"ok": True, "job": job_payload(job)}, HTTPStatus.ACCEPTED)
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def _html(self) -> None:
        status = status_payload()
        try:
            files = list_files("")["items"]
        except Exception:
            files = []
        rows = "".join(
            f"<label><input type='checkbox' value='{html.escape(str(item['path']))}'> "
            f"{html.escape(str(item['name']))} <small>{html.escape(str(item['kind']))} · {html.escape(str(item['size_human']))}</small></label>"
            for item in files[:100]
        ) or "<p>No completed transfer files/folders found.</p>"
        cfg = status["config"]
        body = f"""<!doctype html><html><head><meta charset='utf-8'><title>Rocky Mini-PC Transfer</title>
<style>body{{font-family:system-ui,sans-serif;background:#060a0f;color:#e2e8f0;margin:2rem}}.card{{border:1px solid #1f2937;background:#0b1220;border-radius:14px;padding:1rem;margin:1rem 0}}.ok{{color:#22c55e}}.bad{{color:#ef4444}}small{{color:#94a3b8}}input,button{{font:inherit;margin:.25rem}}input[type=text]{{min-width:18rem}}button{{background:#00d4ff;color:#001018;border:0;border-radius:8px;padding:.5rem .75rem;font-weight:700}}.files label{{display:block;padding:.35rem;border-bottom:1px solid #172033}}pre{{white-space:pre-wrap;background:#020617;padding:1rem;border-radius:10px;overflow:auto}}</style></head><body>
<h1>Rocky → Mini-PC Transfer</h1>
<p>Moves selected completed-transfer files/folders from Rocky to the mini-PC over SSH/SCP. No auto-delete.</p>
<div class='card'><h2>Status</h2><p class='{'ok' if status['healthy'] else 'bad'}'>{'Healthy' if status['healthy'] else 'Needs check'}</p><ul><li>Mini-PC: {html.escape(str(cfg.get('user')))}@{html.escape(str(cfg.get('host')))}:{html.escape(str(cfg.get('destination')))}</li><li>SSH auth: {'OK' if status['auth_ok'] else 'not ready yet'}</li><li>Transfer root: {html.escape(str(status['transfer_root']))}</li></ul><details><summary>Rocky public key to add to Windows authorized_keys</summary><pre>{html.escape(str(status['public_key']))}</pre></details></div>
<div class='card'><h2>Config</h2><label>Host <input id='host' value='{html.escape(str(cfg.get('host')))}'></label><label>User <input id='user' value='{html.escape(str(cfg.get('user')))}'></label><label>Dest <input id='dest' value='{html.escape(str(cfg.get('destination')))}'></label><button onclick='saveConfig()'>Save config</button></div>
<div class='card'><h2>Select exports</h2><div class='files'>{rows}</div><button onclick='startTransfer()'>Transfer selected to mini-PC</button><pre id='out'></pre></div>
<script>
async function j(url,opts){{const r=await fetch(url,opts); const data=await r.json(); if(!r.ok) throw new Error(data.error||r.statusText); return data}}
function log(x){{document.getElementById('out').textContent=typeof x==='string'?x:JSON.stringify(x,null,2)}}
async function saveConfig(){{try{{log(await j('/api/config',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{host:host.value,user:user.value,destination:dest.value}})}}))}}catch(e){{log(e.message)}}}}
async function startTransfer(){{try{{const paths=[...document.querySelectorAll('input[type=checkbox]:checked')].map(x=>x.value); if(!paths.length) throw new Error('Select at least one file/folder'); const data=await j('/api/transfer',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{paths}})}}); log(data); if(data.job) poll(data.job.id)}}catch(e){{log(e.message)}}}}
async function poll(id){{const data=await j('/api/jobs/'+id); log(data); if(data.status==='running') setTimeout(()=>poll(id),2000)}}
</script></body></html>"""
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Rocky to Mini-PC transfer control service")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Rocky Mini-PC Transfer listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
