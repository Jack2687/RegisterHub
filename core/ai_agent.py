"""RegisterHub Lite Autonomous Registration Agent (Zero External Dependencies).

Strictly single-thread safe for Community Edition:
  - Only one agent turn (and one adapter subprocess) may run at a time.
  - Up to 8 LLM/tool steps per user turn, cleanly cancellable via `stop()`.
  - Persists session history, plan steps, tool calls, and token usage to
    `results/agent_sessions/<id>.json` with atomic writes.
  - Exposes 7 real tools to any OpenAI-compatible LLM via Function Calling:
      1. probe_url(url)
      2. list_adapters()
      3. read_adapter(name)
      4. write_adapter(name, code)
      5. run_adapter_once(adapter)
      6. read_failures()
      7. update_plan(steps)
  - Real-time SSE event pub/sub per session (`plan`, `assistant_msg`,
    `tool_start`, `tool_end`, `usage`, `status`, plus `user_msg`).
"""

from __future__ import annotations

import ast
import atexit
import collections
import html.parser
import json
import keyword
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from core import llm_client

ROOT_DIR = Path(__file__).resolve().parent.parent
SITES_DIR = ROOT_DIR / "sites"
RESULTS_DIR = ROOT_DIR / "results"
ACCOUNTS_FILE = RESULTS_DIR / "accounts.txt"
FAILED_FILE = RESULTS_DIR / "failed.txt"
CONFIG_FILE = ROOT_DIR / "config.json"
AGENT_SESSIONS_DIR = RESULTS_DIR / "agent_sessions"

MAX_STEPS_PER_TURN = 8
RUN_ADAPTER_TIMEOUT_S = 120

_SAFE_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_SAFE_ADAPTER_WRITE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,48}$")
_SAFE_ADAPTER_READ_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,48}$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_RESERVED_ADAPTER_NAMES = frozenset({"init", "template", "conftest", "main", "base", "adapter"})
_CREDENTIAL_FIELD_NAMES = frozenset({
    "api_key",
    "apikey",
    "token",
    "access_token",
    "auth_token",
    "bearer_token",
    "secret",
    "secret_key",
    "credential",
})
_FAKE_PLACEHOLDER_VALUES = frozenset({
    "active",
    "ok",
    "success",
    "done",
    "true",
    "verified",
    "registered",
    "placeholder",
    "todo",
    "n/a",
    "none",
    "dummy",
    "fake",
    "mock",
    "mock_key",
    "test_key",
    "your_api_key",
})
_FAKE_LEDGER_SUBSTRINGS = ("----active", "----ok", "----success", "----placeholder", "----todo")

AGENT_SYSTEM_PROMPT = """你是 RegisterHub 开源社区版（单线程安全模式）的自主注册工程师 AI 代理。

工作区契约：
- 站点适配器位于 `sites/<name>.py`，必须定义继承 `core.adapter.BaseSiteAdapter` 的类，实现 `target_url()`、`prepare_identity()`、`fill_form(identity)`、`submit_and_confirm()`。
- 可复用工具：`core.identity.generate_identity`、`core.pilot.{human_type, smart_click, smart_agree_tos}`、`core.mail_client.DisposableMailClient`、`core.imap_mail.SingleImapMailClient`。
- 成功账号由 `StandardRunner` 自动写入 `results/accounts.txt`（支持 4 列 `email----password----username----api_key`），失败日志写入 `results/failed.txt`。

核心工程红线：
1. 开工第一步先调用 `update_plan` 列出 3-5 个清晰步骤，并随进展更新状态（pending / in_progress / done / failed）。
2. 严禁虚构成功或写入假占位符（例如禁止将 `"Active"`、`"OK"`、`"placeholder"` 塞进 `account_data["api_key"]`）。`submit_and_confirm()` 必须在明确检测到注册成功标志时才返回 `True`。
3. 编写适配器前先用 `probe_url` 探测目标页表单与风控特征，必要时用 `list_adapters` / `read_adapter("_template")` 对齐风格。
4. 写完适配器后用 `run_adapter_once` 单线程试跑 1 个号，检查退出码、`accounts_delta` 与输出日志；失败时读取 `read_failures` 精准定位根因并修复。
5. 严格单线程串行执行，回答用简洁中文，明确交代探测结果、改动文件、试跑结论与下一步建议。
"""

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "probe_url",
            "description": "对目标注册页执行 HTTP/HTML 轻量探测，提取表单、输入框选择器、按钮及反爬/鉴权框架特征（Turnstile/reCAPTCHA/Clerk/Supabase 等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "以 http:// 或 https:// 开头的目标页面 URL"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_adapters",
            "description": "扫描并列出 sites/*.py 下已存在的站点适配器及其目标 URL。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_adapter",
            "description": "安全读取指定适配器源码 sites/<name>.py（支持读取 _template 参考标准骨架）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "适配器名称，不含 .py 后缀（如 demo_site 或 _template）"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_adapter",
            "description": "校验语法与安全红线后原子写入 sites/<name>.py（覆盖旧文件前自动生成 .bak 备份）。必须定义 BaseSiteAdapter 子类，禁止假 Active 占位凭据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "合法英文标识符适配器名（如 my_site，不含路径和扩展名）"},
                    "code": {"type": "string", "description": "完整可执行的 Python 适配器源代码"},
                },
                "required": ["name", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_adapter_once",
            "description": "严格单线程试跑指定适配器 1 个账号（python main.py --run <adapter> --count 1，120s 超时），返回退出码、日志尾部和账号增量。",
            "parameters": {
                "type": "object",
                "properties": {
                    "adapter": {"type": "string", "description": "要试跑的适配器名称"},
                },
                "required": ["adapter"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_failures",
            "description": "读取 results/failed.txt 中最近的失败记录，用于排查报错根因。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回最近条数（默认 20，最大 50）"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": "更新界面右侧/左侧实时展示的执行计划清单。",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "description": "计划步骤列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "done", "failed"],
                                },
                            },
                            "required": ["title", "status"],
                        },
                    }
                },
                "required": ["steps"],
            },
        },
    },
]


def _kill_process_tree(pid: Optional[int], proc: Optional[subprocess.Popen[str]] = None) -> None:
    """Terminate ONLY the child process tree we spawned (never touch unrelated Chrome)."""
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


def _read_proxy_from_config() -> str:
    try:
        if CONFIG_FILE.exists():
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return str((raw.get("browser") or {}).get("proxy") or "").strip()
    except Exception:
        pass
    return ""


