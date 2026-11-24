"""纯 Python 内置邮箱池与本地接码引擎（零 Docker / 零 AGPL 依赖）。

核心特性（吸收 Codex 架构评审收敛意见）：
1. SQLite WAL + busy_timeout + BEGIN IMMEDIATE 事务租约，防多并发撞同一邮箱；
2. 敏感凭据（密码、refresh_token、access_token）统一走 core.secrets (Windows DPAPI) 加密落库，
   API 响应与日志仅返回脱敏摘要；
3. 显式导入模板 + 前 N 行解析预览校验（绝不靠正则盲猜交换 client_id 与 refresh_token）；
4. 显式协议验活状态机：批量验活时明确锁定账号支持的协议（graph / imap_xoauth2 / imap_plain / dead），
   收信阶段严格按已验通协议执行，并按账号加锁防并发重复刷新 refresh_token。
"""

from __future__ import annotations

import base64
import concurrent.futures
import email as _stdlib_email
import email.header as _stdlib_email_header
import email.utils as _stdlib_email_utils
import hashlib
import html as _stdlib_html
import imaplib
import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .net_guard import (
    PrivateAddressBlockedError,
    build_safe_urllib_opener,
    validate_public_http_url,
)
from .secrets import (
    SecretProtectionError,
    mask_secret,
    mask_secret_url,
    protect_secret,
    sanitize_error_text,
    unprotect_secret,
)

DEFAULT_BUILTIN_MAIL_DB = Path(__file__).resolve().parent.parent / "data" / "builtin_mail.db"
_MAX_ICLOUD_LINK_BYTES = 512 * 1024
_MAX_ICLOUD_LINK_REDIRECTS = 3

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_GRAPH_SCOPE = "https://graph.microsoft.com/Mail.Read offline_access"
_IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
_MS_TOKEN_ENDPOINTS = (
    "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
    "https://login.microsoftonline.com/common/oauth2/v2.0/token",
)
_PERMANENT_OAUTH_ERRORS = (
    "invalid_grant",
    "unauthorized_client",
    "invalid_client",
    "interaction_required",
    "consent_required",
    "aadsts70000",
    "aadsts70008",
    "aadsts50126",
    "aadsts50053",
    "aadsts50057",
)
_SCOPE_MISMATCH_HINTS = (
    "invalid_scope",
    "one or more scopes requested are unauthorized",
    "scopes requested are unauthorized",
    "aadsts65001",
    "aadsts70011",
    "accessdenied",
    "erroraccessdenied",
    "mailboxnotenabledforrestapi",
)

IMPORT_FORMATS: dict[str, dict[str, str]] = {
    "auto": {
        "id": "auto",
        "label": "智能自动识别（无脑粘贴：Outlook / iCloud 链接 / QQ·163·Gmail·IMAP 混合）",
        "example": "支持任意混合粘贴，自动识别 ---- / : / | / Tab / 空格 分隔符与正反序",
    },
    "outlook_email_pass_cid_rt": {
        "id": "outlook_email_pass_cid_rt",
        "label": "Outlook 四段式：邮箱----密码----client_id----refresh_token",
        "example": "user@outlook.com----Pass123----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C519_BAY...",
    },
    "outlook_email_pass_rt_cid": {
        "id": "outlook_email_pass_rt_cid",
        "label": "Outlook 四段式（反序）：邮箱----密码----refresh_token----client_id",
        "example": "user@outlook.com----Pass123----M.C519_BAY...----9e5f94bc-e8a4-4e73-b8be-63364c29d753",
    },
    "outlook_email_cid_rt": {
        "id": "outlook_email_cid_rt",
        "label": "Outlook 三段式（无密码）：邮箱----client_id----refresh_token",
        "example": "user@outlook.com----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C519_BAY...",
    },
    "imap_email_pass_host": {
        "id": "imap_email_pass_host",
        "label": "通用 IMAP 账号：邮箱----授权码/密码----imap.host[:993]",
        "example": "user@example.com----app_pass----imap.example.com:993",
    },
    "icloud_email_link": {
        "id": "icloud_email_link",
        "label": "iCloud 接码链接：邮箱----https://接码链接",
        "example": "user@icloud.com----https://mail-link.example.com/inbox/abc123",
    },
}

_KNOWN_IMAP_HOSTS: dict[str, tuple[str, int]] = {
    "qq.com": ("imap.qq.com", 993),
    "foxmail.com": ("imap.qq.com", 993),
    "163.com": ("imap.163.com", 993),
    "126.com": ("imap.126.com", 993),
    "yeah.net": ("imap.yeah.net", 993),
    "gmail.com": ("imap.gmail.com", 993),
    "googlemail.com": ("imap.gmail.com", 993),
    "yahoo.com": ("imap.mail.yahoo.com", 993),
    "yandex.com": ("imap.yandex.com", 993),
    "yandex.ru": ("imap.yandex.ru", 993),
    "gmx.com": ("imap.gmx.com", 993),
    "gmx.net": ("imap.gmx.net", 993),
    "mail.ru": ("imap.mail.ru", 993),
    "sina.com": ("imap.sina.com", 993),
    "sohu.com": ("imap.sohu.com", 993),
    "aliyun.com": ("imap.aliyun.com", 993),
    "icloud.com": ("imap.mail.me.com", 993),
    "me.com": ("imap.mail.me.com", 993),
    "mac.com": ("imap.mail.me.com", 993),
    "zoho.com": ("imap.zoho.com", 993),
    "fastmail.com": ("imap.fastmail.com", 993),
}
_MS_PUBLIC_DOMAINS = frozenset({"outlook.com", "hotmail.com", "live.com", "msn.com"})
_IMAP_HOST_RE = re.compile(
    r"^(?:(?:imap|mail|mx|pop3?)\.[a-z0-9.-]+\.[a-z]{2,}(?::\d{1,5})?|[a-z0-9.-]+\.[a-z]{2,}:\d{1,5})$",
    re.I,
)


class OAuthRefreshError(RuntimeError):
    """Outlook OAuth2 刷新失败（区分永久失效、权限不匹配与瞬时网络抖动）。"""

    def __init__(self, message: str, *, permanent: bool = False, scope_mismatch: bool = False):
        super().__init__(message)
        self.permanent = permanent
        self.scope_mismatch = scope_mismatch


def _parse_external_json_dict(raw: str, *, what: str) -> dict[str, Any]:
    """解析外部 HTTP JSON 响应体（优先走 core.jsonbody.parse_json_body，PRO 独立运行兼容回退）。"""
    try:
        from .jsonbody import parse_json_body

        return parse_json_body(raw, what=what, expect=dict)
    except ImportError:
        val = json.loads(raw)
        if not isinstance(val, dict):
            raise ValueError(f"{what} 期望 JSON 对象，实际得到 {type(val).__name__}")
        return val


def _parse_external_json_any(raw: str, *, what: str) -> Any:
    try:
        from .jsonbody import parse_json_body

        return parse_json_body(raw, what=what, expect=None)
    except ImportError:
        return json.loads(raw)


_SRCDOC_RE = re.compile(
    r"<iframe\b[^>]*\bsrcdoc\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
    re.I | re.S,
)
_HTML_TITLE_RE = re.compile(r"<title\b[^>]*>([\s\S]*?)</title>", re.I)
_HTML_TIME_ATTR_RE = re.compile(r"<time\b[^>]*\bdatetime\s*=\s*[\"']([^\"']+)[\"']", re.I)
_ISO_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\b")


def _extract_html_timestamp(html_text: str) -> float:
    """从 HTML 页面中提取真实邮件时间戳（<time datetime> 或 ISO 时间）；无真实时间返回 0.0，严禁伪造当前时间。"""
    if not html_text:
        return 0.0
    m = _HTML_TIME_ATTR_RE.search(html_text)
    if m:
        ts = _coerce_timestamp(m.group(1))
        if ts > 0:
            return ts
    m2 = _ISO_DATE_RE.search(html_text[:8192])
    if m2:
        ts = _coerce_timestamp(m2.group(1))
        if ts > 0:
            return ts
    return 0.0


def _coerce_timestamp(val: Any) -> float:
    if isinstance(val, (int, float)) and val > 0:
        return float(val) / 1000.0 if float(val) > 1e11 else float(val)
    if isinstance(val, str) and val.strip():
        s = val.strip()
        try:
            num = float(s)
            if num > 0:
                return num / 1000.0 if num > 1e11 else num
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        try:
            dt = _stdlib_email_utils.parsedate_to_datetime(s)
            if dt is not None:
                return dt.timestamp()
        except Exception:
            pass
    return 0.0


