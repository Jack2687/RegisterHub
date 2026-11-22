"""RegisterHub Lite Web Server (Zero-Dependency Python Stdlib HTTP + SSE).

Strictly scoped to Open-Source Community Edition single-thread workflows:
  - Single-thread Overview Dashboard (KPI, 7-day trend, recent runs)
  - Environment diagnostics (`core.doctor`)
  - Runtime config editor (`config.json`, single-mailbox IMAP + live proxy/IMAP self-test)
  - Strict single-thread adapter execution + per-run overrides + ANSI-free SSE + process-tree kill + persisted logs
  - Adapter Workbench (honest tier badges, read-only source viewer, standalone DOM FormInspector probe, scaffold)
  - Community Tools Lite (US/UK/DE identity generator, live disposable web inbox, RFC 6238 2FA TOTP, Luhn test cards)
  - Local accounts table (4-col API Key, filter, delete/clear, single-key check) + CSV / JSON / Bitwarden export
  - Failures (`failed.txt`) & Snapshots (`results/snapshots/`) linked triage + cleanup
  - Single-window geometry calibration (`core.calibrator`)
"""

from __future__ import annotations

import atexit
import base64
import collections
import datetime
import hashlib
import hmac
import json
import mimetypes
import os
import queue
import random
import re
import secrets
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core import llm_client
from core.ai_agent import agent_manager

STATIC_DIR = Path(__file__).resolve().parent / "static"
RESULTS_DIR = ROOT_DIR / "results"
ACCOUNTS_FILE = RESULTS_DIR / "accounts.txt"
FAILED_FILE = RESULTS_DIR / "failed.txt"
JOBS_DIR = RESULTS_DIR / "jobs"
SNAPSHOTS_DIR = RESULTS_DIR / "snapshots"
CONFIG_FILE = ROOT_DIR / "config.json"
GEOMETRY_FILE = ROOT_DIR / "config" / "window_geometry.json"

_SAFE_SITE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,48}$")
_SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_OTP_RE = re.compile(r"\b\d{4,8}\b")

_ADAPTER_TIERS: Dict[str, str] = {
    "demo": "demo",
    "demo_site": "demo",
    "automation_exercise": "demo",
    "coarena": "oauth",
    "amd": "verified",
    "deepgram": "verified",
    "milionseek": "verified",
    "quanticdata": "verified",
}


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
            meta = self.to_dict()
            meta["persisted"] = True
            meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            with self._lock:
                lines = [f"[{x['ts']}] [{x['level'].upper()}] {x['text']}" for x in self.logs]
            log_p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        except Exception:
            pass

    def subscribe(self) -> tuple[List[Dict[str, str]], queue.Queue[Optional[Dict[str, str]]]]:
        q: queue.Queue[Optional[Dict[str, str]]] = queue.Queue(maxsize=500)
        with self._lock:
            hist = list(self.logs)
            if self.status == "running":
                self._subs.append(q)
            else:
                q.put_nowait(None)
        return hist, q

    def unsubscribe(self, q: queue.Queue[Optional[Dict[str, str]]]) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            end_ts = self.finished_at or time.time()
            return {
                "id": self.id,
                "adapter": self.adapter,
                "count": self.count,
                "status": self.status,
                "pid": self.pid,
                "returncode": self.returncode,
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at)),
                "duration_s": round(max(0.0, end_ts - self.started_at), 1),
                "log_lines": len(self.logs),
                "persisted": False,
            }