class _FormHTMLParser(html.parser.HTMLParser):
    """Zero-dependency HTML form/input/button/link inspector."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.title_parts: List[str] = []
        self.forms: List[Dict[str, str]] = []
        self.inputs: List[Dict[str, str]] = []
        self.buttons: List[Dict[str, str]] = []
        self.auth_links: List[Dict[str, str]] = []
        self._cur_btn: Optional[Dict[str, str]] = None
        self._cur_link: Optional[Dict[str, str]] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        amap = {k.lower(): (v or "") for k, v in attrs}
        if t == "title":
            self.in_title = True
        elif t == "form" and len(self.forms) < 10:
            self.forms.append({
                "id": amap.get("id", "")[:60],
                "action": amap.get("action", "")[:160],
                "method": (amap.get("method") or "GET").upper()[:12],
            })
        elif t in ("input", "select", "textarea") and len(self.inputs) < 35:
            itype = amap.get("type", "text" if t == "input" else t).lower()
            if itype == "hidden" and not any(k in (amap.get("name", "").lower()) for k in ("csrf", "turnstile", "captcha", "token")):
                return
            entry = {
                "tag": t,
                "type": itype[:24],
                "name": amap.get("name", "")[:60],
                "id": amap.get("id", "")[:60],
                "placeholder": amap.get("placeholder", "")[:80],
                "autocomplete": amap.get("autocomplete", "")[:40],
                "aria_label": amap.get("aria-label", "")[:80],
            }
            self.inputs.append({k: v for k, v in entry.items() if v})
        elif t == "button" and len(self.buttons) < 20:
            self._cur_btn = {
                "type": amap.get("type", "button")[:20],
                "id": amap.get("id", "")[:60],
                "text": "",
            }
        elif t == "a" and len(self.auth_links) < 12:
            href = amap.get("href", "")
            low = href.lower()
            if any(k in low for k in ("signup", "sign-up", "register", "oauth", "auth", "login", "google", "github", "microsoft")):
                self._cur_link = {"href": href[:180], "text": ""}

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "title":
            self.in_title = False
        elif t == "button" and self._cur_btn is not None:
            self._cur_btn["text"] = " ".join(self._cur_btn["text"].split())[:80]
            self.buttons.append({k: v for k, v in self._cur_btn.items() if v})
            self._cur_btn = None
        elif t == "a" and self._cur_link is not None:
            self._cur_link["text"] = " ".join(self._cur_link["text"].split())[:80]
            self.auth_links.append({k: v for k, v in self._cur_link.items() if v})
            self._cur_link = None

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self._cur_btn is not None and len(self._cur_btn["text"]) < 120:
            self._cur_btn["text"] += " " + data
        if self._cur_link is not None and len(self._cur_link["text"]) < 120:
            self._cur_link["text"] += " " + data


def tool_probe_url(url: str) -> Dict[str, Any]:
    """Tool 1: Zero-dependency HTTP/HTML form & anti-bot detection (SSRF guarded)."""
    from core.net_guard import (
        PrivateAddressBlockedError,
        build_safe_urllib_opener,
        validate_proxy_url,
        validate_public_http_url,
    )

    target = str(url or "").strip()
    if not target.startswith(("http://", "https://")):
        return {"ok": False, "error": "URL 必须以 http:// 或 https:// 开头"}

    try:
        clean_target = validate_public_http_url(target, resolve_dns=True)
        raw_px = _read_proxy_from_config()
        proxy = validate_proxy_url(raw_px) if raw_px else ""
    except (PrivateAddressBlockedError, ValueError) as exc:
        return {"ok": False, "url": target, "error": str(exc)}

    opener = build_safe_urllib_opener(proxy=proxy or "")
    req = urllib.request.Request(
        clean_target,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    t0 = time.perf_counter()
    status_code = 0
    final_url = target
    resp_headers: Dict[str, str] = {}
    html_text = ""
    fetch_error = ""

    try:
        with opener.open(req, timeout=15) as resp:
            status_code = int(getattr(resp, "status", 200) or 200)
            final_url = str(resp.geturl() or target)
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            html_text = resp.read(220_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as he:
        status_code = int(he.code)
        final_url = str(getattr(he, "url", target) or target)
        try:
            resp_headers = {k.lower(): v for k, v in (he.headers.items() if he.headers else [])}
            html_text = he.read(120_000).decode("utf-8", errors="replace")
        except Exception:
            pass
        fetch_error = f"HTTP {he.code}: {he.reason}"
    except Exception as exc:
        return {
            "ok": False,
            "url": target,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "error": f"页面请求失败: {exc}",
        }

    parser = _FormHTMLParser()
    try:
        parser.feed(html_text)
    except Exception:
        pass

    title = " ".join("".join(parser.title_parts).split())[:140]
    low_html = html_text.lower()
    headers_blob = " ".join(f"{k}:{v}" for k, v in resp_headers.items()).lower()
    combined = f"{headers_blob}\n{low_html}"

    signal_rules = [
        ("Cloudflare Turnstile", ("challenges.cloudflare.com/turnstile", "cf-turnstile")),
        ("Cloudflare Managed WAF", ("cf-mitigated", "just a moment...", "cdn-cgi/challenge-platform")),
        ("Google reCAPTCHA", ("recaptcha/api.js", "g-recaptcha", "recaptcha/enterprise")),
        ("hCaptcha", ("hcaptcha.com", "h-captcha")),
        ("Arkose / FunCaptcha", ("arkoselabs.com", "funcaptcha")),
        ("GeeTest", ("geetest.com", "gt_captcha")),
        ("Clerk Auth", ("clerk.accounts", "__clerk", "clerk-js")),
        ("Supabase Auth", ("supabase.co", "/auth/v1/")),
        ("Auth0", ("auth0.com", "auth0-lock")),
        ("NextAuth", ("/api/auth/signin", "next-auth")),
        ("Firebase Auth", ("firebaseauth", "identitytoolkit.googleapis.com")),
        ("OAuth Login", ("accounts.google.com/o/oauth", "github.com/login/oauth", "login.microsoftonline.com")),
    ]
    signals = [label for label, needles in signal_rules if any(n in combined for n in needles)]

    is_spa_shell = bool(
        not parser.inputs
        and any(m in low_html for m in ('id="root"', 'id="__next"', 'id="app"', "__next_data__", "data-reactroot"))
    )
    if is_spa_shell:
        signals.append("Client-Side SPA (DOM inputs rendered by JS)")

    return {
        "ok": status_code < 400 or bool(html_text),
        "url": final_url,
        "status_code": status_code,
        "title": title,
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "signals": signals,
        "forms_count": len(parser.forms),
        "forms": parser.forms[:6],
        "inputs": parser.inputs[:25],
        "buttons": parser.buttons[:15],
        "auth_links": parser.auth_links[:8],
        "server": resp_headers.get("server", ""),
        "warning": fetch_error,
    }


def _inspect_adapter_ast(py_file: Path) -> Tuple[List[str], str, int]:
    """Extract BaseSiteAdapter subclass names & target_url literal via AST without importing."""
    text = py_file.read_text(encoding="utf-8", errors="replace")
    lines = len(text.splitlines())
    classes: List[str] = []
    target_url = ""
    try:
        tree = ast.parse(text, filename=str(py_file))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                base_names = []
                for b in node.bases:
                    if isinstance(b, ast.Name):
                        base_names.append(b.id)
                    elif isinstance(b, ast.Attribute):
                        base_names.append(b.attr)
                if "BaseSiteAdapter" in base_names:
                    classes.append(node.name)
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name == "target_url":
                            for sub in ast.walk(item):
                                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Constant) and isinstance(sub.value.value, str):
                                    target_url = sub.value.value[:120]
                                    break
    except Exception:
        pass
    return classes, target_url, lines


def tool_list_adapters() -> Dict[str, Any]:
    """Tool 2: Discover all adapters in sites/*.py."""
    if not SITES_DIR.exists():
        return {"ok": True, "count": 0, "adapters": []}
    rows: List[Dict[str, Any]] = []
    for fp in sorted(SITES_DIR.glob("*.py")):
        if fp.name.startswith("__") or fp.name.startswith("_"):
            continue
        classes, target_url, lines = _inspect_adapter_ast(fp)
        rows.append({
            "name": fp.stem.lower(),
            "file": f"sites/{fp.name}",
            "classes": classes,
            "target_url": target_url,
            "lines": lines,
        })
    return {"ok": True, "count": len(rows), "adapters": rows}


def tool_read_adapter(name: str) -> Dict[str, Any]:
    """Tool 3: Safely read sites/<name>.py (or _template)."""
    raw = str(name or "").strip()
    if raw.endswith(".py"):
        raw = raw[:-3]
    if not raw or ".." in raw or "/" in raw or "\\" in raw or not _SAFE_ADAPTER_READ_RE.fullmatch(raw):
        return {"ok": False, "error": f"非法适配器名称: {name!r}"}
    clean = raw.lower().replace("-", "_")
    if clean.startswith("__"):
        return {"ok": False, "error": "不允许读取内部模块文件"}

    sites_root = SITES_DIR.resolve()
    target = (sites_root / f"{clean}.py").resolve()
    if target.parent != sites_root or not target.is_file():
        return {"ok": False, "error": f"未找到适配器文件: sites/{clean}.py"}

    text = target.read_text(encoding="utf-8", errors="replace")
    max_chars = 24_000
    truncated = len(text) > max_chars
    return {
        "ok": True,
        "name": clean,
        "file": f"sites/{clean}.py",
        "lines": len(text.splitlines()),
        "truncated": truncated,
        "code": text[:max_chars],
    }


def _detect_fake_placeholder_violation(tree: ast.AST) -> Optional[str]:
    """Reject fake placeholder credentials such as api_key='Active', key or 'Active', or {'api_key': 'Active'}."""

    def _is_fake_str(node: Optional[ast.AST]) -> Optional[str]:
        if node is None:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            val = node.value.strip()
            if val.lower() in _FAKE_PLACEHOLDER_VALUES:
                return val
        elif isinstance(node, ast.BoolOp):
            for sub_val in node.values:
                hit = _is_fake_str(sub_val)
                if hit:
                    return hit
        elif isinstance(node, ast.IfExp):
            return _is_fake_str(node.body) or _is_fake_str(node.orelse)
        elif isinstance(node, ast.Dict):
            for v_node in node.values:
                hit = _is_fake_str(v_node)
                if hit:
                    return hit
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in ("get", "setdefault"):
                if len(node.args) >= 2:
                    hit = _is_fake_str(node.args[1])
                    if hit:
                        return hit
                for kw in node.keywords:
                    if kw.arg == "default":
                        hit = _is_fake_str(kw.value)
                        if hit:
                            return hit
            elif isinstance(node.func, ast.Name) and node.func.id == "str" and node.args:
                hit = _is_fake_str(node.args[0])
                if hit:
                    return hit
        return None

    def _target_is_cred(target: ast.AST) -> bool:
        if isinstance(target, ast.Name):
            return target.id.lower() in _CREDENTIAL_FIELD_NAMES
        if isinstance(target, ast.Attribute):
            return target.attr.lower() in _CREDENTIAL_FIELD_NAMES
        if isinstance(target, ast.Subscript):
            sl = target.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                return sl.value.strip().lower() in _CREDENTIAL_FIELD_NAMES
        return False

    for node in ast.walk(tree):
        # 1. Direct string literal containing fake ledger columns like "----Active"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            low_s = node.value.lower()
            for marker in _FAKE_LEDGER_SUBSTRINGS:
                if marker in low_s:
                    return f"禁止写入伪造占位凭据字面量 ({node.value[:40]!r})"

        # 2. Assignment: self.api_key = "Active" / account_data["api_key"] = key or "Active"
        if isinstance(node, ast.Assign):
            if any(_target_is_cred(t) for t in node.targets):
                fake = _is_fake_str(node.value)
                if fake:
                    return f"禁止将假占位值 {fake!r} 写入凭据/API Key 字段"

        if isinstance(node, ast.AnnAssign):
            if _target_is_cred(node.target):
                fake = _is_fake_str(node.value)
                if fake:
                    return f"禁止将假占位值 {fake!r} 写入凭据/API Key 字段"

        # 3. Dict literal: {"api_key": "Active"} / {"api_key": key or "Active"}
        if isinstance(node, ast.Dict):
            for k_node, v_node in zip(node.keys, node.values):
                if isinstance(k_node, ast.Constant) and isinstance(k_node.value, str):
                    if k_node.value.strip().lower() in _CREDENTIAL_FIELD_NAMES:
                        fake = _is_fake_str(v_node)
                        if fake:
                            return f"禁止在字典中将 {k_node.value!r} 设为假占位值 {fake!r}"

        # 4. Keyword args & dict.setdefault("api_key", "Active") / dict.get("api_key", "Active")
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg and kw.arg.lower() in _CREDENTIAL_FIELD_NAMES:
                    fake = _is_fake_str(kw.value)
                    if fake:
                        return f"禁止将参数 {kw.arg} 设为假占位值 {fake!r}"
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in ("setdefault", "get")
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value.strip().lower() in _CREDENTIAL_FIELD_NAMES
            ):
                fake = _is_fake_str(node.args[1])
                if fake:
                    return f"禁止为凭据键 {node.args[0].value!r} 提供假占位默认值 {fake!r}"

        # 5. Key/token getter returning "Active" or `key or "Active"`
        if isinstance(node, ast.FunctionDef) and any(k in node.name.lower() for k in ("api_key", "extract_key", "get_key", "fetch_key", "token")):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return):
                    fake = _is_fake_str(sub.value)
                    if fake:
                        return f"函数 {node.name}() 禁止返回假占位凭据 {fake!r}"

    return None


def tool_write_adapter(name: str, code: str) -> Dict[str, Any]:
    """Tool 4: Validate identifier, AST/compile, anti-fake-Active, backup .bak, atomic write."""
    raw_name = str(name or "").strip()
    if not raw_name or ".." in raw_name or "/" in raw_name or "\\" in raw_name or "." in raw_name:
        return {"ok": False, "error": f"非法适配器名（禁止包含路径分隔符或扩展名）: {name!r}"}
    if not _SAFE_ADAPTER_WRITE_RE.fullmatch(raw_name):
        return {"ok": False, "error": f"适配器名必须以字母开头，仅含字母/数字/下划线/连字符: {name!r}"}

    clean = raw_name.lower().replace("-", "_")
    if keyword.iskeyword(clean) or clean in _RESERVED_ADAPTER_NAMES:
        return {"ok": False, "error": f"适配器名不能使用保留字: {clean!r}"}

    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "error": "代码内容不能为空"}
    if len(code) > 120_000:
        return {"ok": False, "error": "适配器代码超过 120KB 大小限制"}

    filename = f"sites/{clean}.py"
    try:
        tree = ast.parse(code, filename=filename)
        compile(code, filename, "exec")
    except SyntaxError as se:
        return {"ok": False, "error": f"Python 语法校验失败 (第 {se.lineno} 行): {se.msg}"}
    except Exception as exc:
        return {"ok": False, "error": f"Python 编译校验失败: {exc}"}

    # Ensure at least one BaseSiteAdapter subclass is defined so discover_adapters() picks it up
    has_adapter_cls = False
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if (isinstance(base, ast.Name) and base.id == "BaseSiteAdapter") or (
                    isinstance(base, ast.Attribute) and base.attr == "BaseSiteAdapter"
                ):
                    has_adapter_cls = True
                    break
    if not has_adapter_cls:
        return {"ok": False, "error": "适配器代码必须定义继承 BaseSiteAdapter 的类（可先 read_adapter('_template') 参考）"}

    fake_violation = _detect_fake_placeholder_violation(tree)
    if fake_violation:
        return {"ok": False, "error": f"反假成功红线拦截：{fake_violation}"}

    SITES_DIR.mkdir(parents=True, exist_ok=True)
    sites_root = SITES_DIR.resolve()
    target = (sites_root / f"{clean}.py").resolve()
    if target.parent != sites_root:
        return {"ok": False, "error": "安全路径校验失败：写入目标越界"}

    backup_name = ""
    if target.exists():
        bak_path = target.with_suffix(".py.bak")
        bak_tmp = target.with_suffix(".py.bak.tmp")
        bak_tmp.write_bytes(target.read_bytes())
        os.replace(bak_tmp, bak_path)
        backup_name = bak_path.name

    tmp_path = target.with_suffix(".py.tmp")
    normalized_code = code if code.endswith("\n") else (code + "\n")
    tmp_path.write_text(normalized_code, encoding="utf-8")
    os.replace(tmp_path, target)

    # Clear cached module so sites.discover_adapters() loads updated code
    sys.modules.pop(f"sites.{clean}", None)

    return {
        "ok": True,
        "name": clean,
        "file": filename,
        "backup": backup_name,
        "lines": len(normalized_code.splitlines()),
    }


def _snapshot_accounts_count() -> Tuple[int, List[str]]:
    if not ACCOUNTS_FILE.exists():
        return 0, []
    lines = [
        ln.strip()
        for ln in ACCOUNTS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
        if ln.strip() and not ln.strip().startswith("#") and "----" in ln
    ]
    return len(lines), lines


def tool_run_adapter_once(
    adapter: str,
    *,
    session: Optional["AgentSession"] = None,
    stop_ev: Optional[threading.Event] = None,
    timeout: int = RUN_ADAPTER_TIMEOUT_S,
) -> Dict[str, Any]:
    """Tool 5: Single-thread run of `python main.py --run <clean_adapter> --count 1` (120s timeout)."""
    raw = str(adapter or "").strip().lower()
    if not raw or not _SAFE_ADAPTER_WRITE_RE.fullmatch(raw):
        return {"ok": False, "error": f"非法适配器名: {adapter!r}"}

    clean = raw.replace("-", "_")
    # Check adapter exists either as sites/<clean>.py or via alias (e.g. demo -> demo_site.py)
    candidate_exists = (SITES_DIR / f"{clean}.py").is_file() or (SITES_DIR / f"{clean}_site.py").is_file()
    if not candidate_exists:
        return {"ok": False, "error": f"未找到适配器: {clean}（请先 list_adapters 或 write_adapter）"}

    before_cnt, before_lines = _snapshot_accounts_count()
    cmd = [sys.executable, "-u", str(ROOT_DIR / "main.py"), "--run", clean, "--count", "1"]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    def _should_stop() -> bool:
        if stop_ev is not None:
            return stop_ev.is_set()
        return bool(session is not None and session.is_stop_requested())

    t0 = time.time()
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

    if session is not None:
        session.set_active_process(proc, stop_ev=stop_ev)

    tail_lines: collections.deque[str] = collections.deque(maxlen=200)

    def _drain() -> None:
        try:
            assert proc.stdout is not None
            for raw_ln in proc.stdout:
                tail_lines.append(_ANSI_RE.sub("", raw_ln.rstrip("\r\n")))
        except Exception:
            pass

    reader_th = threading.Thread(target=_drain, daemon=True)
    reader_th.start()

    timed_out = False
    stopped = False
    try:
        while proc.poll() is None:
            if _should_stop():
                stopped = True
                _kill_process_tree(proc.pid, proc)
                break
            if (time.time() - t0) >= timeout:
                timed_out = True
                _kill_process_tree(proc.pid, proc)
                break
            time.sleep(0.2)
        reader_th.join(timeout=2.0)
    finally:
        if session is not None:
            session.clear_active_process(proc)

    rc = proc.returncode if proc.returncode is not None else -1
    after_cnt, after_lines = _snapshot_accounts_count()
    delta = max(0, after_cnt - before_cnt)
    new_accounts: List[Dict[str, Any]] = []
    if delta > 0:
        for ln in after_lines[before_cnt:]:
            parts = [p.strip() for p in ln.split("----")]
            email = parts[0] if parts else ""
            ak = parts[3] if len(parts) >= 4 else ""
            new_accounts.append({
                "email": email,
                "has_api_key": bool(ak),
                "api_key_hint": llm_client.mask_api_key(ak) if ak else "",
            })

    output_tail = "\n".join(tail_lines)[-4000:]
    unknown_adapter = "[-] unknown adapter" in output_tail.lower()
    ok = rc == 0 and not timed_out and not stopped and not unknown_adapter
    res: Dict[str, Any] = {
        "ok": ok,
        "adapter": clean,
        "exit_code": rc,
        "timed_out": timed_out,
        "stopped": stopped,
        "duration_s": round(time.time() - t0, 1),
        "accounts_before": before_cnt,
        "accounts_after": after_cnt,
        "accounts_delta": delta,
        "new_accounts": new_accounts,
        "output_tail": output_tail,
    }
    if unknown_adapter:
        res["error"] = f"未识别的适配器: {clean}"
    return res


def tool_read_failures(limit: int = 20) -> Dict[str, Any]:
    """Tool 6: Read recent lines from results/failed.txt."""
    try:
        lim = max(1, min(50, int(limit or 20)))
    except (TypeError, ValueError):
        lim = 20
    if not FAILED_FILE.exists():
        return {"ok": True, "total_lines": 0, "recent": []}
    lines = [
        ln.strip()[:500]
        for ln in FAILED_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    return {
        "ok": True,
        "total_lines": len(lines),
        "recent": lines[-lim:],
    }


def _normalize_plan_steps(steps: Any) -> List[Dict[str, str]]:
    if not isinstance(steps, list):
        return []
    valid_statuses = {"pending", "in_progress", "done", "failed"}
    out: List[Dict[str, str]] = []
    for item in steps[:12]:
        if isinstance(item, dict):
            title = str(item.get("title") or "").strip()[:120]
            status = str(item.get("status") or "pending").strip().lower()
            if status not in valid_statuses:
                status = "pending"
            if title:
                out.append({"title": title, "status": status})
        elif isinstance(item, str) and item.strip():
            out.append({"title": item.strip()[:120], "status": "pending"})
    return out


def _repair_dangling_tool_calls(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Repair ALL unanswered tool_call_ids across prior assistant turns and drop orphaned tool msgs."""
    repaired: List[Dict[str, Any]] = []
    n = len(messages)
    i = 0
    while i < n:
        msg = messages[i]
        if not isinstance(msg, dict):
            i += 1
            continue
        role = msg.get("role")
        if role == "tool":
            # Drop orphaned leading/stray tool messages that have no parent assistant(tool_calls)
            i += 1
            continue
        if role == "assistant" and msg.get("tool_calls"):
            tool_calls = [tc for tc in (msg.get("tool_calls") or []) if isinstance(tc, dict)]
            expected_ids = {str(tc.get("id") or "") for tc in tool_calls if str(tc.get("id") or "")}
            repaired.append(msg)
            i += 1
            answered_ids = set()
            while i < n and isinstance(messages[i], dict) and messages[i].get("role") == "tool":
                tmsg = messages[i]
                tid = str(tmsg.get("tool_call_id") or "")
                if not expected_ids or tid in expected_ids:
                    if tid:
                        answered_ids.add(tid)
                    repaired.append(tmsg)
                i += 1
            for idx, tc in enumerate(tool_calls):
                tid = str(tc.get("id") or f"call_stopped_{idx + 1}")
                if tid not in answered_ids:
                    fn = tc.get("function") or {}
                    repaired.append({
                        "role": "tool",
                        "tool_call_id": tid,
                        "name": str(fn.get("name") or "unknown"),
                        "content": json.dumps(
                            {"ok": False, "stopped": True, "error": "上一轮已被用户手动停止"},
                            ensure_ascii=False,
                        ),
                    })
            continue
        repaired.append(msg)
        i += 1
    return repaired


def _slice_messages_safely(messages: List[Dict[str, Any]], max_count: int = 80) -> List[Dict[str, Any]]:
    """Slice to at most `max_count` recent messages without ever cutting between assistant(tool_calls) and tool replies."""
    if max_count <= 0 or len(messages) <= max_count:
        return list(messages)
    start = len(messages) - max_count
    while start < len(messages) and isinstance(messages[start], dict) and messages[start].get("role") == "tool":
        start += 1
    return list(messages[start:])


class AgentSession:
    """Single persisted AI registration session with SSE pub/sub, steering queue, and process tracking."""

    def __init__(
        self,
        session_id: str,
        title: str = "",
        target_url: str = "",
        *,
        status: str = "idle",
        last_error: str = "",
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        plan: Optional[List[Dict[str, str]]] = None,
        usage: Optional[Dict[str, int]] = None,
        events: Optional[List[Dict[str, Any]]] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        now_s = time.strftime("%Y-%m-%d %H:%M:%S")
        self.id = session_id
        self.title = title or "新 AI 直跑会话"
        self.target_url = target_url
        self.status = status
        self.last_error = str(last_error or "")
        self.created_at = created_at or now_s
        self.updated_at = updated_at or now_s
        self.plan: List[Dict[str, str]] = list(plan or [])
        self.usage: Dict[str, int] = {
            "prompt_tokens": int((usage or {}).get("prompt_tokens") or 0),
            "completion_tokens": int((usage or {}).get("completion_tokens") or 0),
            "total_tokens": int((usage or {}).get("total_tokens") or 0),
        }
        self.events: List[Dict[str, Any]] = list(events or [])
        self.messages: List[Dict[str, Any]] = list(messages or [])

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._steer_queue: queue.Queue[str] = queue.Queue()
        self._subs: List[queue.Queue[Optional[Dict[str, Any]]]] = []
        self._active_proc: Optional[subprocess.Popen[str]] = None
        self._active_pid: Optional[int] = None
        self._active_tool_name: Optional[str] = None
        self._active_tool_started_at: Optional[float] = None

    def is_stop_requested(self) -> bool:
        with self._lock:
            ev = self._stop_event
        return ev.is_set()

    def enqueue_steer(self, text: str) -> None:
        clean = str(text or "").strip()
        if clean:
            self._steer_queue.put_nowait(clean)

    def has_pending_steer(self) -> bool:
        return not self._steer_queue.empty()

    def drain_steers(self) -> List[str]:
        items: List[str] = []
        while True:
            try:
                items.append(self._steer_queue.get_nowait())
            except queue.Empty:
                break
        return items

    def set_active_process(
        self,
        proc: subprocess.Popen[str],
        stop_ev: Optional[threading.Event] = None,
    ) -> None:
        with self._lock:
            if stop_ev is not None and (stop_ev.is_set() or self._stop_event is not stop_ev):
                kill_now = True
            else:
                self._active_proc = proc
                self._active_pid = proc.pid
                kill_now = False
        if kill_now:
            _kill_process_tree(proc.pid, proc)

    def clear_active_process(self, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            if self._active_proc is proc:
                self._active_proc = None
                self._active_pid = None

    def set_active_tool(
        self,
        tool_name: str,
        started_at: float,
        stop_ev: Optional[threading.Event] = None,
    ) -> None:
        with self._lock:
            if stop_ev is not None and (stop_ev.is_set() or self._stop_event is not stop_ev):
                return
            self._active_tool_name = str(tool_name or "").strip() or None
            self._active_tool_started_at = started_at

    def clear_active_tool(self, stop_ev: Optional[threading.Event] = None) -> None:
        with self._lock:
            if stop_ev is not None and self._stop_event is not stop_ev:
                return
            self._active_tool_name = None
            self._active_tool_started_at = None

    def emit(self, event_type: str, **payload: Any) -> Dict[str, Any]:
        ev = {
            "type": event_type,
            "ts": time.strftime("%H:%M:%S"),
            **payload,
        }
        with self._lock:
            self.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self.events.append(ev)
            if len(self.events) > 500:
                self.events = self.events[-400:]
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass
        return ev

    def close_streams(self) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass

    def subscribe(self) -> Tuple[List[Dict[str, Any]], queue.Queue[Optional[Dict[str, Any]]]]:
        q: queue.Queue[Optional[Dict[str, Any]]] = queue.Queue(maxsize=500)
        with self._lock:
            hist = list(self.events)
            if self.status == "running":
                self._subs.append(q)
            else:
                q.put_nowait(None)
        return hist, q

    def unsubscribe(self, q: queue.Queue[Optional[Dict[str, Any]]]) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def stop(self) -> str:
        abort_note = ""
        with self._lock:
            self._stop_event.set()
            pid = self._active_pid
            proc = self._active_proc
            tool_name = self._active_tool_name
            tool_started = self._active_tool_started_at
            self._active_tool_name = None
            self._active_tool_started_at = None
            was_running = self.status == "running"
            if was_running:
                self.status = "stopped"
                if tool_name:
                    elapsed = max(0.0, time.perf_counter() - tool_started) if tool_started is not None else 0.0
                    abort_note = f"已手动停止（中断时正在执行工具 {tool_name}，已耗时 {elapsed:.1f}s）"
                self.last_error = abort_note
        self.drain_steers()
        if pid or proc:
            _kill_process_tree(pid, proc)
        if was_running:
            self.emit("status", status="stopped", error=abort_note)
            self.persist()
            self.close_streams()
            return "stopped"
        return self.status

    def to_summary(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "title": self.title,
                "target_url": self.target_url,
                "status": self.status,
                "error": self.last_error,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "plan": list(self.plan),
                "usage": dict(self.usage),
                "event_count": len(self.events),
            }

    def to_detail(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "title": self.title,
                "target_url": self.target_url,
                "status": self.status,
                "error": self.last_error,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "plan": list(self.plan),
                "usage": dict(self.usage),
                "events": list(self.events),
            }

    def to_markdown(self) -> str:
        """Render full session transcript, plan checklist, and tool calls into Markdown."""
        with self._lock:
            sid = self.id
            title = self.title
            target_url = self.target_url
            status = self.status
            created_at = self.created_at
            updated_at = self.updated_at
            plan = list(self.plan)
            usage = dict(self.usage)
            events = list(self.events)

        lines: List[str] = [
            f"# {title}",
            "",
            f"- **Session ID**: `{sid}`",
            f"- **Status**: `{status}`",
        ]
        if target_url:
            lines.append(f"- **Target URL**: {target_url}")
        lines.extend([
            f"- **Created**: {created_at}",
            f"- **Updated**: {updated_at}",
            (
                f"- **Token Usage**: Prompt `{usage.get('prompt_tokens', 0)}` / "
                f"Completion `{usage.get('completion_tokens', 0)}` / "
                f"Total `{usage.get('total_tokens', 0)}`"
            ),
            "",
        ])

        if plan:
            mark_map = {"done": "x", "in_progress": "-", "failed": "!", "pending": " "}
            lines.append("## 执行计划")
            lines.append("")
            for st in plan:
                mark = mark_map.get(st.get("status", "pending"), " ")
                lines.append(f"- [{mark}] {st.get('title', '')} (`{st.get('status', 'pending')}`)")
            lines.append("")

        lines.append("## 对话与工具调用记录")
        lines.append("")

        tool_end_by_id: Dict[str, Dict[str, Any]] = {}
        for ev in events:
            if ev.get("type") == "tool_end" and ev.get("call_id"):
                tool_end_by_id[str(ev["call_id"])] = ev

        rendered_tool_ends = set()
        for ev in events:
            et = ev.get("type")
            ts = ev.get("ts") or ""
            if et == "user_msg":
                tag = " · 实时纠偏" if ev.get("steered") else ""
                lines.append(f"### 用户 ({ts}{tag})")
                lines.append("")
                lines.append(str(ev.get("content") or ""))
                if ev.get("target_url"):
                    lines.append("")
                    lines.append(f"> Target URL: {ev['target_url']}")
                lines.append("")
            elif et == "assistant_msg":
                step_tag = f"Step {ev['step']}" if ev.get("step") else ""
                wrap_tag = " · 收尾总结" if ev.get("wrap_up") else ""
                meta = " · ".join(p for p in (step_tag, ts) if p)
                lines.append(f"### AI Agent ({meta}{wrap_tag})")
                lines.append("")
                lines.append(str(ev.get("content") or ""))
                lines.append("")
            elif et == "tool_start":
                cid = str(ev.get("call_id") or "")
                tname = str(ev.get("name") or "tool")
                end_ev = tool_end_by_id.get(cid)
                if end_ev:
                    rendered_tool_ends.add(cid)
                    state_txt = "成功" if end_ev.get("ok", True) else "失败"
                    dur_txt = f"{end_ev.get('duration_ms', 0)}ms"
                    header = f"#### 工具调用: `{tname}` ({state_txt} · {dur_txt} · {ts})"
                else:
                    header = f"#### 工具调用: `{tname}` (执行中/已中止 · {ts})"
                lines.append(header)
                lines.append("")
                args_json = json.dumps(ev.get("args") or {}, ensure_ascii=False, indent=2)
                lines.append("**Arguments:**")
                lines.append("```json")
                lines.append(args_json[:4000])
                lines.append("```")
                if end_ev:
                    res_json = json.dumps(end_ev.get("result") if end_ev.get("result") is not None else {}, ensure_ascii=False, indent=2)
                    lines.append("**Result:**")
                    lines.append("```json")
                    lines.append(res_json[:4000])
                    lines.append("```")
                lines.append("")
            elif et == "tool_end":
                cid = str(ev.get("call_id") or "")
                if cid and cid in rendered_tool_ends:
                    continue
                tname = str(ev.get("name") or "tool")
                state_txt = "成功" if ev.get("ok", True) else "失败"
                lines.append(f"#### 工具结果: `{tname}` ({state_txt} · {ev.get('duration_ms', 0)}ms · {ts})")
                lines.append("")
                res_json = json.dumps(ev.get("result") if ev.get("result") is not None else {}, ensure_ascii=False, indent=2)
                lines.append("```json")
                lines.append(res_json[:4000])
                lines.append("```")
                lines.append("")
            elif et == "status" and ev.get("status") == "error" and ev.get("error"):
                lines.append(f"> **运行异常 ({ts})**: {ev['error']}")
                lines.append("")
            elif et == "status" and ev.get("status") == "stopped" and ev.get("error"):
                lines.append(f"> **手动停止 ({ts})**: {ev['error']}")
                lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def persist(self) -> None:
        if not _SAFE_SESSION_ID_RE.fullmatch(self.id):
            return
        with self._lock:
            trimmed_msgs = _slice_messages_safely(self.messages, 80)
            payload = {
                "id": self.id,
                "title": self.title,
                "target_url": self.target_url,
                "status": self.status,
                "error": self.last_error,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "plan": list(self.plan),
                "usage": dict(self.usage),
                "events": list(self.events),
                "messages": trimmed_msgs,
            }
        try:
            AGENT_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            out_path = AGENT_SESSIONS_DIR / f"{self.id}.json"
            tmp_path = out_path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp_path, out_path)
        except Exception:
            pass

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentSession":
        status = str(data.get("status") or "idle")
        if status == "running":
            # Recovered from disk after process restart; never falsely claim running
            status = "stopped"
        raw_msgs = data.get("messages") if isinstance(data.get("messages"), list) else []
        return cls(
            session_id=str(data["id"]),
            title=str(data.get("title") or ""),
            target_url=str(data.get("target_url") or ""),
            status=status,
            last_error=str(data.get("error") or ""),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            plan=data.get("plan") if isinstance(data.get("plan"), list) else [],
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
            events=data.get("events") if isinstance(data.get("events"), list) else [],
            messages=_repair_dangling_tool_calls(raw_msgs),
        )


def _execute_tool_call(
    name: str,
    args: Dict[str, Any],
    session: AgentSession,
    *,
    stop_ev: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    if name == "probe_url":
        return tool_probe_url(str(args.get("url") or ""))
    if name == "list_adapters":
        return tool_list_adapters()
    if name == "read_adapter":
        return tool_read_adapter(str(args.get("name") or ""))
    if name == "write_adapter":
        return tool_write_adapter(str(args.get("name") or ""), str(args.get("code") or ""))
    if name == "run_adapter_once":
        return tool_run_adapter_once(str(args.get("adapter") or ""), session=session, stop_ev=stop_ev)
    if name == "read_failures":
        return tool_read_failures(limit=args.get("limit", 20))
    if name == "update_plan":
        steps = _normalize_plan_steps(args.get("steps"))
        with session._lock:
            session.plan = steps
        session.emit("plan", steps=steps)
        session.persist()
        return {"ok": True, "plan": steps}
    return {"ok": False, "error": f"未知工具: {name}"}


def _compact_tool_result_for_ui(result: Dict[str, Any]) -> Dict[str, Any]:
    """Keep SSE/persisted tool output concise so browser DOM stays responsive."""
    raw = json.dumps(result, ensure_ascii=False)
    if len(raw) <= 6000:
        return result
    compact = dict(result)
    for k in ("code", "output_tail"):
        if isinstance(compact.get(k), str) and len(compact[k]) > 2500:
            compact[k] = compact[k][:2500] + "\n... [truncated]"
    return compact


MAX_EMPTY_NUDGES = 2


class SingleAgentManager:
    """Strict single-thread manager for autonomous registration sessions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, AgentSession] = {}
        self._external_busy_check: Optional[Callable[[], Optional[str]]] = None
        atexit.register(self.stop_all)

    def set_external_busy_check(self, fn: Optional[Callable[[], Optional[str]]]) -> None:
        self._external_busy_check = fn

    def active_session_id(self) -> Optional[str]:
        with self._lock:
            for s in self._sessions.values():
                if s.status == "running":
                    return s.id
        return None

    def _load_from_disk_if_needed(self, session_id: str) -> Optional[AgentSession]:
        if not _SAFE_SESSION_ID_RE.fullmatch(session_id):
            return None
        fp = AGENT_SESSIONS_DIR / f"{session_id}.json"
        if not fp.is_file():
            return None
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("id") == session_id:
                sess = AgentSession.from_dict(data)
                with self._lock:
                    self._sessions.setdefault(session_id, sess)
                    return self._sessions[session_id]
        except Exception:
            return None
        return None

    def get_session(self, session_id: str) -> Optional[AgentSession]:
        if not _SAFE_SESSION_ID_RE.fullmatch(str(session_id or "")):
            return None
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is not None:
            return sess
        return self._load_from_disk_if_needed(session_id)

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self._lock:
            mem = {sid: s.to_summary() for sid, s in self._sessions.items()}
        if AGENT_SESSIONS_DIR.exists():
            for fp in sorted(AGENT_SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
                sid = fp.stem
                if sid in mem or not _SAFE_SESSION_ID_RE.fullmatch(sid):
                    continue
                try:
                    data = json.loads(fp.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and data.get("id"):
                        sess = AgentSession.from_dict(data)
                        mem[sid] = sess.to_summary()
                except Exception:
                    continue
        rows = list(mem.values())
        rows.sort(key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""), reverse=True)
        return rows[:30]

    def create_session(self, title: str = "", target_url: str = "") -> AgentSession:
        clean_url = str(target_url or "").strip()
        clean_title = str(title or "").strip()[:80]
        if not clean_title:
            if clean_url:
                host = (urlparse(clean_url).hostname or "").replace("www.", "")
                clean_title = f"{host} AI 直跑" if host else "AI 直跑会话"
            else:
                clean_title = f"AI 会话 {time.strftime('%H:%M:%S')}"
        sid = f"ag_{int(time.time())}_{secrets.token_hex(3)}"
        sess = AgentSession(sid, title=clean_title, target_url=clean_url)
        with self._lock:
            self._sessions[sid] = sess
        sess.persist()
        return sess

    def delete_session(self, session_id: str) -> bool:
        if not _SAFE_SESSION_ID_RE.fullmatch(str(session_id or "")):
            return False
        sess = self.get_session(session_id)
        existed = sess is not None
        if sess is not None:
            sess.stop()
            with self._lock:
                self._sessions.pop(session_id, None)
        fp = AGENT_SESSIONS_DIR / f"{session_id}.json"
        if fp.is_file():
            try:
                fp.unlink()
                existed = True
            except OSError:
                pass
        return existed

    def rename_session(self, session_id: str, title: str) -> Optional[Dict[str, Any]]:
        clean_title = re.sub(r"\s+", " ", str(title or "")).strip()[:80]
        if not clean_title:
            raise ValueError("会话标题不能为空")
        sess = self.get_session(session_id)
        if sess is None:
            return None
        with sess._lock:
            sess.title = clean_title
            sess.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        sess.persist()
        return sess.to_summary()

    def stop_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        sess = self.get_session(session_id)
        if sess is None:
            return None
        st = sess.stop()
        with sess._lock:
            err = sess.last_error
        return {"ok": True, "id": session_id, "status": st, "error": err}

    def export_session_markdown(self, session_id: str) -> Optional[Dict[str, Any]]:
        sess = self.get_session(session_id)
        if sess is None:
            return None
        return {
            "ok": True,
            "filename": f"{sess.id}.md",
            "markdown": sess.to_markdown(),
        }

    def stop_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for s in sessions:
            if s.status == "running":
                s.stop()

    def send_message(self, session_id: str, message: str, target_url: str = "") -> Dict[str, Any]:
        sess = self.get_session(session_id)
        if sess is None:
            raise KeyError(f"会话不存在: {session_id}")

        user_text = str(message or "").strip()
        if not user_text:
            raise ValueError("发送消息不能为空")

        clean_url = str(target_url or "").strip()
        prompt_for_llm = user_text
        if clean_url and clean_url not in user_text:
            prompt_for_llm = f"目标 URL: {clean_url}\n指令: {user_text}"

        turn_stop_ev: Optional[threading.Event] = None
        steered = False

        with self._lock:
            if sess.status == "running":
                # Mid-run non-destructive steering for the currently active session
                sess.enqueue_steer(prompt_for_llm)
                steered = True
            else:
                for existing in self._sessions.values():
                    if existing.status == "running":
                        raise RuntimeError(f"单线程模式已有运行中的 AI 会话: {existing.title} ({existing.id})")
                if self._external_busy_check is not None:
                    ext_reason = self._external_busy_check()
                    if ext_reason:
                        raise RuntimeError(ext_reason)
                turn_stop_ev = threading.Event()
                with sess._lock:
                    sess._stop_event = turn_stop_ev
                    sess.status = "running"
                    sess.last_error = ""
                    sess._active_tool_name = None
                    sess._active_tool_started_at = None
                    if clean_url:
                        sess.target_url = clean_url
                        if sess.title.startswith("AI 会话"):
                            host = (urlparse(sess.target_url).hostname or "").replace("www.", "")
                            if host:
                                sess.title = f"{host} AI 直跑"
                sess.drain_steers()

        if steered:
            sess.emit("user_msg", content=user_text, target_url=clean_url, steered=True)
            sess.persist()
            return {"ok": True, "steered": True, "session": sess.to_summary()}

        with sess._lock:
            sess.messages = _repair_dangling_tool_calls(sess.messages)
            sess.messages.append({"role": "user", "content": prompt_for_llm})
        sess.emit("user_msg", content=user_text, target_url=clean_url)
        sess.emit("status", status="running", error="")
        sess.persist()

        assert turn_stop_ev is not None
        threading.Thread(target=self._run_turn, args=(sess, turn_stop_ev), daemon=True).start()
        return {"ok": True, "steered": False, "session": sess.to_summary()}

    def _accumulate_usage(self, sess: AgentSession, resp: Dict[str, Any]) -> None:
        u = resp.get("usage") or {}
        p_tok = int(u.get("prompt_tokens") or 0)
        c_tok = int(u.get("completion_tokens") or 0)
        t_tok = int(u.get("total_tokens") or (p_tok + c_tok))
        with sess._lock:
            sess.usage["prompt_tokens"] += p_tok
            sess.usage["completion_tokens"] += c_tok
            sess.usage["total_tokens"] += t_tok
            session_usage = dict(sess.usage)
        sess.emit(
            "usage",
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            total_tokens=t_tok,
            session_usage=session_usage,
            model=resp.get("model", ""),
        )

    def _inject_steers(self, sess: AgentSession) -> int:
        steers = sess.drain_steers()
        if not steers:
            return 0
        combined = "\n\n".join(steers)
        with sess._lock:
            sess.messages.append({
                "role": "user",
                "content": f"[用户运行中实时纠偏]\n{combined}",
            })
        return len(steers)

    def _finish_turn_status(
        self,
        sess: AgentSession,
        stop_ev: threading.Event,
        status: str,
        error: str = "",
    ) -> bool:
        """Atomically transition status ONLY if this turn still owns the session."""
        with self._lock:
            with sess._lock:
                if stop_ev.is_set() or sess._stop_event is not stop_ev:
                    return False
                sess.status = status
                sess.last_error = str(error or "")
                sess._active_tool_name = None
                sess._active_tool_started_at = None
        sess.emit("status", status=status, error=error)
        sess.persist()
        sess.close_streams()
        return True

    def _run_turn(self, sess: AgentSession, stop_ev: threading.Event) -> None:
        empty_nudges = 0
        try:
            for step in range(1, MAX_STEPS_PER_TURN + 1):
                if stop_ev.is_set():
                    return

                self._inject_steers(sess)

                with sess._lock:
                    history_snapshot = _repair_dangling_tool_calls(_slice_messages_safely(sess.messages, 80))

                resp = llm_client.chat_completion(
                    messages=history_snapshot,
                    tools=TOOL_SCHEMAS,
                    system_prompt=AGENT_SYSTEM_PROMPT,
                )

                if stop_ev.is_set():
                    return

                self._accumulate_usage(sess, resp)

                content = str(resp.get("content") or "").strip()
                thinking = str(resp.get("thinking") or "").strip()
                tool_calls = resp.get("tool_calls") or []

                # Resilience #1: Thinking-Only Empty-Turn Nudge (max 2 nudges)
                if not content and not tool_calls:
                    if empty_nudges < MAX_EMPTY_NUDGES and step < MAX_STEPS_PER_TURN:
                        empty_nudges += 1
                        with sess._lock:
                            sess.messages.append({
                                "role": "user",
                                "content": "你上一轮未输出可见回复且未调用工具。请直接调用所需工具推进任务，或给出清晰的中文结论。",
                            })
                        sess.persist()
                        continue
                    fallback_msg = (
                        "模型仅输出了思考过程但未给出最终正文回复，已结束本轮。可检查模型配置或调大 max_tokens 后重试。"
                        if thinking
                        else "模型连续返回空白回复，已结束本轮。可检查模型配置或调大 max_tokens 后重试。"
                    )
                    with sess._lock:
                        sess.messages.append({"role": "assistant", "content": fallback_msg})
                    sess.emit("assistant_msg", content=fallback_msg, step=step)
                    self._finish_turn_status(sess, stop_ev, "done")
                    return

                empty_nudges = 0
                assistant_record: Dict[str, Any] = {"role": "assistant", "content": content or None}
                if tool_calls:
                    assistant_record["tool_calls"] = tool_calls
                with sess._lock:
                    sess.messages.append(assistant_record)

                if content:
                    sess.emit("assistant_msg", content=content, step=step)

                if not tool_calls:
                    # Check atomically whether a steer arrived while LLM was replying
                    with self._lock:
                        if stop_ev.is_set() or sess._stop_event is not stop_ev:
                            return
                        if sess.has_pending_steer() and step < MAX_STEPS_PER_TURN:
                            keep_going = True
                        else:
                            with sess._lock:
                                sess.status = "done"
                            keep_going = False
                    if keep_going:
                        self._inject_steers(sess)
                        sess.persist()
                        continue
                    sess.emit("status", status="done", error="")
                    sess.persist()
                    sess.close_streams()
                    return

                # Execute requested tool calls strictly sequentially (single-thread)
                for tc_idx, tc in enumerate(tool_calls):
                    if stop_ev.is_set():
                        return
                    call_id = str(tc.get("id") or f"call_{step}_{tc_idx + 1}")
                    fn = tc.get("function") or {}
                    fn_name = str(fn.get("name") or "").strip()
                    raw_args = fn.get("arguments") or "{}"

                    parse_err = ""
                    try:
                        parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                        if not isinstance(parsed_args, dict):
                            parsed_args = {}
                    except Exception as pe:
                        parsed_args = {}
                        parse_err = f"工具参数 JSON 解析失败: {pe}"

                    sess.emit("tool_start", call_id=call_id, name=fn_name, args=parsed_args)
                    t_tool = time.perf_counter()
                    sess.set_active_tool(fn_name, t_tool, stop_ev=stop_ev)
                    try:
                        if parse_err:
                            tool_res: Dict[str, Any] = {"ok": False, "error": parse_err}
                        else:
                            try:
                                tool_res = _execute_tool_call(fn_name, parsed_args, sess, stop_ev=stop_ev)
                            except Exception as te:
                                tool_res = {"ok": False, "error": f"工具执行异常: {te}"}
                    finally:
                        sess.clear_active_tool(stop_ev=stop_ev)
                    dur_ms = round((time.perf_counter() - t_tool) * 1000, 1)

                    if stop_ev.is_set():
                        return

                    ui_res = _compact_tool_result_for_ui(tool_res)
                    sess.emit(
                        "tool_end",
                        call_id=call_id,
                        name=fn_name,
                        ok=bool(tool_res.get("ok", True)),
                        duration_ms=dur_ms,
                        result=ui_res,
                    )

                    tool_text = json.dumps(tool_res, ensure_ascii=False)
                    if len(tool_text) > 12_000:
                        tool_text = tool_text[:12_000] + " ... [truncated]"
                    with sess._lock:
                        sess.messages.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": fn_name,
                            "content": tool_text,
                        })
                    sess.persist()

                    # Mid-run steering: inject user's new instruction right after current tool finishes
                    if sess.has_pending_steer():
                        for rem_idx, rem_tc in enumerate(tool_calls[tc_idx + 1 :], start=tc_idx + 2):
                            rem_id = str(rem_tc.get("id") or f"call_{step}_{rem_idx}")
                            rem_fn = str((rem_tc.get("function") or {}).get("name") or "unknown").strip()
                            skip_payload = {"ok": False, "skipped": True, "reason": "收到用户实时纠偏指令，已跳过剩余工具优先响应新指令"}
                            with sess._lock:
                                sess.messages.append({
                                    "role": "tool",
                                    "tool_call_id": rem_id,
                                    "name": rem_fn,
                                    "content": json.dumps(skip_payload, ensure_ascii=False),
                                })
                        sess.persist()
                        break

            # Resilience #2: Graceful Budget Settlement (1 final tool-less wrap-up completion)
            if stop_ev.is_set():
                return
            self._inject_steers(sess)
            with sess._lock:
                wrapup_history = _repair_dangling_tool_calls(_slice_messages_safely(sess.messages, 80))
            wrapup_prompt = (
                f"已达到单轮最大步数上限（{MAX_STEPS_PER_TURN} 步），当前已关闭工具调用。"
                "请立即用简洁中文做收尾总结：1) 本轮已完成的探测/改动进展；"
                "2) 已创建或修改的适配器文件与试跑产出账号情况；3) 剩余未完成事项与下一步建议。"
            )
            wrap_text = ""
            try:
                wrap_resp = llm_client.chat_completion(
                    messages=wrapup_history + [{"role": "user", "content": wrapup_prompt}],
                    tools=[],
                    system_prompt=AGENT_SYSTEM_PROMPT,
                )
                if stop_ev.is_set():
                    return
                self._accumulate_usage(sess, wrap_resp)
                wrap_text = str(wrap_resp.get("content") or "").strip()
            except Exception:
                wrap_text = ""

            if stop_ev.is_set():
                return

            if not wrap_text:
                wrap_text = f"已达到单轮最大推理/工具步数上限（{MAX_STEPS_PER_TURN} 步），已安全暂停。如需继续请发送「继续」。"

            with sess._lock:
                sess.messages.append({"role": "assistant", "content": wrap_text})
            sess.emit("assistant_msg", content=wrap_text, step=MAX_STEPS_PER_TURN, wrap_up=True)
            self._finish_turn_status(sess, stop_ev, "done")
        except Exception as exc:
            if stop_ev.is_set():
                return
            self._finish_turn_status(sess, stop_ev, "error", error=str(exc))


agent_manager = SingleAgentManager()


def rename_session(session_id: str, title: str) -> Optional[Dict[str, Any]]:
    """Rename an agent session title (<= 80 chars, persisted atomically)."""
    return agent_manager.rename_session(session_id, title)


def export_session_markdown(session_id: str) -> Optional[Dict[str, Any]]:
    """Export an agent session into a Markdown report (`{ok, filename, markdown}`)."""
    return agent_manager.export_session_markdown(session_id)
