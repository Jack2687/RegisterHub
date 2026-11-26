"""Multi-Signal IP Purity & Risk Scoring Engine (Zero-Dependency Stdlib).

Evaluates exit IP quality (0–100 purity score, S/A/B/D grade) using live
ASN / hosting / datacenter / proxy / VPN / Tor / abuse intelligence and
optional target-site passability probes.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

_IP_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$|^[0-9a-fA-F:]{3,45}$")
_CLOUD_KEYWORDS = (
    "amazon",
    "aws",
    "google cloud",
    "google llc",
    "digitalocean",
    "vultr",
    "linode",
    "akamai connected cloud",
    "hetzner",
    "ovh",
    "alibaba",
    "aliyun",
    "tencent",
    "oracle cloud",
    "microsoft corporation",
    "azure",
    "cloudflare",
    "choopa",
    "contabo",
    "m247",
    "datacamp",
    "leaseweb",
)


def _fetch_json(url: str, proxy_url: str = "", timeout: float = 7.0) -> Optional[Dict[str, Any]]:
    handlers = [urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})] if proxy_url else []
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "AutoRegister-IPScorer/1.0", "Accept": "application/json"},
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(131072).decode("utf-8", errors="ignore")
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def _validate_http_url(url: str, *, resolve_dns: bool = False) -> str:
    from .net_guard import validate_public_http_url

    return validate_public_http_url(url, resolve_dns=resolve_dns)


def _probe_target(target_url: str, proxy_url: str = "", timeout: float = 8.0) -> Dict[str, Any]:
    from .net_guard import build_safe_urllib_opener

    t0 = time.perf_counter()
    try:
        target_url = _validate_http_url(target_url, resolve_dns=True)
        opener = build_safe_urllib_opener(proxy_url)
    except Exception as e:
        ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return {
            "url": target_url,
            "status_code": 0,
            "passed": False,
            "challenge": False,
            "latency_ms": ms,
            "detail": f"目标地址或代理非法，已跳过: {str(e)[:90]}",
        }
    req = urllib.request.Request(
        target_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/json,*/*;q=0.8",
        },
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            ms = round((time.perf_counter() - t0) * 1000.0, 1)
            cf_mit = (resp.headers.get("cf-mitigated") or "").lower()
            blocked = bool(cf_mit)
            return {
                "url": target_url,
                "status_code": resp.status,
                "passed": not blocked and resp.status < 400,
                "challenge": blocked,
                "latency_ms": ms,
                "detail": "CF Challenge 拦截" if blocked else f"HTTP {resp.status} 放行",
            }
    except urllib.error.HTTPError as he:
        ms = round((time.perf_counter() - t0) * 1000.0, 1)
        cf_mit = (he.headers.get("cf-mitigated") or "").lower() if he.headers else ""
        return {
            "url": target_url,
            "status_code": he.code,
            "passed": False,
            "challenge": bool(cf_mit) or he.code == 403,
            "latency_ms": ms,
            "detail": f"HTTP {he.code} ({'人机盾/风控拦截' if he.code == 403 else '限流' if he.code == 429 else '拒绝'})",
        }
    except Exception as e:
        ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return {
            "url": target_url,
            "status_code": 0,
            "passed": False,
            "challenge": False,
            "latency_ms": ms,
            "detail": f"连接失败: {str(e)[:90]}",
        }


def _parse_abuser_score(raw: Any) -> float:
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)", raw)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return 0.0


def score_ip(
    *,
    ip: str = "",
    proxy_url: str = "",
    target_url: str = "",
    timeout: float = 8.0,
) -> Dict[str, Any]:
    """Probe an IP (directly or through proxy_url) and compute a 0–100 purity score."""
    clean_ip = (ip or "").strip()
    clean_proxy = (proxy_url or "").strip()
    raw_target = (target_url or "").strip()
    clean_target = ""
    target_validation_err = ""
    if raw_target:
        try:
            clean_target = _validate_http_url(raw_target)
        except Exception as exc:
            target_validation_err = str(exc)
    t0 = time.perf_counter()
    intel_timeout = min(float(timeout), 6.0)

    # Step 1 & 2: Query api.ipapi.is (returns exit IP + full intel in 1 RTT) and ip-api.com concurrently
    # Fallback to ipify ONLY if exit IP is still unknown.
    meta_proxy = clean_proxy if not clean_ip else ""
    ipcom_fields = "status,message,country,countryCode,regionName,city,zip,lat,lon,timezone,isp,org,as,asname,mobile,proxy,hosting,query"
    encoded_ip = quote(clean_ip, safe=":") if clean_ip else ""
    q_ipapi = f"https://api.ipapi.is/?q={encoded_ip}" if encoded_ip else "https://api.ipapi.is/"
    q_ipcom = f"http://ip-api.com/json/{encoded_ip}?fields={ipcom_fields}" if encoded_ip else f"http://ip-api.com/json/?fields={ipcom_fields}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_d1 = ex.submit(_fetch_json, q_ipapi, meta_proxy, intel_timeout)
        f_d2 = ex.submit(_fetch_json, q_ipcom, meta_proxy, intel_timeout)
        d1 = f_d1.result()
        d2 = f_d2.result()

    # api.ipapi.is 限流或报错时返回 {"error": "..."}，必须置空，防止跳过兜底或误判成功
    if isinstance(d1, dict) and (d1.get("error") or (not clean_ip and not d1.get("ip"))):
        d1 = None

    if d2 and d2.get("status") != "success":
        d2 = None

    if not clean_ip:
        if d1 and d1.get("ip"):
            clean_ip = str(d1["ip"]).strip()
            # 轮换代理场景若两次并发命中的出口 IP 不一致，丢弃错位的 d2 防止信号串号
            if d2 and d2.get("query") and str(d2["query"]).strip() != clean_ip:
                d2 = None
        elif d2 and d2.get("query"):
            clean_ip = str(d2["query"]).strip()

    if not clean_ip:
        ip_resp = _fetch_json("https://api.ipify.org?format=json", proxy_url=clean_proxy, timeout=intel_timeout)
        if ip_resp and ip_resp.get("ip"):
            clean_ip = str(ip_resp["ip"]).strip()
            if not d2:
                d2 = _fetch_json(f"http://ip-api.com/json/{quote(clean_ip, safe=':')}?fields={ipcom_fields}", proxy_url="", timeout=intel_timeout)
                if d2 and d2.get("status") != "success":
                    d2 = None

    if not clean_ip:
        return {
            "ok": False,
            "ip": "",
            "proxy": clean_proxy or "direct",
            "score": 0,
            "grade": "D",
            "verdict": "代理不可达或出口 IP 探测失败",
            "error": "无法解析出口 IP，请检查代理连通性或网络状态",
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "factors": [],
            "signals": {},
        }

    # Step 3: Merge structured signals（同时兼容 api.ipapi.is 嵌套 dict 与扁平 str 两种响应格式）
    raw_loc = d1.get("location") if isinstance(d1, dict) else None
    raw_asn = d1.get("asn") if isinstance(d1, dict) else None
    raw_comp = d1.get("company") if isinstance(d1, dict) else None
    loc1 = raw_loc if isinstance(raw_loc, dict) else {}
    asn1 = raw_asn if isinstance(raw_asn, dict) else {}
    comp1 = raw_comp if isinstance(raw_comp, dict) else {}

    flat_country = str(d1.get("country") or "").strip() if isinstance(d1, dict) and isinstance(d1.get("country"), str) else ""
    flat_region = str(d1.get("region") or "").strip() if isinstance(d1, dict) and isinstance(d1.get("region"), str) else ""
    flat_city = str(d1.get("city") or "").strip() if isinstance(d1, dict) and isinstance(d1.get("city"), str) else ""
    flat_tz = str(d1.get("timezone") or "").strip() if isinstance(d1, dict) and isinstance(d1.get("timezone"), str) else ""
    flat_comp = raw_comp.strip() if isinstance(raw_comp, str) else ""
    flat_asn_num = ""
    flat_asn_org = ""
    if isinstance(raw_asn, str) and raw_asn.strip():
        asn_parts = raw_asn.strip().split(" ", 1)
        flat_asn_num = asn_parts[0]
        flat_asn_org = asn_parts[1].strip() if len(asn_parts) > 1 else ""

    country = str(loc1.get("country") or (d2 or {}).get("country") or flat_country or "")
    country_code = str(
        loc1.get("country_code")
        or (d2 or {}).get("countryCode")
        or (flat_country if len(flat_country) == 2 and flat_country.isalpha() else "")
    )
    region = str(loc1.get("state") or (d2 or {}).get("regionName") or flat_region or "")
    city = str(loc1.get("city") or (d2 or {}).get("city") or flat_city or "")
    timezone = str(loc1.get("timezone") or (d2 or {}).get("timezone") or flat_tz or "")
    isp = str(comp1.get("name") or asn1.get("org") or (d2 or {}).get("isp") or flat_comp or flat_asn_org or "")
    org = str(asn1.get("org") or (d2 or {}).get("org") or flat_asn_org or isp)
    asn_str = str(
        f"AS{asn1['asn']}"
        if asn1.get("asn")
        else (((d2 or {}).get("as") or "").split(" ")[0] or flat_asn_num)
    )

    is_datacenter = bool((d1 or {}).get("is_datacenter") or (d2 or {}).get("hosting") or str(comp1.get("type") or "").lower() == "hosting")
    is_mobile = bool((d1 or {}).get("is_mobile") or (d2 or {}).get("mobile"))
    is_proxy = bool((d1 or {}).get("is_proxy") or (d2 or {}).get("proxy"))
    is_vpn = bool((d1 or {}).get("is_vpn"))
    is_tor = bool((d1 or {}).get("is_tor"))
    is_abuser = bool((d1 or {}).get("is_abuser"))
    is_crawler = bool((d1 or {}).get("is_crawler"))
    is_bogon = bool((d1 or {}).get("is_bogon"))
    abuser_score = max(
        _parse_abuser_score(comp1.get("abuser_score")),
        _parse_abuser_score(asn1.get("abuser_score")),
    )
    isp_low = f"{isp} {org}".lower()
    is_cloud_org = any(k in isp_low for k in _CLOUD_KEYWORDS)

    # Step 4: Transparent scoring deduction breakdown
    score = 100
    factors: List[Dict[str, Any]] = []

    if is_tor:
        score -= 50
        factors.append({"label": "Tor 匿名出口", "delta": -50, "tone": "rose"})
    if is_datacenter:
        score -= 35
        factors.append({"label": "数据中心 / 机房段 (Datacenter)", "delta": -35, "tone": "rose"})
    elif is_cloud_org:
        score -= 22
        factors.append({"label": "公有云 / 托管商 ASN", "delta": -22, "tone": "amber"})
    elif is_mobile:
        factors.append({"label": "原生移动蜂窝网络 (Mobile)", "delta": 0, "tone": "emerald"})
    else:
        factors.append({"label": "住宅 / 常规 ISP 宽带", "delta": 0, "tone": "emerald"})

    if is_vpn or is_proxy:
        score -= 20
        factors.append({"label": "命中公共代理 / VPN 指纹库", "delta": -20, "tone": "amber"})
    if is_abuser:
        score -= 25
        factors.append({"label": "命中高危滥用黑名单 (Abuser)", "delta": -25, "tone": "rose"})
    elif abuser_score >= 0.05:
        score -= 22
        factors.append({"label": f"ASN 历史滥用率偏高 ({abuser_score:.4f})", "delta": -22, "tone": "rose"})
    elif abuser_score >= 0.01:
        score -= 10
        factors.append({"label": f"ASN 存在轻度滥用记录 ({abuser_score:.4f})", "delta": -10, "tone": "amber"})
    elif abuser_score > 0:
        score -= 3
        factors.append({"label": f"ASN 滥用率极低 ({abuser_score:.4f})", "delta": -3, "tone": "neutral"})

    if is_crawler or is_bogon:
        score -= 18
        factors.append({"label": "爬虫 / 保留地址段", "delta": -18, "tone": "amber"})

    # Optional target-site passability probe
    target_probe: Optional[Dict[str, Any]] = None
    if target_validation_err:
        target_probe = {
            "url": raw_target,
            "status_code": 0,
            "passed": False,
            "challenge": False,
            "latency_ms": 0.0,
            "detail": f"目标 URL 非法或受限已跳过: {target_validation_err[:120]}",
        }
    elif clean_target:
        target_probe = _probe_target(clean_target, proxy_url=clean_proxy, timeout=timeout)
        if target_probe["passed"]:
            factors.append({"label": f"目标站直连放行 ({target_probe['status_code']})", "delta": 0, "tone": "emerald"})
        elif target_probe["status_code"] == 429:
            score -= 20
            factors.append({"label": "目标站触发 429 频控", "delta": -20, "tone": "rose"})
        elif target_probe["challenge"] or target_probe["status_code"] == 403:
            score -= 15
            factors.append({"label": "目标站触发 403 / 人机盾", "delta": -15, "tone": "amber"})
        else:
            score -= 25
            factors.append({"label": f"目标站不可达 ({target_probe['detail']})", "delta": -25, "tone": "rose"})

    score = max(0, min(100, score))
    if score >= 85:
        grade, verdict = "S", "极净原生住宅 · 推荐跑高风控站（Clerk / Stripe / AI 平台）"
    elif score >= 65:
        grade, verdict = "A", "优质可用出口 · 适合绝大多数注册流程，建议单 IP 控频"
    elif score >= 45:
        grade, verdict = "B", "轻度风险 / 机房特征 · 可能触发 Turnstile 人机校验"
    else:
        grade, verdict = "D", "高危污染 / 黑名单段 · 强烈建议剔除或切换节点"

    net_type = (
        "移动蜂窝 (Mobile)"
        if is_mobile
        else ("机房数据中心 (Datacenter)" if (is_datacenter or is_cloud_org) else ("商业宽带 (Business)" if str(comp1.get("type") or "").lower() == "business" else "住宅宽带 (Residential)"))
    )

    return {
        "ok": True,
        "ip": clean_ip,
        "proxy": clean_proxy or "direct",
        "score": score,
        "grade": grade,
        "verdict": verdict,
        "ip_type": net_type,
        "residential": not is_datacenter and not is_cloud_org and not is_tor,
        "country": country,
        "country_code": country_code,
        "region": region,
        "city": city,
        "timezone": timezone,
        "isp": isp,
        "org": org,
        "asn": asn_str,
        "abuser_score": round(abuser_score, 5),
        "signals": {
            "datacenter": is_datacenter,
            "mobile": is_mobile,
            "proxy": is_proxy,
            "vpn": is_vpn,
            "tor": is_tor,
            "abuser": is_abuser,
            "cloud_org": is_cloud_org,
        },
        "factors": factors,
        "target_probe": target_probe,
        "sources": [s for s, d in (("ipapi.is", d1), ("ip-api.com", d2)) if d],
        "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
    }


def score_batch(
    entries: List[Dict[str, str]],
    target_url: str = "",
    max_workers: int = 6,
) -> Dict[str, Any]:
    """Concurrently score up to 24 IPs or proxy URLs, sorted highest score first."""
    raw_target = (target_url or "").strip()
    clean_target = ""
    target_warning = ""
    if raw_target:
        try:
            clean_target = _validate_http_url(raw_target)
        except Exception as exc:
            target_warning = str(exc)
    capped = entries[:24]

    def _one(ent: Dict[str, str]) -> Dict[str, Any]:
        raw = str(ent.get("value") or ent.get("proxy") or ent.get("ip") or "").strip()
        label = str(ent.get("label") or raw)
        if _IP_RE.fullmatch(raw):
            res = score_ip(ip=raw, target_url=clean_target)
        else:
            res = score_ip(proxy_url=raw, target_url=clean_target)
        res["label"] = label
        return res

    results: List[Dict[str, Any]] = []
    if capped:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(capped))) as ex:
            results = list(ex.map(_one, capped))

    results.sort(key=lambda r: (r.get("score", 0), 1 if r.get("ok") else 0), reverse=True)
    grade_counts = {"S": 0, "A": 0, "B": 0, "D": 0}
    for r in results:
        g = str(r.get("grade") or "D")
        if g in grade_counts:
            grade_counts[g] += 1
    avg_score = round(sum(r.get("score", 0) for r in results) / len(results), 1) if results else 0.0
    out: Dict[str, Any] = {
        "total": len(results),
        "avg_score": avg_score,
        "grades": grade_counts,
        "items": results,
    }
    if target_warning:
        out["target_warning"] = f"目标 URL 非法或受限已跳过探测: {target_warning}"
    return out
