"""Single-Account IMAP Verification Client (Python Stdlib Only).

Designed for Open-Source Community Edition single-thread workflows so users can
receive verification codes with their own Gmail / Outlook / QQ / 163 / custom
domain mailbox when target sites block public disposable domains.
"""

from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


def _decode_header_text(val: Optional[str]) -> str:
    if not val:
        return ""
    parts = email.header.decode_header(val)
    out = []
    for raw, enc in parts:
        if isinstance(raw, bytes):
            out.append(raw.decode(enc or "utf-8", errors="ignore"))
        else:
            out.append(str(raw))
    return "".join(out)


def _extract_body_text(msg: email.message.Message) -> str:
    chunks: List[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    chunks.append(payload.decode(charset, errors="ignore"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            chunks.append(payload.decode(charset, errors="ignore"))
    text = "\n".join(chunks)
    return re.sub(r"<[^>]+>", " ", text)


class SingleImapMailClient:
    """Zero-dependency single-mailbox IMAP reader using BODY.PEEK[] (non-destructive)."""

    def __init__(self, server: str, user: str, password: str, port: int = 993, use_ssl: bool = True):
        self.server = (server or "").strip()
        self.user = (user or "").strip()
        self.password = password or ""
        self.port = int(port or 993)
        self.use_ssl = bool(use_ssl)

    def _connect(self) -> imaplib.IMAP4:
        if not self.server or not self.user or not self.password:
            raise ValueError("请先在 config.json 配置完整的 IMAP 服务器、账号与授权密码")
        conn: imaplib.IMAP4 = (
            imaplib.IMAP4_SSL(self.server, self.port, timeout=12)
            if self.use_ssl
            else imaplib.IMAP4(self.server, self.port, timeout=12)
        )
        conn.login(self.user, self.password)
        return conn

    def test_connection(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        conn = None
        try:
            conn = self._connect()
            status, data = conn.select("INBOX", readonly=True)
            total = int(data[0]) if status == "OK" and data and data[0] else 0
            return {
                "ok": True,
                "server": self.server,
                "port": self.port,
                "user": self.user,
                "inbox_messages": total,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        except Exception as e:
            return {
                "ok": False,
                "server": self.server,
                "port": self.port,
                "user": self.user,
                "error": str(e),
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass

    def poll_for_verification_code(
        self,
        recipient: Optional[str] = None,
        since_ts: Optional[float] = None,
        timeout_secs: int = 90,
        pattern: str = r"\b\d{4,8}\b",
    ) -> Optional[str]:
        target_to = (recipient or self.user).strip().lower()
        cutoff = (since_ts or time.time()) - 120.0
        deadline = time.time() + max(5, int(timeout_secs))

        while time.time() < deadline:
            conn = None
            try:
                conn = self._connect()
                conn.select("INBOX")
                typ, data = conn.search(None, "UNSEEN")
                if typ == "OK" and data and data[0]:
                    ids = data[0].split()[-15:]
                    for eid in reversed(ids):
                        ftyp, fdata = conn.fetch(eid, "(BODY.PEEK[])")
                        if ftyp != "OK" or not fdata or not isinstance(fdata[0], tuple):
                            continue
                        raw = fdata[0][1]
                        msg = email.message_from_bytes(raw)
                        date_hdr = msg.get("Date")
                        if date_hdr:
                            try:
                                parsed_dt = email.utils.parsedate_to_datetime(date_hdr)
                                if parsed_dt and parsed_dt.timestamp() < cutoff:
                                    continue
                            except Exception:
                                pass
                        to_hdr = f"{msg.get('To', '')} {msg.get('Delivered-To', '')}".lower()
                        if target_to and target_to not in to_hdr:
                            continue
                        subject = _decode_header_text(msg.get("Subject"))
                        body = _extract_body_text(msg)
                        m = re.search(pattern, f"{subject}\n{body}")
                        if m:
                            try:
                                conn.store(eid, "+FLAGS", "\\Seen")
                            except Exception:
                                pass
                            return m.group(0)
            except Exception:
                pass
            finally:
                if conn is not None:
                    try:
                        conn.logout()
                    except Exception:
                        pass
            time.sleep(4)
        return None


class BuiltinPoolMailClient:
    """Built-in SQLite + DPAPI Mail Pool Client (Outlook OAuth2 / iCloud links / IMAP)."""

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        owner: str = "runner",
        include_unknown: bool = True,
    ):
        from core.mail_builtin import get_builtin_mail_store

        default_db = Path(__file__).resolve().parent.parent / "results" / "builtin_mail.db"
        self.db_path = Path(db_path) if db_path is not None else default_db
        self.owner = (owner or "runner").strip() or "runner"
        self.include_unknown = bool(include_unknown)
        self.store = get_builtin_mail_store(self.db_path)
        self.email: Optional[str] = None
        self._leased_at: float = 0.0

    def create_mailbox(self) -> str:
        """Lease an available mailbox from the built-in SQLite pool."""
        acc = self.store.lease_account(
            owner=self.owner,
            ttl_s=900,
            include_unknown=self.include_unknown,
        )
        if not acc or not acc.get("email"):
            raise RuntimeError("内置邮箱池暂无可用邮箱，请先在控制台「内置邮箱池」导入账号")
        self.email = str(acc["email"]).strip().lower()
        self._leased_at = time.time()
        return self.email

    def poll_for_verification_code(
        self,
        timeout_secs: int = 90,
        pattern: str = r"\b\d{4,8}\b",
        since_ts: Optional[float] = None,
    ) -> Optional[str]:
        """Poll verification code from the leased mailbox via BuiltinMailStore."""
        from core.mail_builtin import extract_verification_candidates

        if not self.email:
            raise RuntimeError("尚未从内置邮箱池租借邮箱，请先调用 create_mailbox()")
        cutoff = float(since_ts if since_ts is not None else (self._leased_at or time.time())) - 120.0
        deadline = time.time() + max(5, int(timeout_secs))
        use_smart = not pattern or pattern == r"\b\d{4,8}\b"
        while time.time() < deadline:
            try:
                msgs = self.store.list_messages(self.email, top=10, since=cutoff)
                for msg in msgs:
                    subj = str(msg.get("subject") or "")
                    txt = str(msg.get("text") or "")
                    htm = str(msg.get("html") or "")
                    if use_smart:
                        cands = [c for c in extract_verification_candidates(subj, txt, htm) if c["kind"] in ("numeric", "alphanumeric")]
                        if cands and float(cands[0].get("confidence", 0)) >= 0.70:
                            return str(cands[0]["value"])
                    else:
                        plain_htm = re.sub(r"<[^>]+>", " ", htm)
                        m = re.search(pattern, f"{subj}\n{txt}\n{plain_htm}")
                        if m:
                            if m.lastindex:
                                non_empty = [g for g in m.groups() if g is not None]
                                return non_empty[-1] if non_empty else m.group(0)
                            return m.group(0)
            except Exception:
                pass
            time.sleep(4)
        return None

    def release(self, mark_status: Optional[str] = None) -> bool:
        """Release the currently leased mailbox back to the pool."""
        if not self.email:
            return False
        em = self.email
        self.email = None
        return self.store.release_lease(em, owner=self.owner, mark_status=mark_status)