def _parse_icloud_link_payload(raw_body: str, *, content_type: str = "", top: int = 10) -> list[dict[str, Any]]:
    """纯内存解析 iCloud 接码链接响应（兼容 JSON API、<iframe srcdoc> 邮件页与普通 HTML），绝不落盘原始 HTML。"""
    text = (raw_body or "").strip()
    if not text:
        return []
    limit_n = max(1, min(int(top), 20))

    # 1. JSON 格式接码接口
    is_json = ("json" in (content_type or "").lower()) or text[0] in ("{", "[")
    if is_json:
        try:
            obj = _parse_external_json_any(text, what="iCloud 接码链接 JSON")
        except Exception:
            obj = None
        if obj is not None:
            raw_items: list[Any] = []
            if isinstance(obj, list):
                raw_items = obj
            elif isinstance(obj, dict):
                for key in ("messages", "data", "emails", "list", "items", "mail", "mails"):
                    val = obj.get(key)
                    if isinstance(val, list):
                        raw_items = val
                        break
                    if isinstance(val, dict):
                        for subk in ("messages", "list", "items", "emails"):
                            if isinstance(val.get(subk), list):
                                raw_items = val[subk]
                                break
                        if raw_items:
                            break
                        if any(k in val for k in ("subject", "html", "text", "body", "content")):
                            raw_items = [val]
                            break
                if not raw_items and any(k in obj for k in ("subject", "html", "text", "body", "content", "code")):
                    raw_items = [obj]

            out_json: list[dict[str, Any]] = []
            for idx, item in enumerate(raw_items[:limit_n]):
                if not isinstance(item, dict):
                    continue
                subj = str(item.get("subject") or item.get("title") or "iCloud 接码邮件").strip()
                sender = str(item.get("from") or item.get("sender") or item.get("from_address") or "").strip()
                htm = str(item.get("html") or item.get("body_html") or item.get("html_body") or "")
                txt = str(item.get("text") or item.get("body") or item.get("content") or item.get("code") or "")
                if not htm and ("<html" in txt.lower() or "<body" in txt.lower() or "<div" in txt.lower()):
                    htm = txt
                    txt = _strip_html_excerpt(htm)
                clean_txt = _strip_html_excerpt(txt) if ("<" in txt and ">" in txt) else re.sub(r"\s+", " ", txt).strip()
                excerpt = (clean_txt or _strip_html_excerpt(htm))[:240]
                ts = _coerce_timestamp(
                    item.get("receivedAt") or item.get("received_at") or item.get("created_at") or item.get("date") or item.get("time")
                )
                mid = str(item.get("id") or "").strip()
                if not mid:
                    digest = hashlib.sha256(f"{idx}:{subj}:{excerpt}:{htm[:512]}".encode("utf-8", errors="ignore")).hexdigest()[:16]
                    mid = f"icl_{digest}"
                out_json.append({
                    "id": mid,
                    "from": sender,
                    "subject": subj,
                    "receivedAt": ts,
                    "date": str(item.get("date") or ""),
                    "excerpt": excerpt,
                    "text": txt or clean_txt,
                    "html": htm,
                })
            if out_json:
                return out_json

    # 2. HTML 页面内嵌 <iframe srcdoc="..."> 邮件正文
    outer_title_m = _HTML_TITLE_RE.search(text)
    outer_title = _stdlib_html.unescape(re.sub(r"\s+", " ", outer_title_m.group(1))).strip() if outer_title_m else ""
    outer_ts = _extract_html_timestamp(text)
    srcdocs = _SRCDOC_RE.findall(text)
    if srcdocs:
        out_frames: list[dict[str, Any]] = []
        for idx, (dq, sq) in enumerate(srcdocs[:limit_n]):
            raw_doc = _stdlib_html.unescape(dq if dq else sq).strip()
            if not raw_doc:
                continue
            inner_tm = _HTML_TITLE_RE.search(raw_doc)
            subj = (
                _stdlib_html.unescape(re.sub(r"\s+", " ", inner_tm.group(1))).strip()
                if inner_tm
                else (outer_title or f"iCloud 接码邮件 #{idx + 1}")
            )
            plain = _strip_html_excerpt(raw_doc)
            ts = _extract_html_timestamp(raw_doc) or outer_ts
            digest = hashlib.sha256(f"{idx}:{subj}:{raw_doc[:2048]}".encode("utf-8", errors="ignore")).hexdigest()[:16]
            out_frames.append({
                "id": f"icl_{digest}",
                "from": "iCloud 接码链接",
                "subject": subj,
                "receivedAt": ts,
                "date": "",
                "excerpt": plain[:240],
                "text": plain,
                "html": raw_doc,
            })
        if out_frames:
            return out_frames

    # 3. 普通 HTML 或纯文本网页
    plain = _strip_html_excerpt(text) if ("<" in text and ">" in text) else re.sub(r"\s+", " ", text).strip()
    if not plain:
        return []
    subj = outer_title or "iCloud 接码页面"
    digest = hashlib.sha256(f"page:{subj}:{plain[:2048]}".encode("utf-8", errors="ignore")).hexdigest()[:16]
    return [{
        "id": f"icl_{digest}",
        "from": "iCloud 接码链接",
        "subject": subj,
        "receivedAt": outer_ts,
        "date": "",
        "excerpt": plain[:240],
        "text": plain,
        "html": text if ("<" in text and ">" in text) else "",
    }]


def _fetch_icloud_link_messages(link_url: str, *, top: int = 10, timeout: float = 15.0) -> list[dict[str, Any]]:
    """SSRF 加固的 iCloud 接码链接拉取：公网 DNS 校验 + 3 跳重定向限制 + 512KB 上限 + 零磁盘落盘。"""
    clean_url = validate_public_http_url(link_url, resolve_dns=True)
    opener = build_safe_urllib_opener()
    for handler in opener.handlers:
        if isinstance(handler, urllib.request.HTTPRedirectHandler):
            handler.max_redirections = _MAX_ICLOUD_LINK_REDIRECTS
    req = urllib.request.Request(
        clean_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )
    with opener.open(req, timeout=timeout) as resp:
        final_url = str(resp.geturl() or clean_url)
        validate_public_http_url(final_url, resolve_dns=True)
        raw_bytes = resp.read(_MAX_ICLOUD_LINK_BYTES + 1)
        if len(raw_bytes) > _MAX_ICLOUD_LINK_BYTES:
            raise ValueError(f"iCloud 接码页面体积超过 {_MAX_ICLOUD_LINK_BYTES // 1024}KB 安全上限")
        ctype = str(resp.headers.get("Content-Type") or "") if getattr(resp, "headers", None) else ""
    body_text = raw_bytes.decode("utf-8", errors="replace")
    return _parse_icloud_link_payload(body_text, content_type=ctype, top=top)


def _decode_mime_header(raw: str | None) -> str:
    if not raw:
        return ""
    out: list[str] = []
    for chunk, enc in _stdlib_email_header.decode_header(raw):
        if isinstance(chunk, bytes):
            out.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(str(chunk))
    return "".join(out).strip()


