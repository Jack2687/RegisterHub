"""公网 HTTP(S) 目标地址校验与防 SSRF 重定向网关 (Outbound Public Network Guard)。

统一拦截以下内网探测与元数据窃取向量（SEC-05）：
1. 非 http/https 协议（file://、ftp://、gopher://、javascript: 等）与内嵌凭据 URL；
2. 本机回环与保留主机名（localhost、*.localhost、*.local、*.internal、metadata.google.internal）；
3. 十进制整数 IP、十六进制/八进制混淆 IPv4（如 2130706433、0x7f000001、0177.0.0.1）；
4. IPv4 / IPv6（含 ::ffff:127.0.0.1 映射地址）私网段、回环段、链路本地段（169.254.169.254）、CGNAT（100.64.0.0/10）；
5. 域名 DNS 解析落地到上述任意非全局公网 IP，以及 30x HTTP 重定向跳往内网地址。
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.parse
import urllib.request
from urllib.parse import urlsplit


class PrivateAddressBlockedError(ValueError):
    """目标 URL 指向本机回环、局域网私网、云元数据或保留地址时抛出。"""


_BLOCKED_EXACT_HOSTS = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "instance-data",
})
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".home.arpa",
    ".arpa",
)
_HEX_INT_RE = re.compile(r"^0x[0-9a-f]+$", re.I)
_UNICODE_DOTS_TABLE = str.maketrans({
    "\u3002": ".",  # Ideographic Full Stop '。'
    "\uff0e": ".",  # Fullwidth Full Stop '．'
    "\uff61": ".",  # Halfwidth Ideographic Full Stop '｡'
})
_ALLOWED_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})
# Clash / Mihomo / Sing-box TUN 模式对公网域名统一分配 RFC 2544 (198.18.0.0/15) Fake-IP 虚拟映射
_FAKE_IP_BENCHMARK_NET = ipaddress.ip_network("198.18.0.0/15")


def _is_public_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        ip_obj = ip_obj.ipv4_mapped
    if (
        ip_obj.is_loopback
        or ip_obj.is_private
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    ):
        return False
    return bool(ip_obj.is_global)


def _looks_like_obfuscated_ip(host: str) -> bool:
    """识别整数/八进制/0x十六进制/简写点分混淆 IP，但不误杀 cafe.de / 0x.org / dead.beef 等正常域名。"""
    if not host or ":" in host:
        return False
    if host.isdigit() or _HEX_INT_RE.fullmatch(host):
        return True
    if "." in host:
        parts = host.split(".")
        if all(seg.isdigit() or bool(_HEX_INT_RE.fullmatch(seg)) for seg in parts):
            if len(parts) != 4:
                return True
            if any(
                bool(_HEX_INT_RE.fullmatch(seg)) or (len(seg) > 1 and seg.startswith("0"))
                for seg in parts
            ):
                return True
    return False


def validate_public_http_url(url: str, *, resolve_dns: bool = True) -> str:
    """校验目标 URL 必须为公网可达的 http(s) 地址；违规抛 ValueError / PrivateAddressBlockedError。"""
    clean = (url or "").strip()
    if not clean:
        raise ValueError("目标 URL 不能为空")

    parts = urlsplit(clean)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https") or not parts.netloc or not parts.hostname:
        raise ValueError(f"非法的目标 URL（仅允许 http:// 或 https://）: {clean!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError("目标 URL 不允许内嵌用户名或密码凭据")

    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"目标 URL 端口非法: {clean!r}") from exc

    host = parts.hostname.strip().translate(_UNICODE_DOTS_TABLE).rstrip(".").lower()
    if not host or ".." in host:
        raise ValueError(f"目标 URL 主机名非法: {clean!r}")
    if "%" in host:
        raise PrivateAddressBlockedError(f"禁止包含网卡作用域标识的地址: {host}")
    try:
        host_ascii = host.encode("idna").decode("ascii").rstrip(".").lower()
    except UnicodeError as exc:
        raise ValueError(f"目标 URL 域名编码非法: {host!r}") from exc
    if (
        host in _BLOCKED_EXACT_HOSTS
        or host.endswith(_BLOCKED_HOST_SUFFIXES)
        or host_ascii in _BLOCKED_EXACT_HOSTS
        or host_ascii.endswith(_BLOCKED_HOST_SUFFIXES)
    ):
        raise PrivateAddressBlockedError(f"禁止访问本机回环或内网保留主机名: {host}")
    host = host_ascii

    # 先按标准 IP 字面量判定
    ip_literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        ip_literal = ipaddress.ip_address(host)
    except ValueError:
        if _looks_like_obfuscated_ip(host):
            raise PrivateAddressBlockedError(f"禁止使用整数/八进制/十六进制混淆 IP 地址: {host}")

    if ip_literal is not None:
        if not _is_public_ip(ip_literal):
            raise PrivateAddressBlockedError(f"禁止访问内网/回环/元数据保留 IP: {ip_literal}")
        return clean

    if "." not in host:
        raise PrivateAddressBlockedError(f"禁止访问无公网顶级域的单标签内网主机名: {host}")

    if resolve_dns:
        eff_port = port or (443 if scheme == "https" else 80)
        try:
            infos = socket.getaddrinfo(host, eff_port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError(f"无法解析目标域名 DNS: {host} ({exc})") from exc
        if not infos:
            raise ValueError(f"目标域名 DNS 解析结果为空: {host}")
        for item in infos:
            sockaddr = item[4]
            if not sockaddr:
                continue
            raw_ip = str(sockaddr[0]).split("%", 1)[0]
            try:
                resolved_ip = ipaddress.ip_address(raw_ip)
            except ValueError as exc:
                raise PrivateAddressBlockedError(f"DNS 返回异常地址 {raw_ip!r}") from exc
            if not _is_public_ip(resolved_ip):
                if isinstance(resolved_ip, ipaddress.IPv4Address) and resolved_ip in _FAKE_IP_BENCHMARK_NET:
                    continue
                raise PrivateAddressBlockedError(
                    f"目标域名 {host} 解析到受限内网/回环/元数据地址 {resolved_ip}，已安全阻断"
                )

    return clean


def validate_proxy_url(proxy_url: str) -> str:
    """校验代理地址格式合法性（允许连接本机代理端口，但限制协议与结构）。"""
    clean = (proxy_url or "").strip()
    if not clean:
        return ""
    parts = urlsplit(clean)
    if (parts.scheme or "").lower() not in _ALLOWED_PROXY_SCHEMES or not parts.netloc or not parts.hostname:
        raise ValueError(f"非法的代理地址格式: {clean!r}")
    try:
        _ = parts.port
    except ValueError as exc:
        raise ValueError(f"代理端口格式非法: {clean!r}") from exc
    return clean


_SENSITIVE_CROSS_ORIGIN_HEADERS = frozenset({
    "authorization",
    "cookie",
    "proxy-authorization",
    "x-api-key",
    "x-auth-token",
    "x-web-token",
})


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """每次 30x 跳转前重新校验落地 URL 与 DNS，跨源跳转自动剥离敏感鉴权头。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        resolved_target = urllib.parse.urljoin(req.full_url, newurl)
        validate_public_http_url(resolved_target, resolve_dns=True)
        redirected = super().redirect_request(req, fp, code, msg, headers, resolved_target)
        if redirected is not None:
            orig_origin = (urlsplit(req.full_url).scheme.lower(), (urlsplit(req.full_url).netloc or "").lower())
            new_origin = (urlsplit(resolved_target).scheme.lower(), (urlsplit(resolved_target).netloc or "").lower())
            if orig_origin != new_origin:
                for hdr_map in (redirected.headers, redirected.unredirected_hdrs):
                    for k in list(hdr_map.keys()):
                        if str(k).lower() in _SENSITIVE_CROSS_ORIGIN_HEADERS:
                            hdr_map.pop(k, None)
        return redirected


