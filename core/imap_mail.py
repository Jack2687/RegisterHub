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
from typing import Any, Dict, List, Optional


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