def _strip_html_excerpt(html_text: str) -> str:
    """剔除 style/script/注释与标签并反转义实体，避免 CSS 源码泄漏进摘要。"""
    if not html_text:
        return ""
    s = re.sub(r"<!--[\s\S]*?-->", " ", html_text)
    s = re.sub(r"<style\b[^>]*>[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", " ", s, flags=re.I)
    s = re.sub(r"<head\b[^>]*>[\s\S]*?</head>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _stdlib_html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_rfc822_message(msg_id: str, raw_bytes: bytes) -> dict[str, Any]:
    msg = _stdlib_email.message_from_bytes(raw_bytes)
    subject = _decode_mime_header(msg.get("Subject"))
    sender = _decode_mime_header(msg.get("From"))
    date_raw = msg.get("Date") or ""
    received_ts = 0.0
    if date_raw:
        try:
            dt = _stdlib_email_utils.parsedate_to_datetime(date_raw)
            if dt is not None:
                received_ts = dt.timestamp()
        except Exception:
            received_ts = 0.0

    text_parts: list[str] = []
    html_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            ctype = (part.get_content_type() or "").lower()
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if ctype == "text/plain":
                text_parts.append(decoded)
            elif ctype == "text/html":
                html_parts.append(decoded)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if (msg.get_content_type() or "").lower() == "text/html":
                html_parts.append(decoded)
            else:
                text_parts.append(decoded)

    text_body = "\n".join(text_parts).strip()
    html_body = "\n".join(html_parts).strip()
    clean_text = _strip_html_excerpt(text_body) if ("<" in text_body and ">" in text_body) else re.sub(r"\s+", " ", text_body).strip()
    excerpt = (clean_text or _strip_html_excerpt(html_body))[:240].strip()
    return {
        "id": str(msg_id),
        "from": sender,
        "subject": subject,
        "receivedAt": received_ts or time.time(),
        "date": date_raw,
        "excerpt": excerpt,
        "text": text_body,
        "html": html_body,
    }


def _split_import_line(line: str, delimiter: str) -> list[str]:
    if delimiter == "auto":
        # 优先按常见多字符/结构分隔符切分
        for sep in ("----", "---", "\t", "|", ";", ","):
            if sep in line:
                # 含 URL 时避免被 URL 内的逗号/分号/短横误切
                if "://" in line:
                    idx_url = line.lower().find("http")
                    if idx_url > 0 and sep not in line[:idx_url]:
                        continue
                return [seg.strip() for seg in line.split(sep)]
        # 兼容邮箱[:密码][:或空格]https://接码链接（支持 2 段与 3 段）
        m_url = re.search(r"[:：\s]+(https?://\S+)$", line, re.I)
        if m_url:
            left = line[: m_url.start()].strip(" :：")
            url_part = m_url.group(1).strip()
            if left:
                left_parts = [p.strip() for p in re.split(r"[\s:：]+", left) if p.strip()]
                return [*left_parts, url_part]
            return [url_part]
        # 兼容全角冒号与卡网高频半角冒号格式：email:pass:client_id:refresh_token 或 email:auth:imap.host:993
        norm_colon = line.replace("：", ":") if "://" not in line else line
        if ":" in norm_colon and "://" not in norm_colon:
            colon_parts = [seg.strip() for seg in norm_colon.split(":")]
            if len(colon_parts) >= 2 and _EMAIL_RE.fullmatch(colon_parts[0]):
                if (
                    len(colon_parts) == 4
                    and colon_parts[3].isdigit()
                    and 1 <= int(colon_parts[3]) <= 65535
                    and "." in colon_parts[2]
                    and not _UUID_RE.fullmatch(colon_parts[2])
                ):
                    return [colon_parts[0], colon_parts[1], f"{colon_parts[2]}:{colon_parts[3]}"]
                return colon_parts
        ws_parts = [seg.strip() for seg in re.split(r"\s+", line) if seg.strip()]
        if len(ws_parts) >= 2:
            return ws_parts
        return [line.strip()]
    return [seg.strip() for seg in line.split(delimiter)]


_LABEL_PREFIX_RE = re.compile(
    r"^(?:邮箱|账号|帐号|邮|email|mail|account|user|链接|接码链接|接码|网址|url|link|密码|授权码|pass|password)\s*[:：=]\s*",
    re.I,
)


def _strip_field_label(token: str) -> str:
    return _LABEL_PREFIX_RE.sub("", (token or "").strip()).strip()


def _parse_auto_line(line: str, delimiter: str = "auto") -> tuple[dict[str, Any] | None, str | None]:
    """按严格结构特征自动识别单行邮箱格式（零盲猜交换，歧义直接报错提示）。"""
    raw_cols = [_strip_field_label(c) for c in _split_import_line(line, delimiter)]
    cols = [c for c in raw_cols if c]

    # 兼容单列纯 iCloud 接码链接（从 URL 参数 ?email=xxx 自动提取邮箱）
    if len(cols) == 1 and cols[0].lower().startswith(("http://", "https://")):
        raw_link = cols[0]
        try:
            clean_link = validate_public_http_url(raw_link, resolve_dns=False)
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(clean_link).query)
            qs_email = ""
            for qk in ("email", "mail", "account", "user", "addr"):
                vals = qs.get(qk)
                if vals and _EMAIL_RE.fullmatch(vals[0].strip().lower()):
                    qs_email = vals[0].strip().lower()
                    break
            if qs_email:
                return {
                    "email": qs_email,
                    "provider_kind": "icloud_link",
                    "protocol": "icloud_link",
                    "password": "",
                    "client_id": "",
                    "refresh_token": "",
                    "imap_host": "",
                    "imap_port": 993,
                    "link_url": clean_link,
                    "detected_label": "iCloud 接码链接",
                }, None
        except ValueError as exc:
            return None, f"接码链接安全校验未通过: {sanitize_error_text(str(exc))}"
        return None, "单列接码链接缺少邮箱地址，请写为 邮箱----https://接码链接"

    if len(cols) < 2:
        return None, f"列数不足（仅识别到 {len(cols)} 段，至少需要 2 段）"

    email_idx = -1
    for i, c in enumerate(cols):
        if _EMAIL_RE.fullmatch(c.lower()) and not c.lower().startswith(("http://", "https://")):
            email_idx = i
            break
    if email_idx < 0:
        return None, f"未识别到合法邮箱地址: {cols[0][:40]}"

    email_addr = cols[email_idx].lower()
    domain = email_addr.rsplit("@", 1)[-1]
    rest = [c for i, c in enumerate(cols) if i != email_idx]

    # 1. 含 http(s):// -> iCloud / HTTPS 接码链接
    url_tokens = [c for c in rest if c.lower().startswith(("http://", "https://"))]
    if url_tokens:
        raw_link = url_tokens[0]
        try:
            clean_link = validate_public_http_url(raw_link, resolve_dns=False)
        except ValueError as exc:
            return None, f"接码链接安全校验未通过: {sanitize_error_text(str(exc))}"
        return {
            "email": email_addr,
            "provider_kind": "icloud_link",
            "protocol": "icloud_link",
            "password": "",
            "client_id": "",
            "refresh_token": "",
            "imap_host": "",
            "imap_port": 993,
            "link_url": clean_link,
            "detected_label": "iCloud 接码链接",
        }, None

    # 2. 含标准 UUID -> Outlook OAuth2（自动兼容正序/反序/三段式/多段备注，绝不把 UUID 当令牌）
    uuid_indices = [i for i, c in enumerate(rest) if _UUID_RE.fullmatch(c)]
    if uuid_indices:
        cid_idx = uuid_indices[0]
        client_id = rest[cid_idx]
        non_uuid = [c for i, c in enumerate(rest) if i != cid_idx]
        # 优先挑选以微软刷新令牌特征开头或最长的非邮箱长串作为 refresh_token
        rt_candidates = [
            (i, c)
            for i, c in enumerate(non_uuid)
            if len(c) >= 20 and not _UUID_RE.fullmatch(c) and not _EMAIL_RE.fullmatch(c.lower())
        ]
        if not rt_candidates:
            return None, "检测到 Outlook client_id (UUID)，但缺少有效的 refresh_token（长度不足 20）"
        rt_candidates.sort(
            key=lambda pair: (
                1 if pair[1].startswith(("M.", "0.A", "1.A")) else 0,
                len(pair[1]),
            ),
            reverse=True,
        )
        rt_idx, refresh_token = rt_candidates[0]
        leftovers = [c for i, c in enumerate(non_uuid) if i != rt_idx]
        password = leftovers[0] if leftovers else ""
        return {
            "email": email_addr,
            "provider_kind": "outlook_oauth",
            "protocol": "unknown",
            "password": password,
            "client_id": client_id,
            "refresh_token": refresh_token,
            "imap_host": "",
            "imap_port": 993,
            "link_url": "",
            "detected_label": "Outlook OAuth2",
        }, None

    # 3. 含显式 IMAP 主机（如 imap.xxx.com[:993]）
    imap_indices = [
        i for i, c in enumerate(rest)
        if _IMAP_HOST_RE.fullmatch(c) and not _EMAIL_RE.fullmatch(c.lower())
    ]
    if imap_indices:
        host_idx = imap_indices[0]
        host_spec = rest[host_idx]
        imap_host, imap_port = host_spec, 993
        if ":" in host_spec:
            h_part, p_part = host_spec.rsplit(":", 1)
            if not p_part.isdigit() or not (1 <= int(p_part) <= 65535):
                return None, f"IMAP 端口非法: {host_spec[:40]}"
            imap_host, imap_port = h_part.strip(), int(p_part)
        pwd_candidates = [c for i, c in enumerate(rest) if i != host_idx]
        if not pwd_candidates or not pwd_candidates[0]:
            return None, "IMAP 授权码/密码不能为空"
        return {
            "email": email_addr,
            "provider_kind": "imap",
            "protocol": "imap_plain",
            "password": pwd_candidates[0],
            "client_id": "",
            "refresh_token": "",
            "imap_host": imap_host,
            "imap_port": imap_port,
            "link_url": "",
            "detected_label": f"IMAP ({imap_host})",
        }, None

    # 4. 两段式（邮箱----授权码）：对常见公共邮箱自动匹配官方 IMAP 服务器
    if len(rest) == 1:
        if domain in _MS_PUBLIC_DOMAINS:
            return None, "微软已关闭 Outlook/Hotmail 纯密码 IMAP 登录，请粘贴含 client_id 与 refresh_token 的三/四段式令牌"
        known = _KNOWN_IMAP_HOSTS.get(domain)
        if known:
            imap_host, imap_port = known
            return {
                "email": email_addr,
                "provider_kind": "imap",
                "protocol": "imap_plain",
                "password": rest[0],
                "client_id": "",
                "refresh_token": "",
                "imap_host": imap_host,
                "imap_port": imap_port,
                "link_url": "",
                "detected_label": f"IMAP ({imap_host})",
            }, None
        return None, f"未识别 @{domain} 的默认 IMAP 服务器，请补全第 3 列（如 邮箱----授权码----imap.{domain}:993）"

    return None, "无法确定该行账号类型（未找到 UUID 令牌、接码链接或 IMAP 服务器）"