def strip_sensitive_cross_origin_headers(
    headers: dict[str, Any] | None,
    from_url: str,
    to_url: str,
) -> dict[str, Any]:
    """跨源 (scheme, netloc) 跳转时原地/副本剥离敏感鉴权请求头。"""
    if not headers:
        return {}
    orig_parts = urlsplit(from_url)
    next_parts = urlsplit(to_url)
    orig_origin = (orig_parts.scheme.lower(), (orig_parts.netloc or "").lower())
    next_origin = (next_parts.scheme.lower(), (next_parts.netloc or "").lower())
    if orig_origin == next_origin:
        return dict(headers)
    return {
        k: v
        for k, v in headers.items()
        if str(k).lower() not in _SENSITIVE_CROSS_ORIGIN_HEADERS
    }


def _strip_session_sensitive_headers(session_obj: Any, _seen: set[int] | None = None) -> None:
    if session_obj is None:
        return
    visited = _seen if _seen is not None else set()
    obj_id = id(session_obj)
    if obj_id in visited:
        return
    visited.add(obj_id)
    for attr in ("headers",):
        hdrs = getattr(session_obj, attr, None)
        if hdrs is not None and hasattr(hdrs, "keys"):
            for k in list(hdrs.keys()):
                if str(k).lower() in _SENSITIVE_CROSS_ORIGIN_HEADERS:
                    try:
                        hdrs.pop(k, None)
                    except Exception:
                        pass
    cookies = getattr(session_obj, "cookies", None)
    if cookies is not None:
        if hasattr(cookies, "clear"):
            try:
                cookies.clear()
            except Exception:
                pass
        elif hasattr(cookies, "keys"):
            for ck in list(cookies.keys()):
                try:
                    cookies.pop(ck, None)
                except Exception:
                    pass
    for sub_attr in ("_session", "session"):
        inner = getattr(session_obj, sub_attr, None)
        if inner is not None and inner is not session_obj:
            _strip_session_sensitive_headers(inner, visited)