class _LiteJobManager:
    """Strict single-thread job manager with persistence & process-tree cleanup."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, _LiteJob] = {}
        self._seq = 0
        atexit.register(self.stop_all)

    def active_job(self) -> Optional[_LiteJob]:
        with self._lock:
            for j in self._jobs.values():
                with j._lock:
                    if j.status == "running":
                        return j
        return None

    def get(self, job_id: str) -> Optional[_LiteJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            mem = {jid: j.to_dict() for jid, j in self._jobs.items()}
        disk: Dict[str, Dict[str, Any]] = {}
        if JOBS_DIR.exists():
            for fp in sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:25]:
                try:
                    d = json.loads(fp.read_text(encoding="utf-8"))
                    if isinstance(d, dict) and d.get("id") and d["id"] not in mem:
                        disk[d["id"]] = d
                except Exception:
                    pass
        merged = list(mem.values()) + list(disk.values())
        merged.sort(key=lambda x: str(x.get("started_at", "")), reverse=True)
        return merged[:25]

    def start(
        self,
        adapter: str,
        count: int = 1,
        headless_override: Optional[bool] = None,
        custom_email: str = "",
    ) -> _LiteJob:
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
        if headless_override is True:
            env["RH_OVERRIDE_HEADLESS"] = "true"
        elif headless_override is False:
            env["RH_OVERRIDE_HEADLESS"] = "false"
        if custom_email.strip():
            env["RH_OVERRIDE_EMAIL"] = custom_email.strip()

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
        extra_note = []
        if headless_override is not None:
            extra_note.append(f"headless={headless_override}")
        if custom_email.strip():
            extra_note.append(f"email={custom_email.strip()}")
        note_s = f", {', '.join(extra_note)}" if extra_note else ""
        job.append(f"[RegisterHub] Started single-thread run: {adapter} (count={count}, PID={proc.pid}{note_s})")

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


def _check_manual_job_busy() -> Optional[str]:
    act = job_manager.active_job()
    if act is not None:
        return f"单线程模式已有运行中的单站试跑任务: {act.adapter} ({act.id})"
    return None


agent_manager.set_external_busy_check(_check_manual_job_busy)

# In-memory live disposable inbox sessions for Tools Lite (max 16)
_TEMPMAIL_LOCK = threading.Lock()
_TEMPMAIL_SESSIONS: "collections.OrderedDict[str, Dict[str, Any]]" = collections.OrderedDict()

# 5-minute cached current exit IP purity score so Tools/IP Scorer opens with live data
_IP_SCORE_CACHE_LOCK = threading.Lock()
_IP_SCORE_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_IP_SCORE_TTL_S = 300.0


def _parse_har_community(har_text: str, site_name: str = "") -> Dict[str, Any]:
    """Zero-dependency stdlib HAR parser that extracts auth/signup API steps & generates Python skeleton."""
    raw = json.loads(har_text)
    entries = ((raw.get("log") or {}).get("entries") or []) if isinstance(raw, dict) else []
    if not isinstance(entries, list):
        raise ValueError("HAR 格式非法：缺少 log.entries 数组")

    static_exts = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".woff", ".woff2", ".ico", ".map")
    noise_hosts = ("google-analytics", "googletagmanager", "sentry.io", "segment.io", "clarity.ms", "doubleclick.net")
    sig_rules = [
        ("Turnstile", ("turnstile", "cf-turnstile", "challenges.cloudflare.com")),
        ("reCAPTCHA", ("recaptcha", "g-recaptcha")),
        ("Clerk", ("clerk.", "/v1/client/sign_ups")),
        ("Supabase", ("supabase", "/auth/v1/signup")),
        ("JWT", ("access_token", "id_token", "eyj")),
        ("API Key", ("api_key", "apikey", "sk-", "gsk_", "sb_live_")),
        ("CSRF", ("csrf", "x-xsrf-token")),
    ]

    steps: List[Dict[str, Any]] = []
    all_sigs: List[str] = []
    origin = ""
    for e in entries:
        req = e.get("request") or {}
        resp = e.get("response") or {}
        method = str(req.get("method") or "GET").upper()
        url = str(req.get("url") or "")
        if not url.startswith(("http://", "https://")):
            continue
        p = urlparse(url)
        if any(h in p.netloc.lower() for h in noise_hosts) or p.path.lower().endswith(static_exts):
            continue
        mime = str(((resp.get("content") or {}).get("mimeType")) or "").lower()
        if method == "GET" and "json" not in mime and not any(k in p.path.lower() for k in ("auth", "signup", "register", "token", "key", "csrf")):
            continue
        req_txt = str(((req.get("postData") or {}).get("text")) or "")
        resp_txt = str(((resp.get("content") or {}).get("text")) or "")
        blob = f"{url} {req_txt[:2000]} {resp_txt[:2000]}".lower()
        sigs = [name for name, kws in sig_rules if any(k in blob for k in kws)]
        all_sigs.extend(sigs)
        if not origin:
            origin = f"{p.scheme}://{p.netloc}"

        req_keys: List[str] = []
        resp_keys: List[str] = []
        try:
            rj = json.loads(req_txt) if req_txt else None
            if isinstance(rj, dict):
                req_keys = list(rj.keys())[:10]
        except Exception:
            pass
        try:
            pj = json.loads(resp_txt) if resp_txt else None
            if isinstance(pj, dict):
                resp_keys = list(pj.keys())[:10]
        except Exception:
            pass

        steps.append({
            "method": method,
            "url": url,
            "path": p.path or "/",
            "status": int(resp.get("status") or 0),
            "req_keys": req_keys,
            "resp_keys": resp_keys,
            "signals": sigs,
        })

    clean_site = re.sub(r"[^a-zA-Z0-9_]", "_", (site_name or (urlparse(origin).hostname or "target").split(".")[0]).lower()).strip("_") or "target"
    cls_name = "".join(w.capitalize() for w in clean_site.split("_") if w) + "ProtocolAdapter"
    step_lines = []
    for idx, st in enumerate(steps[:12], 1):
        step_lines.append(f"        # Step {idx}: {st['method']} {st['path']} (HTTP {st['status']})")
        if st["req_keys"]:
            kv = ", ".join(f"{k!r}: ..." for k in st["req_keys"][:6])
            step_lines.append(f"        # r{idx} = session.{st['method'].lower()}({st['url']!r}, json={{{kv}}})")
        else:
            step_lines.append(f"        # r{idx} = session.{st['method'].lower()}({st['url']!r})")
    body = "\n".join(step_lines) or "        # 未在 HAR 中捕获到注册接口，请核对抓取流程"
    sigs_unique = sorted(set(all_sigs))
    skeleton = f'''"""RegisterHub Community - 由 HAR 抓包自动提炼的协议注册骨架: {clean_site}"""

import requests


class {cls_name}:
    site_name = {clean_site!r}
    target_url = {origin!r}
    detected_signals = {sigs_unique!r}

    def register(self, email: str, password: str, proxy: str | None = None) -> dict:
        session = requests.Session()
        if proxy:
            session.proxies = {{"http": proxy, "https": proxy}}
{body}
        return {{"email": email, "password": password, "api_key": ""}}
'''
    return {
        "ok": True,
        "site": clean_site,
        "origin": origin,
        "total_entries": len(entries),
        "api_steps": steps[:30],
        "signals": sigs_unique,
        "skeleton": skeleton,
    }


def _load_raw_config_dict() -> Dict[str, Any]:
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
        "mail": {
            "imap_server": "",
            "imap_port": 993,
            "imap_user": "",
            "imap_password": "",
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
            merged["mail"] = {**default_cfg["mail"], **(raw.get("mail") or {})}
            if raw.get("output_dir"):
                merged["output_dir"] = str(raw["output_dir"])
            return merged
    except Exception:
        pass
    return default_cfg


def load_config_dict() -> Dict[str, Any]:
    cfg = _load_raw_config_dict()
    safe = json.loads(json.dumps(cfg))
    if safe.get("mail", {}).get("imap_password"):
        safe["mail"]["imap_password"] = "***"
    return safe


def save_config_dict(updates: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _load_raw_config_dict()
    b = cfg["browser"]
    d = cfg["defaults"]
    m = cfg["mail"]

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

    if "imap_server" in updates:
        m["imap_server"] = str(updates["imap_server"] or "").strip()
    if "imap_port" in updates:
        m["imap_port"] = max(1, min(65535, int(updates["imap_port"] or 993)))
    if "imap_user" in updates:
        m["imap_user"] = str(updates["imap_user"] or "").strip()
    if "imap_password" in updates:
        pw = str(updates["imap_password"] or "")
        if pw != "***":
            m["imap_password"] = pw

    from core.locks import atomic_write_text_safe, interprocess_file_lock

    with interprocess_file_lock(CONFIG_FILE):
        atomic_write_text_safe(CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=2))
    return load_config_dict()


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


def list_failures() -> List[Dict[str, Any]]:
    if not FAILED_FILE.exists():
        return []
    snap_names = {s["name"] for s in list_snapshots()}
    rows: List[Dict[str, Any]] = []
    for idx, raw in enumerate(FAILED_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        ts = parts[0][:19].replace("T", " ") if len(parts) >= 1 else ""
        reason = parts[1] if len(parts) >= 2 else line
        account = parts[2] if len(parts) >= 3 else ""
        snap_name = ""
        for p in parts[2:]:
            if p.lower().startswith("snapshot:"):
                cand = p.split(":", 1)[1].strip()
                if cand in snap_names:
                    snap_name = cand
        low_r = reason.lower()
        if any(k in low_r for k in ("already exists", "已存在", "[dead]", "domain banned", "invalid oauth", "blocked domain")):
            cat = "dead"
        elif any(k in low_r for k in ("captcha", "turnstile", "recaptcha", "hcaptcha", "slider")):
            cat = "captcha"
        elif any(k in low_r for k in ("timeout", "429", "403", "502", "proxy", "connection", "reset", "no mail")):
            cat = "soft"
        else:
            cat = "unknown"
        rows.append({
            "id": idx,
            "time": ts,
            "reason": reason[:240],
            "account": account[:200],
            "category": cat,
            "snapshot": snap_name,
            "snapshot_url": f"/api/snapshots/{snap_name}" if snap_name else "",
        })
    rows.reverse()
    return rows[:100]


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


def list_community_adapters() -> List[Dict[str, Any]]:
    from sites import discover_adapters

    adapters = discover_adapters()
    out: List[Dict[str, Any]] = []
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
        mod_file = Path(sys.modules[cls.__module__].__file__ or "") if cls.__module__ in sys.modules else (ROOT_DIR / "sites" / f"{name}.py")
        lines = len(mod_file.read_text(encoding="utf-8", errors="ignore").splitlines()) if mod_file.is_file() else 0
        rel_file = f"sites/{mod_file.name}" if mod_file.is_file() else f"sites/{name}.py"
        out.append({
            "name": name,
            "class_name": cls.__name__,
            "file": rel_file,
            "lines": lines,
            "tier": _ADAPTER_TIERS.get(name, "template"),
            "target_url": url if len(url) <= 120 else (url[:80] + "… [embedded]"),
        })
    return out


def build_overview_payload() -> Dict[str, Any]:
    from core.exporter import parse_accounts_file

    accounts = parse_accounts_file(ACCOUNTS_FILE)
    with_key = sum(1 for a in accounts if (a.get("api_key") or "").strip())
    failures = list_failures()
    snaps = list_snapshots()
    adapters = list_community_adapters()
    jobs = job_manager.list_jobs()
    act = job_manager.active_job()

    # Build 7-day trend from job records & failed.txt dates (deduplicate failed job vs failed.txt line)
    today = datetime.date.today()
    days = [(today - datetime.timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    trend_map = {d: {"date": d[5:], "full_date": d, "done": 0, "failed": 0} for d in days}
    job_failed_by_day: collections.Counter[str] = collections.Counter()
    ledger_failed_by_day: collections.Counter[str] = collections.Counter()
    for j in jobs:
        dkey = str(j.get("started_at") or "")[:10]
        if dkey in trend_map:
            if j.get("status") == "done":
                trend_map[dkey]["done"] += max(1, int(j.get("count") or 1))
            elif j.get("status") == "failed":
                job_failed_by_day[dkey] += 1
    for f in failures:
        dkey = str(f.get("time") or "")[:10]
        if dkey in trend_map:
            ledger_failed_by_day[dkey] += 1
    for dkey in days:
        trend_map[dkey]["failed"] = max(job_failed_by_day[dkey], ledger_failed_by_day[dkey])

    recent_accounts = []
    for a in reversed(accounts[-8:]):
        ak = (a.get("api_key") or "").strip()
        masked = (ak[:10] + "..." + ak[-4:]) if len(ak) > 18 else (ak[:6] + "***" if ak else "")
        recent_accounts.append({
            "email": a.get("email", ""),
            "username": a.get("username", ""),
            "has_key": bool(ak),
            "masked_key": masked,
            "api_key": ak,
        })

    layout_res = ""
    if GEOMETRY_FILE.exists():
        try:
            gd = json.loads(GEOMETRY_FILE.read_text(encoding="utf-8"))
            layout_res = str(gd.get("screen_resolution") or "")
        except Exception:
            pass

    return {
        "kpi": {
            "accounts_total": len(accounts),
            "accounts_with_key": with_key,
            "failures_total": len(failures),
            "snapshots_total": len(snaps),
            "adapters_total": len(adapters),
            "running": 1 if act else 0,
            "agent_running": 1 if agent_manager.active_session_id() else 0,
        },
        "daily_trend": [trend_map[d] for d in days],
        "recent_jobs": jobs[:6],
        "recent_accounts": recent_accounts,
        "layout_info": {"ready": GEOMETRY_FILE.exists(), "screen_resolution": layout_res},
        "adapters": adapters[:8],
    }


def _luhn_complete(prefix: str, total_len: int = 16) -> str:
    digits = [int(c) for c in re.sub(r"\D", "", prefix)][: total_len - 1]
    while len(digits) < total_len - 1:
        digits.append(random.randint(0, 9))
    s = 0
    for idx, d in enumerate(reversed(digits)):
        v = d * 2 if idx % 2 == 0 else d
        s += v - 9 if v > 9 else v
    check = (10 - (s % 10)) % 10
    return "".join(str(x) for x in digits) + str(check)


def _compute_totp(secret: str) -> Dict[str, Any]:
    cleaned = re.sub(r"[\s-]", "", secret).upper()
    if len(cleaned) < 4:
        raise ValueError("Secret 长度不足")
    pad = "=" * ((8 - len(cleaned) % 8) % 8)
    key = base64.b32decode(cleaned + pad, casefold=True)
    now_i = int(time.time())
    counter = now_i // 30
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[off : off + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return {"code": f"{code_int:06d}", "period": 30, "remaining": 30 - (now_i % 30)}


def _probe_target_page(target_url: str) -> Dict[str, Any]:
    from core.inspector import FormInspector
    from core.net_guard import validate_proxy_url, validate_public_http_url
    from playwright.sync_api import sync_playwright

    clean_url = validate_public_http_url(target_url, resolve_dns=True)
    cfg = _load_raw_config_dict()
    raw_px = str((cfg.get("browser") or {}).get("proxy") or "").strip()
    proxy = validate_proxy_url(raw_px) if raw_px else ""
    t0 = time.perf_counter()
    with sync_playwright() as p:
        kw: Dict[str, Any] = {
            "headless": True,
            "args": ["--disable-blink-features=AutomationControlled", "--no-first-run"],
            "ignore_default_args": ["--enable-automation"],
        }
        if proxy:
            kw["proxy"] = {"server": proxy}
        try:
            br = p.chromium.launch(channel="chrome", **kw)
        except Exception:
            br = p.chromium.launch(**kw)
        try:
            ctx = br.new_context()

            def _pw_ssrf_guard(route: Any) -> None:
                req_url = route.request.url
                if req_url.startswith(("data:", "blob:", "about:")):
                    route.continue_()
                    return
                try:
                    validate_public_http_url(req_url, resolve_dns=True)
                    route.continue_()
                except Exception:
                    route.abort("blockedbyclient")

            ctx.route("**/*", _pw_ssrf_guard)
            pg = ctx.new_page()
            resp = pg.goto(clean_url, timeout=20000, wait_until="domcontentloaded")
            status = resp.status if resp else 0
            title = pg.title()
            final_url = pg.url
            fields = FormInspector(pg).detect_inputs()
            html = (pg.content() or "").lower()
            signals: List[str] = []
            for label, needles in [
                ("Cloudflare Turnstile", ("challenges.cloudflare.com", "cf-turnstile")),
                ("Google reCAPTCHA", ("recaptcha/api.js", "g-recaptcha")),
                ("hCaptcha", ("hcaptcha.com", "h-captcha")),
                ("Clerk Auth", ("clerk.accounts", "__clerk")),
                ("Supabase Auth", ("supabase.co", "supabase")),
                ("NextAuth", ("/api/auth/signin", "next-auth")),
                ("OAuth Login", ("accounts.google.com", "github.com/login/oauth")),
            ]:
                if any(n in html for n in needles):
                    signals.append(label)
            return {
                "ok": True,
                "url": final_url,
                "status_code": status,
                "title": title,
                "fields": fields,
                "detected_count": sum(1 for v in fields.values() if v),
                "signals": signals,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        finally:
            try:
                br.close()
            except Exception:
                pass


class LiteRequestHandler(BaseHTTPRequestHandler):
    server_version = "RegisterHubLite/2.0"

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
        if length <= 0 or length > 16_000_000:
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
        if resolved.suffix.lower() == ".ico":
            ctype = "image/x-icon"
        else:
            ctype, _ = mimetypes.guess_type(str(resolved))
            ctype = ctype or "application/octet-stream"
        is_text = ctype.startswith("text/") or ctype in ("application/javascript", "application/json", "image/svg+xml")
        data = resolved.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8" if is_text else ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, rel_path: str) -> None:
        clean = rel_path.lstrip("/") or "index.html"
        self._serve_file(STATIC_DIR / clean, STATIC_DIR)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/meta":
            self._send_json({
                "edition": "community",
                "brand": "RegisterHub Lite",
                "subtitle": "Open-Source Automation Workbench",
                "version": "2.0.0",
                "concurrency_mode": "single-thread",
            })
            return

        if path == "/api/overview":
            self._send_json(build_overview_payload())
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

        m_code = re.fullmatch(r"/api/adapters/([a-zA-Z0-9_-]+)/code", path)
        if m_code:
            name = m_code.group(1).lower()
            adapters_map = {a["name"]: a for a in list_community_adapters()}
            info = adapters_map.get(name)
            if not info:
                self._send_json({"error": f"Adapter not found: {name}"}, status=404)
                return
            sites_root = (ROOT_DIR / "sites").resolve()
            fpath = (ROOT_DIR / info["file"]).resolve()
            if sites_root not in fpath.parents or not fpath.is_file():
                self._send_json({"error": "Adapter file not found"}, status=404)
                return
            self._send_json({
                "name": name,
                "file": info["file"],
                "tier": info["tier"],
                "code": fpath.read_text(encoding="utf-8", errors="replace"),
            })
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

        if path == "/api/failures":
            fails = list_failures()
            self._send_json({"total": len(fails), "failures": fails})
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

        if path == "/api/tempmail/messages":
            sid = (qs.get("session_id") or [""])[0].strip()
            with _TEMPMAIL_LOCK:
                sess = _TEMPMAIL_SESSIONS.get(sid)
            if not sess:
                self._send_json({"ok": False, "error": "临时邮箱会话不存在或已过期"}, status=404)
                return
            client = sess["client"]
            msgs = []
            try:
                raw_list = client.check_messages() or []
                for m in raw_list[:20]:
                    mid = m.get("id")
                    body = str(m.get("body") or "")
                    if mid and len(body) < 40:
                        full = client.read_message(mid)
                        if full:
                            body = full
                    subj = str(m.get("subject") or "")
                    code_m = _OTP_RE.search(f"{subj} {body}")
                    msgs.append({
                        "id": str(mid or ""),
                        "from": str(m.get("from") or ""),
                        "subject": subj,
                        "body": body[:2000],
                        "code": code_m.group(0) if code_m else "",
                    })
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
                return
            self._send_json({"ok": True, "email": sess["email"], "provider": sess["provider"], "messages": msgs})
            return

        if path == "/api/tools/ip-score/current":
            from core.ip_scorer import score_ip

            force = (qs.get("force") or ["0"])[0] in ("1", "true", "yes")
            now = time.time()
            with _IP_SCORE_CACHE_LOCK:
                if not force and _IP_SCORE_CACHE["data"] is not None and (now - float(_IP_SCORE_CACHE["ts"])) < _IP_SCORE_TTL_S:
                    self._send_json({**_IP_SCORE_CACHE["data"], "cached": True})
                    return
            cfg = _load_raw_config_dict()
            px = str((cfg.get("browser") or {}).get("proxy") or "").strip()
            res = score_ip(ip="", proxy_url=px, target_url="")
            with _IP_SCORE_CACHE_LOCK:
                _IP_SCORE_CACHE["ts"] = time.time()
                _IP_SCORE_CACHE["data"] = res
            self._send_json({**res, "cached": False})
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

        if path == "/api/llm/config":
            self._send_json(llm_client.get_public_llm_config())
            return

        if path == "/api/agent/sessions":
            self._send_json({
                "sessions": agent_manager.list_sessions(),
                "active_session_id": agent_manager.active_session_id(),
            })
            return

        m_ag_stream = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/stream", path)
        if m_ag_stream:
            sess = agent_manager.get_session(m_ag_stream.group(1))
            if not sess:
                self._send_json({"error": "Agent session not found"}, status=404)
                return
            try:
                since = max(0, int((qs.get("since") or ["0"])[0]))
            except Exception:
                since = 0
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            hist, sub_q = sess.subscribe()
            sent_live_terminal = False
            last_error = ""
            try:
                for idx, item in enumerate(hist):
                    if item.get("type") == "status":
                        last_error = str(item.get("error") or "")
                    if idx < since:
                        continue
                    chunk = f"data: {json.dumps({**item, 'seq': idx, 'replay': True}, ensure_ascii=False)}\n\n"
                    self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
                seq_cursor = len(hist)
                while True:
                    try:
                        ev = sub_q.get(timeout=12.0)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if ev is None:
                        if not sent_live_terminal:
                            end_ev = {
                                "type": "status",
                                "ts": time.strftime("%H:%M:%S"),
                                "status": sess.status,
                                "error": last_error if sess.status in ("error", "stopped") else "",
                                "seq": seq_cursor,
                                "replay": False,
                                "eof": True,
                            }
                            self.wfile.write(f"data: {json.dumps(end_ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        break
                    if ev.get("type") == "status":
                        last_error = str(ev.get("error") or "")
                        if ev.get("status") in ("done", "error", "stopped"):
                            sent_live_terminal = True
                    chunk = f"data: {json.dumps({**ev, 'seq': seq_cursor, 'replay': False}, ensure_ascii=False)}\n\n"
                    seq_cursor += 1
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                sess.unsubscribe(sub_q)
            return

        m_ag_export = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/export", path)
        if m_ag_export:
            exp = agent_manager.export_session_markdown(m_ag_export.group(1))
            if exp is None:
                self._send_json({"ok": False, "error": "Agent session not found"}, status=404)
                return
            self._send_json(exp)
            return

        m_ag_get = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)", path)
        if m_ag_get:
            sess = agent_manager.get_session(m_ag_get.group(1))
            if not sess:
                self._send_json({"error": "Agent session not found"}, status=404)
                return
            self._send_json({"ok": True, "session": sess.to_detail()})
            return

        if path.startswith("/api/"):
            self._send_json({"error": "API endpoint not found"}, status=404)
            return

        if path == "/favicon.ico":
            if (STATIC_DIR / "favicon.ico").exists():
                self._serve_static("favicon.ico")
            else:
                self._serve_static("logo.svg")
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

        if path == "/api/config/test-proxy":
            from core.net_guard import (
                PrivateAddressBlockedError,
                build_safe_urllib_opener,
                validate_proxy_url,
                validate_public_http_url,
            )

            cfg = _load_raw_config_dict()
            px = str(body.get("proxy") if "proxy" in body else (cfg.get("browser") or {}).get("proxy") or "").strip()
            target = str(body.get("target_url") or "https://api.ipify.org?format=json").strip()
            t0 = time.perf_counter()
            try:
                clean_target = validate_public_http_url(target, resolve_dns=True)
                clean_px = validate_proxy_url(px) if px else ""
            except (PrivateAddressBlockedError, ValueError) as e:
                self._send_json({"ok": False, "proxy": px or "(direct)", "latency_ms": 0.0, "error": str(e)}, status=400)
                return
            try:
                opener = build_safe_urllib_opener(proxy=clean_px or "")
                req = urllib.request.Request(clean_target, headers={"User-Agent": "RegisterHub-Lite/2.0"})
                with opener.open(req, timeout=8) as r:
                    txt = r.read(512).decode("utf-8", errors="ignore").strip()
                    self._send_json({
                        "ok": True,
                        "proxy": clean_px or "(direct)",
                        "status_code": r.status,
                        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                        "body": txt[:200],
                    })
            except Exception as e:
                self._send_json({
                    "ok": False,
                    "proxy": clean_px or "(direct)",
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "error": str(e),
                })
            return

        if path == "/api/config/test-imap":
            from core.imap_mail import SingleImapMailClient

            cfg = _load_raw_config_dict()
            mc = cfg.get("mail") or {}
            server = str(body.get("imap_server") or mc.get("imap_server") or "").strip()
            port = int(body.get("imap_port") or mc.get("imap_port") or 993)
            user = str(body.get("imap_user") or mc.get("imap_user") or "").strip()
            pw = str(body.get("imap_password") or "")
            if not pw or pw == "***":
                pw = str(mc.get("imap_password") or "")
            client = SingleImapMailClient(server=server, port=port, user=user, password=pw)
            self._send_json(client.test_connection())
            return

        if path == "/api/run":
            adapter = str(body.get("adapter") or "").strip().lower()
            try:
                count = max(1, min(20, int(body.get("count") or 1)))
            except Exception:
                count = 1
            raw_hl = body.get("headless")
            hl_override: Optional[bool] = raw_hl if isinstance(raw_hl, bool) else None
            custom_email = str(body.get("custom_email") or "").strip()
            known = {a["name"] for a in list_community_adapters()}
            if adapter not in known:
                self._send_json({"ok": False, "error": f"Unknown adapter: {adapter}"}, status=400)
                return
            if agent_manager.active_session_id():
                self._send_json(
                    {"ok": False, "error": "单线程模式已有运行中的 AI 直跑会话，请等待结束或先停止"},
                    status=409,
                )
                return
            try:
                job = job_manager.start(
                    adapter,
                    count=count,
                    headless_override=hl_override,
                    custom_email=custom_email,
                )
                self._send_json({"ok": True, "job": job.to_dict()})
            except RuntimeError as e:
                self._send_json({"ok": False, "error": str(e)}, status=409)
            return

        m_stop = re.fullmatch(r"/api/jobs/([A-Za-z0-9_.-]+)/stop", path)
        if m_stop:
            res = job_manager.stop(m_stop.group(1))
            self._send_json(res, status=200 if res.get("ok") else 404)
            return

        if path == "/api/inspector/probe":
            url = str(body.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                self._send_json({"ok": False, "error": "请输入以 http:// 或 https:// 开头的目标 URL"}, status=400)
                return
            try:
                self._send_json(_probe_target_page(url))
            except Exception as e:
                self._send_json({"ok": False, "error": f"页面嗅探失败: {e}"}, status=400)
            return

        if path == "/api/tempmail/new":
            try:
                from core.mail_client import DisposableMailClient

                client = DisposableMailClient()
                addr = client.create_mailbox()
                sid = f"tm_{secrets.token_hex(6)}"
                prov = getattr(client, "provider_name", None) or type(getattr(client, "_backend", None)).__name__
                with _TEMPMAIL_LOCK:
                    _TEMPMAIL_SESSIONS[sid] = {
                        "email": addr,
                        "provider": str(prov),
                        "client": client,
                        "created_at": time.strftime("%H:%M:%S"),
                    }
                    while len(_TEMPMAIL_SESSIONS) > 16:
                        _TEMPMAIL_SESSIONS.popitem(last=False)
                self._send_json({"ok": True, "session_id": sid, "email": addr, "provider": str(prov)})
            except Exception as e:
                self._send_json({"ok": False, "error": f"申请临时邮箱失败: {e}"}, status=502)
            return

        if path == "/api/tools/identity":
            from core.identity import generate_identity

            loc = str(body.get("country") or "US").strip().upper()
            if loc not in ("US", "UK", "DE"):
                loc = "US"
            cnt = max(1, min(6, int(body.get("count") or 2)))
            pw_len = max(10, min(32, int(body.get("password_length") or 14)))
            domain = str(body.get("domain") or "").strip() or None
            items = [generate_identity(email_domain=domain, locale=loc, use_disposable=False, password_length=pw_len) for _ in range(cnt)]
            self._send_json({"ok": True, "count": len(items), "identities": items})
            return

        if path == "/api/tools/totp":
            secret = str(body.get("secret") or "").strip()
            try:
                self._send_json({"ok": True, **_compute_totp(secret)})
            except Exception as e:
                self._send_json({"ok": False, "error": f"无效 Base32 密钥: {e}"}, status=400)
            return

        if path == "/api/tools/ip-score":
            from core.ip_scorer import score_ip
            from core.net_guard import PrivateAddressBlockedError, validate_proxy_url, validate_public_http_url

            ip_val = str(body.get("ip") or "").strip()
            px = str(body.get("proxy_url") or "").strip()
            target_url = str(body.get("target_url") or "").strip()
            try:
                if target_url:
                    target_url = validate_public_http_url(target_url, resolve_dns=True)
                if px:
                    px = validate_proxy_url(px)
            except (PrivateAddressBlockedError, ValueError) as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
                return
            if not ip_val and not px:
                cfg = _load_raw_config_dict()
                px = str((cfg.get("browser") or {}).get("proxy") or "").strip()
            self._send_json(score_ip(ip=ip_val, proxy_url=px, target_url=target_url))
            return

        if path == "/api/tools/card":
            brands = {
                "visa": ("Visa", "4532", 16, 3),
                "mastercard": ("Mastercard", "5425", 16, 3),
                "amex": ("American Express", "3782", 15, 4),
                "discover": ("Discover", "6011", 16, 3),
                "jcb": ("JCB", "3530", 16, 3),
            }
            bname, d_bin, clen, cvv_len = brands.get(str(body.get("brand") or "visa").lower(), brands["visa"])
            prefix = re.sub(r"\D", "", str(body.get("bin_prefix") or ""))[:8] or d_bin
            cnt = max(1, min(12, int(body.get("count") or 6)))
            cards = []
            for _ in range(cnt):
                num = _luhn_complete(prefix, clen)
                fmt = f"{num[:4]} {num[4:10]} {num[10:]}" if clen == 15 else " ".join(num[i : i + 4] for i in range(0, len(num), 4))
                mm = f"{random.randint(1, 12):02d}"
                yy = str(random.randint(2027, 2031))
                cvv = "".join(str(random.randint(0, 9)) for _ in range(cvv_len))
                cards.append({
                    "brand": bname,
                    "card_number": num,
                    "formatted": fmt,
                    "exp": f"{mm}/{yy[-2:]}",
                    "exp_month": mm,
                    "exp_year": yy,
                    "cvv": cvv,
                })
            self._send_json({"ok": True, "count": len(cards), "cards": cards})
            return

        if path == "/api/accounts/delete":
            from core.locks import atomic_write_text_safe, interprocess_file_lock

            target_email = str(body.get("email") or "").strip().lower()
            target_key = str(body.get("api_key") or "").strip()
            if not target_email and not target_key:
                self._send_json({"ok": False, "removed": 0, "error": "缺少删除目标字段"}, status=400)
                return
            if not ACCOUNTS_FILE.exists():
                self._send_json({"ok": True, "removed": 0})
                return
            with interprocess_file_lock(ACCOUNTS_FILE):
                if not ACCOUNTS_FILE.exists():
                    self._send_json({"ok": True, "removed": 0})
                    return
                lines = ACCOUNTS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
                kept: List[str] = []
                removed = 0
                for ln in lines:
                    s = ln.strip()
                    if not s or s.startswith("#"):
                        kept.append(ln)
                        continue
                    parts = [p.strip() for p in s.split("----")]
                    em = (parts[0] if parts else "").lower()
                    pwd_col = parts[1] if len(parts) > 1 else ""
                    user_col = parts[2] if len(parts) > 2 else ""
                    ak = parts[3] if len(parts) >= 4 else ""
                    if not ak:
                        if pwd_col.startswith(("hms-", "sk-", "gsk_", "coa_")):
                            ak = pwd_col
                        elif len(parts) == 3 and user_col.startswith(("hms-", "sk-", "gsk_", "coa_")):
                            ak = user_col
                    email_ok = (not target_email) or (em == target_email)
                    key_ok = (not target_key) or (ak == target_key)
                    if removed == 0 and email_ok and key_ok:
                        removed += 1
                        continue
                    kept.append(ln)
                if removed > 0:
                    ACCOUNTS_FILE.with_suffix(".txt.bak").write_text("\n".join(lines) + "\n", encoding="utf-8")
                    atomic_write_text_safe(ACCOUNTS_FILE, "\n".join(kept) + ("\n" if kept else ""))
            self._send_json({"ok": True, "removed": removed})
            return

        if path == "/api/accounts/clear":
            from core.locks import atomic_write_text_safe, interprocess_file_lock

            if ACCOUNTS_FILE.exists():
                with interprocess_file_lock(ACCOUNTS_FILE):
                    if ACCOUNTS_FILE.exists():
                        ACCOUNTS_FILE.with_suffix(".txt.bak").write_text(
                            ACCOUNTS_FILE.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8"
                        )
                        atomic_write_text_safe(ACCOUNTS_FILE, "")
            self._send_json({"ok": True})
            return

        if path == "/api/accounts/test-key":
            from core.net_guard import (
                PrivateAddressBlockedError,
                build_safe_urllib_opener,
                validate_proxy_url,
                validate_public_http_url,
            )

            key_val = str(body.get("key") or "").strip()
            base_url = str(body.get("base_url") or "").strip().rstrip("/")
            if len(key_val) < 8:
                self._send_json({"ok": False, "detail": "Key 长度不足"}, status=400)
                return
            if not base_url:
                self._send_json({"ok": True, "detail": "格式校验通过（填写 Base URL 可测在线 /v1/models）"})
                return
            cfg = _load_raw_config_dict()
            raw_px = str((cfg.get("browser") or {}).get("proxy") or "").strip()
            try:
                clean_base = validate_public_http_url(base_url, resolve_dns=True).rstrip("/")
                px = validate_proxy_url(raw_px) if raw_px else ""
            except (PrivateAddressBlockedError, ValueError) as e:
                self._send_json({"ok": False, "detail": str(e)}, status=400)
                return
            models_url = f"{clean_base}/models" if clean_base.endswith("/v1") else f"{clean_base}/v1/models"
            opener = build_safe_urllib_opener(proxy=px or "")
            req = urllib.request.Request(
                models_url,
                headers={"Authorization": f"Bearer {key_val}", "Accept": "application/json", "User-Agent": "RegisterHub-Lite/2.0"},
            )
            t0 = time.perf_counter()
            try:
                with opener.open(req, timeout=10) as r:
                    raw = r.read(65536).decode("utf-8", errors="ignore")
                    cnt = 0
                    try:
                        j = json.loads(raw)
                        if isinstance(j, dict) and isinstance(j.get("data"), list):
                            cnt = len(j["data"])
                    except Exception:
                        pass
                    ms = round((time.perf_counter() - t0) * 1000, 1)
                    self._send_json({"ok": True, "detail": f"HTTP {r.status} · 存活 ({cnt} 模型 · {ms}ms)"})
            except urllib.error.HTTPError as he:
                self._send_json({"ok": False, "detail": f"HTTP {he.code} 拒绝访问"})
            except Exception as e:
                self._send_json({"ok": False, "detail": f"探测异常: {str(e)[:120]}"})
            return

        if path == "/api/failures/clear":
            from core.locks import atomic_write_text_safe, interprocess_file_lock

            clear_snaps = bool(body.get("clear_snapshots", False))
            if FAILED_FILE.exists():
                with interprocess_file_lock(FAILED_FILE):
                    if FAILED_FILE.exists():
                        atomic_write_text_safe(FAILED_FILE, "")
            snaps_removed = 0
            if clear_snaps and SNAPSHOTS_DIR.exists():
                for fp in SNAPSHOTS_DIR.glob("*.png"):
                    try:
                        fp.unlink()
                        snaps_removed += 1
                    except OSError:
                        pass
            self._send_json({"ok": True, "snapshots_removed": snaps_removed})
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
                    probe_res = _probe_target_page(target_url)
                    detected_fields = probe_res.get("fields")
                    probed_msg = f"（已自动探测 {probe_res.get('detected_count', 0)} 项表单选择器）"
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

        if path == "/api/tools/har":
            har_text = str(body.get("har_text") or "")
            site_name = str(body.get("site_name") or "").strip()
            if len(har_text) < 2 or len(har_text) > 15_000_000:
                self._send_json({"ok": False, "error": "HAR 内容为空或超过 15MB 限制"}, status=400)
                return
            try:
                self._send_json(_parse_har_community(har_text, site_name=site_name))
            except Exception as e:
                self._send_json({"ok": False, "error": f"HAR 解析失败: {e}"}, status=400)
            return

        if path == "/api/layout/auto":
            try:
                from core.calibrator import auto_generate_layout

                q_mode = body.get("quad_mode") if isinstance(body.get("quad_mode"), str) else None
                h_mode = body.get("hex_mode") if isinstance(body.get("hex_mode"), str) else None
                geo = auto_generate_layout(save_path=GEOMETRY_FILE, quad_mode=q_mode, hex_mode=h_mode)
                self._send_json({"ok": True, "geometry": geo})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
            return

        if path == "/api/layout/update":
            try:
                from core.calibrator import auto_generate_layout
                from core.locks import atomic_write_text_safe, interprocess_file_lock

                key = str(body.get("preset_key") or "").strip().lower()
                z = round(max(0.40, min(1.50, float(body.get("zoom") or 1.0))), 2)
                if not GEOMETRY_FILE.exists():
                    auto_generate_layout(save_path=GEOMETRY_FILE)
                with interprocess_file_lock(GEOMETRY_FILE):
                    data = json.loads(GEOMETRY_FILE.read_text(encoding="utf-8"))
                    slots = None
                    if key == "single":
                        slots = (data.get("single_concurrency") or {}).get("slots")
                        data["zoom"] = z
                    elif key == "dual":
                        slots = (data.get("dual_concurrency") or {}).get("slots")
                    elif key == "quad_edge":
                        slots = ((data.get("quad_presets") or {}).get("edge_2x2") or {}).get("slots")
                    elif key == "quad_compact":
                        slots = ((data.get("quad_presets") or {}).get("centered_compact") or {}).get("slots")
                    elif key == "hex_edge":
                        slots = ((data.get("hex_presets") or {}).get("edge_3x2") or {}).get("slots")
                    elif key == "hex_wing":
                        slots = ((data.get("hex_presets") or {}).get("wing_cascade") or {}).get("slots")
                    else:
                        self._send_json({"ok": False, "error": f"未知预设键: {key}"}, status=400)
                        return
                    if not isinstance(slots, list) or not slots:
                        self._send_json({"ok": False, "error": "缺少槽位数据，请先自动适配屏幕"}, status=400)
                        return
                    for s in slots:
                        if isinstance(s, dict):
                            s["zoom"] = z
                    if key == "quad_edge" and data.get("quad_mode") == "edge_2x2":
                        data["slots"] = slots
                    elif key == "quad_compact" and data.get("quad_mode") == "centered_compact":
                        data["slots"] = slots
                    data["calibrated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    atomic_write_text_safe(GEOMETRY_FILE, json.dumps(data, ensure_ascii=False, indent=2))
                self._send_json({"ok": True, "geometry": data})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
            return

        if path == "/api/layout/calibrate":
            try:
                c = int(body.get("concurrency") or 1)
                c = c if c in (1, 2, 4, 6) else 1
                url = str(body.get("url") or "").strip()
                cmd = [sys.executable, str(ROOT_DIR / "main.py"), "--calibrate", "-c", str(c)]
                if url.startswith(("http://", "https://")):
                    cmd.extend(["--calibrate-url", url])
                cflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0
                proc = subprocess.Popen(cmd, cwd=str(ROOT_DIR), creationflags=cflags)
                self._send_json({
                    "ok": True,
                    "pid": proc.pid,
                    "concurrency": c,
                    "message": f"已在桌面启动 {c} 窗口真机交互标定（调整后按弹窗终端回车锁定保存）",
                })
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
            return

        if path == "/api/layout/demo":
            try:
                sec = max(2, min(20, int(body.get("hold_sec") or 5)))
                cmd = [sys.executable, str(ROOT_DIR / "main.py"), "--demo-layouts", "--demo-sec", str(sec)]
                cflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0
                proc = subprocess.Popen(cmd, cwd=str(ROOT_DIR), creationflags=cflags)
                self._send_json({
                    "ok": True,
                    "pid": proc.pid,
                    "hold_sec": sec,
                    "message": f"已在桌面启动 6 套真机视窗实景巡演（每套展示 {sec} 秒）",
                })
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
            return

        if path == "/api/llm/config":
            try:
                saved = llm_client.save_llm_config(body)
                self._send_json({"ok": True, "config": saved})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
            return

        if path == "/api/llm/test":
            res = llm_client.test_connection(overrides=body if body else None)
            self._send_json(res)
            return

        if path == "/api/llm/models":
            res = llm_client.discover_models(overrides=body if body else None)
            self._send_json(res)
            return

        if path == "/api/agent/sessions":
            title = str(body.get("title") or "").strip()
            target_url = str(body.get("target_url") or "").strip()
            sess = agent_manager.create_session(title=title, target_url=target_url)
            self._send_json({"ok": True, "session": sess.to_summary()})
            return

        m_ag_msg = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/message", path)
        if m_ag_msg:
            sid = m_ag_msg.group(1)
            msg_text = str(body.get("message") or "").strip()
            target_url = str(body.get("target_url") or "").strip()
            try:
                res = agent_manager.send_message(sid, msg_text, target_url=target_url)
                self._send_json(res)
            except KeyError as ke:
                self._send_json({"ok": False, "error": str(ke)}, status=404)
            except ValueError as ve:
                self._send_json({"ok": False, "error": str(ve)}, status=400)
            except RuntimeError as re_err:
                self._send_json({"ok": False, "error": str(re_err)}, status=409)
            return

        m_ag_stop = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/stop", path)
        if m_ag_stop:
            res = agent_manager.stop_session(m_ag_stop.group(1))
            if res is None:
                self._send_json({"ok": False, "error": "Agent session not found"}, status=404)
                return
            self._send_json(res)
            return

        self._send_json({"error": "Not Found"}, status=404)

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        body = self._read_json()

        m_ag_patch = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)", path)
        if m_ag_patch:
            sid = m_ag_patch.group(1)
            raw_title = body.get("title")
            if not isinstance(raw_title, str):
                self._send_json({"ok": False, "error": "会话标题不能为空"}, status=400)
                return
            try:
                summary = agent_manager.rename_session(sid, raw_title)
            except ValueError as ve:
                self._send_json({"ok": False, "error": str(ve)}, status=400)
                return
            if summary is None:
                self._send_json({"ok": False, "error": "Agent session not found"}, status=404)
                return
            self._send_json({"ok": True, "session": summary})
            return

        self._send_json({"error": "Not Found"}, status=404)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path

        m_ag_del = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)", path)
        if m_ag_del:
            sid = m_ag_del.group(1)
            ok = agent_manager.delete_session(sid)
            if not ok:
                self._send_json({"ok": False, "error": "Agent session not found"}, status=404)
                return
            self._send_json({"ok": True, "deleted": sid})
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
        agent_manager.stop_all()
        job_manager.stop_all()
        srv.server_close()


if __name__ == "__main__":
    start_server()