def parse_import_text(
    text: str,
    fmt: str = "auto",
    *,
    delimiter: str = "auto",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """解析导入文本：fmt='auto' 支持混合无脑粘贴（严格特征锚定、绝不静默误配），也可指定显式模板。"""
    if fmt not in IMPORT_FORMATS:
        raise ValueError(f"不支持的导入格式: {fmt!r}")
    delim = delimiter or ("auto" if fmt == "auto" else "----")
    valid_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen_emails: set[str] = set()

    for line_no, raw_line in enumerate((text or "").splitlines(), 1):
        s = raw_line.strip()
        if not s or s.startswith("#"):
            continue

        if fmt == "auto":
            row_obj, err_msg = _parse_auto_line(s, delimiter=delim)
            if err_msg or row_obj is None:
                errors.append({"line": line_no, "error": err_msg or "解析失败"})
                continue
            if row_obj["email"] in seen_emails:
                continue
            seen_emails.add(row_obj["email"])
            valid_rows.append(row_obj)
            continue

        cols = _split_import_line(s, delim)

        if fmt in ("outlook_email_pass_cid_rt", "outlook_email_pass_rt_cid"):
            if len(cols) < 4:
                errors.append({"line": line_no, "error": f"列数不足（需要 4 列，当前 {len(cols)} 列）"})
                continue
            email_addr = cols[0].lower()
            password = cols[1]
            if fmt == "outlook_email_pass_cid_rt":
                client_id, refresh_token = cols[2], cols[3]
            else:
                refresh_token, client_id = cols[2], cols[3]
            if not _EMAIL_RE.fullmatch(email_addr):
                errors.append({"line": line_no, "error": f"第 1 列不是合法邮箱: {email_addr[:40]}"})
                continue
            if not _UUID_RE.fullmatch(client_id):
                hint = "（检测到 refresh_token 位置疑似 UUID，可能选反了四段式顺序）" if _UUID_RE.fullmatch(refresh_token) else ""
                errors.append({"line": line_no, "error": f"client_id 不是合法 UUID 格式{hint}"})
                continue
            if len(refresh_token) < 20 or _UUID_RE.fullmatch(refresh_token):
                errors.append({"line": line_no, "error": "refresh_token 长度过短或格式异常"})
                continue
            if email_addr in seen_emails:
                continue
            seen_emails.add(email_addr)
            valid_rows.append({
                "email": email_addr,
                "provider_kind": "outlook_oauth",
                "protocol": "unknown",
                "password": password,
                "client_id": client_id,
                "refresh_token": refresh_token,
                "imap_host": "",
                "imap_port": 993,
            })

        elif fmt == "outlook_email_cid_rt":
            if len(cols) < 3:
                errors.append({"line": line_no, "error": f"列数不足（需要 3 列，当前 {len(cols)} 列）"})
                continue
            email_addr, client_id, refresh_token = cols[0].lower(), cols[1], cols[2]
            if not _EMAIL_RE.fullmatch(email_addr):
                errors.append({"line": line_no, "error": f"第 1 列不是合法邮箱: {email_addr[:40]}"})
                continue
            if not _UUID_RE.fullmatch(client_id):
                errors.append({"line": line_no, "error": "第 2 列 client_id 不是合法 UUID 格式"})
                continue
            if len(refresh_token) < 20 or _UUID_RE.fullmatch(refresh_token):
                errors.append({"line": line_no, "error": "第 3 列 refresh_token 长度过短或格式异常"})
                continue
            if email_addr in seen_emails:
                continue
            seen_emails.add(email_addr)
            valid_rows.append({
                "email": email_addr,
                "provider_kind": "outlook_oauth",
                "protocol": "unknown",
                "password": "",
                "client_id": client_id,
                "refresh_token": refresh_token,
                "imap_host": "",
                "imap_port": 993,
            })

        elif fmt == "imap_email_pass_host":
            if len(cols) < 3:
                errors.append({"line": line_no, "error": f"列数不足（需要 3 列：邮箱、密码、IMAP服务器）"})
                continue
            email_addr, password, host_spec = cols[0].lower(), cols[1], cols[2]
            if not _EMAIL_RE.fullmatch(email_addr):
                errors.append({"line": line_no, "error": f"第 1 列不是合法邮箱: {email_addr[:40]}"})
                continue
            if not password:
                errors.append({"line": line_no, "error": "IMAP 授权码/密码不能为空"})
                continue
            imap_host = host_spec
            imap_port = 993
            if ":" in host_spec:
                h_part, p_part = host_spec.rsplit(":", 1)
                if not p_part.isdigit() or not (1 <= int(p_part) <= 65535):
                    errors.append({"line": line_no, "error": f"IMAP 端口非法: {host_spec[:40]}"})
                    continue
                imap_host, imap_port = h_part.strip(), int(p_part)
            if not imap_host or "." not in imap_host:
                errors.append({"line": line_no, "error": f"IMAP 主机名非法: {host_spec[:40]}"})
                continue
            if email_addr in seen_emails:
                continue
            seen_emails.add(email_addr)
            valid_rows.append({
                "email": email_addr,
                "provider_kind": "imap",
                "protocol": "imap_plain",
                "password": password,
                "client_id": "",
                "refresh_token": "",
                "imap_host": imap_host,
                "imap_port": imap_port,
                "link_url": "",
            })

        elif fmt == "icloud_email_link":
            # 仅按首个分隔符切分，防止接码 URL 参数内包含分隔符时被误拆
            if delim == "auto":
                pair = None
                for sep in ("----", "\t", "|", ","):
                    if sep in s:
                        pair = [seg.strip() for seg in s.split(sep, 1)]
                        break
                pair = pair if pair is not None else [s]
            else:
                pair = [seg.strip() for seg in s.split(delim, 1)]
            if len(pair) < 2:
                errors.append({"line": line_no, "error": "列数不足（需要 2 列：邮箱、https://接码链接）"})
                continue
            email_addr, raw_link = pair[0].lower(), pair[1]
            if not _EMAIL_RE.fullmatch(email_addr):
                errors.append({"line": line_no, "error": f"第 1 列不是合法邮箱: {email_addr[:40]}"})
                continue
            try:
                clean_link = validate_public_http_url(raw_link, resolve_dns=False)
            except ValueError as exc:
                errors.append({"line": line_no, "error": f"接码链接安全校验未通过: {sanitize_error_text(str(exc))}"})
                continue
            if email_addr in seen_emails:
                continue
            seen_emails.add(email_addr)
            valid_rows.append({
                "email": email_addr,
                "provider_kind": "icloud_link",
                "protocol": "icloud_link",
                "password": "",
                "client_id": "",
                "refresh_token": "",
                "imap_host": "",
                "imap_port": 993,
                "link_url": clean_link,
            })

    return valid_rows, errors


_DEFAULT_LOCK_STRIPES = 64
_MAX_MSG_CACHE_ENTRIES = 60
_MSG_CACHE_TTL_S = 180.0
_MAX_INBOX_CACHE_ENTRIES = 16
_INBOX_CACHE_TTL_S = 20.0
_MAX_CACHED_HTML_CHARS = 200_000
_MAX_CACHED_TEXT_CHARS = 100_000


class BuiltinMailStore:
    """内置 SQLite 邮箱池与 Outlook/IMAP/iCloud 收信引擎（有界内存缓存 + 固定分段锁）。"""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_BUILTIN_MAIL_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # 固定 64 段锁：恒定 O(1) 内存占用，海量邮箱长期运行零字典膨胀
        self._token_locks: tuple[threading.Lock, ...] = tuple(
            threading.Lock() for _ in range(_DEFAULT_LOCK_STRIPES)
        )
        self._msg_cache_lock = threading.Lock()
        self._msg_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._inbox_cache: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}
        self._init_schema()

    def _get_token_lock(self, email_low: str) -> threading.Lock:
        idx = int(hashlib.md5(email_low.encode("utf-8")).hexdigest()[:8], 16) % _DEFAULT_LOCK_STRIPES
        return self._token_locks[idx]

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=15.0,
            isolation_level=None,  # 显式事务控制，支持 BEGIN IMMEDIATE
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute("PRAGMA cache_size=-2000;")  # 限制单连接页缓存 <= 2MB
            if write:
                conn.execute("BEGIN IMMEDIATE;")
            yield conn
            if write and conn.in_transaction:
                conn.execute("COMMIT;")
        except Exception:
            if write and conn.in_transaction:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect(write=False) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
        with self._connect(write=True) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mail_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    provider_kind TEXT NOT NULL DEFAULT 'outlook_oauth',
                    protocol TEXT NOT NULL DEFAULT 'unknown',
                    password_enc TEXT NOT NULL DEFAULT '',
                    client_id TEXT NOT NULL DEFAULT '',
                    refresh_token_enc TEXT NOT NULL DEFAULT '',
                    access_token_enc TEXT NOT NULL DEFAULT '',
                    token_expires_at REAL NOT NULL DEFAULT 0,
                    imap_host TEXT NOT NULL DEFAULT '',
                    imap_port INTEGER NOT NULL DEFAULT 993,
                    link_url_enc TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'unknown',
                    leased_by TEXT NOT NULL DEFAULT '',
                    lease_expires_at REAL NOT NULL DEFAULT 0,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    last_checked_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(mail_accounts)").fetchall()}
            if "link_url_enc" not in cols:
                conn.execute("ALTER TABLE mail_accounts ADD COLUMN link_url_enc TEXT NOT NULL DEFAULT '';")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mail_status_lease ON mail_accounts(status, lease_expires_at, use_count);"
            )

    @staticmethod
    def _row_to_public(row: sqlite3.Row, now: float | None = None) -> dict[str, Any]:
        ts_now = now if now is not None else time.time()
        lease_exp = float(row["lease_expires_at"] or 0.0)
        leased = bool(row["leased_by"] and lease_exp > ts_now)
        return {
            "id": int(row["id"]),
            "email": str(row["email"]),
            "provider_kind": str(row["provider_kind"]),
            "protocol": str(row["protocol"]),
            "client_id": str(row["client_id"]),
            "has_password": bool(row["password_enc"]),
            "has_refresh_token": bool(row["refresh_token_enc"]),
            "has_link_url": bool(row["link_url_enc"]),
            "imap_host": str(row["imap_host"]),
            "imap_port": int(row["imap_port"] or 993),
            "status": str(row["status"]),
            "leased": leased,
            "leased_by": str(row["leased_by"]) if leased else "",
            "lease_expires_at": lease_exp if leased else 0.0,
            "use_count": int(row["use_count"] or 0),
            "last_checked_at": float(row["last_checked_at"] or 0.0),
            "last_error": str(row["last_error"] or ""),
            "created_at": float(row["created_at"] or 0.0),
        }

    # ---------- 导入、列表、统计、删改 ----------

    def preview_import(
        self,
        text: str,
        fmt: str = "auto",
        *,
        delimiter: str = "auto",
        limit: int = 8,
    ) -> dict[str, Any]:
        rows, errors = parse_import_text(text, fmt=fmt, delimiter=delimiter)
        summary: dict[str, int] = {}
        for r in rows:
            lbl = str(
                r.get("detected_label")
                or (
                    "Outlook OAuth2"
                    if r["provider_kind"] == "outlook_oauth"
                    else ("iCloud 接码链接" if r["provider_kind"] == "icloud_link" else f"IMAP ({r['imap_host']})")
                )
            )
            summary[lbl] = summary.get(lbl, 0) + 1
        preview = [
            {
                "email": r["email"],
                "provider_kind": r["provider_kind"],
                "detected_label": str(
                    r.get("detected_label")
                    or (
                        "Outlook OAuth2"
                        if r["provider_kind"] == "outlook_oauth"
                        else ("iCloud 接码链接" if r["provider_kind"] == "icloud_link" else f"IMAP ({r['imap_host']})")
                    )
                ),
                "client_id": r["client_id"],
                "refresh_token_masked": mask_secret(r["refresh_token"]),
                "link_url_masked": mask_secret_url(r.get("link_url") or ""),
                "has_password": bool(r["password"]),
                "imap_host": f"{r['imap_host']}:{r['imap_port']}" if r["imap_host"] else "",
            }
            for r in rows[: max(1, min(limit, 20))]
        ]
        return {
            "format": fmt,
            "valid_count": len(rows),
            "error_count": len(errors),
            "summary": summary,
            "preview": preview,
            "errors": errors[:20],
        }

    def import_accounts(
        self,
        text: str,
        fmt: str = "auto",
        *,
        delimiter: str = "auto",
        overwrite_existing: bool = False,
    ) -> dict[str, Any]:
        rows, errors = parse_import_text(text, fmt=fmt, delimiter=delimiter)
        now = time.time()
        added = 0
        updated = 0
        skipped = 0

        with self._connect(write=True) as conn:
            for r in rows:
                pwd_enc = protect_secret(r["password"]) if r["password"] else ""
                rt_enc = protect_secret(r["refresh_token"]) if r["refresh_token"] else ""
                link_enc = protect_secret(r.get("link_url") or "") if r.get("link_url") else ""
                existing = conn.execute(
                    "SELECT id FROM mail_accounts WHERE email = ?", (r["email"],)
                ).fetchone()
                if existing:
                    if not overwrite_existing:
                        skipped += 1
                        continue
                    conn.execute(
                        """
                        UPDATE mail_accounts
                        SET provider_kind=?, protocol=?, password_enc=?, client_id=?,
                            refresh_token_enc=?, access_token_enc='', token_expires_at=0,
                            imap_host=?, imap_port=?, link_url_enc=?, status='unknown', last_error='', updated_at=?
                        WHERE id=?
                        """,
                        (
                            r["provider_kind"],
                            r["protocol"],
                            pwd_enc,
                            r["client_id"],
                            rt_enc,
                            r["imap_host"],
                            int(r["imap_port"] or 993),
                            link_enc,
                            now,
                            int(existing["id"]),
                        ),
                    )
                    updated += 1
                else:
                    conn.execute(
                        """
                        INSERT INTO mail_accounts (
                            email, provider_kind, protocol, password_enc, client_id,
                            refresh_token_enc, imap_host, imap_port, link_url_enc, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?)
                        """,
                        (
                            r["email"],
                            r["provider_kind"],
                            r["protocol"],
                            pwd_enc,
                            r["client_id"],
                            rt_enc,
                            r["imap_host"],
                            int(r["imap_port"] or 993),
                            link_enc,
                            now,
                            now,
                        ),
                    )
                    added += 1

        return {
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "imported_emails": [r["email"] for r in rows[:100]],
            "error_count": len(errors),
            "errors": errors[:20],
            "stats": self.get_stats(),
        }

    def get_stats(self) -> dict[str, Any]:
        now = time.time()
        with self._connect(write=False) as conn:
            rows = conn.execute(
                "SELECT status, protocol, provider_kind, leased_by, lease_expires_at FROM mail_accounts"
            ).fetchall()
        total = len(rows)
        valid = sum(1 for r in rows if r["status"] == "valid")
        unknown = sum(1 for r in rows if r["status"] == "unknown")
        dead = sum(1 for r in rows if r["status"] == "dead")
        leased = sum(
            1 for r in rows if r["leased_by"] and float(r["lease_expires_at"] or 0) > now
        )
        available = sum(
            1
            for r in rows
            if r["status"] == "valid"
            and not (r["leased_by"] and float(r["lease_expires_at"] or 0) > now)
        )
        by_protocol: dict[str, int] = {}
        by_provider: dict[str, int] = {"outlook_oauth": 0, "icloud_link": 0, "imap": 0}
        for r in rows:
            p = str(r["protocol"] or "unknown")
            by_protocol[p] = by_protocol.get(p, 0) + 1
            pk = str(r["provider_kind"] or "outlook_oauth")
            by_provider[pk] = by_provider.get(pk, 0) + 1
        return {
            "total": total,
            "valid": valid,
            "unknown": unknown,
            "dead": dead,
            "leased": leased,
            "available": available,
            "by_protocol": by_protocol,
            "by_provider": by_provider,
        }

    def list_accounts(
        self,
        *,
        status: str = "all",
        provider_kind: str = "all",
        query: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if provider_kind and provider_kind != "all":
            clauses.append("provider_kind = ?")
            params.append(provider_kind)
        if query.strip():
            clauses.append("(email LIKE ? OR last_error LIKE ? OR protocol LIKE ?)")
            pat = f"%{query.strip()}%"
            params.extend([pat, pat, pat])
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        now = time.time()
        with self._connect(write=False) as conn:
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM mail_accounts {where_sql}", params).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT * FROM mail_accounts
                {where_sql}
                ORDER BY
                    CASE status WHEN 'valid' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END,
                    use_count ASC,
                    id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, max(1, min(limit, 1000)), max(0, offset)],
            ).fetchall()
        return {
            "total": total,
            "items": [self._row_to_public(r, now=now) for r in rows],
            "stats": self.get_stats(),
        }

    def get_available_emails(self, *, valid_only: bool = True, available_only: bool = True) -> list[str]:
        now = time.time()
        clauses: list[str] = []
        params: list[Any] = []
        if valid_only:
            clauses.append("status = 'valid'")
        else:
            clauses.append("status != 'dead'")
        if available_only:
            clauses.append("(leased_by = '' OR lease_expires_at <= ?)")
            params.append(now)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect(write=False) as conn:
            rows = conn.execute(
                f"SELECT email FROM mail_accounts {where_sql} ORDER BY use_count ASC, id ASC",
                params,
            ).fetchall()
        return [str(r["email"]) for r in rows]

    def delete_accounts(self, emails: list[str]) -> int:
        clean = [e.strip().lower() for e in emails if e and e.strip()]
        if not clean:
            return 0
        placeholders = ",".join("?" for _ in clean)
        with self._connect(write=True) as conn:
            cur = conn.execute(
                f"DELETE FROM mail_accounts WHERE LOWER(email) IN ({placeholders})", clean
            )
            return int(cur.rowcount or 0)

    def cleanup_dead_accounts(self) -> int:
        """一键移除已确认失效（status='dead'）的邮箱账号。"""
        with self._connect(write=True) as conn:
            cur = conn.execute("DELETE FROM mail_accounts WHERE status = 'dead'")
            return int(cur.rowcount or 0)

    def reset_accounts(self, emails: list[str] | None = None) -> int:
        now = time.time()
        with self._connect(write=True) as conn:
            if emails:
                clean = [e.strip().lower() for e in emails if e and e.strip()]
                if not clean:
                    return 0
                placeholders = ",".join("?" for _ in clean)
                cur = conn.execute(
                    f"""
                    UPDATE mail_accounts
                    SET status='unknown',
                        protocol=CASE
                            WHEN provider_kind='imap' THEN 'imap_plain'
                            WHEN provider_kind='icloud_link' THEN 'icloud_link'
                            ELSE 'unknown'
                        END,
                        leased_by='', lease_expires_at=0, last_error='', updated_at=?
                    WHERE LOWER(email) IN ({placeholders})
                    """,
                    [now, *clean],
                )
                return int(cur.rowcount or 0)
            cur = conn.execute(
                """
                UPDATE mail_accounts
                SET leased_by='', lease_expires_at=0, updated_at=?
                WHERE leased_by != ''
                """,
                (now,),
            )
            return int(cur.rowcount or 0)

    # ---------- 事务租约（防多并发撞号） ----------

    def lease_account(self, owner: str, *, ttl_s: int = 900, include_unknown: bool = False) -> dict[str, Any] | None:
        owner_s = (owner or "worker").strip()[:80]
        now = time.time()
        exp = now + max(30, min(int(ttl_s), 7200))
        status_cond = "status IN ('valid', 'unknown')" if include_unknown else "status = 'valid'"

        with self._connect(write=True) as conn:
            # 回收过期租约
            conn.execute(
                "UPDATE mail_accounts SET leased_by='', lease_expires_at=0 WHERE leased_by != '' AND lease_expires_at <= ?",
                (now,),
            )
            row = conn.execute(
                f"""
                SELECT * FROM mail_accounts
                WHERE {status_cond} AND leased_by = ''
                ORDER BY CASE status WHEN 'valid' THEN 0 ELSE 1 END, use_count ASC, id ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """
                UPDATE mail_accounts
                SET leased_by=?, lease_expires_at=?, use_count=use_count+1, updated_at=?
                WHERE id=?
                """,
                (owner_s, exp, now, int(row["id"])),
            )
            updated = conn.execute("SELECT * FROM mail_accounts WHERE id=?", (int(row["id"]),)).fetchone()
            return self._row_to_public(updated, now=now) if updated else None

    def release_lease(self, email: str, *, owner: str = "", mark_status: str | None = None) -> bool:
        em = (email or "").strip().lower()
        if not em:
            return False
        now = time.time()
        with self._connect(write=True) as conn:
            if owner:
                row = conn.execute(
                    "SELECT id, leased_by FROM mail_accounts WHERE LOWER(email)=?", (em,)
                ).fetchone()
                if not row:
                    return False
                if row["leased_by"] and row["leased_by"] != owner:
                    return False
            if mark_status in ("valid", "dead", "unknown"):
                cur = conn.execute(
                    "UPDATE mail_accounts SET leased_by='', lease_expires_at=0, status=?, updated_at=? WHERE LOWER(email)=?",
                    (mark_status, now, em),
                )
            else:
                cur = conn.execute(
                    "UPDATE mail_accounts SET leased_by='', lease_expires_at=0, updated_at=? WHERE LOWER(email)=?",
                    (now, em),
                )
            return int(cur.rowcount or 0) > 0

    # ---------- OAuth2 Token 刷新与协议验活 ----------

    def _request_ms_token(self, client_id: str, refresh_token: str, scope: str) -> dict[str, Any]:
        last_err: OAuthRefreshError | None = None
        for idx, endpoint in enumerate(_MS_TOKEN_ENDPOINTS):
            form = {
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
            if scope:
                form["scope"] = scope
            body = urllib.parse.urlencode(form).encode("utf-8")
            req = urllib.request.Request(
                endpoint,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw_body = resp.read().decode("utf-8", errors="replace")
                    data = _parse_external_json_dict(raw_body, what="Microsoft OAuth2 Token")
                    if not data.get("access_token"):
                        raise OAuthRefreshError("微软令牌响应缺少 access_token", permanent=False)
                    return data
            except urllib.error.HTTPError as he:
                raw = ""
                try:
                    raw = he.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                low = raw.lower()
                err_code = ""
                err_desc = ""
                try:
                    j = json.loads(raw)
                    err_code = str(j.get("error") or "")
                    err_desc = str(j.get("error_description") or "")
                except Exception:
                    err_desc = raw[:180]
                msg = sanitize_error_text(f"HTTP {he.code} {err_code}: {err_desc}")
                # consumers 端点若报租户/应用不匹配，允许试一次 common
                if idx == 0 and any(k in low for k in ("aadsts50020", "aadsts700016", "aadsts90023")):
                    last_err = OAuthRefreshError(msg, permanent=False)
                    continue
                is_scope = any(k in low for k in _SCOPE_MISMATCH_HINTS)
                is_perm = (not is_scope) and any(k in low for k in _PERMANENT_OAUTH_ERRORS)
                raise OAuthRefreshError(msg, permanent=is_perm, scope_mismatch=is_scope) from he
            except (urllib.error.URLError, TimeoutError, OSError) as ne:
                raise OAuthRefreshError(
                    sanitize_error_text(f"网络异常: {type(ne).__name__}: {ne}"),
                    permanent=False,
                ) from ne
        raise last_err or OAuthRefreshError("无法获取 Outlook OAuth2 令牌", permanent=False)

    def _get_row_by_email(self, email: str) -> sqlite3.Row | None:
        with self._connect(write=False) as conn:
            return conn.execute(
                "SELECT * FROM mail_accounts WHERE LOWER(email)=?", (email.strip().lower(),)
            ).fetchone()

    def _ensure_outlook_access_token(self, email: str, protocol: str, *, force_refresh: bool = False) -> str:
        em = email.strip().lower()
        scope = _GRAPH_SCOPE if protocol == "graph" else _IMAP_SCOPE
        with self._get_token_lock(em):
            row = self._get_row_by_email(em)
            if not row:
                raise ValueError(f"内置邮箱池未找到账号: {em}")
            now = time.time()
            if not force_refresh and row["access_token_enc"] and float(row["token_expires_at"] or 0) > now + 60:
                if row["protocol"] == protocol:
                    try:
                        return unprotect_secret(str(row["access_token_enc"]))
                    except SecretProtectionError:
                        pass
            client_id = str(row["client_id"] or "").strip()
            refresh_token = unprotect_secret(str(row["refresh_token_enc"] or ""))
            if not client_id or not refresh_token:
                raise OAuthRefreshError("缺少 client_id 或 refresh_token", permanent=True)

            tok = self._request_ms_token(client_id, refresh_token, scope)
            access_token = str(tok["access_token"])
            expires_in = max(60, int(tok.get("expires_in") or 3600))
            new_rt = str(tok.get("refresh_token") or "").strip()

            at_enc = protect_secret(access_token)
            rt_enc = protect_secret(new_rt) if new_rt else str(row["refresh_token_enc"])
            with self._connect(write=True) as conn:
                conn.execute(
                    """
                    UPDATE mail_accounts
                    SET access_token_enc=?, token_expires_at=?, refresh_token_enc=?, updated_at=?
                    WHERE id=?
                    """,
                    (at_enc, now + expires_in, rt_enc, now, int(row["id"])),
                )
            return access_token

    def _graph_get_messages(self, access_token: str, top: int = 10) -> list[dict[str, Any]]:
        url = (
            "https://graph.microsoft.com/v1.0/me/messages"
            f"?$top={max(1, min(top, 30))}&$orderby=receivedDateTime%20desc"
            "&$select=id,subject,from,receivedDateTime,bodyPreview,body"
        )
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_body = resp.read().decode("utf-8", errors="replace")
            data = _parse_external_json_dict(raw_body, what="Microsoft Graph Messages")
        out: list[dict[str, Any]] = []
        for m in data.get("value") or []:
            if not isinstance(m, dict):
                continue
            body_obj = m.get("body") if isinstance(m.get("body"), dict) else {}
            ctype = str(body_obj.get("contentType") or "").lower()
            content = str(body_obj.get("content") or "")
            from_obj = m.get("from") if isinstance(m.get("from"), dict) else {}
            from_addr = ((from_obj.get("emailAddress") or {}) if isinstance(from_obj.get("emailAddress"), dict) else {}).get("address", "")
            out.append({
                "id": str(m.get("id") or ""),
                "from": str(from_addr or ""),
                "subject": str(m.get("subject") or ""),
                "receivedAt": str(m.get("receivedDateTime") or ""),
                "excerpt": str(m.get("bodyPreview") or "")[:240],
                "html": content if ctype == "html" else "",
                "text": content if ctype != "html" else str(m.get("bodyPreview") or ""),
            })
        return out

    def _imap_open(self, host: str, port: int, email: str, *, password: str = "", access_token: str = "") -> tuple[imaplib.IMAP4_SSL, int]:
        client = imaplib.IMAP4_SSL(host, int(port or 993), timeout=15)
        try:
            if access_token:
                auth_str = f"user={email}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")
                client.authenticate("XOAUTH2", lambda _challenge: auth_str)
            else:
                client.login(email, password)
            typ, sel_data = client.select("INBOX", readonly=True)
            if typ != "OK":
                raise RuntimeError("IMAP 无法打开 INBOX")
            exists = 0
            if sel_data and sel_data[0]:
                try:
                    exists = int(sel_data[0].decode("ascii", errors="ignore").strip())
                except Exception:
                    exists = 0
            return client, exists
        except Exception:
            try:
                client.logout()
            except Exception:
                pass
            raise

    def _imap_fetch_messages(
        self,
        host: str,
        port: int,
        email: str,
        *,
        password: str = "",
        access_token: str = "",
        top: int = 10,
    ) -> list[dict[str, Any]]:
        opened = self._imap_open(host, port, email, password=password, access_token=access_token)
        # 兼容单测 monkeypatch _imap_open 直接返回 client 的写法
        if isinstance(opened, tuple):
            client, exists = opened
        else:
            client, exists = opened, 0
        try:
            limit_n = max(1, min(top, 20))
            if exists > 0:
                start_seq = max(1, exists - limit_n + 1)
                seq_spec = f"{start_seq}:{exists}"
            else:
                typ, data = client.search(None, "ALL")
                if typ != "OK" or not data or not data[0]:
                    return []
                ids = data[0].split()
                recent = ids[-limit_n:]
                seq_spec = b",".join(recent).decode("ascii", errors="ignore")
                if not seq_spec:
                    return []

            # 单次批量 FETCH 替代逐封串行往返，大幅降低跨境 IMAP RTT 耗时
            ftyp, msg_data = client.fetch(seq_spec, "(UID BODY.PEEK[])")
            if ftyp != "OK" or not msg_data:
                return []
            out: list[dict[str, Any]] = []
            seq_idx = 0
            for item in msg_data:
                if not (isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray))):
                    continue
                seq_idx += 1
                header_bytes = item[0] if isinstance(item[0], (bytes, bytearray)) else b""
                m_uid = re.search(rb"UID\s+(\d+)", bytes(header_bytes), re.I)
                m_seq = re.match(rb"\s*(\d+)", bytes(header_bytes))
                uid = (
                    m_uid.group(1).decode("ascii")
                    if m_uid
                    else (m_seq.group(1).decode("ascii") if m_seq else str(seq_idx))
                )
                out.append(_parse_rfc822_message(uid, bytes(item[1])))
            out.reverse()
            return out
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def verify_account(self, email: str) -> dict[str, Any]:
        """显式检测并锁定单个账号的真实可用协议（graph / imap_xoauth2 / imap_plain / dead）。"""
        em = email.strip().lower()
        row = self._get_row_by_email(em)
        if not row:
            return {"email": em, "ok": False, "status": "missing", "error": "账号不存在"}
        now = time.time()
        kind = str(row["provider_kind"])

        def _commit_status(status: str, protocol: str, err: str = "") -> dict[str, Any]:
            clean_err = sanitize_error_text(err)
            with self._connect(write=True) as conn:
                conn.execute(
                    """
                    UPDATE mail_accounts
                    SET status=?, protocol=?, last_checked_at=?, last_error=?, updated_at=?
                    WHERE id=?
                    """,
                    (status, protocol, now, clean_err, now, int(row["id"])),
                )
            return {
                "email": em,
                "ok": status == "valid",
                "status": status,
                "protocol": protocol,
                "error": clean_err,
            }

        if kind == "imap":
            try:
                pwd = unprotect_secret(str(row["password_enc"] or ""))
                opened = self._imap_open(str(row["imap_host"]), int(row["imap_port"] or 993), em, password=pwd)
                c = opened[0] if isinstance(opened, tuple) else opened
                try:
                    c.logout()
                except Exception:
                    pass
                return _commit_status("valid", "imap_plain", "")
            except imaplib.IMAP4.error as ie:
                return _commit_status("dead", "imap_plain", f"IMAP 鉴权失败: {ie}")
            except Exception as e:
                # 瞬时网络错误不覆写已有 valid 状态
                prev = str(row["status"] or "unknown")
                return _commit_status(prev, "imap_plain", f"IMAP 连通异常: {e}")

        if kind == "icloud_link":
            try:
                link_url = unprotect_secret(str(row["link_url_enc"] or ""))
                if not link_url:
                    return _commit_status("dead", "icloud_link", "接码链接缺失")
                _fetch_icloud_link_messages(link_url, top=1)
                return _commit_status("valid", "icloud_link", "")
            except (PrivateAddressBlockedError, ValueError) as ve:
                return _commit_status("dead", "icloud_link", f"接码链接被拒绝: {ve}")
            except urllib.error.HTTPError as he:
                if he.code in (401, 403, 404, 410):
                    return _commit_status("dead", "icloud_link", f"接码链接已失效 (HTTP {he.code})")
                prev = str(row["status"] or "unknown")
                return _commit_status(prev, "icloud_link", f"接码链接暂不可用 (HTTP {he.code})")
            except Exception as e:
                prev = str(row["status"] or "unknown")
                return _commit_status(prev, "icloud_link", f"接码链接连通异常: {e}")

        # Outlook OAuth2：显式探测 Graph，仅在 Graph scope 不足时探测 IMAP XOAUTH2
        graph_err = ""
        try:
            tok = self._ensure_outlook_access_token(em, "graph", force_refresh=True)
            self._graph_get_messages(tok, top=1)
            return _commit_status("valid", "graph", "")
        except OAuthRefreshError as oe:
            graph_err = str(oe)
            if oe.permanent:
                return _commit_status("dead", "unknown", graph_err)
            if not oe.scope_mismatch:
                prev = str(row["status"] or "unknown")
                return _commit_status(prev, str(row["protocol"] or "unknown"), graph_err)
        except urllib.error.HTTPError as he:
            raw = ""
            try:
                raw = he.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            graph_err = sanitize_error_text(f"Graph HTTP {he.code}: {raw[:140]}")
            if he.code not in (401, 403):
                prev = str(row["status"] or "unknown")
                return _commit_status(prev, str(row["protocol"] or "unknown"), graph_err)
        except Exception as e:
            prev = str(row["status"] or "unknown")
            return _commit_status(prev, str(row["protocol"] or "unknown"), f"Graph 探活异常: {e}")

        # Graph 明确返回权限/scope 不足 -> 显式探测 Outlook IMAP XOAUTH2 并固化
        try:
            tok_imap = self._ensure_outlook_access_token(em, "imap_xoauth2", force_refresh=True)
            opened = self._imap_open("outlook.office365.com", 993, em, access_token=tok_imap)
            c = opened[0] if isinstance(opened, tuple) else opened
            try:
                c.logout()
            except Exception:
                pass
            return _commit_status("valid", "imap_xoauth2", "")
        except OAuthRefreshError as oe2:
            if oe2.permanent or oe2.scope_mismatch:
                return _commit_status("dead", "unknown", f"Graph 与 IMAP 均无权限: {oe2}")
            prev = str(row["status"] or "unknown")
            return _commit_status(prev, str(row["protocol"] or "unknown"), str(oe2))
        except imaplib.IMAP4.error as ie2:
            return _commit_status("dead", "unknown", f"Graph 失败 ({graph_err})；IMAP XOAUTH2 拒绝: {ie2}")
        except Exception as e2:
            prev = str(row["status"] or "unknown")
            return _commit_status(prev, str(row["protocol"] or "unknown"), f"IMAP XOAUTH2 探活异常: {e2}")

    def verify_batch(self, emails: list[str] | None = None, *, limit: int = 30, max_workers: int = 6) -> dict[str, Any]:
        if emails:
            targets = [e.strip().lower() for e in emails if e and e.strip()][:limit]
        else:
            with self._connect(write=False) as conn:
                rows = conn.execute(
                    """
                    SELECT email FROM mail_accounts
                    WHERE status != 'dead'
                    ORDER BY CASE status WHEN 'unknown' THEN 0 ELSE 1 END, last_checked_at ASC
                    LIMIT ?
                    """,
                    (max(1, min(limit, 100)),),
                ).fetchall()
            targets = [str(r["email"]) for r in rows]

        results: list[dict[str, Any]] = []
        if targets:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as ex:
                results = list(ex.map(self.verify_account, targets))

        return {
            "checked": len(results),
            "valid": sum(1 for r in results if r.get("status") == "valid"),
            "dead": sum(1 for r in results if r.get("status") == "dead"),
            "results": results,
            "stats": self.get_stats(),
        }

    # ---------- 实时收信（严格按已验通协议执行） ----------

    def list_messages(self, email: str, *, top: int = 10, since: float = 0.0) -> list[dict[str, Any]]:
        em = email.strip().lower()
        row = self._get_row_by_email(em)
        if not row:
            raise ValueError(f"内置邮箱池中不存在该账号: {em}")
        if row["status"] == "dead":
            raise RuntimeError(f"账号 {em} 已标记 dead: {row['last_error']}")

        protocol = str(row["protocol"] or "unknown")
        if protocol == "unknown":
            v = self.verify_account(em)
            if not v.get("ok"):
                raise RuntimeError(f"账号 {em} 协议验活失败: {v.get('error')}")
            row = self._get_row_by_email(em)
            assert row is not None
            protocol = str(row["protocol"])

        if protocol == "graph":
            try:
                tok = self._ensure_outlook_access_token(em, "graph")
                msgs = self._graph_get_messages(tok, top=top)
            except urllib.error.HTTPError as he:
                if he.code == 401:
                    tok = self._ensure_outlook_access_token(em, "graph", force_refresh=True)
                    msgs = self._graph_get_messages(tok, top=top)
                else:
                    raise
        elif protocol == "imap_xoauth2":
            tok = self._ensure_outlook_access_token(em, "imap_xoauth2")
            try:
                msgs = self._imap_fetch_messages("outlook.office365.com", 993, em, access_token=tok, top=top)
            except imaplib.IMAP4.error:
                tok = self._ensure_outlook_access_token(em, "imap_xoauth2", force_refresh=True)
                msgs = self._imap_fetch_messages("outlook.office365.com", 993, em, access_token=tok, top=top)
        elif protocol == "imap_plain":
            pwd = unprotect_secret(str(row["password_enc"] or ""))
            msgs = self._imap_fetch_messages(
                str(row["imap_host"]), int(row["imap_port"] or 993), em, password=pwd, top=top
            )
        elif protocol == "icloud_link":
            link_url = unprotect_secret(str(row["link_url_enc"] or ""))
            if not link_url:
                raise RuntimeError(f"账号 {em} 缺少 iCloud 接码链接")
            msgs = _fetch_icloud_link_messages(link_url, top=top)
        else:
            raise RuntimeError(f"账号 {em} 协议状态异常: {protocol}")

        now = time.monotonic()
        with self._msg_cache_lock:
            # 严格有界缓存（最多 60 封 / 180s TTL / 截断超大 HTML），防止长驻内存膨胀
            self._msg_cache = {
                k: v for k, v in self._msg_cache.items() if now - v[0] < _MSG_CACHE_TTL_S
            }
            for m in msgs:
                if m.get("id"):
                    slim = dict(m)
                    if isinstance(slim.get("html"), str) and len(slim["html"]) > _MAX_CACHED_HTML_CHARS:
                        slim["html"] = slim["html"][:_MAX_CACHED_HTML_CHARS]
                    if isinstance(slim.get("text"), str) and len(slim["text"]) > _MAX_CACHED_TEXT_CHARS:
                        slim["text"] = slim["text"][:_MAX_CACHED_TEXT_CHARS]
                    self._msg_cache[(em, str(m["id"]))] = (now, slim)
            if len(self._msg_cache) > _MAX_MSG_CACHE_ENTRIES:
                by_age = sorted(self._msg_cache.items(), key=lambda kv: kv[1][0], reverse=True)
                self._msg_cache = dict(by_age[:_MAX_MSG_CACHE_ENTRIES])

        if since and float(since) > 0.0:
            floor = float(since)
            msgs = [m for m in msgs if _coerce_timestamp(m.get("receivedAt")) >= floor]
        return msgs

    def get_message(self, email: str, msg_id: str) -> dict[str, Any]:
        em = email.strip().lower()
        mid = str(msg_id)
        with self._msg_cache_lock:
            hit = self._msg_cache.get((em, mid))
            if hit is not None:
                return hit[1]
        msgs = self.list_messages(em, top=10)
        for m in msgs:
            if str(m.get("id")) == mid:
                return m
        return {}

    @staticmethod
    def _extract_cands(subj: str, txt: str, htm: str) -> list[dict[str, Any]]:
        extractor = None
        try:
            from .mail import extract_candidates as _ext  # type: ignore[attr-defined]
            extractor = _ext
        except Exception:
            extractor = None

        if callable(extractor):
            return [
                {"kind": c.kind, "value": c.value, "confidence": round(float(c.confidence), 2)}
                for c in extractor(subj, txt, htm)[:6]
            ]
        plain = f"{subj}\n{txt}\n{_strip_html_excerpt(htm)}"
        cands: list[dict[str, Any]] = []
        for m_code in re.finditer(r"(?<!\d)(\d{4,8})(?!\d)", plain):
            val = m_code.group(1)
            if not re.fullmatch(r"20[23]\d", val):
                cands.append({"kind": "numeric", "value": val, "confidence": 0.8})
                if len(cands) >= 6:
                    break
        return cands

    @staticmethod
    def _format_ts(ts_raw: Any) -> str:
        if isinstance(ts_raw, (int, float)) and ts_raw > 0:
            return datetime.fromtimestamp(ts_raw).strftime("%Y-%m-%d %H:%M:%S")
        return str(ts_raw or "")

    def inspect_inbox(self, email: str, *, top: int = 8, force: bool = False) -> dict[str, Any]:
        """给前端抽屉返回脱敏收件箱列表及自动提取出的验证码/链接候选（20s 短期热缓存，点刷新传 force=True 穿透）。"""
        em = email.strip().lower()
        cache_key = (em, int(top))
        now_m = time.monotonic()
        cached_msgs: list[dict[str, Any]] | None = None
        if not force:
            with self._msg_cache_lock:
                hit = self._inbox_cache.get(cache_key)
                if hit is not None and (now_m - hit[0]) < 20.0:
                    cached_msgs = hit[1]

        t0 = time.perf_counter()
        msgs = cached_msgs if cached_msgs is not None else self.list_messages(em, top=top)
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        if cached_msgs is None:
            with self._msg_cache_lock:
                cur_m = time.monotonic()
                self._inbox_cache = {
                    k: v for k, v in self._inbox_cache.items() if cur_m - v[0] < _INBOX_CACHE_TTL_S
                }
                self._inbox_cache[cache_key] = (cur_m, msgs)
                if len(self._inbox_cache) > _MAX_INBOX_CACHE_ENTRIES:
                    by_age = sorted(self._inbox_cache.items(), key=lambda kv: kv[1][0], reverse=True)
                    self._inbox_cache = dict(by_age[:_MAX_INBOX_CACHE_ENTRIES])

        items: list[dict[str, Any]] = []
        for m in msgs:
            subj = str(m.get("subject") or "")
            txt = str(m.get("text") or "")
            htm = str(m.get("html") or "")
            items.append({
                "id": str(m.get("id") or ""),
                "from": str(m.get("from") or ""),
                "subject": subj,
                "received_at": self._format_ts(m.get("receivedAt")),
                "excerpt": str(m.get("excerpt") or "")[:240],
                "has_html": bool(htm),
                "candidates": self._extract_cands(subj, txt, htm),
            })
        return {
            "email": em,
            "count": len(items),
            "cached": cached_msgs is not None,
            "elapsed_ms": elapsed_ms,
            "messages": items,
        }

    def inspect_message(self, email: str, msg_id: str) -> dict[str, Any]:
        """返回单封原邮件完整正文（HTML + 纯文本 + 候选验证码/链接），优先 0ms 命中内存缓存。"""
        m = self.get_message(email, msg_id)
        if not m:
            raise KeyError(f"未找到邮件 ID={msg_id}")
        subj = str(m.get("subject") or "")
        txt = str(m.get("text") or "")[:200_000]
        htm = str(m.get("html") or "")[:300_000]
        return {
            "id": str(m.get("id") or msg_id),
            "email": email.strip().lower(),
            "from": str(m.get("from") or ""),
            "subject": subj,
            "received_at": self._format_ts(m.get("receivedAt")),
            "date": str(m.get("date") or ""),
            "excerpt": str(m.get("excerpt") or ""),
            "text": txt,
            "html": htm,
            "candidates": self._extract_cands(subj, txt, htm),
        }


_DEFAULT_MAIL_STORE: BuiltinMailStore | None = None
_DEFAULT_MAIL_STORE_LOCK = threading.Lock()


def get_builtin_mail_store(db_path: Path | str | None = None) -> BuiltinMailStore:
    if db_path is not None:
        return BuiltinMailStore(db_path)
    global _DEFAULT_MAIL_STORE
    with _DEFAULT_MAIL_STORE_LOCK:
        if _DEFAULT_MAIL_STORE is None:
            _DEFAULT_MAIL_STORE = BuiltinMailStore()
        return _DEFAULT_MAIL_STORE
