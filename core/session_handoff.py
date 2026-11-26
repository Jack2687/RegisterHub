"""三仓共享 Agent 会话脱敏接力单（*.handoff.md）与分阶段自动命名引擎（纯标准库，零外部依赖）。

单一真相源（Mother / PRO / Community 三仓 SHA-256 严格一致）：
  1. 统一隐私脱敏管线：自动抹除 API Key / JWT / Bearer / Cookie / 密码 / 代理账密 / 个人邮箱 / 本机绝对路径；
  2. 分阶段自动会话命名：仅在「锁定目标 ➔ 编写调测 ➔ 跑通/卡点收口」三个里程碑克制更新，绝不覆盖用户手动重命名；
  3. 脱敏自包含接力单生成与解析：支持已跑通复刻与未跑通接力续跑，跨母港 / PRO / 开源三版通用。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

HANDOFF_SCHEMA_VERSION = "v1"

DEFAULT_SESSION_TITLES = frozenset({
    "",
    "新 AI 直跑会话",
    "新会话",
    "New Session",
    "AI 注册工程师",
    "自主注册 Agent 工作台",
})

_AUTO_STAGE_SUFFIXES = (
    "探测中",
    "调测中",
    "已跑通",
    "待续跑",
    "已完成",
)

_STRIP_SUBDOMAIN_PREFIXES = (
    "www.",
    "app.",
    "auth.",
    "login.",
    "signin.",
    "signup.",
    "register.",
    "accounts.",
    "account.",
    "console.",
    "portal.",
    "dash.",
    "dashboard.",
    "cloud.",
    "my.",
    "id.",
    "sso.",
    "api.",
)

_GENERIC_TLDS = (".com", ".io", ".ai", ".net", ".org", ".dev", ".co", ".cn", ".top", ".xyz", ".app", ".cc", ".me", ".gg")

_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)）}，。；、]+", re.I)

# 脱敏正则集合
_RE_PROXY_CREDS = re.compile(r"((?:https?|socks5h?)://)([^/\s:@\"']{1,120}):([^/\s@\"']{1,200})@", re.I)
_RE_BEARER = re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}\b", re.I)
_RE_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}\b")
_RE_KNOWN_KEY = re.compile(
    r"\b(?:sk-ant-[A-Za-z0-9_-]{12,}|sk-or-[A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9_-]{14,}|"
    r"gsk_[A-Za-z0-9_-]{14,}|ghp_[A-Za-z0-9]{16,}|gho_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"AIza[0-9A-Za-z_-]{20,}|xox[baprs]-[0-9A-Za-z-]{10,}|hf_[A-Za-z0-9]{16,}|"
    r"nvapi-[A-Za-z0-9_-]{16,}|rk_[A-Za-z0-9_-]{12,}|csk-[A-Za-z0-9_-]{14,}|"
    r"AKIA[0-9A-Z]{16})\b"
)
_RE_ACCOUNT_DUMP = re.compile(
    r"(?m)^[^\s#]{3,120}----[^\s]{3,120}(?:----[^\s]*){1,6}$"
)
_RE_EMAIL = re.compile(
    r"\b[A-Za-z0-9._%+-]{2,64}@(?!example\.(?:com|org|net)\b|test\.local\b|redacted\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,16}\b"
)
_RE_WIN_PATH = re.compile(
    r"(?i)[A-Z]:\\(?:Users|VsCodeProjects|Softwares|Documents|Desktop|Downloads|Projects|Work)(?:\\[^\s\"'`<>)\],;：。]+)*"
)
_RE_UNIX_HOME_PATH = re.compile(r"/(?:Users|home)/[^/\s\"'`<>)\],;]+(?:/[^\s\"'`<>)\],;]+)*")
_RE_SECRET_KV_PROSE = re.compile(
    r"(?i)(['\"]?(?:api_?key|secret_?key|access_?token|refresh_?token|id_?token|auth_?token|"
    r"mail_hub_token|client_secret|x-api-key|authorization|cookie|set-cookie|password|passwd)['\"]?"
    r"\s*[:=]\s*)(['\"]?)([^\s,\"'}{;\n]{8,240})\2"
)
_RE_SECRET_LITERAL_CODE = re.compile(
    r"(?i)((?:api_?key|secret_?key|access_?token|refresh_?token|mail_hub_token|client_secret|"
    r"x-api-key|authorization|cookie)\s*[:=]\s*)(['\"])([^'\"\n]{12,240})\2"
)

_SAFE_VALUE_PREFIXES = (
    "<redacted",
    "os.getenv",
    "os.environ",
    "self.",
    "cfg.",
    "config.",
    "profile.",
    "resp.",
    "data.",
    "...",
    "***",
    "http://",
    "https://",
    "bearer <",
    "${",
    "{{",
)


def _count_sub(pattern: re.Pattern[str], repl: Any, text: str) -> Tuple[str, int]:
    return pattern.subn(repl, text)


def _shorten_local_path(raw_path: str) -> str:
    norm = raw_path.replace("\\", "/")
    lower = norm.lower()
    for marker in ("/sites/", "/core/", "/web/", "/results/", "/scratch/", "/data/", "/docs/"):
        idx = lower.find(marker)
        if idx >= 0:
            return "<WORKSPACE>" + norm[idx:]
    m_reg = re.search(r"/([A-Za-z0-9_-]+-register(?:/[^\s\"'`<>)\],;]+)?)", norm, re.I)
    if m_reg:
        return "<WORKSPACE>/" + m_reg.group(1)
    if lower.startswith(("/users/", "/home/")) or ":/users/" in lower:
        return "<USER_HOME>"
    return "<LOCAL_PATH>"


def redact_handoff_text(text: str) -> Tuple[str, Dict[str, int]]:
    """对分享接力单正文执行严格脱敏，返回 (脱敏文本, 分项统计)。"""
    out = str(text or "")
    stats = {"secrets": 0, "emails": 0, "proxies": 0, "paths": 0, "accounts": 0, "total": 0}
    if not out:
        return out, stats

    out, n = _count_sub(_RE_ACCOUNT_DUMP, "<REDACTED_ACCOUNT_LINE>", out)
    stats["accounts"] += n

    out, n = _count_sub(_RE_PROXY_CREDS, r"\1<proxy-user>:<proxy-pass>@", out)
    stats["proxies"] += n

    out, n = _count_sub(_RE_BEARER, r"\1<REDACTED_TOKEN>", out)
    stats["secrets"] += n

    out, n = _count_sub(_RE_JWT, "<REDACTED_JWT>", out)
    stats["secrets"] += n

    out, n = _count_sub(_RE_KNOWN_KEY, "<REDACTED_KEY>", out)
    stats["secrets"] += n

    def _kv_repl(m: re.Match[str]) -> str:
        prefix, quote, val = m.group(1), m.group(2), m.group(3)
        vl = val.strip().lower()
        if any(vl.startswith(p) for p in _SAFE_VALUE_PREFIXES) or vl in ("none", "null", "true", "false", "empty"):
            return m.group(0)
        # 跳过形如 foo_bar / module.attr 等纯代码标识符（无引号时）
        if not quote and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\(\)]*", val):
            return m.group(0)
        stats["secrets"] += 1
        return f"{prefix}{quote}<REDACTED>{quote}"

    out = _RE_SECRET_KV_PROSE.sub(_kv_repl, out)

    out, n = _count_sub(_RE_EMAIL, "<redacted@email>", out)
    stats["emails"] += n

    def _win_repl(m: re.Match[str]) -> str:
        stats["paths"] += 1
        return _shorten_local_path(m.group(0))

    out = _RE_WIN_PATH.sub(_win_repl, out)

    def _unix_repl(m: re.Match[str]) -> str:
        stats["paths"] += 1
        return _shorten_local_path(m.group(0))

    out = _RE_UNIX_HOME_PATH.sub(_unix_repl, out)

    stats["total"] = stats["secrets"] + stats["emails"] + stats["proxies"] + stats["paths"] + stats["accounts"]
    return out, stats


def redact_source_code(code: str) -> Tuple[str, Dict[str, int]]:
    """对附带的 Python 适配器源码脱敏：只抹除硬编码真实密钥/JWT/账密/路径，绝不破坏合法语法与变量名。"""
    out = str(code or "")
    stats = {"secrets": 0, "emails": 0, "proxies": 0, "paths": 0, "accounts": 0, "total": 0}
    if not out:
        return out, stats

    out, n = _count_sub(_RE_PROXY_CREDS, r"\1<proxy-user>:<proxy-pass>@", out)
    stats["proxies"] += n

    out, n = _count_sub(_RE_BEARER, r"\1<REDACTED_TOKEN>", out)
    stats["secrets"] += n

    out, n = _count_sub(_RE_JWT, "<REDACTED_JWT>", out)
    stats["secrets"] += n

    out, n = _count_sub(_RE_KNOWN_KEY, "<REDACTED_KEY>", out)
    stats["secrets"] += n

    def _code_lit_repl(m: re.Match[str]) -> str:
        prefix, quote, val = m.group(1), m.group(2), m.group(3)
        vl = val.strip().lower()
        if any(vl.startswith(p) for p in _SAFE_VALUE_PREFIXES) or "{" in val or "}" in val:
            return m.group(0)
        # 仅当字面量像真实凭据（含数字/符号且非普通中文/英文提示句）时脱敏
        if " " in val.strip() and not val.strip().lower().startswith("bearer "):
            return m.group(0)
        stats["secrets"] += 1
        return f"{prefix}{quote}<REDACTED>{quote}"

    out = _RE_SECRET_LITERAL_CODE.sub(_code_lit_repl, out)

    out, n = _count_sub(_RE_EMAIL, "<redacted@email>", out)
    stats["emails"] += n

    def _win_repl(m: re.Match[str]) -> str:
        stats["paths"] += 1
        return _shorten_local_path(m.group(0))

    out = _RE_WIN_PATH.sub(_win_repl, out)

    def _unix_repl(m: re.Match[str]) -> str:
        stats["paths"] += 1
        return _shorten_local_path(m.group(0))

    out = _RE_UNIX_HOME_PATH.sub(_unix_repl, out)

    stats["total"] = stats["secrets"] + stats["emails"] + stats["proxies"] + stats["paths"] + stats["accounts"]
    return out, stats


def _merge_redaction_stats(a: Dict[str, int], b: Dict[str, int]) -> Dict[str, int]:
    out = {k: int(a.get(k, 0)) + int(b.get(k, 0)) for k in ("secrets", "emails", "proxies", "paths", "accounts")}
    out["total"] = sum(out.values())
    return out


# ---------------------------------------------------------------------------
# 会话进度分析与分阶段自动命名引擎
# ---------------------------------------------------------------------------

def is_default_title(title: str) -> bool:
    return (title or "").strip() in DEFAULT_SESSION_TITLES


_AUTO_TITLE_PREFIXES = (
    "直跑 · ",
    "修站 · ",
    "接力 · ",
    "复刻 · ",
    "附件分析 · ",
    "AI 注册会话 ",
    "AI 会话 ",
    "站点对接:",
)


def infer_initial_title_source(title: str) -> str:
    """判定新建会话时的初始标题来源：空/默认 -> default；系统前缀 -> auto；显式自定义名 -> user。"""
    t = (title or "").strip()
    if is_default_title(t):
        return "default"
    if t.startswith(_AUTO_TITLE_PREFIXES):
        return "auto"
    return "user"


def is_auto_managed_title(title: str, title_source: str = "") -> bool:
    """判断当前会话标题是否允许系统按阶段自动更新（用户手动改名后永久返回 False）。"""
    src = (title_source or "").strip().lower()
    if src == "user":
        return False
    if src in ("default", "auto"):
        return True
    t = (title or "").strip()
    if is_default_title(t):
        return True
    if t.startswith(("直跑 · ", "修站 · ")):
        return True
    if " · " in t:
        tail = t.rsplit(" · ", 1)[-1].strip()
        if tail in _AUTO_STAGE_SUFFIXES or tail.startswith("卡在"):
            return True
    return False


def _brand_from_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urlparse(raw).hostname or "").strip().lower()
    except Exception:
        return ""
    if not host or host in ("localhost", "127.0.0.1", "0.0.0.0"):
        return ""
    for pfx in _STRIP_SUBDOMAIN_PREFIXES:
        if host.startswith(pfx) and len(host) > len(pfx) + 3:
            host = host[len(pfx):]
            break
    for tld in _GENERIC_TLDS:
        if host.endswith(tld) and len(host) > len(tld) + 1:
            host = host[:-len(tld)]
            break
    # 去掉多余二级前缀（如 sub.foo -> foo，保留短主干）
    if "." in host:
        parts = [p for p in host.split(".") if p]
        if len(parts) >= 2:
            host = parts[-1] if len(parts[-1]) >= 3 else ".".join(parts[-2:])
    return host[:16]


def _clean_site_slug(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    s = s.replace("\\", "/").rsplit("/", 1)[-1]
    if s.endswith("_register.py"):
        s = s[:-len("_register.py")]
    elif s.endswith(".py"):
        s = s[:-3]
    if s.endswith("-register"):
        s = s[:-len("-register")]
    s = re.sub(r"[^A-Za-z0-9_.-]+", "", s).strip("._-")
    if s.lower() in ("_template", "template", "example", "demo", "main", "base"):
        return ""
    return s[:32]


def _extract_tool_events(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """跨三仓兼容提取统一的工具调用与结果列表（Mother / PRO / Community 均可解析）。"""
    out: List[Dict[str, Any]] = []
    call_args_by_id: Dict[str, Tuple[str, Dict[str, Any]]] = {}

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        if role == "assistant" and isinstance(m.get("tool_calls"), list):
            for tc in m["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                cid = str(tc.get("id") or "")
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                name = str(tc.get("name") or fn.get("name") or "").strip()
                args = tc.get("args") if tc.get("args") is not None else fn.get("arguments")
                if isinstance(args, str):
                    try:
                        parsed = json.loads(args)
                        args = parsed if isinstance(parsed, dict) else {}
                    except Exception:
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                if cid and name:
                    call_args_by_id[cid] = (name, args)
                elif name:
                    out.append({"name": name, "args": args, "ok": None, "result_obj": None, "result_text": ""})
        elif role == "tool":
            cid = str(m.get("tool_call_id") or m.get("call_id") or m.get("id") or "")
            mapped_name, mapped_args = call_args_by_id.get(cid, ("", {}))
            name = str(m.get("tool_name") or m.get("name") or m.get("tool") or mapped_name or "").strip()
            args = m.get("tool_args") if isinstance(m.get("tool_args"), dict) else (
                m.get("args") if isinstance(m.get("args"), dict) else mapped_args
            )
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                    args = parsed if isinstance(parsed, dict) else {}
                except Exception:
                    args = {}
            raw_res = m.get("tool_result")
            if raw_res is None:
                raw_res = m.get("output")
            if raw_res is None:
                raw_res = m.get("result")
            if raw_res is None:
                raw_res = m.get("content")
            res_obj: Optional[Dict[str, Any]] = None
            res_text = ""
            if isinstance(raw_res, dict):
                res_obj = raw_res
                res_text = json.dumps(raw_res, ensure_ascii=False)[:1500]
            elif isinstance(raw_res, str):
                res_text = raw_res
                st = raw_res.strip()
                if st.startswith("{"):
                    try:
                        parsed = json.loads(st)
                        if isinstance(parsed, dict):
                            res_obj = parsed
                    except Exception:
                        pass
            ok_val = m.get("ok")
            if ok_val is None and res_obj is not None and "ok" in res_obj:
                ok_val = bool(res_obj.get("ok"))
            if ok_val is None and m.get("status") in ("failed", "error", "denied"):
                ok_val = False
            if ok_val is None and m.get("error"):
                ok_val = False
            out.append({
                "name": name,
                "args": args if isinstance(args, dict) else {},
                "ok": ok_val,
                "result_obj": res_obj,
                "result_text": res_text,
                "error": str(m.get("error") or (res_obj.get("error") if isinstance(res_obj, dict) else "") or ""),
            })
    return out


def _detect_blocker_label(text: str) -> str:
    low = (text or "").lower()
    if not low:
        return ""
    rules = (
        (("turnstile", "cf_challenge", "cloudflare challenge", "cf_fail"), "Turnstile"),
        (("recaptcha", "hcaptcha", "geetest", "arkose", "funcaptcha", "滑块", "人机验证"), "人机验证"),
        (("user_srp_auth", "cognito", "password_verifier", "srp"), "SRP鉴权"),
        (("waitlist", "候补", "邀请码", "invite code", "企业邮箱", "work email", "public email"), "准入限制"),
        (("信用卡", "credit card", "绑卡", "billing", "payment", "付费", "stripe"), "绑卡付费"),
        (("otp", "验证码超时", "收码超时", "no_mail", "未收到验证码", "收信超时", "mail timeout"), "收码超时"),
        (("proxy", "代理不可用", "proxyerror", "socks5", "407 "), "代理异常"),
        (("403", "429", "waf", "rate limit", "too many requests", "风控", "forbidden", "access denied"), "风控403"),
    )
    for keywords, label in rules:
        if any(k in low for k in keywords):
            return label
    return ""


def _extract_user_topic_snippet(messages: List[Dict[str, Any]]) -> str:
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        raw = str(m.get("content") or "").strip()
        if not raw or raw.startswith(("<compaction_summary>", "[用户运行中实时纠偏]")):
            continue
        # 去掉附件头和 /goal 前缀
        lines = [
            ln.strip()
            for ln in raw.splitlines()
            if ln.strip() and not ln.strip().startswith(("[已上传附件:", "---", "registerhub_handoff:"))
        ]
        if not lines:
            continue
        first = lines[0]
        first = re.sub(r"^/(?:goal|probe|fix|audit|run|scaffold)\s+", "", first, flags=re.I)
        first = re.sub(r"^(?:目标站点|站点|网址)[:：]\s*", "", first)
        first = _URL_RE.sub("", first).strip(" ，。！？：:;-—")
        first = re.sub(r"^(?:请|帮我|给我|麻烦|测试一下|试着|帮我看下|看下|跑一下|注册一下)\s*", "", first)
        if first:
            return first[:12].strip()
    return ""


def analyze_session_progress(session_dict: Dict[str, Any]) -> Dict[str, Any]:
    """跨三仓统一提取会话目标站、关联适配器、阶段状态、新增凭据数与关键结论。"""
    messages = session_dict.get("messages") if isinstance(session_dict.get("messages"), list) else []
    status = str(session_dict.get("status") or "idle").strip().lower()
    last_error = str(session_dict.get("error") or session_dict.get("last_error") or "").strip()
    goal_state = session_dict.get("goal_state") if isinstance(session_dict.get("goal_state"), dict) else None
    target_url = str(session_dict.get("target_url") or "").strip()

    tool_evs = _extract_tool_events(messages)

    # 1. 提取目标 URL、站点/适配器 slug、触及文件、运行结果
    site_slug = ""
    touched_files: List[str] = []
    has_probed = False
    has_written_adapter = False
    has_run_adapter = False
    passed = False
    new_accounts_count = 0
    last_run_ok: Optional[bool] = None
    last_run_err = ""
    key_steps: List[str] = []

    for ev in tool_evs:
        tn = ev["name"]
        args = ev["args"]
        res_obj = ev["result_obj"] or {}
        res_text = ev["result_text"] or ""

        for k in ("url", "target_url"):
            u = str(args.get(k) or "").strip()
            if u.startswith(("http://", "https://")) and not target_url:
                target_url = u

        for k in ("adapter", "site", "site_name", "project", "name"):
            if tn in (
                "write_adapter", "patch_adapter", "read_adapter", "run_adapter", "run_adapter_once",
                "scaffold_site", "scaffold_project", "verify_key", "inspect_project", "read_project_file",
                "write_project_file", "patch_project_file", "run_project",
            ):
                cand = _clean_site_slug(str(args.get(k) or ""))
                if cand:
                    site_slug = cand
                    break

        for k in ("file", "path", "written_path", "adapter_file"):
            fp = str(args.get(k) or res_obj.get(k) or "").strip()
            if fp and fp not in touched_files and len(touched_files) < 12:
                touched_files.append(fp)
                if not site_slug and fp.endswith(".py"):
                    cand = _clean_site_slug(fp)
                    if cand:
                        site_slug = cand

        if tn in ("probe_url", "probe_site", "inspect_form", "inspect_site", "capture_network", "browser_probe", "analyze_har"):
            has_probed = True
            key_steps.append(f"探测 `{tn}`" + (f" ({args.get('url')})" if args.get("url") else ""))
        elif tn in ("scaffold_site", "scaffold_project", "write_adapter", "patch_adapter", "write_project_file", "patch_project_file"):
            has_written_adapter = True
            key_steps.append(f"编写/修改 `{site_slug or args.get('adapter') or args.get('path') or tn}`")
        elif tn in ("run_adapter", "run_adapter_once", "run_project"):
            has_run_adapter = True
            added = 0
            for ak in ("new_valid_credentials_added", "new_accounts_added", "new_accounts", "added", "success_count", "created"):
                try:
                    v = int(res_obj.get(ak) or 0)
                    if v > added:
                        added = v
                except Exception:
                    pass
            if added > 0:
                new_accounts_count = max(new_accounts_count, added)
                passed = True
                last_run_ok = True
                key_steps.append(f"试跑 `{site_slug or tn}` 成功（+{added} 账号）")
            else:
                rc = res_obj.get("returncode")
                if ev["ok"] is True and rc == 0 and ("成功" in res_text or "saved" in res_text.lower()):
                    last_run_ok = True
                else:
                    last_run_ok = False
                    last_run_err = ev["error"] or str(res_obj.get("stderr") or res_obj.get("log_tail") or res_text)[:400]
                key_steps.append(f"试跑 `{site_slug or tn}` {'通过' if last_run_ok else '失败'}")
        elif tn == "verify_key":
            if res_obj.get("valid") is True or res_obj.get("ok") is True:
                passed = True
                key_steps.append("在线验活 Key 通过")

    # 从用户消息补抽 URL
    if not target_url:
        for m in messages:
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            txt = str(m.get("content") or "")
            hit = _URL_RE.search(txt)
            if hit:
                cand_u = hit.group(0).rstrip(".,;:!?`'\"")
                if not any(h in cand_u.lower() for h in ("127.0.0.1", "localhost")):
                    target_url = cand_u
                    break

    # 检查 goal_state
    if goal_state:
        gs_status = str(goal_state.get("status") or "").lower()
        if gs_status == "achieved":
            passed = True
            try:
                ach = int(goal_state.get("achieved_credentials") or 0)
                if ach > new_accounts_count:
                    new_accounts_count = ach
            except Exception:
                pass

    # 提取最后若干条高价值 assistant 结论
    assistant_notes: List[str] = []
    compaction_summary = ""
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        content = str(m.get("content") or "").strip()
        if role == "compaction" and content:
            compaction_summary = content
        elif role == "user" and content.startswith("<compaction_summary>"):
            compaction_summary = content.replace("<compaction_summary>", "").replace("</compaction_summary>", "").strip()
        elif role == "assistant" and content and len(content) >= 12:
            if "已达到单轮最大" in content:
                continue
            assistant_notes.append(content)

    # 失败/阻塞归因
    blocker_source = " ".join(filter(None, [
        str((goal_state or {}).get("blocked_reason") or ""),
        last_error,
        last_run_err,
        assistant_notes[-1][:600] if assistant_notes else "",
    ]))
    blocker_label = _detect_blocker_label(blocker_source)

    # 判定阶段与展示标签
    if passed:
        stage = "passed"
        stage_label = "已跑通"
    elif status == "running":
        if has_written_adapter or has_run_adapter:
            stage = "testing"
            stage_label = "调测中"
        else:
            stage = "probing"
            stage_label = "探测中"
    elif status == "waiting_approval":
        stage = "testing"
        stage_label = "待审批"
    elif (goal_state and str(goal_state.get("status") or "").lower() == "blocked") or status in ("error", "failed"):
        stage = "blocked"
        stage_label = f"卡在{blocker_label}" if blocker_label else "待续跑"
    elif has_run_adapter and last_run_ok is False:
        stage = "blocked"
        stage_label = f"卡在{blocker_label}" if blocker_label else "待续跑"
    elif has_written_adapter or has_probed or status == "stopped":
        stage = "pending"
        stage_label = f"卡在{blocker_label}" if (blocker_label and status != "done") else "待续跑"
    elif assistant_notes:
        stage = "done"
        stage_label = "已完成"
    else:
        stage = "probing"
        stage_label = "探测中"

    # 去重里程碑步骤（保留最近 14 条）
    dedup_steps: List[str] = []
    for st in key_steps:
        if not dedup_steps or dedup_steps[-1] != st:
            dedup_steps.append(st)
    dedup_steps = dedup_steps[-14:]

    return {
        "site_slug": site_slug,
        "target_url": target_url,
        "target_label": (
            site_slug.replace("_", "-")
            if site_slug
            else (_brand_from_url(target_url) or _extract_user_topic_snippet(messages))
        ),
        "has_probed": has_probed,
        "has_written_adapter": has_written_adapter,
        "has_run_adapter": has_run_adapter,
        "passed": passed,
        "new_accounts_count": new_accounts_count,
        "blocker_label": blocker_label,
        "last_run_err": last_run_err,
        "stage": stage,
        "stage_label": stage_label,
        "touched_files": touched_files,
        "key_steps": dedup_steps,
        "assistant_notes": assistant_notes[-5:],
        "compaction_summary": compaction_summary,
    }


def compute_staged_session_title(
    session_dict: Dict[str, Any],
    *,
    current_title: str = "",
    title_source: str = "",
) -> Optional[str]:
    """按 3 个关键里程碑（锁定目标 ➔ 调测中 ➔ 已跑通/卡点收口）计算简洁标题。

    - 若用户手动改过名（`title_source == "user"` 或非自动规则标题），直接返回 None 绝不覆盖。
    - 返回 None 表示当前标题无需变更。
    """
    cur = (current_title or str(session_dict.get("title") or "")).strip()
    src = (title_source or str(session_dict.get("title_source") or "")).strip()
    if not is_auto_managed_title(cur, src):
        return None

    prog = analyze_session_progress(session_dict)
    label = str(prog.get("target_label") or "").strip()
    if not label:
        return None

    stage_label = str(prog.get("stage_label") or "").strip()
    # 控制标题长度适中且不截断常见站点主干名（ASCII 宽度约为汉字一半，侧边栏可单行完整展示）
    max_label_len = max(12, 26 - len(stage_label) - 3)
    if len(label) > max_label_len:
        label = label[:max_label_len].rstrip("-_ .")

    candidate = f"{label} · {stage_label}" if stage_label else label
    if candidate == cur:
        return None
    return candidate


# ---------------------------------------------------------------------------
# 脱敏自包含接力单（*.handoff.md）生成与解析
# ---------------------------------------------------------------------------

def _safe_filename_slug(raw: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]+", "-", (raw or "").strip()).strip("-").lower()
    return clean[:40] or "session"


def _yaml_scalar(val: Any) -> str:
    s = str(val or "").replace("\r", " ").replace("\n", " ").strip()
    if not s:
        return '""'
    if re.search(r"[:#\[\]{},&*?|>!%@`\"']", s) or s.lower() in ("true", "false", "null", "yes", "no"):
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


def build_handoff_markdown(
    session_dict: Dict[str, Any],
    *,
    edition: str = "community",
    adapter_code: str = "",
    adapter_path: str = "",
    include_code: bool = True,
    llm_summary: str = "",
) -> Dict[str, Any]:
    """生成可直接分享给他人且跨母港/PRO/开源三仓通用的脱敏接力 Markdown（*.handoff.md）。"""
    prog = analyze_session_progress(session_dict)
    status = "passed" if prog["passed"] else ("blocked" if prog["stage"] == "blocked" else "in_progress")
    status_cn = {"passed": "已跑通（可直接复刻）", "blocked": "未跑通（已定位卡点·可续跑）", "in_progress": "进行中（可接力续跑）"}[status]

    site_slug = prog["site_slug"] or _safe_filename_slug(prog["target_label"]).replace("-", "_")
    target_url = prog["target_url"]
    raw_title = str(session_dict.get("title") or "").strip()
    display_title = (
        compute_staged_session_title(session_dict, current_title=raw_title, title_source="auto")
        or (raw_title if not is_default_title(raw_title) else (prog["target_label"] or site_slug))
    )
    steps_count = int(session_dict.get("steps_count") or session_dict.get("step_count") or len(_extract_tool_events(session_dict.get("messages") or [])))
    created_at = time.strftime("%Y-%m-%d %H:%M:%S")

    total_stats = {"secrets": 0, "emails": 0, "proxies": 0, "paths": 0, "accounts": 0, "total": 0}

    clean_url, st_u = redact_handoff_text(target_url)
    total_stats = _merge_redaction_stats(total_stats, st_u)
    clean_title, st_t = redact_handoff_text(display_title)
    total_stats = _merge_redaction_stats(total_stats, st_t)

    clean_adapter_path = ""
    clean_code = ""
    if include_code and (adapter_code or "").strip():
        clean_code, st_c = redact_source_code(adapter_code)
        total_stats = _merge_redaction_stats(total_stats, st_c)
        clean_adapter_path, st_p = redact_handoff_text(adapter_path or f"sites/{site_slug}.py")
        total_stats = _merge_redaction_stats(total_stats, st_p)

    # 拼装正文段落（全部过脱敏）
    body_lines: List[str] = []
    body_lines.append(f"# RegisterHub 任务接力单：{clean_title}")
    body_lines.append("")
    body_lines.append("> 本文件由 RegisterHub 自动脱敏导出，已剔除本机 API Key、JWT、邮箱、代理账密与绝对路径。直接将本文件导入或拖入 **母港 / PRO / 开源社区版** 任意版本的 AI 对话框即可继续执行。")
    body_lines.append("")
    body_lines.append("## 1. 任务概况与当前状态")
    body_lines.append(f"- **状态**：{status_cn}")
    if clean_url:
        body_lines.append(f"- **目标站点**：`{clean_url}`")
    if site_slug:
        body_lines.append(f"- **站点标识**：`{site_slug}`")
    body_lines.append(f"- **来源版本**：`{edition}` · 已执行 `{steps_count}` 步")
    if prog["new_accounts_count"] > 0:
        body_lines.append(f"- **已验证成果**：单并发实跑成功新增 `{prog['new_accounts_count']}` 个有效账号/凭据")
    if prog["blocker_label"] and status != "passed":
        body_lines.append(f"- **当前主要卡点**：`{prog['blocker_label']}`")
    body_lines.append("")

    # Plan 清单
    raw_plan = session_dict.get("plan_steps") or session_dict.get("plan") or []
    if isinstance(raw_plan, list) and raw_plan:
        body_lines.append("## 2. 阶段清单（Plan）")
        for idx, step in enumerate(raw_plan[:12], 1):
            if not isinstance(step, dict):
                continue
            st = str(step.get("status") or "pending").lower()
            mark = "x" if st in ("done", "completed") else ("-" if st in ("failed", "error") else " ")
            st_title = str(step.get("title") or step.get("label") or step.get("text") or f"步骤 {idx}").strip()
            st_title, st_s = redact_handoff_text(st_title)
            total_stats = _merge_redaction_stats(total_stats, st_s)
            body_lines.append(f"- [{mark}] {st_title}")
        body_lines.append("")

    # 核心发现 / AI 精炼摘要 / 已有压缩检查点
    body_lines.append("## 3. 逆向情报、流程结论与踩坑记录")
    if (llm_summary or "").strip():
        clean_sum, st_ls = redact_handoff_text(llm_summary.strip())
        total_stats = _merge_redaction_stats(total_stats, st_ls)
        body_lines.append(clean_sum)
        body_lines.append("")
    else:
        if prog["compaction_summary"]:
            clean_cp, st_cp = redact_handoff_text(prog["compaction_summary"][:2800])
            total_stats = _merge_redaction_stats(total_stats, st_cp)
            body_lines.append("### 历史压缩检查点")
            body_lines.append(clean_cp)
            body_lines.append("")

        if prog["key_steps"]:
            body_lines.append("### 关键操作轨迹")
            for ks in prog["key_steps"]:
                cks, st_ks = redact_handoff_text(ks)
                total_stats = _merge_redaction_stats(total_stats, st_ks)
                body_lines.append(f"- {cks}")
            body_lines.append("")

        if prog["assistant_notes"]:
            body_lines.append("### 最近关键分析结论")
            for idx, note in enumerate(prog["assistant_notes"][-3:], 1):
                cnote, st_n = redact_handoff_text(note[:1400])
                total_stats = _merge_redaction_stats(total_stats, st_n)
                body_lines.append(f"#### 结论 {idx}")
                body_lines.append(cnote)
                body_lines.append("")

        if status != "passed" and prog["last_run_err"]:
            cerr, st_e = redact_handoff_text(prog["last_run_err"][:800])
            total_stats = _merge_redaction_stats(total_stats, st_e)
            body_lines.append("### 最近一次试跑报错摘录")
            body_lines.append("```text")
            body_lines.append(cerr)
            body_lines.append("```")
            body_lines.append("")

    # 适配器源码段
    if clean_code:
        body_lines.append("## 4. 当前适配器源码（已脱敏）")
        body_lines.append(f"- 参考路径：`{clean_adapter_path}`")
        body_lines.append("")
        body_lines.append("```python")
        body_lines.append(clean_code.rstrip())
        body_lines.append("```")
        body_lines.append("")

    # 下一步接力指令
    sec_no = 5 if clean_code else 4
    body_lines.append(f"## {sec_no}. 接力执行指令（供接收方 Agent 直接续跑）")
    if status == "passed":
        body_lines.append("1. 本站点已在原环境跑通。请检查本机邮箱池/代理池可用性，并按当前运行版本的目录与基类规范落地上述适配器代码（开源/PRO 落 `sites/<site>.py`，母港通过派生脚手架落独立子工程）。")
        body_lines.append("2. 若来源版本与当前版本不同，请先对照本机 `read_core_file` / 模板适配基类接口，切勿照搬不兼容导入。")
        body_lines.append("3. 落地后执行单并发试跑 1 号验证闭环，确认产出真实有效凭据并入账。")
    else:
        body_lines.append("1. 本站点尚未完全跑通，请优先复核上方「逆向情报与踩坑记录」，避免重复踩已经验证过的死路。")
        if clean_code:
            body_lines.append("2. 先按当前版本规范落地上述半成品适配器代码，在此基础上直接针对当前卡点推进修复。")
        else:
            body_lines.append("2. 直接从上述进展与卡点切入继续探测或编写适配器，无需从零重复基础侦察。")
        body_lines.append("3. 修复后执行单并发试跑 1 号验收闭环，拿到真实有效凭据后再回报收工。")
    body_lines.append("")

    fm_lines = [
        "---",
        f"registerhub_handoff: {HANDOFF_SCHEMA_VERSION}",
        f"source_edition: {_yaml_scalar(edition)}",
        f"status: {_yaml_scalar(status)}",
        f"site_slug: {_yaml_scalar(site_slug)}",
        f"target_url: {_yaml_scalar(clean_url)}",
        f"title: {_yaml_scalar(clean_title)}",
        f"steps_count: {steps_count}",
        f"adapter_file: {_yaml_scalar(clean_adapter_path)}",
        f"has_adapter_code: {'true' if clean_code else 'false'}",
        f"created_at: {_yaml_scalar(created_at)}",
        "---",
        "",
    ]

    full_md = "\n".join(fm_lines) + "\n".join(body_lines)
    fname_slug = _safe_filename_slug(site_slug or prog["target_label"])
    return {
        "ok": True,
        "markdown": full_md,
        "filename": f"{fname_slug}-{status}.handoff.md",
        "status": status,
        "status_label": status_cn,
        "site_slug": site_slug,
        "target_url": clean_url,
        "title": clean_title,
        "adapter_file": clean_adapter_path,
        "has_adapter_code": bool(clean_code),
        "redaction_stats": total_stats,
        "char_count": len(full_md),
    }


def build_handoff_llm_refine_prompt(handoff_md: str) -> str:
    """生成用于「AI 精炼提纯」的短系统提示，将冗长轨迹浓缩为高信噪比 1 页情报摘要。"""
    return (
        "你是 RegisterHub 资深注册逆向工程师。请将下面这份任务接力单中的「逆向情报与踩坑记录」"
        "浓缩为极简、高信噪比的 Markdown 摘要（不超过 600 字），必须包含：\n"
        "1. 关键接口与认证链路（精确域名、路径、请求方式、反爬/验证码类型）；\n"
        "2. 已验证可行的突破点与已排除的死路；\n"
        "3. 当前剩余卡点（或已跑通的交付物格式）与最直接的下一步行动。\n"
        "严格要求：绝不编造信息，绝不输出任何真实 Key/邮箱/密码/本机绝对路径，只输出精炼后的正文 Markdown，不要再包 YAML 头或完整代码。\n\n"
        f"{(handoff_md or '')[:12000]}"
    )


def parse_handoff_markdown(raw_text: str, *, target_edition: str = "community") -> Dict[str, Any]:
    """解析 *.handoff.md（或普通任务 Markdown），输出新建接力会话所需的标题、目标 URL 与首轮执行指令。"""
    text = str(raw_text or "").replace("\r\n", "\n").strip()
    if not text:
        raise ValueError("接力单内容不能为空")

    meta: Dict[str, str] = {}
    body = text
    is_handoff = False

    if text.startswith("---\n"):
        end_idx = text.find("\n---\n", 4)
        if end_idx != -1:
            fm_raw = text[4:end_idx]
            body = text[end_idx + 5:].strip()
            for ln in fm_raw.splitlines():
                if ":" not in ln:
                    continue
                k, v = ln.split(":", 1)
                key = k.strip()
                val = v.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                if key:
                    meta[key] = val
            if meta.get("registerhub_handoff"):
                is_handoff = True

    source_edition = meta.get("source_edition", "").strip()
    status = meta.get("status", "in_progress").strip().lower()
    site_slug = _clean_site_slug(meta.get("site_slug", ""))
    target_url = meta.get("target_url", "").strip()
    if not target_url:
        hit = _URL_RE.search(body)
        if hit:
            target_url = hit.group(0).rstrip(".,;:!?`'\"")

    # 提取附带的 Python 代码块（若有）
    adapter_code = ""
    m_code = re.search(r"```python\s*\n(.*?)\n```", body, re.S)
    if m_code:
        adapter_code = m_code.group(1).strip()

    label = (
        site_slug.replace("_", "-")
        if site_slug
        else (_brand_from_url(target_url) or meta.get("title", "").split(" · ")[0].strip()[:14] or "外部接力")
    )
    prefix = "复刻" if status == "passed" else "接力"
    stage = "待验收" if status == "passed" else "待续跑"
    suggested_title = f"{prefix} · {label}"[:22]

    cross_note = ""
    if source_edition and target_edition and source_edition != target_edition:
        cross_note = (
            f"\n- 注意：该接力单导出自 `{source_edition}` 版，当前运行环境为 `{target_edition}` 版；"
            "请按当前版本的目录规范与基类签名适配导入和代码结构。"
        )

    action_hint = (
        "请基于下方已跑通的接力单，在本机环境中落地并适配适配器代码，然后单并发试跑 1 号完成端到端验收。"
        if status == "passed"
        else "请基于下方未跑通的接力单，先吸收已有探测结论与半成品代码，跳过已验证的死路，直接从当前卡点继续修复并单并发试跑 1 号验收。"
    )

    initial_prompt = (
        f"[已导入 RegisterHub 任务接力单（状态：{stage}）]\n"
        f"{action_hint}{cross_note}\n\n"
        f"{text[:28000]}"
    )

    return {
        "ok": True,
        "is_handoff": is_handoff,
        "meta": meta,
        "source_edition": source_edition,
        "status": status,
        "site_slug": site_slug,
        "target_url": target_url,
        "adapter_file": meta.get("adapter_file", ""),
        "has_adapter_code": bool(adapter_code),
        "adapter_code": adapter_code,
        "suggested_title": suggested_title,
        "initial_prompt": initial_prompt,
    }
