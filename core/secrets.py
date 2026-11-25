"""敏感凭据加密存储、日志脱敏与代理地址规范化工具（Single Source of Truth）。

设计目标：
1. Windows 本机通过 DPAPI (CryptProtectData / CryptUnprotectData) 加密高价值凭据
   （Outlook refresh_token、IMAP 密码、代理账密），避免明文落入 SQLite / JSON；
2. 全仓统一代理 URL 解析、结构化 Playwright 字典转换与脱敏展示，严禁凭据进入日志或前端；
3. 严禁把带账密的代理 URL 拼入浏览器命令行参数。
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import os
import re
import secrets as _py_secrets
import sys
import threading
import urllib.parse
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_DPAPI_ENTROPY = b"AutoRegisterStudio-DPAPI-v1"
_DPAPI_PREFIX = "dpapi:"
_LOCAL_V1_PREFIX = "localv1:"
_LOCAL_KEY_LOCK = threading.Lock()
_ALLOWED_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})
_CRED_URL_RE = re.compile(r"((?:https?|socks5h?)://)([^/\s@:]+):([^/\s@]+)@", re.I)
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-~+/=]{8,}", re.I)
_KV_SECRET_RE = re.compile(
    r"((?:refresh_token|access_token|client_secret|password|passwd|api_key|auth_key|secret|token|key)\s*[=:]\s*)([^\s&,\"';]{4,})",
    re.I,
)


class SecretProtectionError(RuntimeError):
    """Windows DPAPI 加解密失败时显式抛出，绝不静默降级成明文或返回损坏值。"""


if sys.platform == "win32":
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL

    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL

    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p


def _bytes_to_blob(data: bytes) -> tuple[Any, Any]:
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def _resolve_posix_master_key_path() -> Path:
    env_override = (os.environ.get("REGISTERHUB_SECRET_KEY_FILE") or "").strip()
    if env_override:
        return Path(env_override).expanduser()
    xdg_cfg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    base_dir = Path(xdg_cfg).expanduser() if xdg_cfg else (Path.home() / ".config")
    return base_dir / "registerhub" / "local_master.key"


def _load_or_create_posix_master_key() -> bytes:
    """加载或原子创建当前用户私有 32 字节主密钥（0600 权限，独立于 SQLite 数据库存放）。"""
    key_path = _resolve_posix_master_key_path()
    with _LOCAL_KEY_LOCK:
        if key_path.exists():
            raw = key_path.read_bytes()
            if len(raw) != 32:
                raise SecretProtectionError(f"本地主密钥文件长度异常（需 32 字节）: {key_path}")
            return raw

        key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(key_path.parent, 0o700)
        except OSError:
            pass

        new_key = _py_secrets.token_bytes(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(str(key_path), flags, 0o600)
            try:
                os.write(fd, new_key)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
            return new_key
        except FileExistsError:
            raw = key_path.read_bytes()
            if len(raw) != 32:
                raise SecretProtectionError(f"本地主密钥文件长度异常（需 32 字节）: {key_path}")
            return raw
        except OSError as exc:
            raise SecretProtectionError(f"无法创建本地主密钥文件 ({key_path}): {exc}") from exc


def _xor_sha256_ctr(enc_key: bytes, nonce: bytes, data: bytes) -> bytes:
    out = bytearray(len(data))
    pos = 0
    counter = 0
    total = len(data)
    while pos < total:
        block = hashlib.sha256(enc_key + nonce + counter.to_bytes(4, "big")).digest()
        chunk_len = min(32, total - pos)
        for idx in range(chunk_len):
            out[pos + idx] = data[pos + idx] ^ block[idx]
        pos += chunk_len
        counter += 1
    return bytes(out)


def _protect_local_v1(plaintext: str) -> str:
    master_key = _load_or_create_posix_master_key()
    raw = plaintext.encode("utf-8")
    nonce = _py_secrets.token_bytes(16)
    enc_key = hmac.digest(master_key, b"rh-localv1-enc:" + nonce, "sha256")
    mac_key = hmac.digest(master_key, b"rh-localv1-mac:" + nonce, "sha256")
    ct = _xor_sha256_ctr(enc_key, nonce, raw)
    tag = hmac.digest(mac_key, b"rh-localv1:" + nonce + ct, "sha256")
    return _LOCAL_V1_PREFIX + base64.b64encode(nonce + tag + ct).decode("ascii")


def _unprotect_local_v1(ciphertext: str) -> str:
    try:
        blob = base64.b64decode(ciphertext[len(_LOCAL_V1_PREFIX):].encode("ascii"), validate=True)
    except Exception as exc:
        raise SecretProtectionError(f"localv1 密文 Base64 损坏: {exc}") from exc
    if len(blob) < 48:
        raise SecretProtectionError("localv1 密文长度不足（缺少 nonce/MAC）")
    nonce = blob[:16]
    tag = blob[16:48]
    ct = blob[48:]
    master_key = _load_or_create_posix_master_key()
    mac_key = hmac.digest(master_key, b"rh-localv1-mac:" + nonce, "sha256")
    expected_tag = hmac.digest(mac_key, b"rh-localv1:" + nonce + ct, "sha256")
    if not hmac.compare_digest(expected_tag, tag):
        raise SecretProtectionError("localv1 密文完整性校验失败（密文被篡改或密钥不匹配）")
    enc_key = hmac.digest(master_key, b"rh-localv1-enc:" + nonce, "sha256")
    raw = _xor_sha256_ctr(enc_key, nonce, ct)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretProtectionError(f"localv1 明文 UTF-8 解码失败: {exc}") from exc


def protect_secret(plaintext: str) -> str:
    """加密单条敏感字符串。Windows 使用当前用户 DPAPI；Linux/macOS 使用用户私有 0600 主密钥 + Encrypt-then-MAC。"""
    if not plaintext:
        return ""
    if sys.platform != "win32":
        return _protect_local_v1(plaintext)
    raw = plaintext.encode("utf-8")
    in_blob, _in_buf = _bytes_to_blob(raw)
    ent_blob, _ent_buf = _bytes_to_blob(_DPAPI_ENTROPY)
    out_blob = _DATA_BLOB()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "AutoRegisterStudio",
        ctypes.byref(ent_blob),
        None,
        None,
        0x01,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(out_blob),
    )
    if not ok:
        raise SecretProtectionError(f"DPAPI CryptProtectData 失败 (err={ctypes.GetLastError()})")
    try:
        enc = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            _kernel32.LocalFree(out_blob.pbData)
    return _DPAPI_PREFIX + base64.b64encode(enc).decode("ascii")


def unprotect_secret(ciphertext: str) -> str:
    """解密由 protect_secret 生成的密文（dpapi: 或 localv1:）；损坏、未知前缀或跨用户解密失败时显式抛 SecretProtectionError。"""
    if not ciphertext:
        return ""
    if ciphertext.startswith(_LOCAL_V1_PREFIX):
        return _unprotect_local_v1(ciphertext)
    if ciphertext.startswith(_DPAPI_PREFIX):
        if sys.platform != "win32":
            raise SecretProtectionError("当前非 Windows 环境，无法解密 DPAPI 密文")
        try:
            enc = base64.b64decode(ciphertext[len(_DPAPI_PREFIX):].encode("ascii"), validate=True)
        except Exception as exc:
            raise SecretProtectionError(f"DPAPI 密文 Base64 损坏: {exc}") from exc
        in_blob, _in_buf = _bytes_to_blob(enc)
        ent_blob, _ent_buf = _bytes_to_blob(_DPAPI_ENTROPY)
        out_blob = _DATA_BLOB()
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            ctypes.byref(ent_blob),
            None,
            None,
            0x01,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise SecretProtectionError(f"DPAPI CryptUnprotectData 失败 (err={ctypes.GetLastError()})")
        try:
            raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            if out_blob.pbData:
                _kernel32.LocalFree(out_blob.pbData)
        return raw.decode("utf-8")
    raise SecretProtectionError("未知密文前缀，拒绝按明文或伪编码透传")


def secret_fingerprint(value: str) -> str:
    """返回凭据的不可逆短指纹（用于去重，绝不暴露原文）。"""
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def mask_secret(value: str, *, head: int = 2, tail: int = 4) -> str:
    """脱敏展示任意敏感字符串。"""
    s = (value or "").strip()
    if not s:
        return ""
    if len(s) <= head + tail + 2:
        return "***"
    return f"{s[:head]}***{s[-tail:]}"


def sanitize_error_text(text: str, max_len: int = 240) -> str:
    """清理异常文本中可能夹带的代理账密、Bearer Token 或 OAuth 密钥后再入库/落日志。"""
    s = str(text or "").strip()
    if not s:
        return ""
    s = _CRED_URL_RE.sub(r"\1***@", s)
    s = _BEARER_RE.sub(r"\1***", s)
    s = _KV_SECRET_RE.sub(r"\1***", s)
    return s[:max_len]


def _format_host_port(host: str, port: int) -> str:
    h = host.strip().strip("[]")
    if ":" in h:
        return f"[{h}]:{port}"
    return f"{h}:{port}"


def mask_proxy_url(proxy_url: str) -> str:
    """将代理 URL 中的用户名/密码抹去，仅保留协议与主机端口。"""
    s = (proxy_url or "").strip()
    if not s:
        return ""
    try:
        parts = urlsplit(s)
        scheme = (parts.scheme or "http").lower()
        host = parts.hostname or ""
        port = parts.port
        if not host:
            return "***"
        hp = _format_host_port(host, port) if port else (f"[{host}]" if ":" in host else host)
        auth = "***@" if (parts.username is not None or parts.password is not None) else ""
        return f"{scheme}://{auth}{hp}"
    except Exception:
        return _CRED_URL_RE.sub(r"\1***@", s)


def mask_secret_url(url: str) -> str:
    """对含敏感 Token/Key 的提取链接、订阅链接或接码链接脱敏（抹去账密、查询串与长路径段）。"""
    s = (url or "").strip()
    if not s:
        return ""
    try:
        parts = urlsplit(s)
        scheme = (parts.scheme or "https").lower()
        host = parts.hostname or ""
        if not host:
            return "***"
        port = parts.port
        hp = _format_host_port(host, port) if port else (f"[{host}]" if ":" in host else host)
        raw_segs = [seg for seg in (parts.path or "").split("/") if seg]
        masked_segs: list[str] = []
        for idx, seg in enumerate(raw_segs[:4]):
            if idx == 0 and len(seg) <= 16 and re.fullmatch(r"[A-Za-z0-9._-]+", seg):
                masked_segs.append(seg)
            elif len(seg) > 8:
                masked_segs.append(mask_secret(seg, head=2, tail=2))
            else:
                masked_segs.append(seg)
        if len(raw_segs) > 4:
            masked_segs.append("***")
        path_str = ("/" + "/".join(masked_segs)) if masked_segs else ""
        query_str = "?***" if parts.query else ""
        return f"{scheme}://{hp}{path_str}{query_str}"
    except Exception:
        return "***"


def parse_proxy_line(raw: str, default_scheme: str = "http") -> dict[str, Any]:
    """解析单行代理字符串为规范结构。

    支持格式：
      - http://user:pass@host:port / https://... / socks5://... / socks5h://...
      - user:pass@host:port
      - host:port:user:pass
      - host:port
    """
    line = (raw or "").strip()
    if not line:
        raise ValueError("代理地址不能为空")
    if any(ch in line for ch in ("\r", "\n", "\x00")):
        raise ValueError("代理地址包含非法控制字符")

    def_scheme = (default_scheme or "http").strip().lower()
    if def_scheme not in _ALLOWED_PROXY_SCHEMES:
        raise ValueError(f"不支持的默认代理协议: {default_scheme!r}")

    scheme = def_scheme
    host = ""
    port = 0
    username = ""
    password = ""

    if "://" in line:
        parts = urlsplit(line)
        scheme = (parts.scheme or "").lower()
        if scheme not in _ALLOWED_PROXY_SCHEMES:
            raise ValueError(f"不支持的代理协议: {scheme!r}")
        if not parts.hostname:
            raise ValueError(f"代理主机名缺失: {mask_proxy_url(line)}")
        try:
            p = parts.port
        except ValueError as exc:
            raise ValueError(f"代理端口格式非法: {mask_proxy_url(line)}") from exc
        if not p or not (1 <= p <= 65535):
            raise ValueError(f"代理端口缺失或越界: {mask_proxy_url(line)}")
        host = parts.hostname.strip()
        port = int(p)
        username = urllib.parse.unquote(parts.username) if parts.username is not None else ""
        password = urllib.parse.unquote(parts.password) if parts.password is not None else ""
    elif re.match(r"^[^:@/\s]+:\d{1,5}:[^:\s]+:.+$", line):
        # 优先识别 host:port:user:pass（即便密码内含 @、:、/ 也不会被误判为 user:pass@host:port）
        h_seg, p_seg, u_seg, pw_seg = line.split(":", 3)
        host = h_seg.strip()
        port = int(p_seg)
        username = u_seg
        password = pw_seg
    elif "@" in line:
        parts = urlsplit(f"{def_scheme}://{line}")
        if not parts.hostname or not parts.port:
            raise ValueError("无法解析带认证代理格式，请使用 scheme://user:pass@host:port")
        host = parts.hostname.strip()
        port = int(parts.port)
        username = urllib.parse.unquote(parts.username) if parts.username is not None else ""
        password = urllib.parse.unquote(parts.password) if parts.password is not None else ""
    else:
        # 支持 host:port 与 IPv6 [::1]:port
        if line.startswith("["):
            parts = urlsplit(f"{def_scheme}://{line}")
            if not parts.hostname or not parts.port:
                raise ValueError("IPv6 代理格式应为 [host]:port")
            host = parts.hostname.strip()
            port = int(parts.port)
        else:
            segs = line.split(":")
            if len(segs) == 2:
                host = segs[0].strip()
                if not segs[1].isdigit():
                    raise ValueError(f"代理端口非数字: {line!r}")
                port = int(segs[1])
            else:
                raise ValueError("无法识别的代理格式，支持 http://u:p@ip:port、socks5://ip:port、ip:port:u:p 或 ip:port")

    if not host or not (1 <= port <= 65535):
        raise ValueError("代理主机或端口无效")
    if any(ch.isspace() for ch in host):
        raise ValueError("代理主机名不能包含空白字符")

    hp = _format_host_port(host, port)
    server = f"{scheme}://{hp}"
    if username or password:
        u_q = urllib.parse.quote(username, safe="")
        p_q = urllib.parse.quote(password, safe="")
        full_url = f"{scheme}://{u_q}:{p_q}@{hp}"
        masked = f"{scheme}://***@{hp}"
    else:
        full_url = server
        masked = server

    has_auth = bool(username or password)
    browser_compatible = not (scheme in ("socks5", "socks5h") and has_auth)

    return {
        "scheme": scheme,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "server": server,
        "url": full_url,
        "masked_url": masked,
        "has_auth": has_auth,
        "browser_compatible": browser_compatible,
    }


def proxy_url_to_playwright_dict(proxy_url: str, *, bypass: str = "") -> dict[str, str]:
    """将完整代理 URL 拆成 Playwright 标准结构化代理对象（server 中绝不夹带账密）。"""
    parsed = parse_proxy_line(proxy_url)
    out: dict[str, str] = {"server": parsed["server"]}
    if parsed["username"]:
        out["username"] = parsed["username"]
    if parsed["password"]:
        out["password"] = parsed["password"]
    if bypass:
        out["bypass"] = bypass
    return out


def proxy_dict_to_url(proxy: dict[str, Any] | None) -> str | None:
    """将 Playwright 代理字典还原为完整协议 URL（保留原协议并正确转义账密特殊字符）。"""
    if not proxy or not proxy.get("server"):
        return None
    raw_server = str(proxy["server"]).strip()
    if not raw_server:
        return None
    parsed = parse_proxy_line(raw_server)
    username = str(proxy.get("username") if proxy.get("username") is not None else parsed["username"] or "")
    password = str(proxy.get("password") if proxy.get("password") is not None else parsed["password"] or "")
    hp = _format_host_port(parsed["host"], parsed["port"])
    if username or password:
        return f"{parsed['scheme']}://{urllib.parse.quote(username, safe='')}:{urllib.parse.quote(password, safe='')}@{hp}"
    return f"{parsed['scheme']}://{hp}"
