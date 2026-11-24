"""RegisterHub Lite Autonomous Registration Agent (Zero External Dependencies).

Strictly single-thread safe for Community Edition:
  - Only one agent turn (and one adapter subprocess) may run at a time.
  - Real-time SSE streaming (`text_delta`, `thinking_delta`, `tool_call_delta`)
    with micro-batch flushing and full cache/TTFT/tok_s telemetry.
  - True LLM context compaction (manual `/compact` + automatic 85% context-window
    compaction with cooldown) that preserves full UI transcript while keeping
    LLM history lean and tool-pair intact.
  - Step-level adapter checkpoints with one-click restore, persistent Guard Rules
    with human-in-the-loop approval gates, follow-up queue, `/goal` mode, and
    streaming `/btw` side questions.
  - Exposes 16 pure-Community tools via Function Calling (zero private leaks).
"""

from __future__ import annotations

import ast
import atexit
import collections
import difflib
import html.parser
import inspect
import json
import keyword
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from core import llm_client, llm_profiles

ROOT_DIR = Path(__file__).resolve().parent.parent
SITES_DIR = ROOT_DIR / "sites"
RESULTS_DIR = ROOT_DIR / "results"
ACCOUNTS_FILE = RESULTS_DIR / "accounts.txt"
FAILED_FILE = RESULTS_DIR / "failed.txt"
CONFIG_FILE = ROOT_DIR / "config.json"
AGENT_SESSIONS_DIR = RESULTS_DIR / "agent_sessions"
AGENT_SKILLS_DIR = ROOT_DIR / "skills"
_SAFE_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

MAX_STEPS_PER_TURN = 16
RUN_ADAPTER_TIMEOUT_S = 120
_AUTO_COMPACT_CONTEXT_RATIO = 0.85
_AUTO_COMPACT_COOLDOWN_STEPS = 4
_DEFAULT_KEEP_RECENT_TOKENS = 6000
_HISTORY_HARD_CAP_MESSAGES = 400
_HISTORY_HARD_CAP_TARGET = 300
_PERSIST_MAX_MESSAGES = 400
_MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 2
_GOAL_DEFAULT_MAX_STEPS = 60
_GOAL_MAX_CONSECUTIVE_NUDGES = 3
_GOAL_MAX_TOTAL_NUDGES = 6
_DELTA_EVENT_TYPES = frozenset({"text_delta", "thinking_delta", "tool_call_delta"})

_COMPACTION_TAG_OPEN = "<compaction-checkpoint>"
_COMPACTION_TAG_CLOSE = "</compaction-checkpoint>"
_COMPACTION_SYSTEM_PROMPT = (
    "你是严谨的工程会话上下文压缩器。你的任务是将历史注册工程对话与工具结果浓缩为结构化中文检查点，"
    "以便主 Agent 无缝继续工作，绝不能丢失关键 URL、已验证的选择器、报错根因、已修改文件或产出凭据状态。"
    "不要调用任何工具，直接输出简洁 Markdown 摘要。"
)
_COMPACTION_INITIAL_PROMPT = (
    "请将以上 `<conversation>` 浓缩为结构化工程检查点（使用以下小节）：\n"
    "## 目标与站点\n## 已完成进度\n## 关键发现（接口/选择器/风控/报错根因）\n## 已改动文件与试跑状态\n## 下一步行动"
)
_COMPACTION_UPDATE_PROMPT = (
    "请把 `<previous-summary>` 与新增 `<conversation>` 合并更新为一份新的完整工程检查点"
    "（保留有效结论，淘汰过时信息，使用相同 5 个小节）。"
)

_GOAL_DONE_RE = re.compile(r"\[GOAL_DONE\]", re.I)
_GOAL_BLOCKED_RE = re.compile(r"\[GOAL_BLOCKED\s*[:：]?\s*([^\]]*)\]", re.I)
_SAFE_CHECKPOINT_ID_RE = re.compile(r"^(cp|pre_restore)_[a-zA-Z0-9_-]{4,48}$")

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
- 可复用工具：`core.identity.generate_identity`、`core.pilot.{human_type, smart_click, smart_agree_tos}`、`core.mail_client.DisposableMailClient`、`core.imap_mail.{SingleImapMailClient, BuiltinPoolMailClient}`、`core.mail_builtin.get_builtin_mail_store`、`core.proxy_builtin.get_builtin_proxy_store`。如需确认方法签名，优先用 `read_core_file` 只读查阅 `core/*.py` 或 `sites/_template.py`。
- 成功账号由 `StandardRunner` 自动写入 `results/accounts.txt`（支持 4 列 `email----password----username----api_key`），失败日志写入 `results/failed.txt`。

