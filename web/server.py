"""RegisterHub Lite Web Server (Zero-Dependency Python Stdlib HTTP + SSE).

Strictly scoped to Community Edition workflows:
  - Environment diagnostics (`core.doctor`)
  - Single-thread adapter execution + live SSE log stream
  - Adapter scaffolding (`core.scaffold`)
  - Local accounts table + CSV / JSON / Bitwarden export (`core.exporter`)
  - Single-window geometry calibration (`core.calibrator`)
"""

from __future__ import annotations

import atexit
import collections
import json
import mimetypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

STATIC_DIR = Path(__file__).resolve().parent / "static"
RESULTS_DIR = ROOT_DIR / "results"
ACCOUNTS_FILE = RESULTS_DIR / "accounts.txt"
GEOMETRY_FILE = ROOT_DIR / "config" / "window_geometry.json"

_SAFE_SITE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,48}$")


class _LiteJob:
    def __init__(self, job_id: str, adapter: str, count: int) -> None:
        self.id = job_id
        self.adapter = adapter
        self.count = count
        self.status = "running"
        self.pid: Optional[int] = None
        self.returncode: Optional[int] = None
        self.proc: Optional[subprocess.Popen[str]] = None
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.logs: collections.deque[Dict[str, str]] = collections.deque(maxlen=1000)
        self._lock = threading.Lock()
        self._subs: List[queue.Queue[Optional[Dict[str, str]]]] = []

    def append(self, text: str, level: str = "info") -> None:
        item = {"ts": time.strftime("%H:%M:%S"), "level": level, "text": text}
        with self._lock:
            self.logs.append(item)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(item)
            except queue.Full:
                pass

    def finish(self, status: str, rc: Optional[int]) -> None:
        self.status = status
        self.returncode = rc
        self.finished_at = time.time()
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass

    def subscribe(self):
        q: queue.Queue[Optional[Dict[str, str]]] = queue.Queue(maxsize=300)
        with self._lock:
            hist = list(self.logs)
            if self.status == "running":
                self._subs.append(q)
            else:
                q.put_nowait(None)
        return hist, q

    def unsubscribe(self, q) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "adapter": self.adapter,
            "count": self.count,
            "status": self.status,
            "pid": self.pid,
            "returncode": self.returncode,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at)),
            "duration_s": round((self.finished_at or time.time()) - self.started_at, 1),
        }


