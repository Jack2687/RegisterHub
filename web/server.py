"""RegisterHub Lite Web Server (Zero-Dependency Python Stdlib HTTP + SSE).

Strictly scoped to Community Edition single-thread workflows:
  - Environment diagnostics (`core.doctor`)
  - Runtime config editor (`config.json`, atomic save)
  - Strict single-thread adapter execution + clean ANSI-free SSE stream + process-tree kill + persisted logs
  - Adapter scaffolding with optional DOM FormInspector probe (`core.scaffold`)
  - Local accounts table (with API Key column & reveal/copy) + CSV / JSON / Bitwarden export (`core.exporter`)
  - Failure snapshots viewer (`results/snapshots/`)
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
JOBS_DIR = RESULTS_DIR / "jobs"
SNAPSHOTS_DIR = RESULTS_DIR / "snapshots"
CONFIG_FILE = ROOT_DIR / "config.json"
GEOMETRY_FILE = ROOT_DIR / "config" / "window_geometry.json"

_SAFE_SITE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,48}$")
_SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _kill_process_tree(pid: Optional[int], proc: Optional[subprocess.Popen[str]] = None) -> None:
    """Terminate child process tree cleanly so Playwright/Chrome windows never leak."""
    if pid and os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        except Exception:
            pass
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


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
        self.logs: collections.deque[Dict[str, str]] = collections.deque(maxlen=1500)
        self._lock = threading.Lock()
        self._subs: List[queue.Queue[Optional[Dict[str, str]]]] = []

    def append(self, text: str, level: str = "info") -> None:
        clean = _ANSI_RE.sub("", text)
        item = {"ts": time.strftime("%H:%M:%S"), "level": level, "text": clean}
        with self._lock:
            self.logs.append(item)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(item)
            except queue.Full:
                pass

    def finish(self, status: str, rc: Optional[int]) -> None:
        with self._lock:
            if self.status != "running" and status != "stopped":
                return
            self.status = status
            self.returncode = rc
            self.finished_at = time.time()
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        self._persist()

    def _persist(self) -> None:
        try:
            JOBS_DIR.mkdir(parents=True, exist_ok=True)
            meta_p = JOBS_DIR / f"{self.id}.json"
            log_p = JOBS_DIR / f"{self.id}.log"
            tmp_m = meta_p.with_suffix(".json.tmp")
            tmp_m.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_m, meta_p)
            with self._lock:
                lines = [f"[{x['ts']}] [{x['level'].upper()}] {x['text']}" for x in self.logs]
            log_p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        except Exception:
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
        with self._lock:
            st = self.status
            rc = self.returncode
            fin = self.finished_at
            n_logs = len(self.logs)
        return {
            "id": self.id,
            "adapter": self.adapter,
            "count": self.count,
            "status": st,
            "pid": self.pid,
            "returncode": rc,
            "log_lines": n_logs,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at)),
            "duration_s": round((fin or time.time()) - self.started_at, 1),
        }


class _LiteJobManager:
    """Strict single-thread job manager with process-tree cleanup & persisted history."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, _LiteJob] = {}
        self._seq = 0
        atexit.register(self.stop_all)

    def _persisted_history(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not JOBS_DIR.exists():
            return out
        for fp in sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            try:
                d = json.loads(fp.read_text(encoding="utf-8", errors="ignore"))
                if isinstance(d, dict) and d.get("id"):
                    out.append(d)
            except Exception:
                continue
        return out

    def active_job(self) -> Optional[_LiteJob]:
        with self._lock:
            for j in self._jobs.values():
                with j._lock:
                    if j.status == "running":
                        return j
        return None

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            live = {j.id: j.to_dict() for j in self._jobs.values()}
        for old in self._persisted_history():
            live.setdefault(str(old["id"]), old)
        items = sorted(live.values(), key=lambda x: str(x.get("started_at", "")), reverse=True)
        return items[:25]

    def get(self, job_id: str) -> Optional[_LiteJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, adapter: str, count: int = 1) -> _LiteJob:
        cmd = [sys.executable, "-u", str(ROOT_DIR / "main.py"), "--run", adapter, "--count", str(count)]
        with self._lock:
            for existing in self._jobs.values():
                with existing._lock:
                    if existing.status == "running":
                        raise RuntimeError(f"单线程模式已有运行中任务: {existing.adapter} ({existing.id})")
            self._seq += 1
            jid = f"lite_{int(time.time())}_{self._seq}"
            job = _LiteJob(jid, adapter, count)
            self._jobs[jid] = job

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
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
                    line = _ANSI_RE.sub("", raw.rstrip("\r\n"))
                    low = line.lower()
                    lvl = (
                        "error"
                        if ("[-]" in line or "[error]" in low or "traceback" in low or "error:" in low)
                        else (
                            "warn"
                            if ("[warn]" in low or "[!]" in line)
                            else ("ok" if ("[+]" in line or "[success]" in low or "success" in low) else "info")
                        )
                    )
                    job.append(line, lvl)
                rc = proc.wait()
                job.finish("done" if rc == 0 else "failed", rc)
            except Exception as e:
                job.append(f"[RegisterHub] Stream error: {e}", "error")
                job.finish("failed", -1)

        threading.Thread(target=_reader, daemon=True).start()
        return job

    def stop(self, job_id: str) -> Dict[str, Any]:
        job = self.get(job_id)
        if not job:
            return {"ok": False, "error": f"Unknown job: {job_id}"}
        with job._lock:
            if job.status != "running" or job.proc is None:
                return {"ok": True, "status": job.status}
            job.status = "stopped"
            pid = job.pid
            proc = job.proc
        _kill_process_tree(pid, proc)
        job.append(f"[RegisterHub] Stopped process tree by user (PID={pid})", "warn")
        job.finish("stopped", proc.returncode if proc else -1)
        return {"ok": True, "status": "stopped"}

    def stop_all(self) -> None:
        with self._lock:
            running = [j.id for j in self._jobs.values() if j.status == "running"]
        for jid in running:
            self.stop(jid)


job_manager = _LiteJobManager()


def load_config_dict() -> Dict[str, Any]:
    default_cfg: Dict[str, Any] = {
        "browser": {
            "headless": False,
            "slow_mo": 50,
            "timeout": 30000,
            "enable_stealth": True,
            "use_chrome_channel": True,
            "proxy": None,
            "cdp_endpoint": None,
        },
        "output_dir": "./results",
        "defaults": {
            "country": "US",
            "password_length": 14,
            "use_disposable_mail": True,
        },
    }
    if not CONFIG_FILE.exists():
        return default_cfg
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            merged = dict(default_cfg)
            merged["browser"] = {**default_cfg["browser"], **(raw.get("browser") or {})}
            merged["defaults"] = {**default_cfg["defaults"], **(raw.get("defaults") or {})}
            if raw.get("output_dir"):
                merged["output_dir"] = str(raw["output_dir"])
            return merged
    except Exception:
        pass
    return default_cfg


def save_config_dict(updates: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config_dict()
    b = cfg["browser"]
    d = cfg["defaults"]

    if "headless" in updates:
        b["headless"] = bool(updates["headless"])
    if "slow_mo" in updates:
        b["slow_mo"] = max(0, min(2000, int(updates["slow_mo"] or 0)))
    if "timeout" in updates:
        b["timeout"] = max(5000, min(120000, int(updates["timeout"] or 30000)))
    if "enable_stealth" in updates:
        b["enable_stealth"] = bool(updates["enable_stealth"])
    if "use_chrome_channel" in updates:
        b["use_chrome_channel"] = bool(updates["use_chrome_channel"])
    if "proxy" in updates:
        val = str(updates["proxy"] or "").strip()
        b["proxy"] = val or None
    if "cdp_endpoint" in updates:
        val = str(updates["cdp_endpoint"] or "").strip()
        b["cdp_endpoint"] = val or None

    if "country" in updates and str(updates["country"]).upper() in ("US", "UK", "DE"):
        d["country"] = str(updates["country"]).upper()
    if "password_length" in updates:
        d["password_length"] = max(10, min(32, int(updates["password_length"] or 14)))
    if "use_disposable_mail" in updates:
        d["use_disposable_mail"] = bool(updates["use_disposable_mail"])

    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)
    return cfg


def list_snapshots() -> List[Dict[str, Any]]:
    if not SNAPSHOTS_DIR.exists():
        return []
    files = sorted(SNAPSHOTS_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]
    out: List[Dict[str, Any]] = []
    for fp in files:
        st = fp.stat()
        out.append({
            "name": fp.name,
            "size_kb": round(st.st_size / 1024.0, 1),
            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "url": f"/api/snapshots/{fp.name}",
        })
    return out


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
    server_version = "RegisterHubLite/1.3"

    def log_message(self, format: str, *args: Any) -> None:
        return

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

    def _serve_file(self, target: Path, base_dir: Path) -> None:
        resolved = target.resolve()
        root = base_dir.resolve()
        if root not in resolved.parents or not resolved.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return
        ctype, _ = mimetypes.guess_type(str(resolved))
        ctype = ctype or "application/octet-stream"
        is_text = ctype.startswith("text/") or ctype in ("application/javascript", "application/json", "image/svg+xml")
        data = resolved.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8" if is_text else ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, rel_path: str) -> None:
        clean = rel_path.lstrip("/") or "index.html"
        self._serve_file(STATIC_DIR / clean, STATIC_DIR)

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == "/api/meta":
            self._send_json({
                "edition": "community",
                "brand": "RegisterHub Lite",
                "subtitle": "Open-Source Automation Workbench",
                "version": "1.3.0",
                "concurrency_mode": "single-thread",
            })
            return

        if path == "/api/doctor":
            self._send_json(collect_doctor_report(include_network=True))
            return

        if path == "/api/config":
            self._send_json(load_config_dict())
            return

        if path == "/api/adapters":
            self._send_json({"adapters": list_community_adapters()})
            return

        if path == "/api/jobs":
            act = job_manager.active_job()
            self._send_json({"jobs": job_manager.list_jobs(), "active_job": act.to_dict() if act else None})
            return

        m_log = re.fullmatch(r"/api/jobs/([A-Za-z0-9_.-]+)/log", path)
        if m_log:
            jid = m_log.group(1)
            job = job_manager.get(jid)
            if job:
                with job._lock:
                    text = "\n".join(f"[{x['ts']}] [{x['level'].upper()}] {x['text']}" for x in job.logs)
                self._send_json({"id": jid, "log": text})
                return
            log_file = (JOBS_DIR / f"{jid}.log").resolve()
            if JOBS_DIR.exists() and JOBS_DIR.resolve() in log_file.parents and log_file.is_file():
                self._send_json({"id": jid, "log": log_file.read_text(encoding="utf-8", errors="replace")})
                return
            self._send_json({"error": "Job log not found"}, status=404)
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

        if path == "/api/snapshots":
            snaps = list_snapshots()
            self._send_json({"total": len(snaps), "snapshots": snaps})
            return

        m_snap = re.fullmatch(r"/api/snapshots/([A-Za-z0-9._-]+)", path)
        if m_snap:
            fname = m_snap.group(1)
            if not _SAFE_FILE_RE.fullmatch(fname) or ".." in fname:
                self._send_json({"error": "Invalid snapshot filename"}, status=400)
                return
            self._serve_file(SNAPSHOTS_DIR / fname, SNAPSHOTS_DIR)
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

        if path.startswith("/api/"):
            self._send_json({"error": "API endpoint not found"}, status=404)
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

        if path == "/api/config":
            try:
                saved = save_config_dict(body)
                self._send_json({"ok": True, "config": saved})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
            return

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
            try:
                job = job_manager.start(adapter, count=count)
                self._send_json({"ok": True, "job": job.to_dict()})
            except RuntimeError as e:
                self._send_json({"ok": False, "error": str(e)}, status=409)
            return

        m_stop = re.fullmatch(r"/api/jobs/([A-Za-z0-9_.-]+)/stop", path)
        if m_stop:
            res = job_manager.stop(m_stop.group(1))
            self._send_json(res, status=200 if res.get("ok") else 404)
            return

        if path == "/api/scaffold":
            site_name = str(body.get("site_name") or "").strip().lower()
            target_url = str(body.get("url") or "").strip()
            probe = bool(body.get("probe", False))
            overwrite = bool(body.get("overwrite", False))
            if not _SAFE_SITE_RE.fullmatch(site_name):
                self._send_json({"ok": False, "error": "Invalid site name (alphanumeric/underscore/hyphen only)"}, status=400)
                return
            clean = site_name.replace("-", "_")
            existing = ROOT_DIR / "sites" / f"{clean}.py"
            if existing.exists() and not overwrite:
                self._send_json({"ok": False, "error": f"适配器 sites/{clean}.py 已存在（如需覆盖请勾选允许覆盖）"}, status=409)
                return
            detected_fields = None
            probed_msg = ""
            if target_url and probe:
                try:
                    from core.inspector import FormInspector
                    from playwright.sync_api import sync_playwright

                    cfg = load_config_dict()
                    proxy = (cfg.get("browser") or {}).get("proxy")
                    with sync_playwright() as p:
                        kw: Dict[str, Any] = {"headless": True}
                        if proxy:
                            kw["proxy"] = {"server": proxy}
                        br = p.chromium.launch(**kw)
                        pg = br.new_page()
                        pg.goto(target_url, timeout=15000, wait_until="domcontentloaded")
                        detected_fields = FormInspector.inspect(pg)
                        br.close()
                        probed_msg = f"（已自动探测 {sum(1 for v in (detected_fields or {}).values() if v)} 项表单选择器）"
                except Exception as pe:
                    probed_msg = f"（DOM 探测未完成，已回退标准选择器: {type(pe).__name__}）"
            try:
                from core.scaffold import scaffold_site

                out_path = scaffold_site(site_name, target_url or None, detected_fields=detected_fields)
                self._send_json({
                    "ok": True,
                    "site": clean,
                    "path": str(out_path),
                    "probed": bool(detected_fields),
                    "message": f"生成成功: {out_path}{probed_msg}",
                })
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


def create_server(host: str = "127.0.0.1", port: int = 18886) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), LiteRequestHandler)


def start_server(host: str = "127.0.0.1", port: int = 18886) -> None:
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
