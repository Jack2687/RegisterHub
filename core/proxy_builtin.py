"""纯 Python 统一内置代理池（静态代理 + 动态住宅网关 + 动态 API 提取链接 + 受控订阅节点）。

设计原则（吸收 Codex 评审意见）：
1. SQLite WAL + busy_timeout 存储，所有代理账密与提取/订阅链接均经 core.secrets (Windows DPAPI) 加密落库；
2. 对用户彻底隐藏底层本地端口概念：支持直接粘贴静态代理、配置动态住宅代理网关、粘贴短效 API 提取链接自动补货、以及挂载受控订阅节点；
3. 显式标识 browser_compatible（Chromium/Playwright 不支持带账密 SOCKS5），杜绝静默断连；
4. 提取 API 强制过 core.net_guard SSRF 防护 + 3 跳重定向限制 + 512KB 体积上限，所有 API 与日志仅输出脱敏地址，绝不回传明文密码或 Token。
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import secrets
import sqlite3
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .net_guard import build_safe_urllib_opener, validate_public_http_url
from .secrets import (
    mask_proxy_url,
    mask_secret_url,
    parse_proxy_line,
    protect_secret,
    proxy_url_to_playwright_dict,
    sanitize_error_text,
    secret_fingerprint,
    unprotect_secret,
)

DEFAULT_BUILTIN_PROXY_DB = Path(__file__).resolve().parent.parent / "data" / "builtin_proxies.db"
_MAX_EXTRACT_BYTES = 512 * 1024
_MAX_EXTRACT_REDIRECTS = 3

RESIDENTIAL_PRESETS: dict[str, dict[str, str]] = {
    "smartproxy": {
        "id": "smartproxy",
        "label": "Smartproxy / Decodo",
        "template": "{user}-country-{country}-session-{session}",
        "hint": "用户名格式：user-country-us-session-xxxx",
    },
    "iproyal": {
        "id": "iproyal",
        "label": "IPRoyal",
        "template": "{user}_country-{country}_session-{session}",
        "hint": "用户名格式：user_country-us_session-xxxx",
    },
    "oxylabs": {
        "id": "oxylabs",
        "label": "Oxylabs",
        "template": "customer-{user}-cc-{country}-sessid-{session}",
        "hint": "用户名格式：customer-user-cc-us-sessid-xxxx",
    },
    "brightdata": {
        "id": "brightdata",
        "label": "Bright Data",
        "template": "{user}-country-{country}-session-{session}",
        "hint": "用户名格式：brd-customer-xxx-zone-residential-country-us-session-xxxx",
    },
    "custom": {
        "id": "custom",
        "label": "自定义模板（含 {session} 占位符）",
        "template": "{user}-session-{session}",
        "hint": "支持 {user}、{country}、{session} 占位符",
    },
}


def _gen_session_id(length: int = 10) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _parse_external_json_any(raw: str, *, what: str) -> Any:
    try:
        from .jsonbody import parse_json_body

        return parse_json_body(raw, what=what, expect=None)
    except ImportError:
        return json.loads(raw)


def _safe_fetch_public_text(url: str, *, timeout: float = 15.0, max_bytes: int = _MAX_EXTRACT_BYTES) -> str:
    """防 SSRF 公网拉取：校验 DNS、限制 3 跳重定向与最大响应字节数。"""
    clean_url = validate_public_http_url(url, resolve_dns=True)
    opener = build_safe_urllib_opener()
    for handler in opener.handlers:
        if isinstance(handler, urllib.request.HTTPRedirectHandler):
            handler.max_redirections = _MAX_EXTRACT_REDIRECTS
    req = urllib.request.Request(
        clean_url,
        headers={
            "User-Agent": "AutoRegisterStudio/3.1",
            "Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
        },
        method="GET",
    )
    with opener.open(req, timeout=timeout) as resp:
        final_url = str(resp.geturl() or clean_url)
        validate_public_http_url(final_url, resolve_dns=True)
        raw = resp.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError(f"代理提取响应超过 {max_bytes // 1024}KB 安全上限")
        return raw.decode("utf-8", errors="replace")


def _parse_json_proxy_item(item: Any, default_scheme: str) -> dict[str, Any] | None:
    if isinstance(item, str):
        s = item.strip()
        return parse_proxy_line(s, default_scheme=default_scheme) if s else None
    if not isinstance(item, dict):
        return None
    for url_key in ("url", "proxy", "proxy_url", "addr", "address"):
        val = item.get(url_key)
        if isinstance(val, str) and val.strip():
            return parse_proxy_line(val.strip(), default_scheme=default_scheme)
    host = str(item.get("ip") or item.get("host") or item.get("server") or "").strip()
    port = item.get("port")
    if not host or port is None:
        return None
    scheme = str(item.get("scheme") or item.get("protocol") or item.get("type") or default_scheme).strip().lower()
    if scheme not in ("http", "https", "socks5", "socks5h"):
        scheme = default_scheme
    user = str(item.get("username") or item.get("user") or "").strip()
    pwd = str(item.get("password") or item.get("pass") or item.get("pwd") or "").strip()
    hp = f"[{host}]:{int(port)}" if ":" in host else f"{host}:{int(port)}"
    if user or pwd:
        line = f"{scheme}://{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(pwd, safe='')}@{hp}"
    else:
        line = f"{scheme}://{hp}"
    return parse_proxy_line(line, default_scheme=default_scheme)


def parse_api_extract_payload(raw_text: str, *, default_scheme: str = "http") -> list[dict[str, Any]]:
    """解析代理商 API 提取结果（兼容纯文本多行/逗号分隔与常见 JSON 结构）。"""
    text = (raw_text or "").strip()
    if not text:
        return []
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()

    if text[0] in ("{", "["):
        try:
            obj = _parse_external_json_any(text, what="代理提取 API")
        except Exception:
            obj = None
        if obj is not None:
            candidates: list[Any] = []
            if isinstance(obj, list):
                candidates = obj
            elif isinstance(obj, dict):
                for key in ("data", "list", "proxies", "items", "result", "ips"):
                    val = obj.get(key)
                    if isinstance(val, list):
                        candidates = val
                        break
                    if isinstance(val, dict):
                        for subk in ("list", "items", "proxies", "proxy_list", "ips"):
                            if isinstance(val.get(subk), list):
                                candidates = val[subk]
                                break
                        if candidates:
                            break
                        if val.get("ip") or val.get("host"):
                            candidates = [val]
                            break
                    if isinstance(val, str) and (":" in val):
                        candidates = [seg.strip() for seg in re.split(r"[\r\n,;]+", val) if seg.strip()]
                        break
                if not candidates and (obj.get("ip") or obj.get("host") or obj.get("proxy")):
                    candidates = [obj]

            for cand in candidates:
                try:
                    p = _parse_json_proxy_item(cand, default_scheme)
                except ValueError:
                    continue
                if p and p["url"] not in seen:
                    seen.add(p["url"])
                    parsed.append(p)
            if parsed:
                return parsed

    for token in re.split(r"[\r\n,;]+", text):
        s = token.strip()
        if not s or s.startswith("#"):
            continue
        try:
            p = parse_proxy_line(s, default_scheme=default_scheme)
        except ValueError:
            continue
        if p["url"] not in seen:
            seen.add(p["url"])
            parsed.append(p)
    return parsed


def render_gateway_username(
    template: str,
    *,
    user: str,
    country: str = "us",
    session_id: str | None = None,
) -> tuple[str, str]:
    """按模板生成带唯一粘性会话的住宅代理用户名，返回 (rendered_username, session_id)。"""
    sid = (session_id or "").strip() or _gen_session_id()
    tpl = (template or "{user}-session-{session}").strip()
    cc = (country or "us").strip().lower()
    rendered = tpl.replace("{user}", user).replace("{country}", cc).replace("{session}", sid)
    if "{session}" not in tpl and sid not in rendered:
        rendered = f"{rendered}-session-{sid}"
    return rendered, sid


class BuiltinProxyStore:
    """内置 SQLite 代理池（支持静态代理、动态住宅网关、API 提取源与受控订阅节点）。"""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_BUILTIN_PROXY_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=15.0,
            isolation_level=None,
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
                CREATE TABLE IF NOT EXISTS proxy_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL DEFAULT 'static',
                    label TEXT NOT NULL DEFAULT '',
                    scheme TEXT NOT NULL DEFAULT 'http',
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    username_enc TEXT NOT NULL DEFAULT '',
                    password_enc TEXT NOT NULL DEFAULT '',
                    cred_fp TEXT NOT NULL DEFAULT '',
                    preset TEXT NOT NULL DEFAULT '',
                    session_template TEXT NOT NULL DEFAULT '',
                    country TEXT NOT NULL DEFAULT '',
                    source_id INTEGER NOT NULL DEFAULT 0,
                    expires_at REAL NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    available INTEGER NOT NULL DEFAULT 1,
                    browser_compatible INTEGER NOT NULL DEFAULT 1,
                    exit_ip TEXT NOT NULL DEFAULT '',
                    residential INTEGER NOT NULL DEFAULT 0,
                    score INTEGER NOT NULL DEFAULT 0,
                    grade TEXT NOT NULL DEFAULT '',
                    latency_ms REAL NOT NULL DEFAULT 0,
                    last_checked_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(kind, scheme, host, port, cred_fp, session_template)
                );
                """
            )
            # 兼容 Phase 1 旧库热迁移
            cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(proxy_entries)").fetchall()}
            if "source_id" not in cols:
                conn.execute("ALTER TABLE proxy_entries ADD COLUMN source_id INTEGER NOT NULL DEFAULT 0;")
            if "expires_at" not in cols:
                conn.execute("ALTER TABLE proxy_entries ADD COLUMN expires_at REAL NOT NULL DEFAULT 0;")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_proxy_enabled_avail ON proxy_entries(enabled, available, score DESC);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_proxy_source_exp ON proxy_entries(source_id, expires_at);"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS proxy_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    url_enc TEXT NOT NULL DEFAULT '',
                    url_fp TEXT NOT NULL DEFAULT '',
                    default_scheme TEXT NOT NULL DEFAULT 'http',
                    min_available INTEGER NOT NULL DEFAULT 3,
                    ttl_minutes INTEGER NOT NULL DEFAULT 15,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_pulled_at REAL NOT NULL DEFAULT 0,
                    last_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(kind, url_fp)
                );
                """
            )

    @staticmethod
    def _row_to_public(row: sqlite3.Row) -> dict[str, Any]:
        host = str(row["host"])
        port = int(row["port"])
        kind = str(row["kind"])
        hp = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        has_auth = bool(row["username_enc"] or row["password_enc"])
        if kind == "subscription_node":
            masked = "受控订阅托管出口"
        else:
            masked = f"{row['scheme']}://{'***@' if has_auth else ''}{hp}"
        return {
            "id": int(row["id"]),
            "kind": kind,
            "label": str(row["label"] or f"{host}:{port}"),
            "scheme": str(row["scheme"]),
            "host": "" if kind == "subscription_node" else host,
            "port": 0 if kind == "subscription_node" else port,
            "masked_url": masked,
            "has_auth": has_auth,
            "preset": str(row["preset"] or ""),
            "session_template": str(row["session_template"] or ""),
            "country": str(row["country"] or ""),
            "source_id": int(row["source_id"] or 0),
            "expires_at": float(row["expires_at"] or 0.0),
            "enabled": bool(row["enabled"]),
            "available": bool(row["available"]),
            "browser_compatible": bool(row["browser_compatible"]),
            "exit_ip": str(row["exit_ip"] or ""),
            "residential": bool(row["residential"]),
            "score": int(row["score"] or 0),
            "grade": str(row["grade"] or ""),
            "latency_ms": round(float(row["latency_ms"] or 0.0), 1),
            "last_checked_at": float(row["last_checked_at"] or 0.0),
            "last_error": str(row["last_error"] or ""),
            "created_at": float(row["created_at"] or 0.0),
        }

    def materialize_row(self, row: sqlite3.Row, *, session_id: str | None = None) -> dict[str, Any]:
        """将数据库行解密并实例化为可执行代理结构（仅供受控后端/调度器内部消费，严禁直接返回前端）。"""
        scheme = str(row["scheme"])
        host = str(row["host"])
        port = int(row["port"])
        hp = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        username = unprotect_secret(str(row["username_enc"])) if row["username_enc"] else ""
        password = unprotect_secret(str(row["password_enc"])) if row["password_enc"] else ""
        kind = str(row["kind"])
        sid = ""
        if kind == "residential_gateway":
            username, sid = render_gateway_username(
                str(row["session_template"] or "{user}-session-{session}"),
                user=username,
                country=str(row["country"] or "us"),
                session_id=session_id,
            )
        if username or password:
            u_q = urllib.parse.quote(username, safe="")
            p_q = urllib.parse.quote(password, safe="")
            full_url = f"{scheme}://{u_q}:{p_q}@{hp}"
        else:
            full_url = f"{scheme}://{hp}"

        pw_dict = proxy_url_to_playwright_dict(full_url)
        ip_key = (
            f"res_gw:{int(row['id'])}:{sid}"
            if kind == "residential_gateway"
            else (str(row["exit_ip"] or "") or f"builtin:{int(row['id'])}:{host}:{port}")
        )
        return {
            "id": int(row["id"]),
            "kind": kind,
            "label": str(row["label"] or f"{host}:{port}"),
            "proxy_url": full_url,
            "masked_url": "受控订阅托管出口" if kind == "subscription_node" else mask_proxy_url(full_url),
            "playwright_proxy": pw_dict,
            "ip_key": ip_key,
            "exit_ip": str(row["exit_ip"] or ""),
            "residential": bool(row["residential"]) or (kind == "residential_gateway"),
            "latency_ms": float(row["latency_ms"] or 0.0),
            "score": int(row["score"] or 0),
            "expires_at": float(row["expires_at"] or 0.0),
            "browser_compatible": bool(row["browser_compatible"]),
        }

    # ---------- 过期清理 ----------

    def purge_expired_entries(self) -> int:
        now = time.time()
        with self._connect(write=True) as conn:
            cur = conn.execute(
                "DELETE FROM proxy_entries WHERE expires_at > 0 AND expires_at <= ?",
                (now,),
            )
            return int(cur.rowcount or 0)

    # ---------- 动态 API 提取源与订阅源管理 ----------

    def add_source(
        self,
        *,
        kind: str,
        url: str,
        label: str = "",
        default_scheme: str = "http",
        min_available: int = 3,
        ttl_minutes: int = 15,
    ) -> dict[str, Any]:
        clean_kind = (kind or "").strip().lower()
        if clean_kind not in ("api_extract", "subscription"):
            raise ValueError(f"不支持的代理源类型: {kind!r}")
        clean_url = validate_public_http_url(url, resolve_dns=False)
        scheme = (default_scheme or "http").strip().lower()
        if scheme not in ("http", "https", "socks5", "socks5h"):
            raise ValueError(f"不支持的默认代理协议: {default_scheme!r}")
        min_avail = max(1, min(int(min_available), 100))
        ttl_m = max(1, min(int(ttl_minutes), 1440))

        now = time.time()
        url_enc = protect_secret(clean_url)
        url_fp = secret_fingerprint(clean_url)
        host_hint = urllib.parse.urlsplit(clean_url).hostname or "source"
        lbl = (label or f"{'API提取' if clean_kind == 'api_extract' else '机场订阅'} ({host_hint})").strip()[:80]

        with self._connect(write=True) as conn:
            conn.execute(
                """
                INSERT INTO proxy_sources (
                    kind, label, url_enc, url_fp, default_scheme, min_available, ttl_minutes,
                    enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(kind, url_fp) DO UPDATE SET
                    label=excluded.label,
                    url_enc=excluded.url_enc,
                    default_scheme=excluded.default_scheme,
                    min_available=excluded.min_available,
                    ttl_minutes=excluded.ttl_minutes,
                    enabled=1,
                    last_error='',
                    updated_at=excluded.updated_at
                """,
                (clean_kind, lbl, url_enc, url_fp, scheme, min_avail, ttl_m, now, now),
            )
            row = conn.execute(
                "SELECT * FROM proxy_sources WHERE kind=? AND url_fp=?",
                (clean_kind, url_fp),
            ).fetchone()

        return {
            "source": self._source_to_public(row, active_entries=0) if row else None,
            "sources": self.list_sources(),
            "stats": self.get_stats(),
        }

    def _source_to_public(self, row: sqlite3.Row, *, active_entries: int = 0) -> dict[str, Any]:
        masked = "***"
        if row["url_enc"]:
            try:
                masked = mask_secret_url(unprotect_secret(str(row["url_enc"])))
            except Exception:
                masked = "*** (凭据不可读)"
        return {
            "id": int(row["id"]),
            "kind": str(row["kind"]),
            "label": str(row["label"] or masked),
            "masked_url": masked,
            "default_scheme": str(row["default_scheme"] or "http"),
            "min_available": int(row["min_available"] or 3),
            "ttl_minutes": int(row["ttl_minutes"] or 15),
            "enabled": bool(row["enabled"]),
            "last_pulled_at": float(row["last_pulled_at"] or 0.0),
            "last_count": int(row["last_count"] or 0),
            "last_error": str(row["last_error"] or ""),
            "active_entries": int(active_entries),
            "created_at": float(row["created_at"] or 0.0),
        }

    def list_sources(self, *, kind: str = "") -> list[dict[str, Any]]:
        now = time.time()
        with self._connect(write=False) as conn:
            if kind:
                rows = conn.execute(
                    "SELECT * FROM proxy_sources WHERE kind=? ORDER BY enabled DESC, id DESC",
                    (kind,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM proxy_sources ORDER BY kind ASC, enabled DESC, id DESC"
                ).fetchall()
            counts_rows = conn.execute(
                """
                SELECT source_id, COUNT(*) AS cnt
                FROM proxy_entries
                WHERE source_id > 0 AND enabled = 1 AND available = 1
                  AND (expires_at = 0 OR expires_at > ?)
                GROUP BY source_id
                """,
                (now,),
            ).fetchall()
        counts = {int(r["source_id"]): int(r["cnt"]) for r in counts_rows}
        return [self._source_to_public(r, active_entries=counts.get(int(r["id"]), 0)) for r in rows]

    def toggle_source(self, source_id: int, enabled: bool) -> bool:
        with self._connect(write=True) as conn:
            cur = conn.execute(
                "UPDATE proxy_sources SET enabled=?, updated_at=? WHERE id=?",
                (1 if enabled else 0, time.time(), int(source_id)),
            )
            return int(cur.rowcount or 0) > 0

    def delete_source(self, source_id: int) -> dict[str, Any]:
        sid = int(source_id)
        with self._connect(write=True) as conn:
            cur = conn.execute("DELETE FROM proxy_sources WHERE id=?", (sid,))
            deleted_entries = conn.execute("DELETE FROM proxy_entries WHERE source_id=?", (sid,)).rowcount
        return {
            "deleted": int(cur.rowcount or 0) > 0,
            "removed_entries": int(deleted_entries or 0),
            "sources": self.list_sources(),
            "stats": self.get_stats(),
        }

    def get_decrypted_subscription_urls(self) -> list[tuple[int, str, str]]:
        """仅供受控代理内核内部读取已启用的订阅 URL，严禁向外暴露。"""
        with self._connect(write=False) as conn:
            rows = conn.execute(
                "SELECT id, label, url_enc FROM proxy_sources WHERE kind='subscription' AND enabled=1 ORDER BY id ASC"
            ).fetchall()
        out: list[tuple[int, str, str]] = []
        for r in rows:
            try:
                u = unprotect_secret(str(r["url_enc"] or ""))
                if u:
                    out.append((int(r["id"]), str(r["label"] or ""), u))
            except Exception:
                continue
        return out

    def pull_api_source(self, source_id: int, *, timeout: float = 15.0) -> dict[str, Any]:
        """按防 SSRF 规则拉取单条动态 API 提取链接并按 TTL 入库。"""
        sid = int(source_id)
        with self._connect(write=False) as conn:
            row = conn.execute(
                "SELECT * FROM proxy_sources WHERE id=? AND kind='api_extract'",
                (sid,),
            ).fetchone()
        if not row:
            return {"ok": False, "source_id": sid, "error": "API 提取源不存在"}

        now = time.time()
        try:
            raw_url = unprotect_secret(str(row["url_enc"] or ""))
            payload_text = _safe_fetch_public_text(raw_url, timeout=timeout)
            parsed = parse_api_extract_payload(
                payload_text, default_scheme=str(row["default_scheme"] or "http")
            )
            if not parsed:
                raise ValueError("API 返回内容中未解析到合法代理地址（支持每行一条 ip:port 或标准 JSON 列表）")
        except Exception as exc:
            clean_err = sanitize_error_text(f"{type(exc).__name__}: {exc}")
            with self._connect(write=True) as conn:
                conn.execute(
                    "UPDATE proxy_sources SET last_pulled_at=?, last_error=?, updated_at=? WHERE id=?",
                    (now, clean_err, now, sid),
                )
            return {
                "ok": False,
                "source_id": sid,
                "error": clean_err,
                "sources": self.list_sources(),
                "stats": self.get_stats(),
            }

        ttl_s = max(60, int(row["ttl_minutes"] or 15) * 60)
        expires_at = now + ttl_s
        src_label = str(row["label"] or "API提取").strip()[:48]
        added = 0
        refreshed = 0

        with self._connect(write=True) as conn:
            conn.execute(
                "DELETE FROM proxy_entries WHERE source_id=? AND expires_at > 0 AND expires_at <= ?",
                (sid, now),
            )
            for p in parsed:
                u_enc = protect_secret(p["username"]) if p["username"] else ""
                p_enc = protect_secret(p["password"]) if p["password"] else ""
                fp = secret_fingerprint(f"{p['username']}:{p['password']}") if p["has_auth"] else ""
                label = f"{src_label} · {p['host']}:{p['port']}"
                existing = conn.execute(
                    """
                    SELECT id FROM proxy_entries
                    WHERE kind='api_extract' AND scheme=? AND host=? AND port=? AND cred_fp=? AND session_template=''
                    """,
                    (p["scheme"], p["host"], int(p["port"]), fp),
                ).fetchone()
                if existing:
                    conn.execute(
                        """
                        UPDATE proxy_entries
                        SET label=?, source_id=?, expires_at=?, enabled=1, available=1,
                            browser_compatible=?, last_error='', updated_at=?
                        WHERE id=?
                        """,
                        (label, sid, expires_at, 1 if p["browser_compatible"] else 0, now, int(existing["id"])),
                    )
                    refreshed += 1
                else:
                    conn.execute(
                        """
                        INSERT INTO proxy_entries (
                            kind, label, scheme, host, port, username_enc, password_enc, cred_fp,
                            source_id, expires_at, enabled, available, browser_compatible,
                            created_at, updated_at
                        ) VALUES ('api_extract', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)
                        """,
                        (
                            label,
                            p["scheme"],
                            p["host"],
                            int(p["port"]),
                            u_enc,
                            p_enc,
                            fp,
                            sid,
                            expires_at,
                            1 if p["browser_compatible"] else 0,
                            now,
                            now,
                        ),
                    )
                    added += 1

            conn.execute(
                """
                UPDATE proxy_sources
                SET last_pulled_at=?, last_count=?, last_error='', updated_at=?
                WHERE id=?
                """,
                (now, len(parsed), now, sid),
            )

        return {
            "ok": True,
            "source_id": sid,
            "pulled": len(parsed),
            "added": added,
            "refreshed": refreshed,
            "expires_at": expires_at,
            "sources": self.list_sources(),
            "stats": self.get_stats(),
        }

    def pull_due_api_sources(self, *, min_cooldown_s: float = 30.0) -> dict[str, Any]:
        """当已启用的 API 提取源可用库存低于 min_available 水位线且过冷却期时原子抢占并补货（防跨进程并发重复拉取）。"""
        now = time.time()
        claimed_ids: list[int] = []
        cutoff = now - max(0.0, float(min_cooldown_s))
        with self._connect(write=True) as conn:
            conn.execute(
                "DELETE FROM proxy_entries WHERE expires_at > 0 AND expires_at <= ?",
                (now,),
            )
            rows = conn.execute(
                """
                SELECT s.id, s.min_available, s.last_pulled_at,
                       COALESCE(e.cnt, 0) AS active_entries
                FROM proxy_sources s
                LEFT JOIN (
                    SELECT source_id, COUNT(*) AS cnt
                    FROM proxy_entries
                    WHERE source_id > 0 AND enabled = 1 AND available = 1
                      AND (expires_at = 0 OR expires_at > ?)
                    GROUP BY source_id
                ) e ON e.source_id = s.id
                WHERE s.kind = 'api_extract' AND s.enabled = 1
                ORDER BY s.id ASC
                """,
                (now,),
            ).fetchall()
            for r in rows:
                if int(r["active_entries"]) >= int(r["min_available"]):
                    continue
                if float(r["last_pulled_at"] or 0.0) > cutoff:
                    continue
                cur = conn.execute(
                    """
                    UPDATE proxy_sources
                    SET last_pulled_at = ?, updated_at = ?
                    WHERE id = ? AND (last_pulled_at <= ? OR last_pulled_at = 0)
                    """,
                    (now, now, int(r["id"]), cutoff),
                )
                if cur.rowcount:
                    claimed_ids.append(int(r["id"]))

        pulled_sources = 0
        added_total = 0
        errors: list[dict[str, Any]] = []
        for sid in claimed_ids:
            res = self.pull_api_source(sid)
            pulled_sources += 1
            if res.get("ok"):
                added_total += int(res.get("added") or 0) + int(res.get("refreshed") or 0)
            else:
                errors.append({"source_id": sid, "error": str(res.get("error") or "")})
        return {"pulled_sources": pulled_sources, "added_total": added_total, "errors": errors}

    def sync_subscription_nodes(self, nodes: list[dict[str, Any]], *, owner_tag: str = "default") -> dict[str, Any]:
        """将受控订阅内核发现的出口按 owner_tag 隔离同步入池（对外隐藏底层本地端口，不误删其他内核实例节点）。"""
        now = time.time()
        tag = (owner_tag or "default").strip()[:64]
        active_ports: set[int] = set()
        avail_count = 0
        with self._connect(write=True) as conn:
            for item in nodes:
                port = int(item.get("port") or 0)
                if not (1 <= port <= 65535):
                    continue
                active_ports.add(port)
                name = str(item.get("name") or item.get("tag") or f"订阅出口-{len(active_ports)}").strip()[:80]
                is_avail = item.get("available") is not False
                if is_avail:
                    avail_count += 1
                lat_ms = float(item.get("last_latency_ms") or item.get("latency_ms") or 0.0)
                existing = conn.execute(
                    """
                    SELECT id FROM proxy_entries
                    WHERE kind='subscription_node' AND scheme='http' AND host='127.0.0.1'
                      AND port=? AND cred_fp='' AND session_template=''
                    """,
                    (port,),
                ).fetchone()
                if existing:
                    conn.execute(
                        """
                        UPDATE proxy_entries
                        SET label=?, preset=?, available=?, latency_ms=CASE WHEN ? > 0 THEN ? ELSE latency_ms END,
                            updated_at=?
                        WHERE id=?
                        """,
                        (name, tag, 1 if is_avail else 0, lat_ms, lat_ms, now, int(existing["id"])),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO proxy_entries (
                            kind, label, scheme, host, port, username_enc, password_enc, cred_fp,
                            preset, enabled, available, browser_compatible, latency_ms, created_at, updated_at
                        ) VALUES ('subscription_node', ?, 'http', '127.0.0.1', ?, '', '', '', ?, 1, ?, 1, ?, ?, ?)
                        """,
                        (name, port, tag, 1 if is_avail else 0, lat_ms, now, now),
                    )

            if active_ports:
                placeholders = ",".join("?" for _ in active_ports)
                conn.execute(
                    f"DELETE FROM proxy_entries WHERE kind='subscription_node' AND preset=? AND port NOT IN ({placeholders})",
                    [tag, *active_ports],
                )
            else:
                conn.execute("DELETE FROM proxy_entries WHERE kind='subscription_node' AND preset=?", (tag,))

        return {"synced": len(active_ports), "available": avail_count}

    def clear_subscription_nodes(self, *, owner_tag: str = "") -> int:
        tag = (owner_tag or "").strip()[:64]
        with self._connect(write=True) as conn:
            if tag:
                cur = conn.execute(
                    "DELETE FROM proxy_entries WHERE kind='subscription_node' AND preset=?",
                    (tag,),
                )
            else:
                cur = conn.execute("DELETE FROM proxy_entries WHERE kind='subscription_node'")
            return int(cur.rowcount or 0)

    # ---------- 导入、配置住宅网关、查询、删改 ----------

    def import_static_proxies(self, text: str, *, default_scheme: str = "http") -> dict[str, Any]:
        now = time.time()
        added = 0
        skipped = 0
        errors: list[dict[str, Any]] = []

        with self._connect(write=True) as conn:
            for line_no, raw in enumerate((text or "").splitlines(), 1):
                s = raw.strip()
                if not s or s.startswith("#"):
                    continue
                try:
                    p = parse_proxy_line(s, default_scheme=default_scheme)
                except ValueError as exc:
                    errors.append({"line": line_no, "error": sanitize_error_text(str(exc))})
                    continue

                u_enc = protect_secret(p["username"]) if p["username"] else ""
                p_enc = protect_secret(p["password"]) if p["password"] else ""
                fp = secret_fingerprint(f"{p['username']}:{p['password']}") if p["has_auth"] else ""
                label = f"{p['host']}:{p['port']}"
                try:
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO proxy_entries (
                            kind, label, scheme, host, port, username_enc, password_enc, cred_fp,
                            enabled, available, browser_compatible, created_at, updated_at
                        ) VALUES ('static', ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)
                        """,
                        (
                            label,
                            p["scheme"],
                            p["host"],
                            int(p["port"]),
                            u_enc,
                            p_enc,
                            fp,
                            1 if p["browser_compatible"] else 0,
                            now,
                            now,
                        ),
                    )
                    if cur.rowcount:
                        added += 1
                    else:
                        skipped += 1
                except sqlite3.IntegrityError:
                    skipped += 1

        return {
            "added": added,
            "skipped": skipped,
            "error_count": len(errors),
            "errors": errors[:20],
            "stats": self.get_stats(),
        }

    def add_residential_gateway(
        self,
        *,
        gateway_url: str,
        label: str = "",
        preset: str = "smartproxy",
        session_template: str = "",
        country: str = "us",
    ) -> dict[str, Any]:
        p = parse_proxy_line(gateway_url, default_scheme="http")
        if preset not in RESIDENTIAL_PRESETS:
            preset = "custom"
        tpl = (session_template or RESIDENTIAL_PRESETS[preset]["template"]).strip()
        if "{session}" not in tpl:
            raise ValueError("住宅代理用户名模板必须包含 {session} 占位符，以确保按次轮换独立出口 IP")

        now = time.time()
        u_enc = protect_secret(p["username"]) if p["username"] else ""
        p_enc = protect_secret(p["password"]) if p["password"] else ""
        fp = secret_fingerprint(f"{p['username']}:{p['password']}") if p["has_auth"] else ""
        lbl = (label or f"{RESIDENTIAL_PRESETS[preset]['label']} ({country.upper() or 'AUTO'})").strip()[:80]
        cc = (country or "us").strip().lower()[:12]

        with self._connect(write=True) as conn:
            conn.execute(
                """
                INSERT INTO proxy_entries (
                    kind, label, scheme, host, port, username_enc, password_enc, cred_fp,
                    preset, session_template, country, enabled, available, browser_compatible,
                    residential, created_at, updated_at
                ) VALUES ('residential_gateway', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, 1, ?, ?)
                ON CONFLICT(kind, scheme, host, port, cred_fp, session_template) DO UPDATE SET
                    label=excluded.label,
                    preset=excluded.preset,
                    country=excluded.country,
                    enabled=1,
                    available=1,
                    browser_compatible=excluded.browser_compatible,
                    updated_at=excluded.updated_at
                """,
                (
                    lbl,
                    p["scheme"],
                    p["host"],
                    int(p["port"]),
                    u_enc,
                    p_enc,
                    fp,
                    preset,
                    tpl,
                    cc,
                    1 if p["browser_compatible"] else 0,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM proxy_entries
                WHERE kind='residential_gateway' AND scheme=? AND host=? AND port=? AND cred_fp=? AND session_template=?
                """,
                (p["scheme"], p["host"], int(p["port"]), fp, tpl),
            ).fetchone()
        return {
            "entry": self._row_to_public(row) if row else None,
            "stats": self.get_stats(),
        }

    def get_stats(self) -> dict[str, Any]:
        now = time.time()
        with self._connect(write=False) as conn:
            rows = conn.execute(
                """
                SELECT kind, enabled, available, residential, browser_compatible
                FROM proxy_entries
                WHERE expires_at = 0 OR expires_at > ?
                """,
                (now,),
            ).fetchall()
            src_rows = conn.execute("SELECT kind, enabled FROM proxy_sources").fetchall()
        total = len(rows)
        enabled = sum(1 for r in rows if r["enabled"])
        available = sum(1 for r in rows if r["enabled"] and r["available"])
        static_n = sum(1 for r in rows if r["kind"] == "static")
        gw_n = sum(1 for r in rows if r["kind"] == "residential_gateway")
        api_n = sum(1 for r in rows if r["kind"] == "api_extract")
        sub_n = sum(1 for r in rows if r["kind"] == "subscription_node")
        res_n = sum(1 for r in rows if r["enabled"] and (r["residential"] or r["kind"] == "residential_gateway"))
        browser_ok = sum(1 for r in rows if r["enabled"] and r["available"] and r["browser_compatible"])
        return {
            "total": total,
            "enabled": enabled,
            "available": available,
            "static": static_n,
            "residential_gateways": gw_n,
            "api_extracted": api_n,
            "subscription_nodes": sub_n,
            "api_sources": sum(1 for r in src_rows if r["kind"] == "api_extract" and r["enabled"]),
            "subscription_sources": sum(1 for r in src_rows if r["kind"] == "subscription" and r["enabled"]),
            "residential": res_n,
            "browser_compatible": browser_ok,
        }

    def list_entries(self, *, enabled_only: bool = False) -> dict[str, Any]:
        self.purge_expired_entries()
        where = "WHERE enabled = 1" if enabled_only else ""
        with self._connect(write=False) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM proxy_entries
                {where}
                ORDER BY
                    enabled DESC,
                    available DESC,
                    CASE kind WHEN 'residential_gateway' THEN 0 WHEN 'api_extract' THEN 1 WHEN 'subscription_node' THEN 2 ELSE 3 END,
                    residential DESC,
                    score DESC,
                    CASE WHEN latency_ms > 0 THEN latency_ms ELSE 999999 END ASC,
                    id DESC
                """
            ).fetchall()
        return {
            "items": [self._row_to_public(r) for r in rows],
            "sources": self.list_sources(),
            "stats": self.get_stats(),
        }

    def list_runtime_entries(
        self,
        *,
        browser_only: bool = False,
        auto_replenish: bool = False,
    ) -> list[dict[str, Any]]:
        """供 ProxyPool(mode='builtin') 加载可执行代理列表（按需解密，绝不向外暴露）。"""
        self.purge_expired_entries()
        if auto_replenish:
            try:
                self.pull_due_api_sources()
            except Exception:
                pass
        now = time.time()
        cond = "WHERE enabled = 1 AND available = 1 AND (expires_at = 0 OR expires_at > ?)"
        if browser_only:
            cond += " AND browser_compatible = 1"
        with self._connect(write=False) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM proxy_entries
                {cond}
                ORDER BY
                    residential DESC,
                    score DESC,
                    CASE WHEN latency_ms > 0 THEN latency_ms ELSE 999999 END ASC,
                    id ASC
                """,
                (now,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                out.append(self.materialize_row(r))
            except Exception:
                continue
        return out

    def materialize_gateway_by_id(self, entry_id: int, *, session_id: str | None = None) -> dict[str, Any] | None:
        with self._connect(write=False) as conn:
            row = conn.execute("SELECT * FROM proxy_entries WHERE id=?", (int(entry_id),)).fetchone()
        if not row or not row["enabled"]:
            return None
        return self.materialize_row(row, session_id=session_id)

    def toggle_entries(self, ids: list[int], enabled: bool) -> int:
        clean = [int(i) for i in ids]
        if not clean:
            return 0
        placeholders = ",".join("?" for _ in clean)
        with self._connect(write=True) as conn:
            cur = conn.execute(
                f"UPDATE proxy_entries SET enabled=?, updated_at=? WHERE id IN ({placeholders})",
                [1 if enabled else 0, time.time(), *clean],
            )
            return int(cur.rowcount or 0)

    def delete_entries(self, ids: list[int]) -> int:
        clean = [int(i) for i in ids]
        if not clean:
            return 0
        placeholders = ",".join("?" for _ in clean)
        with self._connect(write=True) as conn:
            cur = conn.execute(f"DELETE FROM proxy_entries WHERE id IN ({placeholders})", clean)
            return int(cur.rowcount or 0)

    def cleanup_unavailable_entries(self) -> int:
        """一键移除已测活且不可达（available=0 且已探测过）的失效代理条目，同时顺带清理过期条目。"""
        now = time.time()
        with self._connect(write=True) as conn:
            cur1 = conn.execute("DELETE FROM proxy_entries WHERE expires_at > 0 AND expires_at <= ?", (now,))
            cur2 = conn.execute(
                "DELETE FROM proxy_entries WHERE available = 0 AND last_checked_at > 0 AND kind != 'residential_gateway'"
            )
            return int(cur1.rowcount or 0) + int(cur2.rowcount or 0)

    # ---------- 实时连通性与纯净度探测 ----------

    def probe_entry(self, entry_id: int, *, target_url: str = "") -> dict[str, Any]:
        from .ip_scorer import score_ip

        with self._connect(write=False) as conn:
            row = conn.execute("SELECT * FROM proxy_entries WHERE id=?", (int(entry_id),)).fetchone()
        if not row:
            return {"id": entry_id, "ok": False, "error": "代理条目不存在"}

        mat = self.materialize_row(row)
        now = time.time()
        try:
            res = score_ip(proxy_url=mat["proxy_url"], target_url=target_url.strip())
        except Exception as exc:
            err = sanitize_error_text(f"{type(exc).__name__}: {exc}")
            with self._connect(write=True) as conn:
                conn.execute(
                    "UPDATE proxy_entries SET available=0, last_checked_at=?, last_error=?, updated_at=? WHERE id=?",
                    (now, err, now, int(entry_id)),
                )
            return {"id": int(entry_id), "ok": False, "error": err}

        ok = bool(res.get("ok"))
        exit_ip = str(res.get("ip") or "")
        cc = str(res.get("country_code") or row["country"] or "").lower()
        is_res = bool(res.get("residential")) or (str(row["kind"]) == "residential_gateway")
        score_val = int(res.get("score") or 0)
        grade_val = str(res.get("grade") or "")
        lat_ms = float(res.get("latency_ms") or 0.0)
        err_msg = "" if ok else sanitize_error_text(str(res.get("error") or res.get("verdict") or "探测失败"))

        with self._connect(write=True) as conn:
            conn.execute(
                """
                UPDATE proxy_entries
                SET available=?, exit_ip=?, country=?, residential=?, score=?, grade=?,
                    latency_ms=?, last_checked_at=?, last_error=?, updated_at=?
                WHERE id=?
                """,
                (
                    1 if ok else 0,
                    exit_ip if ok else str(row["exit_ip"] or ""),
                    cc,
                    1 if is_res else 0,
                    score_val if ok else 0,
                    grade_val if ok else "",
                    lat_ms if ok else 0.0,
                    now,
                    err_msg,
                    now,
                    int(entry_id),
                ),
            )
            fresh = conn.execute("SELECT * FROM proxy_entries WHERE id=?", (int(entry_id),)).fetchone()
        return {
            "id": int(entry_id),
            "ok": ok,
            "entry": self._row_to_public(fresh) if fresh else None,
            "error": err_msg,
        }

    def probe_batch(
        self,
        ids: list[int] | None = None,
        *,
        target_url: str = "",
        limit: int = 20,
        max_workers: int = 6,
    ) -> dict[str, Any]:
        self.purge_expired_entries()
        if ids:
            targets = [int(i) for i in ids][:limit]
        else:
            with self._connect(write=False) as conn:
                rows = conn.execute(
                    "SELECT id FROM proxy_entries WHERE enabled=1 ORDER BY last_checked_at ASC LIMIT ?",
                    (max(1, min(limit, 50)),),
                ).fetchall()
            targets = [int(r["id"]) for r in rows]

        results: list[dict[str, Any]] = []
        if targets:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as ex:
                results = list(ex.map(lambda eid: self.probe_entry(eid, target_url=target_url), targets))
        return {
            "checked": len(results),
            "ok_count": sum(1 for r in results if r.get("ok")),
            "results": results,
            "stats": self.get_stats(),
        }


_DEFAULT_PROXY_STORE: BuiltinProxyStore | None = None
_DEFAULT_PROXY_STORE_LOCK = threading.Lock()


def get_builtin_proxy_store(db_path: Path | str | None = None) -> BuiltinProxyStore:
    if db_path is not None:
        return BuiltinProxyStore(db_path)
    global _DEFAULT_PROXY_STORE
    with _DEFAULT_PROXY_STORE_LOCK:
        if _DEFAULT_PROXY_STORE is None:
            _DEFAULT_PROXY_STORE = BuiltinProxyStore()
        return _DEFAULT_PROXY_STORE