def safe_follow_redirects(
    fetch_step,
    initial_url: str,
    *,
    headers: dict[str, Any] | None = None,
    session: Any = None,
    max_redirects: int = 5,
):
    """逐跳执行公网 URL + DNS 校验并在跨源跳转时剥离敏感鉴权头，彻底阻断 30x SSRF 与凭据泄漏。"""
    import inspect as _inspect

    current_url = validate_public_http_url(initial_url, resolve_dns=True)
    cur_headers: dict[str, Any] = dict(headers) if headers else {}
    accepts_headers = False
    try:
        sig = _inspect.signature(fetch_step)
        params = list(sig.parameters.values())
        accepts_headers = len(params) >= 2 or any(
            p.kind in (_inspect.Parameter.VAR_POSITIONAL, _inspect.Parameter.VAR_KEYWORD)
            or p.name == "headers"
            for p in params
        )
    except (TypeError, ValueError):
        accepts_headers = False

    for _ in range(max_redirects + 1):
        resp = fetch_step(current_url, dict(cur_headers)) if accepts_headers else fetch_step(current_url)
        status = int(getattr(resp, "status_code", 0) or 0)
        hdrs = getattr(resp, "headers", None) or {}
        loc = ""
        if isinstance(hdrs, dict):
            loc = str(hdrs.get("Location") or hdrs.get("location") or "").strip()
        else:
            try:
                loc = str(hdrs.get("Location") or hdrs.get("location") or "").strip()
            except Exception:
                loc = ""
        if status in (301, 302, 303, 307, 308) and loc:
            next_url = urllib.parse.urljoin(current_url, loc)
            validated_next = validate_public_http_url(next_url, resolve_dns=True)
            orig_parts = urlsplit(current_url)
            next_parts = urlsplit(validated_next)
            if (orig_parts.scheme.lower(), (orig_parts.netloc or "").lower()) != (
                next_parts.scheme.lower(),
                (next_parts.netloc or "").lower(),
            ):
                cur_headers = strip_sensitive_cross_origin_headers(cur_headers, current_url, validated_next)
                if headers is not None and hasattr(headers, "keys"):
                    for hk in list(headers.keys()):
                        if str(hk).lower() in _SENSITIVE_CROSS_ORIGIN_HEADERS:
                            try:
                                headers.pop(hk, None)
                            except Exception:
                                pass
                _strip_session_sensitive_headers(session)
            current_url = validated_next
            continue
        final_u = str(getattr(resp, "url", "") or "").strip()
        if final_u:
            validate_public_http_url(final_u, resolve_dns=True)
        return resp, current_url
    raise ValueError(f"HTTP 重定向次数超过上限 ({max_redirects})")


def build_safe_urllib_opener(proxy: str = "", *, disable_env_proxy: bool = False) -> urllib.request.OpenerDirector:
    """构建带防 SSRF 重定向守卫的 urllib Opener（标准库 urllib 仅支持 http/https 代理）。"""
    clean_px = validate_proxy_url(proxy)
    handlers: list[urllib.request.BaseHandler] = []
    if clean_px:
        px_scheme = (urlsplit(clean_px).scheme or "").lower()
        if px_scheme not in ("http", "https"):
            raise ValueError(
                f"urllib 仅支持 http:// 或 https:// 代理，不支持 {px_scheme}://（请改用 HTTP 代理或支持 SOCKS 的客户端）"
            )
        handlers.append(urllib.request.ProxyHandler({"http": clean_px, "https": clean_px}))
    elif disable_env_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    handlers.append(_SafeRedirectHandler())
    return urllib.request.build_opener(*handlers)
