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


def _probe_target(target_url: str, proxy_url: str = "", timeout: float = 8.0) -> Dict[str, Any]:
    handlers = [urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})] if proxy_url else []
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        target_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/json,*/*;q=0.8",
        },
    )
    t0 = time.perf_counter()
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
    t0 = time.perf_counter()

    # Step 1: Resolve exit IP if caller gave proxy_url (or wants current exit IP)
    if not clean_ip:
        ip_resp = _fetch_json("https://api.ipify.org?format=json", proxy_url=clean_proxy, timeout=timeout)
        if ip_resp and ip_resp.get("ip"):
            clean_ip = str(ip_resp["ip"]).strip()

    # Step 2: Query primary risk intelligence (api.ipapi.is) + fallback (ip-api.com)
    # Note: when scoring a known IP string, query metadata directly (faster & works even if proxy only permits target)
    meta_proxy = clean_proxy if not clean_ip else ""
    q_ipapi = f"https://api.ipapi.is/?q={clean_ip}" if clean_ip else "https://api.ipapi.is/"
    d1 = _fetch_json(q_ipapi, proxy_url=meta_proxy, timeout=timeout)

    if not clean_ip and d1 and d1.get("ip"):
        clean_ip = str(d1["ip"]).strip()

    q_ipcom = (
        f"http://ip-api.com/json/{clean_ip}?fields=status,message,country,countryCode,regionName,city,zip,lat,lon,timezone,isp,org,as,asname,mobile,proxy,hosting,query"
        if clean_ip
        else "http://ip-api.com/json/?fields=status,message,country,countryCode,regionName,city,zip,lat,lon,timezone,isp,org,as,asname,mobile,proxy,hosting,query"
    )
    d2 = _fetch_json(q_ipcom, proxy_url=meta_proxy, timeout=timeout)
    if d2 and d2.get("status") != "success":
        d2 = None

    if not clean_ip and d2 and d2.get("query"):
        clean_ip = str(d2["query"]).strip()

    if not clean_ip and not d1 and not d2:
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

    # Step 3: Merge structured signals
    loc1 = (d1.get("location") or {}) if isinstance(d1, dict) else {}
    asn1 = (d1.get("asn") or {}) if isinstance(d1, dict) else {}
    comp1 = (d1.get("company") or {}) if isinstance(d1, dict) else {}

    country = str(loc1.get("country") or (d2 or {}).get("country") or "")
    country_code = str(loc1.get("country_code") or (d2 or {}).get("countryCode") or "")
    region = str(loc1.get("state") or (d2 or {}).get("regionName") or "")
    city = str(loc1.get("city") or (d2 or {}).get("city") or "")
    timezone = str(loc1.get("timezone") or (d2 or {}).get("timezone") or "")
    isp = str(comp1.get("name") or asn1.get("org") or (d2 or {}).get("isp") or "")
    org = str(asn1.get("org") or (d2 or {}).get("org") or isp)
    asn_str = str(f"AS{asn1['asn']}" if asn1.get("asn") else ((d2 or {}).get("as") or "").split(" ")[0])

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
    if target_url.strip():
        target_probe = _probe_target(target_url.strip(), proxy_url=clean_proxy, timeout=timeout)
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
    capped = entries[:24]

    def _one(ent: Dict[str, str]) -> Dict[str, Any]:
        raw = str(ent.get("value") or ent.get("proxy") or ent.get("ip") or "").strip()
        label = str(ent.get("label") or raw)
        if _IP_RE.fullmatch(raw):
            res = score_ip(ip=raw, target_url=target_url)
        else:
            res = score_ip(proxy_url=raw, target_url=target_url)
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
    return {
        "total": len(results),
        "avg_score": avg_score,
        "grades": grade_counts,
        "items": results,
    }