核心工程红线：
1. 开工第一步先调用 `update_plan` 列出 3-5 个清晰步骤，并随进展更新状态（pending / in_progress / done / failed）。
2. 严禁虚构成功或写入假占位符（例如禁止将 `"Active"`、`"OK"`、`"placeholder"` 塞进 `account_data["api_key"]`）。`submit_and_confirm()` 必须在明确检测到注册成功标志时才返回 `True`。
3. 编写适配器前先用 `probe_url` 探测目标页表单与风控特征；用户上传了 HAR/日志时用 `read_session_file` / `analyze_har` 分析；对已有适配器做小改动优先用 `patch_adapter` 精准修补而非整文件重写。
4. 需要排查代理/网络/IMAP/依赖与内置邮箱池/内置代理池余量时调用 `check_infra`；批量导入、验活、查收件箱或维护本地邮箱/IP池时调用 `manage_builtin_mail` / `manage_builtin_proxies`；需要做短时 API 或算法诊断验证时调用 `run_scratch_python`。
5. 写完或修完适配器后用 `run_adapter_once` 单线程试跑 1 个号，检查退出码、`accounts_delta` 与输出日志；失败时读取 `read_failures` 精准定位根因并修复；必要时调用 `verify_accounts` 盘点/验活已落盘凭据。
6. 严格单线程串行执行，回答用简洁中文，明确交代探测结果、改动文件、试跑结论与下一步建议。
"""

TOOL_META: Dict[str, Dict[str, str]] = {
    "update_plan": {"label": "更新执行计划", "risk": "read", "category": "planning"},
    "probe_url": {"label": "探测目标页面", "risk": "read", "category": "recon"},
    "list_adapters": {"label": "列出站点适配器", "risk": "read", "category": "workspace"},
    "read_adapter": {"label": "读取适配器源码", "risk": "read", "category": "workspace"},
    "write_adapter": {"label": "写入站点适配器", "risk": "write", "category": "workspace"},
    "patch_adapter": {"label": "局部补丁修改适配器", "risk": "write", "category": "workspace"},
    "run_adapter_once": {"label": "单线程试跑适配器", "risk": "exec", "category": "execution"},
    "read_failures": {"label": "查看失败日志与截图", "risk": "read", "category": "debug"},
    "read_core_file": {"label": "只读查看框架源码", "risk": "read", "category": "workspace"},
    "run_scratch_python": {"label": "运行临时 Python 诊断", "risk": "exec", "category": "debug"},
    "read_session_file": {"label": "读取会话上传附件", "risk": "read", "category": "session"},
    "analyze_har": {"label": "解析 HAR 抓包链路", "risk": "read", "category": "recon"},
    "check_infra": {"label": "诊断本地代理/环境", "risk": "read", "category": "infra"},
    "manage_builtin_mail": {"label": "管理内置邮箱池", "risk": "write", "category": "infra"},
    "manage_builtin_proxies": {"label": "管理内置代理池", "risk": "write", "category": "infra"},
    "verify_accounts": {"label": "核验账号台账与 Key", "risk": "read", "category": "assets"},
}

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
            "description": "安全读取指定适配器源码 sites/<name>.py（支持读取 _template 参考标准骨架，支持按行分页）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "适配器名称，不含 .py 后缀（如 demo_site 或 _template）"},
                    "start_line": {"type": "integer", "description": "可选起始行号（从 1 开始）"},
                    "end_line": {"type": "integer", "description": "可选结束行号（含）"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_adapter",
            "description": "校验语法与安全红线后原子写入 sites/<name>.py（覆盖旧文件前自动生成 .bak 备份与步级检查点）。必须定义 BaseSiteAdapter 子类，禁止假 Active 占位凭据。",
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
            "name": "patch_adapter",
            "description": "对已存在的 sites/<name>.py 执行精准局部替换（手术刀补丁，支持单次 old_text/new_text 或 edits 数组），自动执行 AST/反假Active 校验、检查点快照、原子写入与 unified diff 预览。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "目标适配器名称（不含 .py 后缀）"},
                    "old_text": {"type": "string", "description": "需要精确匹配的原始代码片段（单次替换模式）"},
                    "new_text": {"type": "string", "description": "替换后的新代码片段（单次替换模式）"},
                    "edits": {
                        "type": "array",
                        "description": "多处顺序替换列表，每项包含 old_text 与 new_text",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_text": {"type": "string"},
                                "new_text": {"type": "string"},
                            },
                            "required": ["old_text", "new_text"],
                        },
                    },
                },
                "required": ["name"],
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
            "description": "读取 results/failed.txt 中最近的失败记录以及最近失败截图列表，用于排查报错根因。",
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
            "name": "read_core_file",
            "description": "只读白名单查看框架自身源码（core/*.py、sites/_template.py、main.py、README.md），用于确认内置类/方法签名，支持分页或关键词检索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对项目根路径（如 core/adapter.py、core/mail_client.py、sites/_template.py）"},
                    "start_line": {"type": "integer", "description": "可选起始行号（默认 1）"},
                    "end_line": {"type": "integer", "description": "可选结束行号（默认 start_line + 199）"},
                    "query": {"type": "string", "description": "可选关键词过滤（大小写不敏感，返回命中行及上下文）"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_scratch_python",
            "description": "在当前会话隔离 scratch/ 目录下运行短时 Python 诊断脚本（1~45s 超时，适合探测接口响应、验证正则或调试解码逻辑，绝不修改核心文件）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要执行的 Python 诊断代码"},
                    "filename": {"type": "string", "description": "可选脚本文件名（如 probe_api.py）"},
                    "timeout_sec": {"type": "integer", "description": "超时秒数（默认 20，范围 1..45）"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_session_file",
            "description": "安全读取当前会话上传的附件（如 .har、.json、.log、.txt）；省略 name 时列出当前会话全部已上传文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "上传文件名（留空则列出全部附件）"},
                    "start_line": {"type": "integer", "description": "可选起始行号"},
                    "end_line": {"type": "integer", "description": "可选结束行号"},
                    "query": {"type": "string", "description": "可选关键词检索"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_har",
            "description": "解析用户上传到当前会话的 .har 抓包文件（或直接传入 HAR JSON 文本），提取注册/验证码/鉴权接口链路、风控信号并生成 BaseSiteAdapter 骨架。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "当前会话中已上传的 .har 文件名"},
                    "har_text": {"type": "string", "description": "可选直接传入的 HAR JSON 文本"},
                    "site_name": {"type": "string", "description": "可选目标适配器名（用于生成骨架类名）"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_infra",
            "description": "诊断本地社区版运行环境：运行 doctor 环境体检、查看脱敏代理/邮箱配置、盘点内置邮箱池与内置代理池余量，可选实时检测出口 IP 纯净度评分。",
            "parameters": {
                "type": "object",
                "properties": {
                    "check_ip_score": {"type": "boolean", "description": "是否同时发起实时出口 IP 纯净度打分（默认 false）"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_builtin_mail",
            "description": "管理本地 SQLite + DPAPI 内置邮箱池（零 Docker，支持 Outlook OAuth2 / iCloud 接码链接 / 通用 IMAP）：查看库存、批量导入、在线验活、查收件箱验证码、租借/释放、清理死号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "list", "preview", "import", "verify", "inbox", "lease", "release", "cleanup_dead"],
                        "description": "操作类型",
                    },
                    "text": {"type": "string", "description": "action=preview/import 时的多行导入文本"},
                    "format": {"type": "string", "description": "导入模板格式，默认 auto 自动识别"},
                    "email": {"type": "string", "description": "action=inbox/release/verify 时的目标邮箱"},
                    "status": {"type": "string", "description": "action=list 时的状态过滤（all/valid/unknown/dead）"},
                    "mark_status": {"type": "string", "description": "action=release 时可选标记状态（valid/dead/unknown）"},
                    "limit": {"type": "integer", "description": "列表/验活条数上限（默认 20，最大 50）"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_builtin_proxies",
            "description": "管理本地 SQLite + DPAPI 内置代理池（零外部依赖，支持批量导入 HTTP/HTTPS/SOCKS5 静态代理、添加动态住宅网关、配置 API 提取源、连通性测活打分与清理失效代理）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "list", "import", "add_gateway", "add_api_source", "pull_source", "probe", "cleanup_dead"],
                        "description": "操作类型",
                    },
                    "text": {"type": "string", "description": "action=import 时的多行代理文本"},
                    "default_scheme": {"type": "string", "description": "默认协议（http/https/socks5/socks5h）"},
                    "gateway_url": {"type": "string", "description": "action=add_gateway 时的住宅代理网关 URL"},
                    "preset": {"type": "string", "description": "住宅网关预设（smartproxy/iproyal/oxylabs/brightdata/custom）"},
                    "country": {"type": "string", "description": "出口国家代码（如 us/jp/gb）"},
                    "label": {"type": "string", "description": "自定义标签名"},
                    "url": {"type": "string", "description": "action=add_api_source 时的公网 API 提取链接"},
                    "source_id": {"type": "integer", "description": "action=pull_source 时的提取源 ID"},
                    "pull_now": {"type": "boolean", "description": "添加 API 提取源后是否立即拉取"},
                    "target_url": {"type": "string", "description": "action=probe 时的可选目标探测 URL"},
                    "limit": {"type": "integer", "description": "列表/探测条数上限（默认 20，最大 50）"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_accounts",
            "description": "盘点本地 results/accounts.txt 账号与凭据台账统计（脱敏展示）；如提供 OpenAI 兼容 base_url，可在线探测最近 Key 的 /v1/models 存活状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "base_url": {"type": "string", "description": "可选：用于在线 /v1/models 验活的公开 Base URL"},
                    "limit": {"type": "integer", "description": "返回/抽检最近记录数（默认 10，在线验活上限 5）"},
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


def tool_read_adapter(
    name: str,
    *,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> Dict[str, Any]:
    """Tool 3: Safely read sites/<name>.py (or _template), with optional line pagination."""
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
    all_lines = text.splitlines()
    total_lines = len(all_lines)
    if start_line is not None or end_line is not None:
        s_ln = max(1, int(start_line or 1))
        e_ln = min(total_lines, int(end_line or (s_ln + 199)))
        if e_ln < s_ln:
            e_ln = min(total_lines, s_ln + 199)
        sliced = "\n".join(f"{i}: {all_lines[i - 1]}" for i in range(s_ln, e_ln + 1))
        max_chars = 24_000
        return {
            "ok": True,
            "name": clean,
            "file": f"sites/{clean}.py",
            "lines": total_lines,
            "start_line": s_ln,
            "end_line": e_ln,
            "truncated": len(sliced) > max_chars or e_ln < total_lines,
            "code": sliced[:max_chars],
        }

    max_chars = 24_000
    truncated = len(text) > max_chars
    return {
        "ok": True,
        "name": clean,
        "file": f"sites/{clean}.py",
        "lines": total_lines,
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


def _save_adapter_checkpoint(
    session_id: str,
    *,
    adapter_name: str,
    target_file: Path,
    before_text: str,
    after_text: str,
    existed_before: bool,
    kind: str = "write_adapter",
    prefix: str = "cp",
) -> str:
    """Save a reversible pre-write checkpoint under AGENT_SESSIONS_DIR/<sid>/checkpoints/<cp_id>/."""
    if not _SAFE_SESSION_ID_RE.fullmatch(str(session_id or "")):
        return ""
    cp_id = f"{prefix}_{int(time.time())}_{secrets.token_hex(3)}"
    cp_root = (AGENT_SESSIONS_DIR / session_id / "checkpoints").resolve()
    cp_dir = (cp_root / cp_id).resolve()
    if cp_dir.parent != cp_root:
        return ""
    cp_dir.mkdir(parents=True, exist_ok=True)
    tmp_py = cp_dir / "before.py.tmp"
    tmp_py.write_text(before_text, encoding="utf-8")
    os.replace(tmp_py, cp_dir / "before.py")
    meta = {
        "id": cp_id,
        "session_id": session_id,
        "kind": kind,
        "site": adapter_name,
        "project": adapter_name,
        "file": str(target_file.resolve()),
        "rel_file": f"sites/{adapter_name}.py",
        "existed_before": bool(existed_before),
        "before_lines": len(before_text.splitlines()),
        "after_lines": len(after_text.splitlines()),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp_meta = cp_dir / "meta.json.tmp"
    tmp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_meta, cp_dir / "meta.json")
    return cp_id


def _validate_adapter_source(clean: str, code: str) -> Tuple[Optional[ast.AST], Optional[str]]:
    if not isinstance(code, str) or not code.strip():
        return None, "代码内容不能为空"
    if len(code) > 120_000:
        return None, "适配器代码超过 120KB 大小限制"
    filename = f"sites/{clean}.py"
    try:
        tree = ast.parse(code, filename=filename)
        compile(code, filename, "exec")
    except SyntaxError as se:
        return None, f"Python 语法校验失败 (第 {se.lineno} 行): {se.msg}"
    except Exception as exc:
        return None, f"Python 编译校验失败: {exc}"

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
        return None, "适配器代码必须定义继承 BaseSiteAdapter 的类（可先 read_adapter('_template') 参考）"

    fake_violation = _detect_fake_placeholder_violation(tree)
    if fake_violation:
        return None, f"反假成功红线拦截：{fake_violation}"
    return tree, None


def tool_write_adapter(name: str, code: str, *, session_id: str = "") -> Dict[str, Any]:
    """Tool 4: Validate identifier, AST/compile, anti-fake-Active, backup .bak + checkpoint, atomic write."""
    raw_name = str(name or "").strip()
    if not raw_name or ".." in raw_name or "/" in raw_name or "\\" in raw_name or "." in raw_name:
        return {"ok": False, "error": f"非法适配器名（禁止包含路径分隔符或扩展名）: {name!r}"}
    if not _SAFE_ADAPTER_WRITE_RE.fullmatch(raw_name):
        return {"ok": False, "error": f"适配器名必须以字母开头，仅含字母/数字/下划线/连字符: {name!r}"}

    clean = raw_name.lower().replace("-", "_")
    if keyword.iskeyword(clean) or clean in _RESERVED_ADAPTER_NAMES:
        return {"ok": False, "error": f"适配器名不能使用保留字: {clean!r}"}

    _, err = _validate_adapter_source(clean, code)
    if err:
        return {"ok": False, "error": err}

    filename = f"sites/{clean}.py"
    SITES_DIR.mkdir(parents=True, exist_ok=True)
    sites_root = SITES_DIR.resolve()
    target = (sites_root / f"{clean}.py").resolve()
    if target.parent != sites_root:
        return {"ok": False, "error": "安全路径校验失败：写入目标越界"}

    existed_before = target.exists()
    before_text = target.read_text(encoding="utf-8", errors="replace") if existed_before else ""
    backup_name = ""
    if existed_before:
        bak_path = target.with_suffix(".py.bak")
        bak_tmp = target.with_suffix(".py.bak.tmp")
        bak_tmp.write_bytes(target.read_bytes())
        os.replace(bak_tmp, bak_path)
        backup_name = bak_path.name

    normalized_code = code if code.endswith("\n") else (code + "\n")
    checkpoint_id = ""
    if session_id:
        try:
            checkpoint_id = _save_adapter_checkpoint(
                session_id,
                adapter_name=clean,
                target_file=target,
                before_text=before_text,
                after_text=normalized_code,
                existed_before=existed_before,
                kind="write_adapter",
            )
        except Exception:
            checkpoint_id = ""

    tmp_path = target.with_suffix(".py.tmp")
    tmp_path.write_text(normalized_code, encoding="utf-8")
    os.replace(tmp_path, target)

    if target.read_text(encoding="utf-8") != normalized_code:
        return {"ok": False, "error": "写入后独立回读校验不一致"}

    # Clear cached module so sites.discover_adapters() loads updated code
    sys.modules.pop(f"sites.{clean}", None)

    out: Dict[str, Any] = {
        "ok": True,
        "name": clean,
        "file": filename,
        "backup": backup_name,
        "lines": len(normalized_code.splitlines()),
    }
    if checkpoint_id:
        out["checkpoint_id"] = checkpoint_id
    return out


def tool_patch_adapter(
    name: str,
    *,
    old_text: str = "",
    new_text: str = "",
    edits: Optional[List[Dict[str, Any]]] = None,
    session_id: str = "",
) -> Dict[str, Any]:
    """Tool: Surgical patch on an existing sites/<name>.py with AST + anti-fake-Active + checkpoint + diff."""
    raw_name = str(name or "").strip()
    if raw_name.endswith(".py"):
        raw_name = raw_name[:-3]
    if not raw_name or ".." in raw_name or "/" in raw_name or "\\" in raw_name or "." in raw_name:
        return {"ok": False, "error": f"非法适配器名: {name!r}"}
    if not _SAFE_ADAPTER_WRITE_RE.fullmatch(raw_name):
        return {"ok": False, "error": f"非法适配器名: {name!r}"}
    clean = raw_name.lower().replace("-", "_")
    if keyword.iskeyword(clean) or clean in _RESERVED_ADAPTER_NAMES:
        return {"ok": False, "error": f"适配器名不能使用保留字: {clean!r}"}

    sites_root = SITES_DIR.resolve()
    target = (sites_root / f"{clean}.py").resolve()
    if target.parent != sites_root or not target.is_file():
        return {"ok": False, "error": f"未找到可修补的适配器文件: sites/{clean}.py（请先 write_adapter 创建）"}

    ops: List[Tuple[str, str]] = []
    if isinstance(edits, list) and edits:
        for idx, item in enumerate(edits):
            if not isinstance(item, dict):
                return {"ok": False, "error": f"edits[{idx}] 必须为对象"}
            o = str(item.get("old_text") or "")
            n = str(item.get("new_text") if item.get("new_text") is not None else "")
            if not o:
                return {"ok": False, "error": f"edits[{idx}].old_text 不能为空"}
            ops.append((o, n))
    elif old_text:
        ops.append((str(old_text), str(new_text)))
    else:
        return {"ok": False, "error": "必须提供 old_text/new_text 或非空 edits 列表"}

    before_text = target.read_text(encoding="utf-8", errors="replace")
    updated = before_text
    for idx, (o, n) in enumerate(ops):
        cnt = updated.count(o)
        if cnt == 0:
            return {
                "ok": False,
                "error": f"第 {idx + 1} 处补丁未找到精确匹配文本，请先 read_adapter('{clean}') 确认当前代码",
            }
        if cnt > 1:
            return {
                "ok": False,
                "error": f"第 {idx + 1} 处补丁在文件中匹配到 {cnt} 次，请补充前后文使其唯一匹配",
            }
        updated = updated.replace(o, n, 1)

    _, err = _validate_adapter_source(clean, updated)
    if err:
        return {"ok": False, "error": err}

    normalized = updated if updated.endswith("\n") else (updated + "\n")
    bak_path = target.with_suffix(".py.bak")
    bak_tmp = target.with_suffix(".py.bak.tmp")
    bak_tmp.write_bytes(target.read_bytes())
    os.replace(bak_tmp, bak_path)

    checkpoint_id = ""
    if session_id:
        try:
            checkpoint_id = _save_adapter_checkpoint(
                session_id,
                adapter_name=clean,
                target_file=target,
                before_text=before_text,
                after_text=normalized,
                existed_before=True,
                kind="patch_adapter",
            )
        except Exception:
            checkpoint_id = ""

    tmp_path = target.with_suffix(".py.tmp")
    tmp_path.write_text(normalized, encoding="utf-8")
    os.replace(tmp_path, target)
    if target.read_text(encoding="utf-8") != normalized:
        return {"ok": False, "error": "补丁写入后回读校验不一致"}

    sys.modules.pop(f"sites.{clean}", None)

    diff_lines = list(
        difflib.unified_diff(
            before_text.splitlines(),
            normalized.splitlines(),
            fromfile=f"a/sites/{clean}.py",
            tofile=f"b/sites/{clean}.py",
            lineterm="",
            n=2,
        )
    )
    diff_preview = "\n".join(diff_lines[:120])[:3500]
    out: Dict[str, Any] = {
        "ok": True,
        "name": clean,
        "file": f"sites/{clean}.py",
        "applied_edits": len(ops),
        "lines": len(normalized.splitlines()),
        "backup": bak_path.name,
        "diff": diff_preview,
    }
    if checkpoint_id:
        out["checkpoint_id"] = checkpoint_id
    return out


def tool_read_core_file(
    rel_path: str,
    *,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    query: str = "",
) -> Dict[str, Any]:
    """Tool: Whitelisted read-only viewer for Community framework source files."""
    clean_rel = str(rel_path or "").strip().replace("\\", "/").lstrip("/")
    if not clean_rel or ".." in clean_rel or ":" in clean_rel:
        return {"ok": False, "error": f"非法路径: {rel_path!r}"}

    allowed_exact = {"main.py", "README.md", "sites/_template.py", "web/server.py", "web/studio_bridge.py"}
    is_allowed = (
        clean_rel in allowed_exact
        or (clean_rel.startswith("core/") and clean_rel.endswith(".py") and clean_rel.count("/") == 1)
    )
    if not is_allowed:
        return {
            "ok": False,
            "error": (
                f"路径不在只读白名单中: {clean_rel!r}（仅允许 core/*.py、sites/_template.py、"
                "main.py、README.md、web/server.py、web/studio_bridge.py）"
            ),
        }

    root_resolved = ROOT_DIR.resolve()
    target = (root_resolved / clean_rel).resolve()
    if root_resolved not in target.parents or not target.is_file():
        return {"ok": False, "error": f"文件不存在: {clean_rel}"}

    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    kw = str(query or "").strip().lower()
    if kw:
        hits: List[str] = []
        for idx, ln in enumerate(lines, start=1):
            if kw in ln.lower():
                lo = max(1, idx - 1)
                hi = min(total, idx + 2)
                block = "\n".join(f"{j}: {lines[j - 1]}" for j in range(lo, hi + 1))
                hits.append(block)
                if len(hits) >= 25:
                    break
        return {
            "ok": True,
            "path": clean_rel,
            "total_lines": total,
            "query": query,
            "matches": len(hits),
            "content": "\n---\n".join(hits)[:16_000] if hits else f"未命中关键词: {query}",
        }

    s_ln = max(1, int(start_line or 1))
    e_ln = min(total, int(end_line or (s_ln + 199)))
    if e_ln < s_ln:
        e_ln = min(total, s_ln + 199)
    numbered = "\n".join(f"{i}: {lines[i - 1]}" for i in range(s_ln, e_ln + 1))
    return {
        "ok": True,
        "path": clean_rel,
        "total_lines": total,
        "start_line": s_ln,
        "end_line": e_ln,
        "truncated": e_ln < total or len(numbered) > 20_000,
        "content": numbered[:20_000],
    }


def tool_run_scratch_python(
    code: str,
    *,
    filename: str = "",
    timeout_sec: int = 20,
    session: Optional["AgentSession"] = None,
    stop_ev: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Tool: Run an isolated short diagnostic Python script in AGENT_SESSIONS_DIR/<sid>/scratch/."""
    src = str(code or "")
    if not src.strip():
        return {"ok": False, "error": "Python 诊断代码不能为空"}
    if len(src) > 64_000:
        return {"ok": False, "error": "诊断脚本超过 64KB 大小限制"}
    try:
        ast.parse(src, filename="<scratch>")
    except SyntaxError as se:
        return {"ok": False, "error": f"诊断脚本语法错误 (第 {se.lineno} 行): {se.msg}"}

    sid = session.id if (session is not None and _SAFE_SESSION_ID_RE.fullmatch(session.id)) else "shared"
    raw_fn = Path(str(filename or f"diag_{int(time.time())}_{secrets.token_hex(2)}.py")).name
    safe_fn = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_fn).strip("._") or "diag.py"
    if not safe_fn.endswith(".py"):
        safe_fn += ".py"

    scratch_dir = (AGENT_SESSIONS_DIR / sid / "scratch").resolve()
    scratch_dir.mkdir(parents=True, exist_ok=True)
    script_path = (scratch_dir / safe_fn).resolve()
    if script_path.parent != scratch_dir:
        return {"ok": False, "error": "非法脚本文件名"}
    script_path.write_text(src if src.endswith("\n") else (src + "\n"), encoding="utf-8")

    try:
        eff_timeout = max(1, min(45, int(timeout_sec or 20)))
    except (TypeError, ValueError):
        eff_timeout = 20

    def _should_stop() -> bool:
        if stop_ev is not None:
            return stop_ev.is_set()
        return bool(session is not None and session.is_stop_requested())

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    existing_pp = env.get("PYTHONPATH") or ""
    env["PYTHONPATH"] = f"{ROOT_DIR}{os.pathsep}{existing_pp}" if existing_pp else str(ROOT_DIR)

    t0 = time.time()
    proc = subprocess.Popen(
        [sys.executable, "-u", str(script_path)],
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

    th = threading.Thread(target=_drain, daemon=True)
    th.start()
    timed_out = False
    stopped = False
    try:
        while proc.poll() is None:
            if _should_stop():
                stopped = True
                _kill_process_tree(proc.pid, proc)
                break
            if (time.time() - t0) >= eff_timeout:
                timed_out = True
                _kill_process_tree(proc.pid, proc)
                break
            time.sleep(0.1)
        th.join(timeout=2.0)
    finally:
        if session is not None:
            session.clear_active_process(proc)

    rc = proc.returncode if proc.returncode is not None else -1
    out_tail = "\n".join(tail_lines)[-4000:]
    return {
        "ok": rc == 0 and not timed_out and not stopped,
        "file": str(script_path),
        "exit_code": rc,
        "timed_out": timed_out,
        "stopped": stopped,
        "duration_s": round(time.time() - t0, 2),
        "output_tail": out_tail,
    }


def tool_read_session_file(
    session_id: str,
    *,
    name: str = "",
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    query: str = "",
) -> Dict[str, Any]:
    """Tool: List or read uploaded files for the current session under RESULTS_DIR/agent_uploads/<sid>/."""
    if not _SAFE_SESSION_ID_RE.fullmatch(str(session_id or "")):
        return {"ok": False, "error": "非法会话 ID"}
    up_dir = (RESULTS_DIR / "agent_uploads" / session_id).resolve()
    if not str(name or "").strip():
        files: List[Dict[str, Any]] = []
        if up_dir.is_dir():
            for fp in sorted(up_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if fp.is_file():
                    files.append({
                        "name": fp.name,
                        "size_bytes": fp.stat().st_size,
                        "path": f"results/agent_uploads/{session_id}/{fp.name}",
                    })
        return {"ok": True, "count": len(files), "files": files}

    safe_name = Path(str(name).strip()).name
    if not safe_name or ".." in safe_name:
        return {"ok": False, "error": f"非法附件文件名: {name!r}"}
    target = (up_dir / safe_name).resolve()
    if target.parent != up_dir or not target.is_file():
        return {"ok": False, "error": f"当前会话中未找到附件: {safe_name}"}

    raw = target.read_bytes()
    if len(raw) > 5 * 1024 * 1024:
        return {"ok": False, "error": "附件超过 5MB 读取限制"}
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    kw = str(query or "").strip().lower()
    if kw:
        hits: List[str] = []
        for idx, ln in enumerate(lines, start=1):
            if kw in ln.lower():
                hits.append(f"{idx}: {ln[:400]}")
                if len(hits) >= 40:
                    break
        return {
            "ok": True,
            "name": safe_name,
            "size_bytes": len(raw),
            "total_lines": total,
            "query": query,
            "matches": len(hits),
            "content": "\n".join(hits)[:16_000] if hits else f"未命中关键词: {query}",
        }

    s_ln = max(1, int(start_line or 1))
    e_ln = min(total, int(end_line or (s_ln + 199)))
    if e_ln < s_ln:
        e_ln = min(total, s_ln + 199)
    body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(s_ln, e_ln + 1)) if total else ""
    return {
        "ok": True,
        "name": safe_name,
        "size_bytes": len(raw),
        "total_lines": total,
        "start_line": s_ln,
        "end_line": e_ln,
        "truncated": e_ln < total or len(body) > 16_000,
        "content": body[:16_000],
    }


def _analyze_har_payload(har_text: str, site_name: str = "") -> Dict[str, Any]:
    """Pure stdlib HAR analyzer extracting registration/OTP/auth API steps and adapter skeleton."""
    data = json.loads(har_text)
    entries = ((data.get("log") or {}).get("entries") or []) if isinstance(data, dict) else []
    if not isinstance(entries, list):
        raise ValueError("HAR 缺少 log.entries 数组")

    noise_exts = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf", ".map", ".webp")
    api_steps: List[Dict[str, Any]] = []
    all_signals: List[str] = []
    origin = ""

    def _json_keys(txt: str) -> List[str]:
        try:
            obj = json.loads(txt)
            if isinstance(obj, dict):
                return [str(k) for k in list(obj.keys())[:20]]
        except Exception:
            pass
        return []

    for ent in entries:
        if not isinstance(ent, dict):
            continue
        req = ent.get("request") or {}
        resp = ent.get("response") or {}
        method = str(req.get("method") or "GET").upper()
        url = str(req.get("url") or "")
        if not url.startswith(("http://", "https://")):
            continue
        parsed = urlparse(url)
        path_low = (parsed.path or "").lower()
        if any(path_low.endswith(ext) for ext in noise_exts):
            continue
        mime = str(((resp.get("content") or {}).get("mimeType") or "")).lower()
        if method == "GET" and ("json" not in mime) and not any(k in path_low for k in ("auth", "register", "signup", "verify", "otp", "token", "key", "api")):
            continue
        if not origin:
            origin = f"{parsed.scheme}://{parsed.netloc}"

        req_body = str(((req.get("postData") or {}).get("text") or ""))
        resp_body = str(((resp.get("content") or {}).get("text") or ""))
        blob = f"{url}\n{req_body[:1500]}\n{resp_body[:1500]}".lower()
        sigs: List[str] = []
        for label, needles in (
            ("Turnstile", ("turnstile", "cf-turnstile")),
            ("reCAPTCHA", ("recaptcha", "g-recaptcha")),
            ("hCaptcha", ("hcaptcha",)),
            ("Email OTP", ("otp", "verify", "verification", "code")),
            ("JWT / Bearer", ("access_token", "id_token", "bearer", "eyj")),
            ("API Key", ("api_key", "apikey", "sk-")),
            ("Clerk", ("clerk",)),
            ("Supabase", ("supabase", "/auth/v1/")),
        ):
            if any(n in blob for n in needles):
                sigs.append(label)
                if label not in all_signals:
                    all_signals.append(label)
        api_steps.append({
            "method": method,
            "url": url[:240],
            "path": parsed.path or "/",
            "status": int(resp.get("status") or 0),
            "req_keys": _json_keys(req_body),
            "resp_keys": _json_keys(resp_body),
            "signals": sigs,
        })
        if len(api_steps) >= 25:
            break

    clean_site = re.sub(r"[^a-zA-Z0-9_]+", "_", (site_name or urlparse(origin).hostname or "custom_site").replace(".", "_")).strip("_").lower() or "custom_site"
    cls_name = "".join(p.capitalize() for p in clean_site.split("_") if p) + "Adapter"
    step_comments = "\n".join(
        f"        # {i + 1}. {s['method']} {s['path']} -> HTTP {s['status']} keys={s['req_keys']}"
        for i, s in enumerate(api_steps[:12])
    )
    skeleton = (
        f'"""Auto-generated skeleton from HAR ({origin or clean_site})."""\n\n'
        "from core.adapter import BaseSiteAdapter\n"
        "from core.identity import generate_identity\n\n\n"
        f"class {cls_name}(BaseSiteAdapter):\n"
        "    def target_url(self) -> str:\n"
        f'        return "{origin or "https://example.com"}"\n\n'
        "    def prepare_identity(self) -> dict:\n"
        "        return generate_identity()\n\n"
        "    def fill_form(self, identity: dict) -> None:\n"
        f"{step_comments or '        # TODO: implement steps'}\n"
        "        pass\n\n"
        "    def submit_and_confirm(self) -> bool:\n"
        "        return False\n"
    )
    return {
        "ok": True,
        "site_name": clean_site,
        "origin": origin,
        "total_entries": len(entries),
        "api_steps_count": len(api_steps),
        "api_steps": api_steps,
        "signals": all_signals,
        "skeleton": skeleton,
    }


def tool_analyze_har(
    session_id: str,
    *,
    filename: str = "",
    har_text: str = "",
    site_name: str = "",
) -> Dict[str, Any]:
    """Tool: Analyze an uploaded .har file or inline HAR JSON text."""
    raw_text = str(har_text or "").strip()
    src_label = "inline"
    if not raw_text:
        fname = Path(str(filename or "").strip()).name
        if not fname:
            return {"ok": False, "error": "请提供已上传的 filename（可先 read_session_file 查看）或直接传 har_text"}
        if not _SAFE_SESSION_ID_RE.fullmatch(str(session_id or "")):
            return {"ok": False, "error": "非法会话 ID"}
        up_dir = (RESULTS_DIR / "agent_uploads" / session_id).resolve()
        target = (up_dir / fname).resolve()
        if target.parent != up_dir or not target.is_file():
            return {"ok": False, "error": f"当前会话中未找到 HAR 文件: {fname}"}
        raw_text = target.read_text(encoding="utf-8", errors="replace")
        src_label = fname
    try:
        res = _analyze_har_payload(raw_text, site_name=site_name)
        res["source"] = src_label
        return res
    except Exception as exc:
        return {"ok": False, "error": f"HAR 解析失败: {exc}"}


def tool_check_infra(*, check_ip_score: bool = False) -> Dict[str, Any]:
    """Tool: Run local environment diagnostics (doctor + masked config.json + optional IP score)."""
    report: Dict[str, Any] = {}
    try:
        from core.doctor import collect_doctor_report

        report = collect_doctor_report(include_network=False)
    except Exception as exc:
        report = {"ready": False, "checks": [], "error": str(exc)}

    cfg_summary: Dict[str, Any] = {
        "proxy_mode": "direct",
        "proxy_configured": False,
        "mail_mode": "tempmail",
    }
    raw_proxy = ""
    try:
        if CONFIG_FILE.is_file():
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                b = raw.get("browser") or {}
                m = raw.get("mail") or {}
                d = raw.get("defaults") or {}
                raw_proxy = str(b.get("proxy") or "").strip()
                p_parsed = urlparse(raw_proxy) if raw_proxy else None
                pm = str(b.get("proxy_mode") or ("proxy" if raw_proxy else "direct")).strip().lower()
                mp = str(m.get("provider") or ("tempmail" if d.get("use_disposable_mail", True) else "imap")).strip().lower()
                cfg_summary = {
                    "proxy_mode": pm,
                    "proxy_configured": bool(raw_proxy),
                    "proxy_host": f"{p_parsed.scheme}://{p_parsed.hostname}:{p_parsed.port}" if (p_parsed and p_parsed.hostname) else "",
                    "headless": bool(b.get("headless", False)),
                    "mail_mode": mp,
                    "imap_configured": bool(str(m.get("imap_server") or "").strip() and str(m.get("imap_user") or "").strip()),
                }
    except Exception as exc:
        cfg_summary["config_error"] = str(exc)

    mail_stats: Dict[str, Any] = {"total": 0, "valid": 0, "unknown": 0, "dead": 0, "available": 0, "leased": 0}
    mail_db = RESULTS_DIR / "builtin_mail.db"
    if mail_db.is_file():
        try:
            from core.mail_builtin import get_builtin_mail_store

            mail_stats = get_builtin_mail_store(mail_db).get_stats()
        except Exception as exc:
            mail_stats = {"error": str(exc)}

    proxy_stats: Dict[str, Any] = {"total": 0, "enabled": 0, "available": 0, "browser_compatible": 0}
    proxy_db = RESULTS_DIR / "builtin_proxies.db"
    if proxy_db.is_file():
        try:
            from core.proxy_builtin import get_builtin_proxy_store

            proxy_stats = get_builtin_proxy_store(proxy_db).get_stats()
        except Exception as exc:
            proxy_stats = {"error": str(exc)}

    out: Dict[str, Any] = {
        "ok": True,
        "ready": bool(report.get("ready", False)),
        "checks": report.get("checks") or [],
        "config": cfg_summary,
        "builtin_mail": mail_stats,
        "builtin_proxies": proxy_stats,
    }
    if check_ip_score:
        try:
            from core.ip_scorer import score_ip

            ip_res = score_ip(ip="", proxy_url=raw_proxy, target_url="")
            out["ip_score"] = {
                "ip": ip_res.get("ip"),
                "score": ip_res.get("score"),
                "grade": ip_res.get("grade"),
                "verdict": ip_res.get("verdict"),
                "country": ip_res.get("country"),
            }
        except Exception as exc:
            out["ip_score"] = {"ok": False, "error": str(exc)}
    return out


def tool_manage_builtin_mail(
    action: str,
    *,
    text: str = "",
    fmt: str = "auto",
    email: str = "",
    status: str = "all",
    mark_status: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """Tool: Manage local SQLite + DPAPI built-in mail pool (pure stdlib)."""
    from core.mail_builtin import get_builtin_mail_store
    from core.secrets import sanitize_error_text

    act = str(action or "status").strip().lower()
    lim = max(1, min(50, int(limit or 20)))
    db_path = RESULTS_DIR / "builtin_mail.db"
    store = get_builtin_mail_store(db_path)

    try:
        if act == "status":
            return {"ok": True, "stats": store.get_stats()}
        if act == "list":
            res = store.list_accounts(status=status or "all", limit=lim)
            return {"ok": True, "total": res["total"], "items": res["items"], "stats": res["stats"]}
        if act == "preview":
            if not str(text or "").strip():
                return {"ok": False, "error": "preview 操作需要提供 text"}
            return {"ok": True, **store.preview_import(str(text), fmt=fmt or "auto", limit=min(lim, 12))}
        if act == "import":
            if not str(text or "").strip():
                return {"ok": False, "error": "import 操作需要提供 text"}
            return {"ok": True, **store.import_accounts(str(text), fmt=fmt or "auto")}
        if act == "verify":
            targets = [email.strip()] if str(email or "").strip() else None
            return {"ok": True, **store.verify_batch(emails=targets, limit=min(lim, 20))}
        if act == "inbox":
            if not str(email or "").strip():
                return {"ok": False, "error": "inbox 操作需要提供 email"}
            return {"ok": True, **store.inspect_inbox(email.strip(), top=min(lim, 10), force=True)}
        if act == "lease":
            acc = store.lease_account(owner="ai_agent", ttl_s=900, include_unknown=True)
            return {"ok": acc is not None, "account": acc, "stats": store.get_stats()}
        if act == "release":
            if not str(email or "").strip():
                return {"ok": False, "error": "release 操作需要提供 email"}
            ok = store.release_lease(email.strip(), mark_status=mark_status)
            return {"ok": ok, "released": ok, "stats": store.get_stats()}
        if act == "cleanup_dead":
            deleted = store.cleanup_dead_accounts()
            return {"ok": True, "deleted": deleted, "stats": store.get_stats()}
        return {"ok": False, "error": f"未知内置邮箱池操作: {act}"}
    except Exception as exc:
        return {"ok": False, "error": sanitize_error_text(str(exc))}


def tool_manage_builtin_proxies(
    action: str,
    *,
    text: str = "",
    default_scheme: str = "http",
    gateway_url: str = "",
    preset: str = "smartproxy",
    country: str = "us",
    label: str = "",
    url: str = "",
    source_id: int = 0,
    pull_now: bool = False,
    target_url: str = "",
    limit: int = 20,
) -> Dict[str, Any]:
    """Tool: Manage local SQLite + DPAPI built-in proxy pool (pure stdlib)."""
    from core.net_guard import validate_public_http_url
    from core.proxy_builtin import get_builtin_proxy_store
    from core.secrets import sanitize_error_text

    act = str(action or "status").strip().lower()
    lim = max(1, min(50, int(limit or 20)))
    db_path = RESULTS_DIR / "builtin_proxies.db"
    store = get_builtin_proxy_store(db_path)

    try:
        if act == "status":
            return {"ok": True, "stats": store.get_stats(), "sources_count": len(store.list_sources())}
        if act == "list":
            res = store.list_entries(enabled_only=False)
            return {
                "ok": True,
                "items": res["items"][:lim],
                "sources": res["sources"],
                "stats": res["stats"],
            }
        if act == "import":
            if not str(text or "").strip():
                return {"ok": False, "error": "import 操作需要提供 text"}
            scheme = str(default_scheme or "http").strip().lower()
            if scheme not in ("http", "https", "socks5", "socks5h"):
                scheme = "http"
            return {"ok": True, **store.import_static_proxies(str(text), default_scheme=scheme)}
        if act == "add_gateway":
            if not str(gateway_url or "").strip():
                return {"ok": False, "error": "add_gateway 操作需要提供 gateway_url"}
            return {
                "ok": True,
                **store.add_residential_gateway(
                    gateway_url=str(gateway_url).strip(),
                    label=str(label or "")[:80],
                    preset=str(preset or "smartproxy")[:32],
                    country=str(country or "us")[:16],
                ),
            }
        if act == "add_api_source":
            if not str(url or "").strip():
                return {"ok": False, "error": "add_api_source 操作需要提供 url"}
            scheme = str(default_scheme or "http").strip().lower()
            if scheme not in ("http", "https", "socks5", "socks5h"):
                scheme = "http"
            res = store.add_source(
                kind="api_extract",
                url=str(url).strip(),
                label=str(label or "")[:80],
                default_scheme=scheme,
            )
            if pull_now and res.get("source"):
                res["pull"] = store.pull_api_source(int(res["source"]["id"]))
                res["stats"] = store.get_stats()
            return {"ok": True, **res}
        if act == "pull_source":
            if int(source_id or 0) < 1:
                return {"ok": False, "error": "pull_source 操作需要提供 source_id"}
            return store.pull_api_source(int(source_id))
        if act == "probe":
            clean_target = str(target_url or "").strip()
            if clean_target:
                clean_target = validate_public_http_url(clean_target, resolve_dns=False)
            return {"ok": True, **store.probe_batch(target_url=clean_target, limit=min(lim, 20))}
        if act == "cleanup_dead":
            deleted = store.cleanup_unavailable_entries()
            return {"ok": True, "deleted": deleted, "stats": store.get_stats()}
        return {"ok": False, "error": f"未知内置代理池操作: {act}"}
    except Exception as exc:
        return {"ok": False, "error": sanitize_error_text(str(exc))}


def tool_verify_accounts(*, base_url: str = "", limit: int = 10) -> Dict[str, Any]:
    """Tool: Audit local results/accounts.txt ledger and optionally probe /v1/models for recent keys."""
    try:
        lim = max(1, min(30, int(limit or 10)))
    except (TypeError, ValueError):
        lim = 10

    if not ACCOUNTS_FILE.exists():
        return {"ok": True, "total": 0, "with_key": 0, "without_key": 0, "recent": [], "online_checks": []}

    rows: List[Dict[str, str]] = []
    for ln in ACCOUNTS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or "----" not in s:
            continue
        parts = [p.strip() for p in s.split("----")]
        email = parts[0] if parts else ""
        key = parts[3] if len(parts) >= 4 else ""
        if not key and len(parts) >= 2 and parts[1].startswith(("sk-", "gsk_", "rk_", "api_")):
            key = parts[1]
        elif not key and len(parts) == 3 and parts[2].startswith(("sk-", "gsk_", "rk_", "api_")):
            key = parts[2]
        rows.append({"email": email, "key": key})

    with_key_rows = [r for r in rows if r["key"]]
    recent = [
        {
            "email": r["email"],
            "has_key": bool(r["key"]),
            "key_hint": llm_client.mask_api_key(r["key"]) if r["key"] else "",
        }
        for r in rows[-lim:][::-1]
    ]

    online_checks: List[Dict[str, Any]] = []
    clean_base = str(base_url or "").strip().rstrip("/")
    if clean_base:
        from core.net_guard import (
            PrivateAddressBlockedError,
            build_safe_urllib_opener,
            validate_proxy_url,
            validate_public_http_url,
        )

        try:
            safe_base = validate_public_http_url(clean_base, resolve_dns=True).rstrip("/")
            raw_px = _read_proxy_from_config()
            px = validate_proxy_url(raw_px) if raw_px else ""
            opener = build_safe_urllib_opener(proxy=px or "")
        except (PrivateAddressBlockedError, ValueError) as exc:
            return {
                "ok": False,
                "total": len(rows),
                "with_key": len(with_key_rows),
                "without_key": len(rows) - len(with_key_rows),
                "recent": recent,
                "error": f"Base URL 安全校验拦截: {exc}",
            }

        models_url = f"{safe_base}/models" if safe_base.endswith("/v1") else f"{safe_base}/v1/models"
        for r in with_key_rows[-min(5, lim):][::-1]:
            req = urllib.request.Request(
                models_url,
                headers={"Authorization": f"Bearer {r['key']}", "Accept": "application/json", "User-Agent": "RegisterHub-Lite/2.2"},
            )
            t0 = time.perf_counter()
            try:
                with opener.open(req, timeout=10) as resp:
                    raw = resp.read(65536).decode("utf-8", errors="ignore")
                    cnt = 0
                    try:
                        j = json.loads(raw)
                        if isinstance(j, dict) and isinstance(j.get("data"), list):
                            cnt = len(j["data"])
                    except Exception:
                        pass
                    online_checks.append({
                        "email": r["email"],
                        "key_hint": llm_client.mask_api_key(r["key"]),
                        "ok": True,
                        "status": resp.status,
                        "models_count": cnt,
                        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    })
            except urllib.error.HTTPError as he:
                online_checks.append({
                    "email": r["email"],
                    "key_hint": llm_client.mask_api_key(r["key"]),
                    "ok": False,
                    "status": he.code,
                    "error": f"HTTP {he.code}",
                })
            except Exception as exc:
                online_checks.append({
                    "email": r["email"],
                    "key_hint": llm_client.mask_api_key(r["key"]),
                    "ok": False,
                    "error": str(exc)[:140],
                })

    return {
        "ok": True,
        "total": len(rows),
        "with_key": len(with_key_rows),
        "without_key": len(rows) - len(with_key_rows),
        "recent": recent,
        "online_checks": online_checks,
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
    """Tool 6: Read recent lines from results/failed.txt and recent failure snapshots."""
    try:
        lim = max(1, min(50, int(limit or 20)))
    except (TypeError, ValueError):
        lim = 20
    lines: List[str] = []
    if FAILED_FILE.exists():
        lines = [
            ln.strip()[:500]
            for ln in FAILED_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    snapshots: List[str] = []
    snap_dir = RESULTS_DIR / "snapshots"
    if snap_dir.is_dir():
        try:
            snapshots = [
                p.name
                for p in sorted(snap_dir.glob("*.png"), key=lambda x: x.stat().st_mtime, reverse=True)[:10]
            ]
        except Exception:
            snapshots = []
    return {
        "ok": True,
        "total_lines": len(lines),
        "recent": lines[-lim:],
        "recent_snapshots": snapshots,
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


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return max(1, int(cjk * 0.75 + other * 0.28))


def _estimate_message_tokens(msg: Dict[str, Any]) -> int:
    if not isinstance(msg, dict):
        return 0
    total = 8
    content = msg.get("content")
    if isinstance(content, str):
        total += _estimate_text_tokens(content)
    elif content is not None:
        total += _estimate_text_tokens(json.dumps(content, ensure_ascii=False))
    for tc in msg.get("tool_calls") or []:
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            total += 12 + _estimate_text_tokens(str(fn.get("name") or "")) + _estimate_text_tokens(str(fn.get("arguments") or ""))
    return total


def _estimate_history_tokens(messages: List[Dict[str, Any]], system_prompt: str = "") -> int:
    total = _estimate_text_tokens(system_prompt) + 16
    for m in messages:
        total += _estimate_message_tokens(m)
    return total


def _extract_previous_compaction_summary(messages: List[Dict[str, Any]]) -> Tuple[str, int]:
    if not messages or not isinstance(messages[0], dict):
        return "", 0
    content = str(messages[0].get("content") or "")
    if _COMPACTION_TAG_OPEN in content and _COMPACTION_TAG_CLOSE in content:
        inner = content.split(_COMPACTION_TAG_OPEN, 1)[1].split(_COMPACTION_TAG_CLOSE, 1)[0].strip()
        return inner, 1
    return "", 0


def _find_compaction_cut_point(
    messages: List[Dict[str, Any]],
    start_idx: int,
    keep_recent_tokens: int,
    *,
    force: bool = True,
) -> int:
    """Find a safe split index >= start_idx that NEVER separates assistant(tool_calls) from its tool replies."""
    n = len(messages)
    if n - start_idx <= 2:
        return start_idx

    # Valid cut boundaries: indices where messages[i] is NOT a tool reply
    valid_cuts = [
        i
        for i in range(start_idx + 1, n)
        if isinstance(messages[i], dict) and messages[i].get("role") != "tool"
    ]
    if not valid_cuts:
        return start_idx

    acc = 0
    chosen = start_idx
    for idx in range(n - 1, start_idx - 1, -1):
        acc += _estimate_message_tokens(messages[idx])
        if acc >= keep_recent_tokens:
            # Pick the nearest valid cut <= idx (or >= idx if none below)
            below = [c for c in valid_cuts if c <= idx]
            chosen = below[-1] if below else valid_cuts[0]
            break

    if chosen <= start_idx and force and (n - start_idx) >= 4:
        mid = start_idx + max(2, (n - start_idx) // 2)
        candidates = [c for c in valid_cuts if c <= mid]
        chosen = candidates[-1] if candidates else valid_cuts[0]

    # Always keep at least 1 trailing message if possible
    if chosen >= n:
        chosen = valid_cuts[-1]
    return chosen


def _serialize_history_for_compaction(messages: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "user")
        content = str(m.get("content") or "").strip()
        if role == "user":
            parts.append(f"[USER]\n{content[:1800]}")
        elif role == "assistant":
            tc_names = [str((tc.get("function") or {}).get("name") or "") for tc in (m.get("tool_calls") or []) if isinstance(tc, dict)]
            tc_note = f" (tool_calls: {', '.join(tc_names)})" if tc_names else ""
            parts.append(f"[ASSISTANT{tc_note}]\n{content[:1800]}")
        elif role == "tool":
            tname = str(m.get("name") or "tool")
            parts.append(f"[TOOL:{tname}]\n{content[:1200]}")
    text = "\n\n".join(parts)
    if len(text) > 28_000:
        text = text[:14_000] + "\n\n... [中间过长记录已省略] ...\n\n" + text[-12_000:]
    return text


def _extract_compaction_artifacts(messages: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    sites: List[str] = []
    files: List[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        blob = str(m.get("content") or "")
        for tc in m.get("tool_calls") or []:
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                blob += "\n" + str(fn.get("arguments") or "")
        for u in re.findall(r"https?://[A-Za-z0-9._-]+", blob):
            if u not in sites and len(sites) < 8:
                sites.append(u)
        for f in re.findall(r"(?:sites|core)/[A-Za-z0-9_.-]+\.py", blob):
            if f not in files and len(files) < 12:
                files.append(f)
    return sites, files


def _push_control_event_nowait(q: "queue.Queue[Optional[Dict[str, Any]]]", ev: Optional[Dict[str, Any]]) -> None:
    try:
        q.put_nowait(ev)
        return
    except queue.Full:
        pass
    # Evict buffered ephemeral deltas first so control events are never lost
    saved: List[Optional[Dict[str, Any]]] = []
    evicted = False
    for _ in range(16):
        try:
            item = q.get_nowait()
        except queue.Empty:
            break
        if not evicted and isinstance(item, dict) and item.get("_ephemeral"):
            evicted = True
            continue
        saved.append(item)
    for item in saved:
        try:
            q.put_nowait(item)
        except queue.Full:
            break
    try:
        q.put_nowait(ev)
    except queue.Full:
        pass


class AgentSession:
    """Single persisted AI registration session with SSE pub/sub, steering/followup queue, telemetry, and process tracking."""

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
        next_seq: Optional[int] = None,
        llm_profile_id: str = "",
        llm_model: str = "",
        model: str = "",
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        context_tokens: int = 0,
        last_tok_s: float = 0.0,
        last_ttft_ms: float = 0.0,
        steps_count: int = 0,
        side_questions: Optional[List[Dict[str, Any]]] = None,
        uploads: Optional[List[Dict[str, Any]]] = None,
        queued_messages: Optional[List[Dict[str, Any]]] = None,
        goal_state: Optional[Dict[str, Any]] = None,
    ) -> None:
        now_s = time.strftime("%Y-%m-%d %H:%M:%S")
        self.id = session_id
        self.title = title or "新 AI 直跑会话"
        self.target_url = target_url
        self.status = status
        self.last_error = str(last_error or "")
        self.llm_profile_id = str(llm_profile_id or "").strip()
        self.llm_model = str(llm_model or "").strip()
        self.model = str(model or "").strip()
        self.created_at = created_at or now_s
        self.updated_at = updated_at or now_s
        self.plan: List[Dict[str, str]] = list(plan or [])
        u_in = usage or {}
        self.usage: Dict[str, int] = {
            "prompt_tokens": int(u_in.get("prompt_tokens") or u_in.get("input_tokens") or 0),
            "completion_tokens": int(u_in.get("completion_tokens") or u_in.get("output_tokens") or 0),
            "total_tokens": int(u_in.get("total_tokens") or 0),
        }
        if self.usage["total_tokens"] == 0 and (self.usage["prompt_tokens"] or self.usage["completion_tokens"]):
            self.usage["total_tokens"] = self.usage["prompt_tokens"] + self.usage["completion_tokens"]
        self.cache_read_tokens: int = max(0, int(cache_read_tokens or u_in.get("cache_read_tokens") or 0))
        self.cache_write_tokens: int = max(0, int(cache_write_tokens or u_in.get("cache_write_tokens") or 0))
        self.context_tokens: int = max(0, int(context_tokens or u_in.get("context_tokens") or 0))
        self.last_tok_s: float = round(max(0.0, float(last_tok_s or 0.0)), 1)
        self.last_ttft_ms: float = round(max(0.0, float(last_ttft_ms or 0.0)), 1)
        self.steps_count: int = max(0, int(steps_count or 0))
        self.side_questions: List[Dict[str, Any]] = [dict(x) for x in (side_questions or []) if isinstance(x, dict)]
        self.uploads: List[Dict[str, Any]] = [dict(x) for x in (uploads or []) if isinstance(x, dict)]
        self.queued_messages: List[Dict[str, Any]] = [dict(x) for x in (queued_messages or []) if isinstance(x, dict)]
        self.goal_state: Optional[Dict[str, Any]] = dict(goal_state) if isinstance(goal_state, dict) else None

        norm_events: List[Dict[str, Any]] = []
        max_seq = -1
        for idx, raw_ev in enumerate(events or []):
            if not isinstance(raw_ev, dict):
                continue
            ev_copy = dict(raw_ev)
            raw_seq = ev_copy.get("seq")
            if isinstance(raw_seq, int) and not isinstance(raw_seq, bool) and raw_seq >= 0:
                s_val = raw_seq
            else:
                s_val = max_seq + 1 if max_seq >= 0 else idx
                ev_copy["seq"] = s_val
            if s_val > max_seq:
                max_seq = s_val
            norm_events.append(ev_copy)
        self.events: List[Dict[str, Any]] = norm_events
        computed_next = max_seq + 1
        self._next_seq: int = (
            max(int(next_seq), computed_next)
            if isinstance(next_seq, int) and not isinstance(next_seq, bool)
            else computed_next
        )
        self.messages: List[Dict[str, Any]] = list(messages or [])

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._steer_queue: queue.Queue[str] = queue.Queue()
        self._subs: List[Tuple[queue.Queue[Optional[Dict[str, Any]]], bool]] = []
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

    @property
    def next_seq(self) -> int:
        with self._lock:
            return self._next_seq

    def emit_delta(self, event_type: str, **payload: Any) -> None:
        """Push transient streaming delta ONLY to live subscribers (never persisted or sequenced)."""
        ev = {"type": event_type, "_ephemeral": True, **payload}
        with self._lock:
            subs = list(self._subs)
        for q, _ in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass

    def emit(self, event_type: str, **payload: Any) -> Dict[str, Any]:
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            ev = {
                "type": event_type,
                "ts": time.strftime("%H:%M:%S"),
                **payload,
                "seq": seq,
            }
            self.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self.events.append(ev)
            if len(self.events) > 500:
                self.events = self.events[-400:]
            subs = list(self._subs)
        for q, _ in subs:
            _push_control_event_nowait(q, ev)
        return ev

    def close_streams(self, *, force_all: bool = False) -> None:
        with self._lock:
            subs = list(self._subs)
        for q, keep_open in subs:
            if not force_all and keep_open:
                continue
            _push_control_event_nowait(q, None)

    def subscribe(self, *, keep_open: bool = False) -> Tuple[List[Dict[str, Any]], queue.Queue[Optional[Dict[str, Any]]]]:
        q: queue.Queue[Optional[Dict[str, Any]]] = queue.Queue(maxsize=1024)
        with self._lock:
            hist = list(self.events)
            if self.status in ("running", "waiting_approval") or keep_open:
                self._subs.append((q, keep_open))
            else:
                q.put_nowait(None)
        return hist, q

    def unsubscribe(self, q: queue.Queue[Optional[Dict[str, Any]]]) -> None:
        with self._lock:
            self._subs = [(sq, ko) for sq, ko in self._subs if sq is not q]

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
            was_running = self.status in ("running", "waiting_approval")
            if was_running:
                self.status = "stopped"
                if tool_name:
                    elapsed = max(0.0, time.perf_counter() - tool_started) if tool_started is not None else 0.0
                    abort_note = f"已手动停止（中断时正在执行工具 {tool_name}，已耗时 {elapsed:.1f}s）"
                self.last_error = abort_note
            if self.goal_state and self.goal_state.get("active"):
                self.goal_state["active"] = False
                self.goal_state["status"] = "stopped"
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
            goal_active = bool(self.goal_state and self.goal_state.get("active") and self.status == "running")
            return {
                "id": self.id,
                "title": self.title,
                "target_url": self.target_url,
                "status": self.status,
                "error": self.last_error,
                "llm_profile_id": self.llm_profile_id,
                "llm_model": self.llm_model,
                "model": self.model,
                "steps_count": self.steps_count,
                "input_tokens": int(self.usage.get("prompt_tokens") or 0),
                "output_tokens": int(self.usage.get("completion_tokens") or 0),
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
                "context_tokens": self.context_tokens,
                "last_tok_s": self.last_tok_s,
                "last_ttft_ms": self.last_ttft_ms,
                "queued_count": len(self.queued_messages),
                "goal_active": goal_active,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "plan": list(self.plan),
                "usage": {
                    **dict(self.usage),
                    "cache_read_tokens": self.cache_read_tokens,
                    "cache_write_tokens": self.cache_write_tokens,
                    "context_tokens": self.context_tokens,
                    "last_tok_s": self.last_tok_s,
                    "last_ttft_ms": self.last_ttft_ms,
                },
                "event_count": self._next_seq,
                "next_seq": self._next_seq,
            }

    def to_detail(self) -> Dict[str, Any]:
        with self._lock:
            goal_active = bool(self.goal_state and self.goal_state.get("active") and self.status == "running")
            return {
                "id": self.id,
                "title": self.title,
                "target_url": self.target_url,
                "status": self.status,
                "error": self.last_error,
                "llm_profile_id": self.llm_profile_id,
                "llm_model": self.llm_model,
                "model": self.model,
                "steps_count": self.steps_count,
                "input_tokens": int(self.usage.get("prompt_tokens") or 0),
                "output_tokens": int(self.usage.get("completion_tokens") or 0),
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
                "context_tokens": self.context_tokens,
                "last_tok_s": self.last_tok_s,
                "last_ttft_ms": self.last_ttft_ms,
                "side_questions": [dict(x) for x in self.side_questions],
                "uploads": [dict(x) for x in self.uploads],
                "queued_messages": [dict(x) for x in self.queued_messages],
                "queued_count": len(self.queued_messages),
                "goal_state": dict(self.goal_state) if self.goal_state else None,
                "goal_active": goal_active,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "plan": list(self.plan),
                "usage": {
                    **dict(self.usage),
                    "cache_read_tokens": self.cache_read_tokens,
                    "cache_write_tokens": self.cache_write_tokens,
                    "context_tokens": self.context_tokens,
                    "last_tok_s": self.last_tok_s,
                    "last_ttft_ms": self.last_ttft_ms,
                },
                "next_seq": self._next_seq,
                "events": list(self.events),
            }

    def to_markdown(self) -> str:
        """Render full session transcript, telemetry, plan checklist, compaction cards, tool calls, and /btw Q&A into Markdown."""
        with self._lock:
            sid = self.id
            title = self.title
            target_url = self.target_url
            status = self.status
            model = self.model
            created_at = self.created_at
            updated_at = self.updated_at
            plan = list(self.plan)
            usage = dict(self.usage)
            cr = self.cache_read_tokens
            cw = self.cache_write_tokens
            ctx = self.context_tokens
            tok_s = self.last_tok_s
            ttft = self.last_ttft_ms
            events = list(self.events)
            side_qs = list(self.side_questions)

        in_t = int(usage.get("prompt_tokens") or 0)
        out_t = int(usage.get("completion_tokens") or 0)
        hit_pct = min(100, max(0, round(cr / in_t * 100))) if in_t > 0 else 0

        lines: List[str] = [
            f"# {title}",
            "",
            f"- **Session ID**: `{sid}`",
            f"- **Status**: `{status}`",
        ]
        if target_url:
            lines.append(f"- **Target URL**: {target_url}")
        if model:
            lines.append(f"- **Model**: `{model}`")
        lines.extend([
            f"- **Created**: {created_at}",
            f"- **Updated**: {updated_at}",
            (
                f"- **Token Usage**: Prompt `{in_t}` / Completion `{out_t}` / Total `{usage.get('total_tokens', in_t + out_t)}`"
            ),
            (
                f"- **Cache & Performance**: Cache Read `{cr}` ({hit_pct}%) / Cache Write `{cw}` / "
                f"Context `{ctx}` / Speed `{tok_s:.1f} tok/s` / TTFT `{ttft:.1f} ms`"
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
                tag = " · 实时纠偏" if ev.get("steered") else (" · /goal" if ev.get("goal") else "")
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
                if ev.get("thinking"):
                    lines.extend(["<details>", "<summary>思考过程</summary>", "", str(ev["thinking"]), "", "</details>", ""])
                lines.append(str(ev.get("content") or ""))
                lines.append("")
            elif et == "compaction":
                item = ev.get("item") if isinstance(ev.get("item"), dict) else ev
                tb = int(item.get("tokens_before") or 0)
                ta = int(item.get("tokens_after") or 0)
                dropped = int(item.get("dropped_messages") or 0)
                lines.append(f"### 上下文压缩检查点 ({tb} -> {ta} tokens · 归档 {dropped} 条 · {ts})")
                lines.append("")
                lines.append(str(item.get("content") or ""))
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

        if side_qs:
            lines.extend(["## 旁白提问 (/btw)", ""])
            for sq in side_qs:
                lines.extend([
                    f"### Q ({sq.get('ts', '')} · `{sq.get('model', '')}` · {sq.get('latency_ms', 0)}ms)",
                    "",
                    f"**问**：{sq.get('question', '')}",
                    "",
                    f"**答**：{sq.get('answer', '')}",
                    "",
                ])

        return "\n".join(lines).rstrip() + "\n"

    def persist(self) -> None:
        if not _SAFE_SESSION_ID_RE.fullmatch(self.id):
            return
        with self._lock:
            trimmed_msgs = _slice_messages_safely(self.messages, _PERSIST_MAX_MESSAGES)
            payload = {
                "id": self.id,
                "title": self.title,
                "target_url": self.target_url,
                "status": self.status,
                "error": self.last_error,
                "llm_profile_id": self.llm_profile_id,
                "llm_model": self.llm_model,
                "model": self.model,
                "steps_count": self.steps_count,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
                "context_tokens": self.context_tokens,
                "last_tok_s": self.last_tok_s,
                "last_ttft_ms": self.last_ttft_ms,
                "side_questions": [dict(x) for x in self.side_questions[-30:]],
                "uploads": [dict(x) for x in self.uploads[-50:]],
                "queued_messages": [dict(x) for x in self.queued_messages[-20:]],
                "goal_state": dict(self.goal_state) if self.goal_state else None,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "plan": list(self.plan),
                "usage": dict(self.usage),
                "next_seq": self._next_seq,
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
        if status in ("running", "waiting_approval"):
            # Recovered from disk after process restart; never falsely claim running
            status = "stopped"
        raw_msgs = data.get("messages") if isinstance(data.get("messages"), list) else []
        raw_next_seq = data.get("next_seq")
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
            next_seq=raw_next_seq if isinstance(raw_next_seq, int) and not isinstance(raw_next_seq, bool) else None,
            llm_profile_id=str(data.get("llm_profile_id") or ""),
            llm_model=str(data.get("llm_model") or ""),
            model=str(data.get("model") or ""),
            cache_read_tokens=int(data.get("cache_read_tokens") or 0),
            cache_write_tokens=int(data.get("cache_write_tokens") or 0),
            context_tokens=int(data.get("context_tokens") or 0),
            last_tok_s=float(data.get("last_tok_s") or 0.0),
            last_ttft_ms=float(data.get("last_ttft_ms") or 0.0),
            steps_count=int(data.get("steps_count") or 0),
            side_questions=data.get("side_questions") if isinstance(data.get("side_questions"), list) else [],
            uploads=data.get("uploads") if isinstance(data.get("uploads"), list) else [],
            queued_messages=data.get("queued_messages") if isinstance(data.get("queued_messages"), list) else [],
            goal_state=data.get("goal_state") if isinstance(data.get("goal_state"), dict) else None,
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
        return tool_read_adapter(
            str(args.get("name") or ""),
            start_line=args.get("start_line"),
            end_line=args.get("end_line"),
        )
    if name == "write_adapter":
        return tool_write_adapter(str(args.get("name") or ""), str(args.get("code") or ""), session_id=session.id)
    if name == "patch_adapter":
        return tool_patch_adapter(
            str(args.get("name") or ""),
            old_text=str(args.get("old_text") or ""),
            new_text=str(args.get("new_text") if args.get("new_text") is not None else ""),
            edits=args.get("edits") if isinstance(args.get("edits"), list) else None,
            session_id=session.id,
        )
    if name == "run_adapter_once":
        return tool_run_adapter_once(str(args.get("adapter") or ""), session=session, stop_ev=stop_ev)
    if name == "read_failures":
        return tool_read_failures(limit=args.get("limit", 20))
    if name == "read_core_file":
        return tool_read_core_file(
            str(args.get("path") or ""),
            start_line=args.get("start_line"),
            end_line=args.get("end_line"),
            query=str(args.get("query") or ""),
        )
    if name == "run_scratch_python":
        return tool_run_scratch_python(
            str(args.get("code") or ""),
            filename=str(args.get("filename") or ""),
            timeout_sec=int(args.get("timeout_sec") or 20),
            session=session,
            stop_ev=stop_ev,
        )
    if name == "read_session_file":
        return tool_read_session_file(
            session.id,
            name=str(args.get("name") or ""),
            start_line=args.get("start_line"),
            end_line=args.get("end_line"),
            query=str(args.get("query") or ""),
        )
    if name == "analyze_har":
        return tool_analyze_har(
            session.id,
            filename=str(args.get("filename") or ""),
            har_text=str(args.get("har_text") or ""),
            site_name=str(args.get("site_name") or ""),
        )
    if name == "check_infra":
        return tool_check_infra(check_ip_score=bool(args.get("check_ip_score", False)))
    if name == "manage_builtin_mail":
        return tool_manage_builtin_mail(
            str(args.get("action") or "status"),
            text=str(args.get("text") or ""),
            fmt=str(args.get("format") or "auto"),
            email=str(args.get("email") or ""),
            status=str(args.get("status") or "all"),
            mark_status=str(args["mark_status"]) if args.get("mark_status") is not None else None,
            limit=int(args.get("limit") or 20),
        )
    if name == "manage_builtin_proxies":
        return tool_manage_builtin_proxies(
            str(args.get("action") or "status"),
            text=str(args.get("text") or ""),
            default_scheme=str(args.get("default_scheme") or "http"),
            gateway_url=str(args.get("gateway_url") or ""),
            preset=str(args.get("preset") or "smartproxy"),
            country=str(args.get("country") or "us"),
            label=str(args.get("label") or ""),
            url=str(args.get("url") or ""),
            source_id=int(args.get("source_id") or 0),
            pull_now=bool(args.get("pull_now", False)),
            target_url=str(args.get("target_url") or ""),
            limit=int(args.get("limit") or 20),
        )
    if name == "verify_accounts":
        return tool_verify_accounts(
            base_url=str(args.get("base_url") or ""),
            limit=int(args.get("limit") or 10),
        )
    if name == "update_plan":
        steps = _normalize_plan_steps(args.get("steps"))
        plan_steps = [
            {
                "id": f"p_{i}",
                "title": s["title"],
                "status": "running" if s["status"] == "in_progress" else s["status"],
            }
            for i, s in enumerate(steps)
        ]
        with session._lock:
            session.plan = steps
        session.emit("plan", steps=steps, plan_steps=plan_steps)
        session.persist()
        return {"ok": True, "plan": steps}
    return {"ok": False, "error": f"未知工具: {name}"}


def _compact_tool_result_for_ui(result: Dict[str, Any]) -> Dict[str, Any]:
    """Keep SSE/persisted tool output concise so browser DOM stays responsive."""
    raw = json.dumps(result, ensure_ascii=False)
    if len(raw) <= 6000:
        return result
    compact = dict(result)
    for k in ("code", "output_tail", "content", "skeleton", "diff"):
        if isinstance(compact.get(k), str) and len(compact[k]) > 2500:
            compact[k] = compact[k][:2500] + "\n... [truncated]"
    return compact


MAX_EMPTY_NUDGES = 2
_GUARD_CFG_LOCK = threading.Lock()


def _guard_rules_file() -> Path:
    return RESULTS_DIR / "agent_guard_rules.json"


def _default_guard_rules() -> List[Dict[str, Any]]:
    return [
        {
            "id": "rule_no_fake_active",
            "name": "拦截假 Active 占位凭据",
            "description": "禁止在写入/修补适配器时硬编码 Active/OK/placeholder 作为 api_key 伪造成功。",
            "tool": "write_adapter",
            "pattern": r"""["']api_key["']\s*[:=]\s*["'](Active|OK|placeholder)["']""",
            "policy": "deny",
            "priority": 100,
            "enabled": True,
        },
        {
            "id": "rule_confirm_run",
            "name": "试跑适配器前人工审批（默认关闭）",
            "description": "开启后，Agent 在执行 run_adapter_once 前会暂停等待人工批准。",
            "tool": "run_adapter_once",
            "pattern": r".*",
            "policy": "ask",
            "priority": 30,
            "enabled": False,
        },
    ]


def _normalize_guard_rule(raw: Dict[str, Any]) -> Dict[str, Any]:
    policy = str(raw.get("policy") or "ask").strip().lower()
    if policy not in ("deny", "ask", "allow"):
        policy = "ask"
    try:
        prio = max(0, min(1000, int(raw.get("priority", 50))))
    except (TypeError, ValueError):
        prio = 50
    return {
        "id": str(raw.get("id") or f"rule_{secrets.token_hex(4)}").strip(),
        "name": str(raw.get("name") or "未命名规则").strip()[:100],
        "description": str(raw.get("description") or "").strip()[:300],
        "tool": str(raw.get("tool") or "*").strip() or "*",
        "pattern": str(raw.get("pattern") or ".*").strip()[:300],
        "policy": policy,
        "priority": prio,
        "enabled": bool(raw.get("enabled", True)),
    }


def load_guard_rules() -> List[Dict[str, Any]]:
    fp = _guard_rules_file()
    with _GUARD_CFG_LOCK:
        if not fp.is_file():
            return _default_guard_rules()
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [_normalize_guard_rule(r) for r in data if isinstance(r, dict)]
        except Exception:
            pass
        return _default_guard_rules()


def _save_guard_rules_unlocked(rules: List[Dict[str, Any]]) -> None:
    fp = _guard_rules_file()
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rules, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, fp)


def get_available_tools_meta() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in TOOL_SCHEMAS:
        fn = t["function"]
        name = fn["name"]
        meta = TOOL_META.get(name, {})
        out.append({
            "name": name,
            "label": meta.get("label") or name,
            "description": fn.get("description") or "",
            "risk": meta.get("risk") or ("exec" if "run_" in name else "write" if ("write_" in name or "patch_" in name) else "read"),
            "category": meta.get("category") or "general",
        })
    return out


def _compute_next_max_tokens_override(eff_cap: int, cur_override: Optional[int]) -> int:
    base = max(int(eff_cap or 0), int(cur_override or 0), 4096)
    if base < 16384:
        return 16384
    if base < 32768:
        return 32768
    return 65536


def _call_compat_chat_completion(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    system_prompt: str,
    *,
    config_override: Optional[Dict[str, Any]] = None,
    timeout: float = 90.0,
    max_tokens_override: Optional[int] = None,
    session_id: str = "",
) -> Dict[str, Any]:
    """Call `llm_client.chat_completion` safely even when unit tests monkeypatch it with a narrow signature."""
    fn = llm_client.chat_completion
    kwargs: Dict[str, Any] = {
        "messages": messages,
        "tools": tools,
        "system_prompt": system_prompt,
    }
    try:
        sig = inspect.signature(fn)
        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        if "config_override" in sig.parameters or has_varkw:
            kwargs["config_override"] = config_override
        if "timeout" in sig.parameters or has_varkw:
            kwargs["timeout"] = timeout
        if max_tokens_override is not None and ("max_tokens_override" in sig.parameters or has_varkw):
            kwargs["max_tokens_override"] = max_tokens_override
        if session_id and ("session_id" in sig.parameters or has_varkw):
            kwargs["session_id"] = session_id
    except (TypeError, ValueError):
        kwargs["config_override"] = config_override
    return fn(**kwargs)


class SingleAgentManager:
    """Strict single-thread manager for autonomous registration sessions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, AgentSession] = {}
        self._external_busy_check: Optional[Callable[[], Optional[str]]] = None
        self._compacting_sessions: set[str] = set()
        self._approvals: List[Dict[str, Any]] = []
        self._approval_events: Dict[str, threading.Event] = {}
        atexit.register(self.stop_all)

    def set_external_busy_check(self, fn: Optional[Callable[[], Optional[str]]]) -> None:
        self._external_busy_check = fn

    def active_session_id(self) -> Optional[str]:
        with self._lock:
            for s in self._sessions.values():
                if s.status in ("running", "waiting_approval"):
                    return s.id
        return None

    # ---------------------- Guard Rules & Approvals ----------------------

    def list_guard_rules(self) -> List[Dict[str, Any]]:
        return load_guard_rules()

    def upsert_guard_rule(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("拦截规则必须为 JSON 对象")
        name = str(data.get("name") or "").strip()
        pattern = str(data.get("pattern") or "").strip()
        if not name or not pattern:
            raise ValueError("规则名称与匹配正则不能为空")
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(f"正则表达式语法错误: {e}") from e
        rule = _normalize_guard_rule(data)
        with _GUARD_CFG_LOCK:
            fp = _guard_rules_file()
            if fp.is_file():
                try:
                    raw = json.loads(fp.read_text(encoding="utf-8"))
                    rules = [_normalize_guard_rule(r) for r in raw if isinstance(r, dict)] if isinstance(raw, list) else _default_guard_rules()
                except Exception:
                    rules = _default_guard_rules()
            else:
                rules = _default_guard_rules()
            idx = next((i for i, r in enumerate(rules) if r["id"] == rule["id"]), -1)
            if idx >= 0:
                rules[idx] = rule
            else:
                rules.append(rule)
            _save_guard_rules_unlocked(rules)
        return {"ok": True, "rule": rule, "guard_rules": rules}

    def delete_guard_rule(self, rule_id: str) -> Dict[str, Any]:
        rid = str(rule_id or "").strip()
        with _GUARD_CFG_LOCK:
            fp = _guard_rules_file()
            if fp.is_file():
                try:
                    raw = json.loads(fp.read_text(encoding="utf-8"))
                    rules = [_normalize_guard_rule(r) for r in raw if isinstance(r, dict)] if isinstance(raw, list) else _default_guard_rules()
                except Exception:
                    rules = _default_guard_rules()
            else:
                rules = _default_guard_rules()
            before = len(rules)
            rules = [r for r in rules if r["id"] != rid]
            if len(rules) == before:
                raise KeyError(f"未找到拦截规则: {rid}")
            _save_guard_rules_unlocked(rules)
        return {"ok": True, "deleted": rid, "guard_rules": rules}

    def _evaluate_guard_detail(self, tool_name: str, args: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
        haystack = json.dumps(args, ensure_ascii=False)
        rules = sorted(load_guard_rules(), key=lambda r: int(r.get("priority") or 0), reverse=True)
        for r in rules:
            if not r.get("enabled", True):
                continue
            rt = str(r.get("tool") or "*")
            if rt != "*" and rt != tool_name:
                continue
            try:
                if re.search(str(r.get("pattern") or ""), haystack, re.I):
                    return str(r.get("policy") or "allow"), r
            except re.error:
                continue
        return "allow", None

    def test_guard_rule(self, tool: str, args: Any = None) -> Dict[str, Any]:
        tname = str(tool or "*").strip() or "*"
        if isinstance(args, str):
            try:
                parsed = json.loads(args) if args.strip() else {}
                args_dict = parsed if isinstance(parsed, dict) else {"value": parsed}
            except Exception:
                args_dict = {"_raw": args}
        elif isinstance(args, dict):
            args_dict = args
        else:
            args_dict = {}
        policy, matched = self._evaluate_guard_detail(tname, args_dict)
        rule_name = str(matched.get("name") or matched.get("id") or "") if matched else ""
        desc = str(matched.get("description") or "") if matched else ""
        if policy == "deny":
            msg = f"命中安全拦截规则「{rule_name}」，拒绝执行 {tname}" + (f"：{desc}" if desc else "")
        elif policy == "ask":
            msg = f"命中审批规则「{rule_name}」，执行 {tname} 前将暂停等待人工批准。"
        else:
            msg = f"规则「{rule_name}」允许执行 {tname}。" if matched else f"未命中拦截规则，默认放行工具 {tname}。"
        return {
            "ok": True,
            "policy": policy,
            "matched_rule": dict(matched) if matched else None,
            "rendered_message": msg,
        }

    def list_approvals(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(a) for a in self._approvals[-50:]]

    def resolve_approval(self, approval_id: str, decision: str) -> Dict[str, Any]:
        dec = "approved" if str(decision or "").strip().lower() in ("approve", "approved") else "rejected"
        target_sess: Optional[AgentSession] = None
        with self._lock:
            target = next((a for a in self._approvals if a.get("id") == approval_id), None)
            if not target:
                raise KeyError(f"未找到审批记录: {approval_id}")
            target["status"] = dec
            target["decided_at"] = time.strftime("%H:%M:%S")
            sid = str(target.get("session_id") or "")
            target_sess = self._sessions.get(sid)
            ev = self._approval_events.get(approval_id)
            if ev:
                ev.set()
        if target_sess is not None:
            with target_sess._lock:
                if target_sess.status == "waiting_approval":
                    target_sess.status = "running"
            target_sess.emit("approval_resolved", approval_id=approval_id, status=dec)
            target_sess.emit("status", status="running", error="")
            target_sess.persist()
        return {"ok": True, "id": approval_id, "status": dec}

    # ---------------------- Session Storage ----------------------

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

    def create_session(
        self,
        title: str = "",
        target_url: str = "",
        *,
        llm_profile_id: str = "",
        llm_model: str = "",
    ) -> AgentSession:
        clean_url = str(target_url or "").strip()
        clean_title = str(title or "").strip()[:80]
        raw_pid = str(llm_profile_id or "").strip()
        clean_pid = "" if raw_pid == "__follow_global__" else raw_pid
        raw_mod = str(llm_model or "").strip()
        clean_mod = "" if raw_mod == "__follow_profile__" else raw_mod
        if not clean_title:
            if clean_url:
                host = (urlparse(clean_url).hostname or "").replace("www.", "")
                clean_title = f"{host} AI 直跑" if host else "AI 直跑会话"
            else:
                clean_title = f"AI 会话 {time.strftime('%H:%M:%S')}"
        sid = f"ag_{int(time.time())}_{secrets.token_hex(3)}"
        sess = AgentSession(
            sid,
            title=clean_title,
            target_url=clean_url,
            llm_profile_id=clean_pid,
            llm_model=clean_mod,
        )
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
                self._approvals = [a for a in self._approvals if a.get("session_id") != session_id]
        fp = AGENT_SESSIONS_DIR / f"{session_id}.json"
        if fp.is_file():
            try:
                fp.unlink()
                existed = True
            except OSError:
                pass
        sess_sub = (AGENT_SESSIONS_DIR / session_id).resolve()
        if AGENT_SESSIONS_DIR.resolve() in sess_sub.parents and sess_sub.exists():
            shutil.rmtree(sess_sub, ignore_errors=True)
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
        sess.emit("session_renamed", session_id=session_id, title=clean_title)
        sess.persist()
        return sess.to_summary()

    def stop_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        sess = self.get_session(session_id)
        if sess is None:
            return None
        with self._lock:
            for app in self._approvals:
                if app.get("session_id") == session_id and app.get("status") == "pending":
                    app["status"] = "rejected"
                    app["decided_at"] = time.strftime("%H:%M:%S")
                    ev = self._approval_events.get(app["id"])
                    if ev:
                        ev.set()
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
            if s.status in ("running", "waiting_approval"):
                s.stop()
            s.close_streams(force_all=True)

    # ---------------------- Step Checkpoints & Restore ----------------------

    def list_checkpoints(self, session_id: str) -> Dict[str, Any]:
        sess = self.get_session(session_id)
        if sess is None:
            raise KeyError(f"会话不存在: {session_id}")
        cp_root = (AGENT_SESSIONS_DIR / session_id / "checkpoints").resolve()
        items: List[Dict[str, Any]] = []
        if cp_root.is_dir():
            for d in sorted((p for p in cp_root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True):
                mp = d / "meta.json"
                if not mp.is_file():
                    continue
                try:
                    obj = json.loads(mp.read_text(encoding="utf-8"))
                    if isinstance(obj, dict) and obj.get("id"):
                        items.append(obj)
                except Exception:
                    continue
        return {"ok": True, "session_id": session_id, "count": len(items), "checkpoints": items}

    def restore_checkpoint(self, session_id: str, checkpoint_id: str) -> Dict[str, Any]:
        sess = self.get_session(session_id)
        if sess is None:
            raise KeyError(f"会话不存在: {session_id}")
        cp_id = str(checkpoint_id or "").strip()
        if not _SAFE_CHECKPOINT_ID_RE.fullmatch(cp_id):
            raise ValueError(f"非法的检查点 ID: {checkpoint_id!r}")
        cp_root = (AGENT_SESSIONS_DIR / session_id / "checkpoints").resolve()
        cp_dir = (cp_root / cp_id).resolve()
        if cp_dir.parent != cp_root or not cp_dir.is_dir():
            raise KeyError(f"检查点不存在: {cp_id}")
        meta_path = cp_dir / "meta.json"
        before_path = cp_dir / "before.py"
        if not meta_path.is_file() or not before_path.is_file():
            raise KeyError(f"检查点快照不完整: {cp_id}")

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        adapter_name = str(meta.get("site") or "").strip()
        sites_root = SITES_DIR.resolve()
        target_file = Path(str(meta.get("file") or "")).resolve()
        if target_file.parent != sites_root:
            raise ValueError("安全校验失败：检查点目标文件不在 sites/ 目录下")

        before_text = before_path.read_text(encoding="utf-8")
        current_text = target_file.read_text(encoding="utf-8", errors="ignore") if target_file.exists() else ""

        backup_cp_id = _save_adapter_checkpoint(
            session_id,
            adapter_name=adapter_name,
            target_file=target_file,
            before_text=current_text,
            after_text=before_text,
            existed_before=target_file.exists(),
            kind="pre_restore",
            prefix="pre_restore",
        )

        target_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = target_file.with_suffix(".py.tmp")
        tmp.write_text(before_text, encoding="utf-8")
        os.replace(tmp, target_file)
        if target_file.read_text(encoding="utf-8") != before_text:
            raise RuntimeError("回滚后文件独立回读校验不一致")
        if adapter_name:
            sys.modules.pop(f"sites.{adapter_name}", None)

        note = (
            f"已将 sites/{target_file.name} 回滚至检查点 {cp_id} 写入前状态"
            f"（{len(before_text.splitlines())} 行；原代码已备份为 {backup_cp_id}）"
        )
        sess.emit("system_msg", content=note)
        sess.persist()
        return {
            "ok": True,
            "session_id": session_id,
            "checkpoint_id": cp_id,
            "backup_checkpoint_id": backup_cp_id,
            "project": adapter_name,
            "file": str(target_file),
            "restored_lines": len(before_text.splitlines()),
        }

    # ---------------------- Follow-up Queue ----------------------

    def remove_queued_message(self, session_id: str, queue_id: str) -> Dict[str, Any]:
        sess = self.get_session(session_id)
        if sess is None:
            raise KeyError(f"会话不存在: {session_id}")
        qid = str(queue_id or "").strip()
        with sess._lock:
            before = len(sess.queued_messages)
            sess.queued_messages = [it for it in sess.queued_messages if str(it.get("id") or "") != qid]
            if len(sess.queued_messages) == before:
                raise KeyError(f"排队消息不存在: {qid}")
            snap = [dict(it) for it in sess.queued_messages]
        sess.emit("queue_updated", queued_messages=snap)
        sess.persist()
        return {"ok": True, "session_id": session_id, "queued_messages": snap}

    def promote_queued_message(self, session_id: str, queue_id: str) -> Dict[str, Any]:
        sess = self.get_session(session_id)
        if sess is None:
            raise KeyError(f"会话不存在: {session_id}")
        qid = str(queue_id or "").strip()
        with sess._lock:
            idx = next((i for i, it in enumerate(sess.queued_messages) if str(it.get("id") or "") == qid), -1)
            if idx < 0:
                raise KeyError(f"排队消息不存在: {qid}")
            item = sess.queued_messages.pop(idx)
            snap = [dict(it) for it in sess.queued_messages]
        sess.emit("queue_updated", queued_messages=snap)
        sess.persist()
        try:
            res = self.send_message(
                session_id,
                str(item.get("content") or ""),
                target_url=str(item.get("target_url") or ""),
                llm_profile_id=str(item.get("llm_profile_id") or "") or None,
                llm_model=str(item.get("llm_model") or "") or None,
                mode="steer",
                goal_mode=bool(item.get("goal_mode")),
            )
            res["queued_messages"] = snap
            return res
        except Exception:
            with sess._lock:
                sess.queued_messages.insert(0, item)
                restored = [dict(it) for it in sess.queued_messages]
            sess.emit("queue_updated", queued_messages=restored)
            sess.persist()
            raise

    # ---------------------- Runtime Config & LLM Calls ----------------------

    def _resolve_runtime_override(
        self,
        prof_id: str = "",
        mod_ov: str = "",
        default_cfg_pid: str = "",
    ) -> Optional[Dict[str, Any]]:
        eff_pid = str(prof_id or "").strip() or str(default_cfg_pid or "").strip()
        eff_mod = str(mod_ov or "").strip()
        if eff_pid or eff_mod or llm_profiles._profiles_file().exists():
            return llm_profiles.resolve_runtime_override(profile_id=eff_pid, model_override=eff_mod)
        return None

    def _call_llm_for_turn(
        self,
        sess: AgentSession,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        system_prompt: str,
        runtime_ov: Optional[Dict[str, Any]],
        stop_ev: threading.Event,
        *,
        max_tokens_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Use real SSE streaming in production; seamlessly honor monkeypatched `chat_completion` in unit tests."""
        use_sync = (
            llm_client.chat_completion is not llm_client._ORIG_CHAT_COMPLETION
            and llm_client.stream_chat_completion is llm_client._ORIG_STREAM_CHAT_COMPLETION
        )
        if use_sync:
            return _call_compat_chat_completion(
                messages=messages,
                tools=tools,
                system_prompt=system_prompt,
                config_override=runtime_ov,
                max_tokens_override=max_tokens_override,
                session_id=sess.id,
            )

        buf_type = ""
        buf_chunks: List[str] = []
        buf_chars = 0
        last_flush = time.monotonic()
        tc_name = ""
        tc_total_chars = 0
        tc_pending_chars = 0
        tc_tail = ""
        tc_last_flush = time.monotonic()
        tc_announced = False

        def _flush() -> None:
            nonlocal buf_type, buf_chunks, buf_chars, last_flush
            if buf_chunks and buf_type:
                sess.emit_delta(buf_type, delta="".join(buf_chunks))
                buf_chunks = []
                buf_chars = 0
                buf_type = ""
                last_flush = time.monotonic()

        def _flush_tool_delta(*, force: bool = False) -> None:
            nonlocal tc_pending_chars, tc_last_flush, tc_announced
            if not tc_name and tc_pending_chars == 0:
                return
            now_m = time.monotonic()
            if (
                force
                or not tc_announced
                or tc_pending_chars >= 120
                or (tc_pending_chars > 0 and (now_m - tc_last_flush) >= 0.12)
            ):
                sess.emit_delta(
                    "tool_call_delta",
                    name=tc_name or "tool",
                    delta_chars=tc_pending_chars,
                    total_chars=tc_total_chars,
                    preview=tc_tail,
                )
                tc_pending_chars = 0
                tc_last_flush = now_m
                tc_announced = True

        def _on_stream_event(ev: Dict[str, Any]) -> None:
            nonlocal buf_type, buf_chars, last_flush, tc_name, tc_total_chars, tc_pending_chars, tc_tail, tc_announced
            et = str(ev.get("type") or "")
            if et not in _DELTA_EVENT_TYPES:
                return
            if et == "tool_call_delta":
                _flush()
                incoming_name = str(ev.get("name") or "").strip()
                delta = str(ev.get("delta") or "")
                switched_tool = bool(incoming_name and incoming_name != tc_name)
                if switched_tool:
                    tc_name = incoming_name
                    tc_announced = False
                elif not tc_name and incoming_name:
                    tc_name = incoming_name
                if delta:
                    tc_total_chars += len(delta)
                    tc_pending_chars += len(delta)
                    tc_tail = (tc_tail + delta)[-180:]
                _flush_tool_delta(force=switched_tool)
                return

            delta = str(ev.get("delta") or "")
            if not delta:
                return
            if buf_type and buf_type != et:
                _flush()
            if not buf_type:
                buf_type = et
                last_flush = time.monotonic()
            buf_chunks.append(delta)
            buf_chars += len(delta)
            if buf_chars >= 24 or (time.monotonic() - last_flush) >= 0.04:
                _flush()

        try:
            return llm_client.stream_chat_completion(
                messages=messages,
                tools=tools,
                system_prompt=system_prompt,
                config_override=runtime_ov,
                max_tokens_override=max_tokens_override,
                on_event=_on_stream_event,
                stop_event=stop_ev,
                session_id=sess.id,
            )
        finally:
            _flush()
            if tc_pending_chars > 0:
                _flush_tool_delta(force=True)

    # ---------------------- True LLM Context Compaction ----------------------

    def _execute_compaction_summary(
        self,
        history: List[Dict[str, Any]],
        sys_prompt: str,
        runtime_ov: Optional[Dict[str, Any]],
        *,
        keep_recent_tokens: int,
        custom_instructions: str = "",
        force: bool = True,
        recorded_context_tokens: int = 0,
        stop_ev: Optional[threading.Event] = None,
        session_id: str = "",
    ) -> Dict[str, Any]:
        if stop_ev is not None and stop_ev.is_set():
            return {"ok": False, "aborted": True, "compacted": False, "error": "已手动停止"}

        clean_hist = _repair_dangling_tool_calls(history)
        previous_summary, start_idx = _extract_previous_compaction_summary(clean_hist)
        est_before = _estimate_history_tokens(clean_hist, sys_prompt)
        tokens_before = max(int(recorded_context_tokens or 0), est_before)

        cut_idx = _find_compaction_cut_point(clean_hist, start_idx, keep_recent_tokens, force=force)
        to_summarize = clean_hist[start_idx:cut_idx]
        kept_tail = clean_hist[cut_idx:]

        if not to_summarize:
            return {
                "ok": True,
                "compacted": False,
                "reason": "当前对话轮次过少或已高度精简，暂无可归档的旧上下文",
                "tokens_before": tokens_before,
                "tokens_after": tokens_before,
                "dropped_messages": 0,
                "kept_messages": len(clean_hist),
            }

        conv_text = _serialize_history_for_compaction(to_summarize)
        prompt = f"<conversation>\n{conv_text}\n</conversation>\n\n"
        if previous_summary:
            prompt += f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
            base_inst = _COMPACTION_UPDATE_PROMPT
        else:
            base_inst = _COMPACTION_INITIAL_PROMPT
        if custom_instructions:
            base_inst = f"{base_inst}\n\n额外重点关注：{custom_instructions}"

        resp = _call_compat_chat_completion(
            messages=[{"role": "user", "content": prompt + base_inst}],
            tools=None,
            system_prompt=_COMPACTION_SYSTEM_PROMPT,
            config_override=runtime_ov,
            timeout=60.0,
            session_id=f"{session_id}:compact" if session_id else "",
        )
        if stop_ev is not None and stop_ev.is_set():
            return {"ok": False, "aborted": True, "compacted": False, "error": "已手动停止"}
        summary_text = str(resp.get("content") or "").strip()
        if not summary_text:
            return {"ok": False, "compacted": False, "error": "模型未返回有效压缩摘要，原上下文保持不变"}

        touched_sites, touched_files = _extract_compaction_artifacts(to_summarize)
        extra_blocks: List[str] = []
        if touched_sites:
            extra_blocks.append("<touched-sites>\n" + "\n".join(touched_sites) + "\n</touched-sites>")
        if touched_files:
            extra_blocks.append("<touched-files>\n" + "\n".join(touched_files) + "\n</touched-files>")
        if extra_blocks:
            summary_text = summary_text + "\n\n" + "\n\n".join(extra_blocks)

        checkpoint_msg = {
            "role": "user",
            "content": (
                f"{_COMPACTION_TAG_OPEN}\n"
                "以下是之前已压缩的历史对话检查点摘要，请基于此上下文无缝继续工作：\n\n"
                f"{summary_text}\n"
                f"{_COMPACTION_TAG_CLOSE}"
            ),
        }
        new_history = _repair_dangling_tool_calls([checkpoint_msg, *kept_tail])
        tokens_after = _estimate_history_tokens(new_history, sys_prompt)
        u = resp.get("usage") or {}
        return {
            "ok": True,
            "compacted": True,
            "summary": summary_text,
            "new_history": new_history,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "dropped_messages": len(to_summarize),
            "kept_messages": len(kept_tail),
            "touched_sites": touched_sites,
            "touched_files": touched_files,
            "model": str(resp.get("model") or ""),
            "input_tokens": max(0, int(u.get("prompt_tokens") or resp.get("input_tokens") or 0)),
            "output_tokens": max(0, int(u.get("completion_tokens") or resp.get("output_tokens") or 0)),
        }

    def compact_session(
        self,
        session_id: str,
        *,
        custom_instructions: str = "",
        keep_recent_tokens: Optional[int] = None,
        llm_profile_id: str = "",
        llm_model: str = "",
    ) -> Dict[str, Any]:
        """Manual LLM context compaction: summarizes older messages into a checkpoint card while preserving UI history."""
        sess = self.get_session(session_id)
        if sess is None:
            raise KeyError(f"会话不存在: {session_id}")
        custom_clean = str(custom_instructions or "").strip()[:1000]
        keep_budget = max(512, min(64000, int(keep_recent_tokens or _DEFAULT_KEEP_RECENT_TOKENS)))

        with self._lock:
            if sess.status in ("running", "waiting_approval"):
                raise RuntimeError("当前会话正在执行中，请等待本轮完成或先停止后再压缩上下文")
            if session_id in self._compacting_sessions:
                raise RuntimeError("当前会话正在压缩中，请稍候")
            self._compacting_sessions.add(session_id)

        try:
            saved_cfg = _load_saved_agent_config_if_exists()
            sys_prompt = str(saved_cfg.get("system_prompt") or AGENT_SYSTEM_PROMPT) if saved_cfg else AGENT_SYSTEM_PROMPT
            default_pid = str(saved_cfg.get("llm_profile_id") or "").strip() if saved_cfg else ""
            with sess._lock:
                hist_copy = list(sess.messages)
                before_len = len(hist_copy)
                eff_pid = str(llm_profile_id or "").strip() or sess.llm_profile_id
                eff_mod = str(llm_model or "").strip() or sess.llm_model
                rec_ctx = sess.context_tokens
                cur_model = sess.model
            runtime_ov = self._resolve_runtime_override(eff_pid, eff_mod, default_pid)
            res = self._execute_compaction_summary(
                history=hist_copy,
                sys_prompt=sys_prompt,
                runtime_ov=runtime_ov,
                keep_recent_tokens=keep_budget,
                custom_instructions=custom_clean,
                force=True,
                recorded_context_tokens=rec_ctx,
                session_id=session_id,
            )
            if not res.get("ok"):
                raise RuntimeError(str(res.get("error") or "上下文压缩失败"))
            if not res.get("compacted"):
                return {
                    **res,
                    "before": before_len,
                    "after": before_len,
                    "message": res.get("reason") or "暂无可压缩上下文",
                    "session": sess.to_detail(),
                }

            comp_item: Dict[str, Any] = {
                "id": f"cmp_{secrets.token_hex(4)}",
                "role": "compaction",
                "content": res["summary"],
                "tokens_before": res["tokens_before"],
                "tokens_after": res["tokens_after"],
                "dropped_messages": res["dropped_messages"],
                "kept_messages": res["kept_messages"],
                "touched_sites": res["touched_sites"],
                "touched_files": res["touched_files"],
                "auto": False,
                "custom_instructions": custom_clean,
                "model": res.get("model") or cur_model,
                "ts": time.strftime("%H:%M:%S"),
            }
            with sess._lock:
                if sess.status in ("running", "waiting_approval"):
                    raise RuntimeError("压缩期间会话已重新启动，本次压缩已放弃")
                sess.messages = res["new_history"]
                sess.context_tokens = int(res["tokens_after"])
                sess.usage["prompt_tokens"] += int(res.get("input_tokens") or 0)
                sess.usage["completion_tokens"] += int(res.get("output_tokens") or 0)
                sess.usage["total_tokens"] = sess.usage["prompt_tokens"] + sess.usage["completion_tokens"]
                in_tot = sess.usage["prompt_tokens"]
                out_tot = sess.usage["completion_tokens"]
                cr_tot = sess.cache_read_tokens
                cw_tot = sess.cache_write_tokens
                mod_now = sess.model or comp_item["model"]
                after_len = len(sess.messages)

            sess.emit("compaction", item=comp_item)
            sess.emit(
                "usage",
                prompt_tokens=int(res.get("input_tokens") or 0),
                completion_tokens=int(res.get("output_tokens") or 0),
                total_tokens=int(res.get("input_tokens") or 0) + int(res.get("output_tokens") or 0),
                input_tokens=in_tot,
                output_tokens=out_tot,
                cache_read_tokens=cr_tot,
                cache_write_tokens=cw_tot,
                cache_hit_pct=min(100, max(0, round(cr_tot / in_tot * 100))) if in_tot > 0 else 0,
                context_tokens=int(res["tokens_after"]),
                model=mod_now,
            )
            sess.persist()
            return {
                "ok": True,
                "compacted": True,
                "tokens_before": res["tokens_before"],
                "tokens_after": res["tokens_after"],
                "dropped_messages": res["dropped_messages"],
                "kept_messages": res["kept_messages"],
                "summary": res["summary"],
                "item": comp_item,
                "before": before_len,
                "after": after_len,
                "message": f"已完成 LLM 上下文压缩：{res['tokens_before']} → {res['tokens_after']} tokens（归档 {res['dropped_messages']} 条）",
                "session": sess.to_detail(),
            }
        finally:
            with self._lock:
                self._compacting_sessions.discard(session_id)

    # ---------------------- Streaming /btw Side Questions ----------------------

    def _prepare_side_question_context(
        self,
        sess: AgentSession,
        question: str,
        llm_profile_id: str = "",
        llm_model: str = "",
    ) -> Tuple[str, str, List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        q_text = str(question or "").strip()
        if not q_text:
            raise ValueError("旁白提问内容不能为空")
        saved_cfg = _load_saved_agent_config_if_exists()
        default_pid = str(saved_cfg.get("llm_profile_id") or "").strip() if saved_cfg else ""
        with sess._lock:
            prof_id = str(llm_profile_id or "").strip() or sess.llm_profile_id
            mod_ov = str(llm_model or "").strip() or sess.llm_model
            title = sess.title
            status = sess.status
            t_url = sess.target_url
            plan_lines = [f"- [{st.get('status', 'pending')}] {st.get('title', '')}" for st in sess.plan]
            recent_lines: List[str] = []
            for ev in sess.events[-12:]:
                et = str(ev.get("type") or "")
                if et == "user_msg":
                    recent_lines.append(f"[user] {str(ev.get('content') or '')[:450]}")
                elif et == "assistant_msg":
                    recent_lines.append(f"[assistant] {str(ev.get('content') or '')[:450]}")
                elif et == "tool_end":
                    recent_lines.append(f"[tool:{ev.get('name')}] ok={ev.get('ok')} -> {json.dumps(ev.get('result') or {}, ensure_ascii=False)[:350]}")
            prev_sqs = list(sess.side_questions[-4:])

        side_sys = (
            "你是 RegisterHub 会话的独立旁白答疑助手（/btw）。\n"
            "请仅依据以下只读会话快照用简洁中文直接回答用户问题，不中断主任务，也不要虚构工具调用。\n\n"
            f"- 会话标题: {title}\n"
            f"- 当前状态: {status}\n"
            f"- 目标 URL: {t_url or '(未设置)'}\n"
            f"- 执行计划:\n{chr(10).join(plan_lines) if plan_lines else '(暂无)'}\n\n"
            f"- 最近进展:\n{chr(10).join(recent_lines) if recent_lines else '(暂无)'}"
        )
        side_messages: List[Dict[str, Any]] = []
        for sq in prev_sqs:
            pq = str(sq.get("question") or "").strip()
            pa = str(sq.get("answer") or "").strip()
            if pq and pa:
                side_messages.append({"role": "user", "content": pq})
                side_messages.append({"role": "assistant", "content": pa})
        side_messages.append({"role": "user", "content": q_text})
        runtime_ov = self._resolve_runtime_override(prof_id, mod_ov, default_pid)
        return q_text, side_sys, side_messages, runtime_ov

    def _finalize_side_question_item(
        self,
        sess: AgentSession,
        q_text: str,
        res: Dict[str, Any],
        latency_ms: float,
        item_id: str = "",
    ) -> Dict[str, Any]:
        u = res.get("usage") or {}
        in_tok = max(0, int(res.get("input_tokens") or u.get("prompt_tokens") or 0))
        out_tok = max(0, int(res.get("output_tokens") or u.get("completion_tokens") or 0))
        cr_tok = max(0, int(res.get("cache_read_tokens") or u.get("cache_read_tokens") or 0))
        if in_tok > 0 and cr_tok > in_tok:
            cr_tok = in_tok
        hit_pct = min(100, max(0, round(cr_tok / in_tok * 100))) if in_tok > 0 else 0
        item: Dict[str, Any] = {
            "id": item_id or f"btw_{secrets.token_hex(4)}",
            "question": q_text,
            "answer": str(res.get("content") or "").strip() or "（模型未返回正文）",
            "model": str(res.get("model") or ""),
            "latency_ms": round(max(0.0, float(res.get("latency_ms") or latency_ms)), 1),
            "ttft_ms": round(max(0.0, float(res.get("ttft_ms") or latency_ms)), 1),
            "tok_s": round(max(0.0, float(res.get("tok_s") or 0.0)), 1),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_read_tokens": cr_tok,
            "cache_hit_pct": hit_pct,
            "ts": time.strftime("%H:%M:%S"),
        }
        thinking_txt = str(res.get("thinking") or "").strip()
        if thinking_txt:
            item["thinking"] = thinking_txt
        with sess._lock:
            sess.side_questions.append(item)
            if len(sess.side_questions) > 30:
                sess.side_questions = sess.side_questions[-30:]
        sess.emit("side_question", item=item)
        sess.persist()
        return item

    def ask_side_question(
        self,
        session_id: str,
        question: str,
        *,
        llm_profile_id: str = "",
        llm_model: str = "",
    ) -> Dict[str, Any]:
        sess = self.get_session(session_id)
        if sess is None:
            raise KeyError(f"会话不存在: {session_id}")
        q_text, side_sys, side_messages, runtime_ov = self._prepare_side_question_context(
            sess, question, llm_profile_id=llm_profile_id, llm_model=llm_model
        )
        t0 = time.perf_counter()
        res = _call_compat_chat_completion(
            messages=side_messages,
            tools=None,
            system_prompt=side_sys,
            config_override=runtime_ov,
            timeout=45.0,
            session_id=f"{session_id}:btw",
        )
        ms = round((time.perf_counter() - t0) * 1000.0, 1)
        item = self._finalize_side_question_item(sess, q_text, res, ms)
        return {"ok": True, "item": item}

    def stream_side_question(
        self,
        session_id: str,
        question: str,
        *,
        llm_profile_id: str = "",
        llm_model: str = "",
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        sess = self.get_session(session_id)
        if sess is None:
            raise KeyError(f"会话不存在: {session_id}")
        q_text, side_sys, side_messages, runtime_ov = self._prepare_side_question_context(
            sess, question, llm_profile_id=llm_profile_id, llm_model=llm_model
        )
        item_id = f"btw_{secrets.token_hex(4)}"

        def _push(ev: Dict[str, Any]) -> None:
            if callable(on_event):
                try:
                    on_event(ev)
                except Exception:
                    pass

        _push({"type": "start", "id": item_id, "question": q_text})
        t0 = time.perf_counter()

        use_sync = (
            llm_client.chat_completion is not llm_client._ORIG_CHAT_COMPLETION
            and llm_client.stream_chat_completion is llm_client._ORIG_STREAM_CHAT_COMPLETION
        )
        if use_sync:
            res = _call_compat_chat_completion(
                messages=side_messages,
                tools=None,
                system_prompt=side_sys,
                config_override=runtime_ov,
                timeout=45.0,
                session_id=f"{session_id}:btw",
            )
            if res.get("thinking"):
                _push({"type": "thinking_delta", "delta": str(res["thinking"])})
            if res.get("content"):
                _push({"type": "delta", "delta": str(res["content"])})
        else:
            buf_type = ""
            buf_chunks: List[str] = []
            buf_chars = 0
            last_flush = time.monotonic()

            def _flush() -> None:
                nonlocal buf_type, buf_chunks, buf_chars, last_flush
                if buf_chunks and buf_type:
                    out_type = "thinking_delta" if buf_type == "thinking_delta" else "delta"
                    _push({"type": out_type, "delta": "".join(buf_chunks)})
                    buf_chunks = []
                    buf_chars = 0
                    buf_type = ""
                    last_flush = time.monotonic()

            def _on_ev(ev: Dict[str, Any]) -> None:
                nonlocal buf_type, buf_chars, last_flush
                et = str(ev.get("type") or "")
                delta = str(ev.get("delta") or "")
                if et not in ("text_delta", "thinking_delta") or not delta:
                    return
                if buf_type and buf_type != et:
                    _flush()
                if not buf_type:
                    buf_type = et
                    last_flush = time.monotonic()
                buf_chunks.append(delta)
                buf_chars += len(delta)
                if buf_chars >= 16 or (time.monotonic() - last_flush) >= 0.035:
                    _flush()

            try:
                res = llm_client.stream_chat_completion(
                    messages=side_messages,
                    tools=None,
                    system_prompt=side_sys,
                    config_override=runtime_ov,
                    timeout=60.0,
                    on_event=_on_ev,
                    stop_event=stop_event,
                    session_id=f"{session_id}:btw",
                )
            finally:
                _flush()

        ms = round((time.perf_counter() - t0) * 1000.0, 1)
        if res.get("aborted"):
            partial = str(res.get("partial_content") or res.get("content") or "").strip()
            if partial:
                res_p = dict(res)
                res_p["content"] = partial
                item = self._finalize_side_question_item(sess, q_text, res_p, ms, item_id=item_id)
                _push({"type": "done", "ok": True, "aborted": True, "item": item})
                return {"ok": True, "aborted": True, "item": item}
            _push({"type": "done", "ok": False, "aborted": True, "error": "已停止生成"})
            return {"ok": False, "aborted": True, "error": "已停止生成", "item": None}

        item = self._finalize_side_question_item(sess, q_text, res, ms, item_id=item_id)
        _push({
            "type": "done",
            "ok": True,
            "item": item,
            "answer": item["answer"],
            "model": item["model"],
            "latency_ms": item["latency_ms"],
        })
        return {"ok": True, "item": item}

    # ---------------------- Message Dispatch & Main Loop ----------------------

    def send_message(
        self,
        session_id: str,
        message: str,
        target_url: str = "",
        *,
        llm_profile_id: Optional[str] = None,
        llm_model: Optional[str] = None,
        mode: str = "steer",
        goal_mode: bool = False,
    ) -> Dict[str, Any]:
        sess = self.get_session(session_id)
        if sess is None:
            raise KeyError(f"会话不存在: {session_id}")

        user_text = str(message or "").strip()
        if not user_text:
            raise ValueError("发送消息不能为空")

        is_goal = bool(goal_mode)
        if user_text.lower().startswith("/goal "):
            is_goal = True
            user_text = user_text[6:].strip()
            if not user_text:
                raise ValueError("/goal 指令不能为空")

        clean_url = str(target_url or "").strip()
        prompt_for_llm = user_text
        if clean_url and clean_url not in user_text:
            prompt_for_llm = f"目标 URL: {clean_url}\n指令: {user_text}"
        if is_goal:
            prompt_for_llm = (
                f"【/goal 目标直通模式】请自主制定计划、探测站点、编写/修补适配器并调用 run_adapter_once 实测直到达成目标。\n"
                f"{prompt_for_llm}\n"
                "完成并实测验证后请在回复末尾输出 [GOAL_DONE]；若遇到必须人工处理的硬阻塞请输出 [GOAL_BLOCKED: 原因]。"
            )

        if llm_profile_id is not None or llm_model is not None:
            with sess._lock:
                if llm_profile_id is not None:
                    rp = str(llm_profile_id or "").strip()
                    if rp == "__follow_global__":
                        sess.llm_profile_id = ""
                    elif rp:
                        sess.llm_profile_id = rp
                if llm_model is not None:
                    rm = str(llm_model or "").strip()
                    if rm == "__follow_profile__":
                        sess.llm_model = ""
                    elif rm:
                        sess.llm_model = rm

        turn_stop_ev: Optional[threading.Event] = None
        steered = False
        queued = False
        queue_snap: List[Dict[str, Any]] = []
        eff_mode = str(mode or "steer").strip().lower()

        with self._lock:
            if sess.status in ("running", "waiting_approval"):
                if eff_mode == "queue":
                    q_item = {
                        "id": f"q_{secrets.token_hex(4)}",
                        "content": user_text,
                        "target_url": clean_url,
                        "llm_profile_id": str(llm_profile_id or ""),
                        "llm_model": str(llm_model or ""),
                        "goal_mode": is_goal,
                        "ts": time.strftime("%H:%M:%S"),
                    }
                    with sess._lock:
                        sess.queued_messages.append(q_item)
                        queue_snap = [dict(x) for x in sess.queued_messages]
                    queued = True
                else:
                    # Mid-run non-destructive steering for the currently active session
                    sess.enqueue_steer(prompt_for_llm)
                    steered = True
            else:
                if session_id in self._compacting_sessions:
                    raise RuntimeError("当前会话正在压缩上下文，请稍候片刻")
                for existing in self._sessions.values():
                    if existing.status in ("running", "waiting_approval"):
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
                    if is_goal:
                        sess.goal_state = {
                            "active": True,
                            "objective": user_text[:240],
                            "nudges": 0,
                            "consecutive_nudges": 0,
                            "max_steps": _GOAL_DEFAULT_MAX_STEPS,
                            "status": "running",
                            "blocked_reason": "",
                        }
                    else:
                        sess.goal_state = None
                    if clean_url:
                        sess.target_url = clean_url
                        if sess.title.startswith("AI 会话"):
                            host = (urlparse(sess.target_url).hostname or "").replace("www.", "")
                            if host:
                                sess.title = f"{host} AI 直跑"
                sess.drain_steers()

        if queued:
            sess.emit("queue_updated", queued_messages=queue_snap)
            sess.persist()
            return {"ok": True, "steered": False, "queued": True, "queued_messages": queue_snap, "session": sess.to_summary()}

        if steered:
            sess.emit("user_msg", id=f"m_u_{secrets.token_hex(4)}", content=user_text, target_url=clean_url, steered=True)
            sess.persist()
            return {"ok": True, "steered": True, "queued": False, "session": sess.to_summary()}

        with sess._lock:
            sess.messages = _repair_dangling_tool_calls(sess.messages)
            sess.messages.append({"role": "user", "content": prompt_for_llm})
            gs_snap = dict(sess.goal_state) if sess.goal_state else None
        sess.emit("user_msg", id=f"m_u_{secrets.token_hex(4)}", content=user_text, target_url=clean_url, goal=is_goal)
        if gs_snap:
            sess.emit("goal_state", goal_state=gs_snap)
        sess.emit("status", status="running", error="")
        sess.persist()

        assert turn_stop_ev is not None
        threading.Thread(target=self._run_turn, args=(sess, turn_stop_ev), daemon=True).start()
        return {"ok": True, "steered": False, "queued": False, "session": sess.to_summary()}

    def _accumulate_usage(self, sess: AgentSession, resp: Dict[str, Any]) -> Dict[str, Any]:
        u = resp.get("usage") or {}
        p_tok = max(0, int(resp.get("input_tokens") or u.get("prompt_tokens") or u.get("input_tokens") or 0))
        c_tok = max(0, int(resp.get("output_tokens") or u.get("completion_tokens") or u.get("output_tokens") or 0))
        t_tok = max(p_tok + c_tok, int(u.get("total_tokens") or 0))
        cr_tok = max(0, int(resp.get("cache_read_tokens") or u.get("cache_read_tokens") or 0))
        if p_tok > 0 and cr_tok > p_tok:
            cr_tok = p_tok
        cw_tok = max(0, int(resp.get("cache_write_tokens") or u.get("cache_write_tokens") or 0))
        tok_s = round(max(0.0, float(resp.get("tok_s") or 0.0)), 1)
        ttft_ms = round(max(0.0, float(resp.get("ttft_ms") or 0.0)), 1)
        latency_ms = round(max(0.0, float(resp.get("latency_ms") or 0.0)), 1)
        model_used = str(resp.get("model") or "")

        with sess._lock:
            sess.usage["prompt_tokens"] += p_tok
            sess.usage["completion_tokens"] += c_tok
            sess.usage["total_tokens"] += t_tok
            sess.cache_read_tokens += cr_tok
            sess.cache_write_tokens += cw_tok
            if p_tok > 0:
                sess.context_tokens = p_tok
            if tok_s > 0:
                sess.last_tok_s = tok_s
            if ttft_ms > 0:
                sess.last_ttft_ms = ttft_ms
            if model_used:
                sess.model = model_used
            session_usage = {
                **dict(sess.usage),
                "cache_read_tokens": sess.cache_read_tokens,
                "cache_write_tokens": sess.cache_write_tokens,
                "context_tokens": sess.context_tokens,
                "last_tok_s": sess.last_tok_s,
                "last_ttft_ms": sess.last_ttft_ms,
            }
            tot_in = sess.usage["prompt_tokens"]
            tot_out = sess.usage["completion_tokens"]
            tot_cr = sess.cache_read_tokens
            tot_cw = sess.cache_write_tokens
            cur_ctx = sess.context_tokens
            mod_now = sess.model

        turn_hit_pct = min(100, max(0, round(cr_tok / p_tok * 100))) if p_tok > 0 else 0
        sess_hit_pct = min(100, max(0, round(tot_cr / tot_in * 100))) if tot_in > 0 else 0

        sess.emit(
            "usage",
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            total_tokens=t_tok,
            session_usage=session_usage,
            input_tokens=tot_in,
            output_tokens=tot_out,
            cache_read_tokens=tot_cr,
            cache_write_tokens=tot_cw,
            cache_hit_pct=sess_hit_pct,
            context_tokens=cur_ctx,
            turn_input_tokens=p_tok,
            turn_output_tokens=c_tok,
            turn_cache_read_tokens=cr_tok,
            turn_cache_write_tokens=cw_tok,
            turn_cache_hit_pct=turn_hit_pct,
            tok_s=tok_s,
            ttft_ms=ttft_ms,
            latency_ms=latency_ms,
            model=mod_now,
        )
        return {
            "model": mod_now,
            "input_tokens": p_tok,
            "output_tokens": c_tok,
            "cache_read_tokens": cr_tok,
            "cache_write_tokens": cw_tok,
            "cache_hit_pct": turn_hit_pct,
            "tok_s": tok_s,
            "ttft_ms": ttft_ms,
            "latency_ms": latency_ms,
        }

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

    def _finalize_goal_state(self, sess: AgentSession, status: str, blocked_reason: str = "") -> None:
        snap: Optional[Dict[str, Any]] = None
        with sess._lock:
            if sess.goal_state and sess.goal_state.get("active"):
                sess.goal_state["active"] = False
                sess.goal_state["status"] = status
                if blocked_reason:
                    sess.goal_state["blocked_reason"] = blocked_reason[:240]
                snap = dict(sess.goal_state)
        if snap is not None:
            sess.emit("goal_state", goal_state=snap)

    def _finish_turn_status(
        self,
        sess: AgentSession,
        stop_ev: threading.Event,
        status: str,
        error: str = "",
        *,
        drain_followup: bool = False,
    ) -> bool:
        """Atomically transition status ONLY if this turn still owns the session."""
        next_q: Optional[Dict[str, Any]] = None
        q_snap: List[Dict[str, Any]] = []
        with self._lock:
            with sess._lock:
                if stop_ev.is_set() or sess._stop_event is not stop_ev:
                    return False
                sess.status = status
                sess.last_error = str(error or "")
                sess._active_tool_name = None
                sess._active_tool_started_at = None
                if drain_followup and status == "done" and sess.queued_messages:
                    next_q = sess.queued_messages.pop(0)
                    q_snap = [dict(x) for x in sess.queued_messages]
        sess.emit("status", status=status, error=error)
        if next_q is not None:
            sess.emit("queue_updated", queued_messages=q_snap)
        sess.persist()
        if next_q is not None:
            try:
                self.send_message(
                    sess.id,
                    str(next_q.get("content") or ""),
                    target_url=str(next_q.get("target_url") or ""),
                    llm_profile_id=str(next_q.get("llm_profile_id") or "") or None,
                    llm_model=str(next_q.get("llm_model") or "") or None,
                    mode="steer",
                    goal_mode=bool(next_q.get("goal_mode")),
                )
            except Exception:
                sess.close_streams()
            return True
        sess.close_streams()
        return True

    def _run_turn(self, sess: AgentSession, stop_ev: threading.Event) -> None:
        empty_nudges = 0
        max_tokens_override: Optional[int] = None
        max_tokens_escalated = False
        max_tokens_recovery_count = 0
        last_compact_step = -999

        saved_cfg = _load_saved_agent_config_if_exists()
        base_max_steps = int(saved_cfg["max_steps"]) if (saved_cfg and saved_cfg.get("max_steps")) else MAX_STEPS_PER_TURN
        with sess._lock:
            goal_init = dict(sess.goal_state) if (sess.goal_state and sess.goal_state.get("active")) else None
        max_steps = max(base_max_steps, int(goal_init.get("max_steps") or _GOAL_DEFAULT_MAX_STEPS)) if goal_init else base_max_steps
        eff_system_prompt = str(saved_cfg.get("system_prompt") or AGENT_SYSTEM_PROMPT) if saved_cfg else AGENT_SYSTEM_PROMPT
        eff_cfg = load_agent_config()
        skills_block = _render_enabled_skills_prompt(eff_cfg.get("enabled_skills") or [])
        if skills_block:
            eff_system_prompt = f"{eff_system_prompt.rstrip()}\n\n{skills_block}"
        allowed_tools = set(eff_cfg.get("enabled_tools") or [t["function"]["name"] for t in TOOL_SCHEMAS])
        eff_tools = [t for t in TOOL_SCHEMAS if t["function"]["name"] in allowed_tools]
        default_cfg_pid = str(saved_cfg.get("llm_profile_id") or "").strip() if saved_cfg else ""

        try:
            step = 0
            while step < max_steps:
                if stop_ev.is_set():
                    self._finalize_goal_state(sess, "stopped")
                    return

                self._inject_steers(sess)

                with sess._lock:
                    # Keep the message prefix strictly append-only across turns so LLM KV
                    # prompt caching hits 95%+ (85% context-window auto-compaction trims cleanly).
                    # Only if compaction repeatedly fails and raw length hits the hard failsafe
                    # cap, drop a coarse chunk in-place once instead of shifting every step.
                    if len(sess.messages) > _HISTORY_HARD_CAP_MESSAGES:
                        sess.messages = _slice_messages_safely(sess.messages, _HISTORY_HARD_CAP_TARGET)
                    history_snapshot = _repair_dangling_tool_calls(list(sess.messages))
                    prof_id = sess.llm_profile_id
                    mod_ov = sess.llm_model
                    rec_ctx = sess.context_tokens
                    cur_model = sess.model

                runtime_ov = self._resolve_runtime_override(prof_id, mod_ov, default_cfg_pid)

                # Automatic context compaction when approaching 85% of context window (cooldown >= 4 steps)
                if (step - last_compact_step) >= _AUTO_COMPACT_COOLDOWN_STEPS and len(history_snapshot) >= 6:
                    ctx_win = llm_profiles.resolve_context_window(prof_id or default_cfg_pid, mod_ov or cur_model)
                    cur_ctx = max(rec_ctx, _estimate_history_tokens(history_snapshot, eff_system_prompt))
                    if cur_ctx > int(ctx_win * _AUTO_COMPACT_CONTEXT_RATIO):
                        try:
                            cmp_res = self._execute_compaction_summary(
                                history=history_snapshot,
                                sys_prompt=eff_system_prompt,
                                runtime_ov=runtime_ov,
                                keep_recent_tokens=_DEFAULT_KEEP_RECENT_TOKENS,
                                force=True,
                                recorded_context_tokens=cur_ctx,
                                stop_ev=stop_ev,
                                session_id=sess.id,
                            )
                            if stop_ev.is_set() or cmp_res.get("aborted"):
                                return
                            last_compact_step = step
                            if cmp_res.get("ok") and cmp_res.get("compacted"):
                                comp_item = {
                                    "id": f"cmp_{secrets.token_hex(4)}",
                                    "role": "compaction",
                                    "content": cmp_res["summary"],
                                    "tokens_before": cmp_res["tokens_before"],
                                    "tokens_after": cmp_res["tokens_after"],
                                    "dropped_messages": cmp_res["dropped_messages"],
                                    "kept_messages": cmp_res["kept_messages"],
                                    "touched_sites": cmp_res["touched_sites"],
                                    "touched_files": cmp_res["touched_files"],
                                    "auto": True,
                                    "custom_instructions": "",
                                    "model": cmp_res.get("model") or cur_model,
                                    "ts": time.strftime("%H:%M:%S"),
                                }
                                with sess._lock:
                                    sess.messages = cmp_res["new_history"]
                                    sess.context_tokens = int(cmp_res["tokens_after"])
                                    sess.usage["prompt_tokens"] += int(cmp_res.get("input_tokens") or 0)
                                    sess.usage["completion_tokens"] += int(cmp_res.get("output_tokens") or 0)
                                    sess.usage["total_tokens"] = sess.usage["prompt_tokens"] + sess.usage["completion_tokens"]
                                    history_snapshot = list(sess.messages)
                                    in_tot = sess.usage["prompt_tokens"]
                                    out_tot = sess.usage["completion_tokens"]
                                    cr_tot = sess.cache_read_tokens
                                    cw_tot = sess.cache_write_tokens
                                sess.emit("compaction", item=comp_item)
                                sess.emit(
                                    "usage",
                                    input_tokens=in_tot,
                                    output_tokens=out_tot,
                                    cache_read_tokens=cr_tot,
                                    cache_write_tokens=cw_tot,
                                    context_tokens=int(cmp_res["tokens_after"]),
                                    model=cur_model,
                                )
                                sess.persist()
                        except Exception:
                            last_compact_step = step

                resp = self._call_llm_for_turn(
                    sess,
                    messages=history_snapshot,
                    tools=eff_tools,
                    system_prompt=eff_system_prompt,
                    runtime_ov=runtime_ov,
                    stop_ev=stop_ev,
                    max_tokens_override=max_tokens_override,
                )

                if stop_ev.is_set() or resp.get("aborted"):
                    self._finalize_goal_state(sess, "stopped")
                    return

                turn_metrics = self._accumulate_usage(sess, resp)

                content = str(resp.get("content") or "").strip()
                thinking = str(resp.get("thinking") or "").strip()
                tool_calls = resp.get("tool_calls") or []
                raw_fr = str(resp.get("stop_reason") or resp.get("finish_reason") or "").strip().lower()
                stop_reason = "max_tokens" if raw_fr in ("length", "max_tokens", "incomplete") else raw_fr
                truncated_tc = bool(resp.get("truncated_tool_calls"))
                eff_cap = int(resp.get("effective_max_tokens") or 0)

                # Resilience: max_tokens truncation auto-recovery (corrupted tool JSON or truncated text)
                if (truncated_tc or (stop_reason == "max_tokens" and (not tool_calls or truncated_tc))) and (
                    (not max_tokens_escalated) or (max_tokens_recovery_count < _MAX_OUTPUT_TOKENS_RECOVERY_LIMIT)
                ):
                    if content:
                        with sess._lock:
                            sess.messages.append({"role": "assistant", "content": content})
                        sess.emit("assistant_msg", content=content, thinking=thinking, metrics=turn_metrics, step=step + 1)
                    if not max_tokens_escalated:
                        max_tokens_escalated = True
                    else:
                        max_tokens_recovery_count += 1
                    max_tokens_override = _compute_next_max_tokens_override(eff_cap, max_tokens_override)
                    nudge_txt = (
                        f"上一轮输出因触发 max_tokens 上限被截断，系统已将输出上限自动抬升至 {max_tokens_override} tokens。"
                        "请勿道歉或复述，精简思考并直接继续输出或重新发出完整的工具调用。"
                    )
                    with sess._lock:
                        sess.messages.append({"role": "user", "content": nudge_txt})
                    sess.persist()
                    continue

                max_tokens_recovery_count = 0

                # Resilience #1: Thinking-Only Empty-Turn Nudge (max 2 nudges)
                if not content and not tool_calls:
                    if empty_nudges < MAX_EMPTY_NUDGES and (step + 1) < max_steps:
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
                    sess.emit("assistant_msg", content=fallback_msg, thinking=thinking, metrics=turn_metrics, step=step + 1)
                    self._finalize_goal_state(sess, "limit_reached")
                    self._finish_turn_status(sess, stop_ev, "done", drain_followup=True)
                    return

                empty_nudges = 0
                step += 1
                with sess._lock:
                    sess.steps_count += 1

                assistant_record: Dict[str, Any] = {"role": "assistant", "content": content or None}
                if tool_calls:
                    assistant_record["tool_calls"] = tool_calls
                with sess._lock:
                    sess.messages.append(assistant_record)

                step_tool_thinking = ""
                if content:
                    msg_extra: Dict[str, Any] = {"id": f"m_a_{secrets.token_hex(4)}", "step": step, "metrics": turn_metrics}
                    if thinking:
                        msg_extra["thinking"] = thinking
                    sess.emit("assistant_msg", content=content, **msg_extra)
                elif thinking and tool_calls:
                    step_tool_thinking = thinking

                if not tool_calls:
                    # Check atomically whether a steer arrived while LLM was replying
                    with self._lock:
                        if stop_ev.is_set() or sess._stop_event is not stop_ev:
                            return
                        if sess.has_pending_steer() and step < max_steps:
                            keep_going = True
                        else:
                            keep_going = False
                    if keep_going:
                        self._inject_steers(sess)
                        sess.persist()
                        continue

                    # /goal completion gate
                    with sess._lock:
                        gs_chk = dict(sess.goal_state) if (sess.goal_state and sess.goal_state.get("active")) else None
                        plan_snap = list(sess.plan)
                    if gs_chk is not None:
                        m_blk = _GOAL_BLOCKED_RE.search(content)
                        if m_blk:
                            self._finalize_goal_state(sess, "blocked", blocked_reason=(m_blk.group(1) or "硬阻塞").strip())
                        else:
                            unfinished = [st.get("title", "") for st in plan_snap if st.get("status") != "done"]
                            gaps: List[str] = []
                            if unfinished:
                                gaps.append(f"计划清单尚有 {len(unfinished)} 项未完成（{'、'.join(unfinished[:3])}）")
                            if not _GOAL_DONE_RE.search(content):
                                gaps.append("回复末尾未附带 [GOAL_DONE] 或 [GOAL_BLOCKED: 原因]")
                            if not gaps:
                                self._finalize_goal_state(sess, "achieved")
                            elif (
                                int(gs_chk.get("consecutive_nudges") or 0) < _GOAL_MAX_CONSECUTIVE_NUDGES
                                and int(gs_chk.get("nudges") or 0) < _GOAL_MAX_TOTAL_NUDGES
                                and step < max_steps
                            ):
                                with sess._lock:
                                    assert sess.goal_state is not None
                                    sess.goal_state["nudges"] = int(sess.goal_state.get("nudges") or 0) + 1
                                    sess.goal_state["consecutive_nudges"] = int(sess.goal_state.get("consecutive_nudges") or 0) + 1
                                    gs_upd = dict(sess.goal_state)
                                sess.emit("goal_state", goal_state=gs_upd)
                                gap_lines = "\n".join(f"- {g}" for g in gaps)
                                with sess._lock:
                                    sess.messages.append({
                                        "role": "user",
                                        "content": (
                                            f"【/goal 守门员督战（第 {gs_upd['nudges']} 次）】目标尚未收口：\n{gap_lines}\n"
                                            "请继续调用工具完成剩余步骤并实测验证；完成后在末尾输出 [GOAL_DONE]，遇到硬阻塞输出 [GOAL_BLOCKED: 原因]。"
                                        ),
                                    })
                                sess.persist()
                                continue
                            else:
                                self._finalize_goal_state(sess, "limit_reached")

                    self._finish_turn_status(sess, stop_ev, "done", drain_followup=True)
                    return

                # Reset /goal consecutive nudge counter when real tools run
                with sess._lock:
                    if sess.goal_state and sess.goal_state.get("active") and sess.goal_state.get("consecutive_nudges"):
                        sess.goal_state["consecutive_nudges"] = 0

                # Execute requested tool calls strictly sequentially (single-thread)
                for tc_idx, tc in enumerate(tool_calls):
                    if stop_ev.is_set():
                        self._finalize_goal_state(sess, "stopped")
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

                    # Guard Rule Evaluation
                    guard_policy, matched_rule = ("allow", None) if parse_err else self._evaluate_guard_detail(fn_name, parsed_args)
                    rule_name = str(matched_rule.get("name") or matched_rule.get("id") or "") if matched_rule else ""

                    sess.emit("tool_start", call_id=call_id, name=fn_name, args=parsed_args)
                    t_tool = time.perf_counter()
                    sess.set_active_tool(fn_name, t_tool, stop_ev=stop_ev)
                    try:
                        if parse_err:
                            tool_res: Dict[str, Any] = {"ok": False, "error": parse_err}
                        elif guard_policy == "deny":
                            tool_res = {
                                "ok": False,
                                "blocked_by_guard": True,
                                "rule": rule_name,
                                "error": f"被安全拦截规则「{rule_name}」拒绝执行",
                            }
                        else:
                            approved = True
                            if guard_policy == "ask":
                                app_id = f"app_{secrets.token_hex(4)}"
                                wait_ev = threading.Event()
                                app_rec = {
                                    "id": app_id,
                                    "approval_id": app_id,
                                    "session_id": sess.id,
                                    "tool": fn_name,
                                    "tool_name": fn_name,
                                    "rule_name": rule_name,
                                    "args": parsed_args,
                                    "status": "pending",
                                    "created_at": time.strftime("%H:%M:%S"),
                                }
                                with self._lock:
                                    self._approvals.append(app_rec)
                                    self._approval_events[app_id] = wait_ev
                                with sess._lock:
                                    sess.status = "waiting_approval"
                                sess.emit(
                                    "approval_required",
                                    approval_id=app_id,
                                    tool_name=fn_name,
                                    rule_name=rule_name,
                                    tool_args=parsed_args,
                                    status="pending",
                                )
                                sess.emit("status", status="waiting_approval", error="")
                                sess.persist()
                                deadline = time.monotonic() + 300.0
                                while not wait_ev.is_set() and not stop_ev.is_set() and time.monotonic() < deadline:
                                    wait_ev.wait(timeout=0.25)
                                with self._lock:
                                    self._approval_events.pop(app_id, None)
                                    dec_status = str(app_rec.get("status") or "rejected")
                                if stop_ev.is_set():
                                    return
                                approved = dec_status == "approved"
                                if not approved:
                                    tool_res = {
                                        "ok": False,
                                        "rejected_by_user": True,
                                        "rule": rule_name,
                                        "error": f"人工审批未通过或超时（规则：{rule_name}）",
                                    }
                            if approved:
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
                    tool_end_extra: Dict[str, Any] = {}
                    if step_tool_thinking and tc_idx == 0:
                        tool_end_extra["thinking"] = step_tool_thinking
                        step_tool_thinking = ""
                    if isinstance(tool_res, dict) and tool_res.get("checkpoint_id"):
                        tool_end_extra["checkpoint_id"] = str(tool_res["checkpoint_id"])
                    sess.emit(
                        "tool_end",
                        call_id=call_id,
                        name=fn_name,
                        ok=bool(tool_res.get("ok", True)),
                        duration_ms=dur_ms,
                        result=ui_res,
                        **tool_end_extra,
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
                wrapup_history = _repair_dangling_tool_calls(list(sess.messages))
                prof_id = sess.llm_profile_id
                mod_ov = sess.llm_model
            wrapup_prompt = (
                f"已达到单轮最大步数上限（{max_steps} 步），当前已关闭工具调用。"
                "请立即用简洁中文做收尾总结：1) 本轮已完成的探测/改动进展；"
                "2) 已创建或修改的适配器文件与试跑产出账号情况；3) 剩余未完成事项与下一步建议。"
            )
            wrap_text = ""
            wrap_metrics: Dict[str, Any] = {}
            try:
                wrap_resp = self._call_llm_for_turn(
                    sess,
                    messages=wrapup_history + [{"role": "user", "content": wrapup_prompt}],
                    tools=[],
                    system_prompt=eff_system_prompt,
                    runtime_ov=self._resolve_runtime_override(prof_id, mod_ov, default_cfg_pid),
                    stop_ev=stop_ev,
                )
                if stop_ev.is_set() or wrap_resp.get("aborted"):
                    return
                wrap_metrics = self._accumulate_usage(sess, wrap_resp)
                wrap_text = str(wrap_resp.get("content") or "").strip()
            except Exception:
                wrap_text = ""

            if stop_ev.is_set():
                return

            if not wrap_text:
                wrap_text = f"已达到单轮最大推理/工具步数上限（{max_steps} 步），已安全暂停。如需继续请发送「继续」。"

            with sess._lock:
                sess.messages.append({"role": "assistant", "content": wrap_text})
            sess.emit("assistant_msg", content=wrap_text, step=max_steps, wrap_up=True, metrics=wrap_metrics)
            self._finalize_goal_state(sess, "limit_reached")
            self._finish_turn_status(sess, stop_ev, "done", drain_followup=True)
        except Exception as exc:
            if stop_ev.is_set():
                return
            self._finalize_goal_state(sess, "stopped", blocked_reason=str(exc))
            self._finish_turn_status(sess, stop_ev, "error", error=str(exc))


_AGENT_CFG_LOCK = threading.Lock()


def _parse_skill_meta(fp: Path) -> Tuple[str, str]:
    """Extract skill description from YAML frontmatter or first non-heading line."""
    text = fp.read_text(encoding="utf-8", errors="replace")
    desc = ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for ln in parts[1].splitlines():
                if ln.strip().lower().startswith("description:"):
                    desc = ln.split(":", 1)[1].strip().strip("\"'")
                    break
    if not desc:
        for ln in text.splitlines():
            s = ln.strip().lstrip("#").strip()
            if s and not s.startswith("---"):
                desc = s[:140]
                break
    return fp.stem, desc or "内置工程技能规范"


def list_skills(enabled_names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """List pure-Community Markdown skill recipes under skills/*.md."""
    if not AGENT_SKILLS_DIR.is_dir():
        return []
    enabled_set = set(enabled_names) if enabled_names is not None else None
    out: List[Dict[str, Any]] = []
    for fp in sorted(AGENT_SKILLS_DIR.glob("*.md")):
        if not _SAFE_SKILL_NAME_RE.fullmatch(fp.stem):
            continue
        name, desc = _parse_skill_meta(fp)
        size_kb = round(fp.stat().st_size / 1024.0, 1)
        out.append({
            "name": name,
            "size_kb": max(0.1, size_kb),
            "description": desc,
            "path": f"skills/{fp.name}",
            "enabled": True if enabled_set is None else (name in enabled_set),
        })
    return out


def read_skill(skill_name: str) -> Dict[str, Any]:
    raw = str(skill_name or "").strip().removesuffix(".md")
    if not _SAFE_SKILL_NAME_RE.fullmatch(raw):
        raise ValueError(f"非法技能名称: {skill_name!r}")
    root = AGENT_SKILLS_DIR.resolve()
    fp = (root / f"{raw}.md").resolve()
    if fp.parent != root or not fp.is_file():
        raise KeyError(f"未找到技能文件: {raw}")
    content = fp.read_text(encoding="utf-8", errors="replace")
    _, desc = _parse_skill_meta(fp)
    return {
        "ok": True,
        "name": raw,
        "path": f"skills/{fp.name}",
        "description": desc,
        "size_kb": max(0.1, round(len(content.encode("utf-8")) / 1024.0, 1)),
        "content": content,
    }


def write_skill(skill_name: str, content: str) -> Dict[str, Any]:
    raw = str(skill_name or "").strip().removesuffix(".md")
    if not _SAFE_SKILL_NAME_RE.fullmatch(raw):
        raise ValueError(f"非法技能名称: {skill_name!r}")
    text = str(content or "")
    if not text.strip() or len(text) > 200_000:
        raise ValueError("技能内容不能为空且不能超过 200KB")
    AGENT_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    root = AGENT_SKILLS_DIR.resolve()
    fp = (root / f"{raw}.md").resolve()
    if fp.parent != root:
        raise ValueError("非法技能路径")
    tmp = fp.with_suffix(".md.tmp")
    tmp.write_text(text.rstrip() + "\n", encoding="utf-8")
    os.replace(tmp, fp)
    _, desc = _parse_skill_meta(fp)
    return {
        "ok": True,
        "name": raw,
        "path": f"skills/{fp.name}",
        "description": desc,
        "size_kb": max(0.1, round(fp.stat().st_size / 1024.0, 1)),
    }


def _render_enabled_skills_prompt(enabled_skills: List[str]) -> str:
    if not enabled_skills or not AGENT_SKILLS_DIR.is_dir():
        return ""
    blocks: List[str] = []
    budget = 6000
    for name in enabled_skills[:12]:
        if not _SAFE_SKILL_NAME_RE.fullmatch(str(name or "")):
            continue
        fp = AGENT_SKILLS_DIR / f"{name}.md"
        if not fp.is_file():
            continue
        body = fp.read_text(encoding="utf-8", errors="replace").strip()
        if body.startswith("---"):
            parts = body.split("---", 2)
            if len(parts) >= 3:
                body = parts[2].strip()
        chunk = f"### Skill: {name}\n{body[:1400]}"
        if len(chunk) > budget:
            break
        blocks.append(chunk)
        budget -= len(chunk)
    if not blocks:
        return ""
    return "## 已启用工程技能规范\n\n" + "\n\n".join(blocks)


def _agent_config_file() -> Path:
    return RESULTS_DIR / "agent_config.json"


def _default_agent_config() -> Dict[str, Any]:
    all_names = [t["function"]["name"] for t in TOOL_SCHEMAS]
    all_skills = [s["name"] for s in list_skills()]
    return {
        "name": "RegisterHub 开源单线程 Agent",
        "description": "纯 Python 标准库驱动的自主注册工程师 Agent（单线程安全模式，支持流式输出、真压缩与全套诊断工具）",
        "llm_profile_id": "",
        "max_steps": MAX_STEPS_PER_TURN,
        "system_prompt": AGENT_SYSTEM_PROMPT,
        "enabled_tools": all_names,
        "known_tools": all_names,
        "enabled_skills": all_skills,
        "known_skills": all_skills,
    }


def _load_saved_agent_config_if_exists() -> Optional[Dict[str, Any]]:
    fp = _agent_config_file()
    if not fp.is_file():
        return None
    with _AGENT_CFG_LOCK:
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception:
            return None
    return None


def load_agent_config() -> Dict[str, Any]:
    base = _default_agent_config()
    saved = _load_saved_agent_config_if_exists()
    if not saved:
        return base
    all_tool_names = [t["function"]["name"] for t in TOOL_SCHEMAS]
    valid_tool_names = set(all_tool_names)
    all_skill_names = [s["name"] for s in list_skills()]
    valid_skill_names = set(all_skill_names)
    out = dict(base)
    if isinstance(saved.get("name"), str) and saved["name"].strip():
        out["name"] = saved["name"].strip()[:80]
    if isinstance(saved.get("description"), str):
        out["description"] = saved["description"].strip()[:300]
    if isinstance(saved.get("llm_profile_id"), str):
        out["llm_profile_id"] = saved["llm_profile_id"].strip()
    try:
        ms = int(saved.get("max_steps") or MAX_STEPS_PER_TURN)
        out["max_steps"] = max(1, min(60, ms))
    except (TypeError, ValueError):
        pass
    if isinstance(saved.get("system_prompt"), str) and saved["system_prompt"].strip():
        out["system_prompt"] = saved["system_prompt"]
    if isinstance(saved.get("enabled_tools"), list):
        enabled = [str(x) for x in saved["enabled_tools"] if str(x) in valid_tool_names]
        # Auto-enable newly introduced tools on upgrade while respecting manual disables of previously known tools
        known = set(str(x) for x in (saved.get("known_tools") or []))
        if not known:
            known = set(enabled)
        for tname in all_tool_names:
            if tname not in known and tname not in enabled:
                enabled.append(tname)
        out["enabled_tools"] = enabled
    if isinstance(saved.get("enabled_skills"), list):
        en_sk = [str(x) for x in saved["enabled_skills"] if str(x) in valid_skill_names]
        known_sk = set(str(x) for x in (saved.get("known_skills") or []))
        for sname in all_skill_names:
            if sname not in known_sk and sname not in en_sk:
                en_sk.append(sname)
        out["enabled_skills"] = en_sk
    out["known_tools"] = all_tool_names
    out["known_skills"] = all_skill_names
    return out


def save_agent_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(updates, dict):
        raise ValueError("Agent 配置必须为 JSON 对象")
    all_tool_names = [t["function"]["name"] for t in TOOL_SCHEMAS]
    valid_tool_names = set(all_tool_names)
    all_skill_names = [s["name"] for s in list_skills()]
    valid_skill_names = set(all_skill_names)
    with _AGENT_CFG_LOCK:
        cur = load_agent_config()
        if "name" in updates and isinstance(updates.get("name"), str) and updates["name"].strip():
            cur["name"] = updates["name"].strip()[:80]
        if "description" in updates and isinstance(updates.get("description"), str):
            cur["description"] = updates["description"].strip()[:300]
        if "llm_profile_id" in updates:
            raw_pid = str(updates.get("llm_profile_id") or "").strip()
            cur["llm_profile_id"] = "" if raw_pid == "__follow_global__" else raw_pid
        if "max_steps" in updates:
            try:
                cur["max_steps"] = max(1, min(60, int(updates["max_steps"])))
            except (TypeError, ValueError):
                pass
        if "system_prompt" in updates and isinstance(updates.get("system_prompt"), str):
            cur["system_prompt"] = updates["system_prompt"] or AGENT_SYSTEM_PROMPT
        if "enabled_tools" in updates and isinstance(updates.get("enabled_tools"), list):
            cur["enabled_tools"] = [str(x) for x in updates["enabled_tools"] if str(x) in valid_tool_names]
        if "enabled_skills" in updates and isinstance(updates.get("enabled_skills"), list):
            cur["enabled_skills"] = [str(x) for x in updates["enabled_skills"] if str(x) in valid_skill_names]
        cur["known_tools"] = all_tool_names
        cur["known_skills"] = all_skill_names
        fp = _agent_config_file()
        fp.parent.mkdir(parents=True, exist_ok=True)
        tmp = fp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, fp)
        return cur


agent_manager = SingleAgentManager()


def rename_session(session_id: str, title: str) -> Optional[Dict[str, Any]]:
    """Rename an agent session title (<= 80 chars, persisted atomically)."""
    return agent_manager.rename_session(session_id, title)


def export_session_markdown(session_id: str) -> Optional[Dict[str, Any]]:
    """Export an agent session into a Markdown report (`{ok, filename, markdown}`)."""
    return agent_manager.export_session_markdown(session_id)