class _LiteJobManager:
    """Single-thread job manager with exact PID lifecycle cleanup."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, _LiteJob] = {}
        self._seq = 0
        atexit.register(self.stop_all)

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = sorted(self._jobs.values(), key=lambda x: x.started_at, reverse=True)
            return [j.to_dict() for j in items[:20]]

    def get(self, job_id: str) -> Optional[_LiteJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, adapter: str, count: int = 1) -> _LiteJob:
        cmd = [sys.executable, "-u", str(ROOT_DIR / "main.py"), "--run", adapter, "--count", str(count)]
        with self._lock:
            self._seq += 1
            jid = f"lite_{int(time.time())}_{self._seq}"
            job = _LiteJob(jid, adapter, count)
            self._jobs[jid] = job

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        job.proc = proc
        job.pid = proc.pid
        job.append(f"[RegisterHub] Started single-thread run: {adapter} (count={count}, PID={proc.pid})")

        def _reader() -> None:
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    line = raw.rstrip("\r\n")
                    low = line.lower()
                    lvl = "error" if ("[-]" in line or "error" in low or "traceback" in low) else (
                        "ok" if ("[+]" in line or "success" in low) else "info"
                    )
                    job.append(line, lvl)
                rc = proc.wait()
                if job.status == "running":
                    job.finish("done" if rc == 0 else "failed", rc)
            except Exception as e:
                job.append(f"[RegisterHub] Stream error: {e}", "error")
                if job.status == "running":
                    job.finish("failed", -1)

        threading.Thread(target=_reader, daemon=True).start()
        return job

    def stop(self, job_id: str) -> Dict[str, Any]:
        job = self.get(job_id)
        if not job:
            return {"ok": False, "error": f"Unknown job: {job_id}"}
        if job.status != "running" or job.proc is None:
            return {"ok": True, "status": job.status}
        job.status = "stopped"
        job.proc.terminate()
        try:
            job.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            job.proc.kill()
        job.append(f"[RegisterHub] Stopped by user (PID={job.pid})", "error")
        job.finish("stopped", job.proc.returncode)
        return {"ok": True, "status": "stopped"}

    def stop_all(self) -> None:
        with self._lock:
            running = [j.id for j in self._jobs.values() if j.status == "running"]
        for jid in running:
            self.stop(jid)


job_manager = _LiteJobManager()


def collect_doctor_report(include_network: bool = True) -> Dict[str, Any]:
    """Run real executable checks from core.doctor and serialize for Lite UI."""
    from core import doctor

    checks = [
        doctor._check_python_version(),
        doctor._check_module("playwright", "playwright", "browser engine"),
        doctor._check_module("requests", "requests", "HTTP client"),
        doctor._check_module("PIL", "pillow", "image handling"),
        doctor._check_module("numpy", "numpy", "vision math"),
        doctor._check_opencv_functional(),
        *doctor._check_browser_binaries(),
        doctor._check_config(),
        doctor._check_write_permission(str(RESULTS_DIR)),
    ]
    if include_network:
        checks.append(doctor._check_network(timeout=2.5))

    rows = []
    blockers = 0
    warnings = 0
    for c in checks:
        state = "pass" if c.ok else ("warn" if c.warn_only else "fail")
        if state == "fail":
            blockers += 1
        elif state == "warn":
            warnings += 1
        rows.append({
            "name": c.name,
            "state": state,
            "detail": c.detail,
            "hint": c.hint or "",
        })
    return {
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ready": blockers == 0,
        "blockers": blockers,
        "warnings": warnings,
        "checks": rows,
    }


def list_community_adapters() -> List[Dict[str, str]]:
    from sites import discover_adapters

    adapters = discover_adapters()
    out: List[Dict[str, str]] = []
    for name, cls in sorted(adapters.items()):
        url = ""
        try:
            attr = getattr(cls, "target_url", None)
            if callable(attr):
                url = str(attr(None) or "")
            elif isinstance(attr, property) and attr.fget:
                url = str(attr.fget(None) or "")
            elif attr:
                url = str(attr)
        except Exception:
            url = ""
        out.append({
            "name": name,
            "target_url": url if len(url) <= 120 else (url[:80] + "… [embedded]"),
        })
    return out


class LiteRequestHandler(BaseHTTPRequestHandler):
    server_version = "RegisterHubLite/1.2"

    def log_message(self, format: str, *args: Any) -> None:
        return  # Keep console clean

    def _send_json(self, payload: Any, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > 2_000_000:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _serve_static(self, rel_path: str) -> None:
        clean = rel_path.lstrip("/") or "index.html"
        target = (STATIC_DIR / clean).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return
        ctype, _ = mimetypes.guess_type(str(target))
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == "/api/meta":
            self._send_json({
                "edition": "community",
                "brand": "RegisterHub Lite",
                "subtitle": "Open-Source Automation Workbench",
                "version": "1.2.0",
                "concurrency_mode": "single-thread",
            })
            return

        if path == "/api/doctor":
            self._send_json(collect_doctor_report(include_network=True))
            return

        if path == "/api/adapters":
            self._send_json({"adapters": list_community_adapters()})
            return

        if path == "/api/jobs":
            self._send_json({"jobs": job_manager.list_jobs()})
            return

        m_stream = re.fullmatch(r"/api/jobs/([A-Za-z0-9_.-]+)/stream", path)
        if m_stream:
            job = job_manager.get(m_stream.group(1))
            if not job:
                self._send_json({"error": "Job not found"}, status=404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            hist, sub_q = job.subscribe()
            try:
                for item in hist:
                    chunk = f"data: {json.dumps({'type': 'log', **item}, ensure_ascii=False)}\n\n"
                    self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        ev = sub_q.get(timeout=12.0)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if ev is None:
                        end_chunk = f"data: {json.dumps({'type': 'status', 'status': job.status, 'returncode': job.returncode})}\n\n"
                        self.wfile.write(end_chunk.encode("utf-8"))
                        self.wfile.flush()
                        break
                    chunk = f"data: {json.dumps({'type': 'log', **ev}, ensure_ascii=False)}\n\n"
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                job.unsubscribe(sub_q)
            return

        if path == "/api/accounts":
            from core.exporter import parse_accounts_file

            accounts = parse_accounts_file(ACCOUNTS_FILE)
            self._send_json({"total": len(accounts), "accounts": accounts[::-1]})
            return

        if path == "/api/layout":
            if GEOMETRY_FILE.exists():
                try:
                    data = json.loads(GEOMETRY_FILE.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            else:
                data = {}
            self._send_json({"geometry": data})
            return

        if path == "/" or path == "/index.html":
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return
        self._serve_static("index.html")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._read_json()

        if path == "/api/run":
            adapter = str(body.get("adapter") or "").strip().lower()
            try:
                count = max(1, min(20, int(body.get("count") or 1)))
            except Exception:
                count = 1
            known = {a["name"] for a in list_community_adapters()}
            if adapter not in known:
                self._send_json({"ok": False, "error": f"Unknown adapter: {adapter}"}, status=400)
                return
            job = job_manager.start(adapter, count=count)
            self._send_json({"ok": True, "job": job.to_dict()})
            return

        m_stop = re.fullmatch(r"/api/jobs/([A-Za-z0-9_.-]+)/stop", path)
        if m_stop:
            res = job_manager.stop(m_stop.group(1))
            self._send_json(res, status=200 if res.get("ok") else 404)
            return

        if path == "/api/scaffold":
            site_name = str(body.get("site_name") or "").strip().lower()
            target_url = str(body.get("url") or "").strip()
            if not _SAFE_SITE_RE.fullmatch(site_name):
                self._send_json({"ok": False, "error": "Invalid site name (alphanumeric/underscore/hyphen only)"}, status=400)
                return
            try:
                from core.scaffold import scaffold_site

                out_path = scaffold_site(site_name, target_url or None)
                self._send_json({"ok": True, "site": site_name, "path": str(out_path)})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
            return

        m_exp = re.fullmatch(r"/api/export/(csv|json|bitwarden)", path)
        if m_exp:
            fmt = m_exp.group(1)
            try:
                from core.exporter import run_export

                out_path = run_export(fmt=fmt, input_file=str(ACCOUNTS_FILE), out_dir=str(RESULTS_DIR))
                content = out_path.read_text(encoding="utf-8", errors="replace")
                self._send_json({"ok": True, "format": fmt, "filename": out_path.name, "content": content})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
            return

        if path == "/api/layout/auto":
            try:
                from core.calibrator import auto_generate_layout

                geo = auto_generate_layout(save_path=GEOMETRY_FILE)
                self._send_json({"ok": True, "geometry": geo})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
            return

        self._send_json({"error": "Not Found"}, status=404)


def create_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), LiteRequestHandler)


def start_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Start RegisterHub Lite Web Workbench on loopback."""
    srv = create_server(host=host, port=port)
    print("=" * 64)
    print(f"  RegisterHub Lite Workbench -> http://{host}:{port}")
    print("  Open-Source Single-Thread Edition (Press Ctrl+C to stop)")
    print("=" * 64, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        job_manager.stop_all()
        srv.server_close()


if __name__ == "__main__":
    start_server()
