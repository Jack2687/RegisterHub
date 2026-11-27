"""LLM Token 消耗统计、缓存省流核算与多维聚合引擎。

负责：
  - 将 SQLite 持久化小时桶 (`llm_usage_hourly`) 与会话全量台账 (`agent_sessions`) 聚合为前端图表、热力图、模型/渠道/会话排行；
  - 守恒拆分输入 Token（直算输入 + 缓存读取 + 缓存写入 = 总输入），并采用诚实 1 位小数缓存命中率（未满 100% 不虚标 100%）；
  - 按主流模型公开标价估算参考费用与缓存节省额，明确区分已匹配单价、免费模型与未知模型。
"""

from __future__ import annotations

from contextlib import nullcontext
import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Any


# (匹配正则, input $/1M, output $/1M, cache_read $/1M, cache_write $/1M, 计价档位说明)
_PRICING_RULES: tuple[tuple[re.Pattern[str], float, float, float, float, str], ...] = (
    # 明确免费路由或本地模型
    (re.compile(r":free$|\bfree\b|ollama", re.I), 0.0, 0.0, 0.0, 0.0, "免费"),
    # DeepSeek 系列（缓存命中通常为原价 10%~25%）
    (re.compile(r"deepseek.*(?:r1|reasoner|pro)", re.I), 0.55, 2.19, 0.14, 0.55, "DeepSeek R1/Pro"),
    (re.compile(r"deepseek", re.I), 0.28, 0.42, 0.028, 0.28, "DeepSeek Flash/V3"),
    # Anthropic Claude 系列（缓存读 0.1x，5m 缓存写 1.25x）
    (re.compile(r"claude.*(?:opus-4-[568]|opus-5|fable)", re.I), 5.0, 25.0, 0.50, 6.25, "Claude Opus"),
    (re.compile(r"claude.*opus", re.I), 15.0, 75.0, 1.50, 18.75, "Claude Opus"),
    (re.compile(r"claude.*sonnet", re.I), 3.0, 15.0, 0.30, 3.75, "Claude Sonnet"),
    (re.compile(r"claude.*haiku", re.I), 0.80, 4.0, 0.08, 1.00, "Claude Haiku"),
    # OpenAI GPT / o 系列
    (re.compile(r"gpt-(?:5|6).*(?:mini|nano|fast)", re.I), 0.25, 2.0, 0.025, 0.25, "GPT-5 Mini"),
    (re.compile(r"gpt-(?:5|6)", re.I), 1.25, 10.0, 0.125, 1.25, "GPT-5"),
    (re.compile(r"gpt-4\.1-nano", re.I), 0.10, 0.40, 0.025, 0.10, "GPT-4.1 Nano"),
    (re.compile(r"gpt-4\.1-mini", re.I), 0.40, 1.60, 0.10, 0.40, "GPT-4.1 Mini"),
    (re.compile(r"gpt-4\.1", re.I), 2.0, 8.0, 0.50, 2.0, "GPT-4.1"),
    (re.compile(r"gpt-4o-mini", re.I), 0.15, 0.60, 0.075, 0.15, "GPT-4o Mini"),
    (re.compile(r"gpt-4o", re.I), 2.5, 10.0, 1.25, 2.5, "GPT-4o"),
    (re.compile(r"\bo[134]-mini\b", re.I), 1.10, 4.40, 0.275, 1.10, "OpenAI o-mini"),
    (re.compile(r"\bo[13]\b", re.I), 10.0, 40.0, 2.50, 10.0, "OpenAI o-series"),
    # Google Gemini 系列
    (re.compile(r"gemini.*flash", re.I), 0.15, 0.60, 0.0375, 0.15, "Gemini Flash"),
    (re.compile(r"gemini.*pro", re.I), 1.25, 10.0, 0.31, 1.25, "Gemini Pro"),
    # 国产主流与 xAI
    (re.compile(r"qwen.*(?:flash|9b|7b|8b|14b)", re.I), 0.10, 0.30, 0.02, 0.10, "Qwen Lite"),
    (re.compile(r"qwen", re.I), 0.40, 1.20, 0.08, 0.40, "Qwen"),
    (re.compile(r"kimi", re.I), 0.60, 2.50, 0.15, 0.60, "Kimi"),
    (re.compile(r"glm.*flash", re.I), 0.10, 0.30, 0.02, 0.10, "GLM Flash"),
    (re.compile(r"glm", re.I), 0.60, 2.20, 0.12, 0.60, "GLM"),
    (re.compile(r"minimax", re.I), 0.30, 1.20, 0.06, 0.30, "MiniMax"),
    (re.compile(r"grok", re.I), 3.0, 15.0, 0.75, 3.0, "Grok"),
)


def split_token_components(
    input_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
) -> tuple[int, int, int, int, int]:
    """守恒拆分 Token，保证 uncached + cache_read + cache_write == input_tokens。"""
    in_tok = max(0, int(input_tokens or 0))
    out_tok = max(0, int(output_tokens or 0))
    cr = min(max(0, int(cache_read_tokens or 0)), in_tok) if in_tok > 0 else 0
    cw = min(max(0, int(cache_write_tokens or 0)), max(0, in_tok - cr)) if in_tok > 0 else 0
    uncached = max(0, in_tok - cr - cw)
    total = in_tok + out_tok
    return uncached, cr, cw, out_tok, total


def compute_exact_cache_hit_pct(input_tokens: int, cache_read_tokens: int) -> float | None:
    """返回保留 1 位小数的真实缓存命中率；只要存在未缓存输入，绝不虚标 100.0%。"""
    in_tok = max(0, int(input_tokens or 0))
    if in_tok <= 0:
        return None
    cr = max(0, int(cache_read_tokens or 0))
    if cr <= 0:
        return 0.0
    if cr >= in_tok:
        return 100.0
    return min(99.9, round((cr / in_tok) * 100.0, 1))


def resolve_model_pricing(model: str) -> tuple[float, float, float, float, str, bool]:
    """按模型名匹配参考单价（$/1M tokens），未命中则返回 0 与 matched=False。"""
    name = (model or "").strip()
    if not name:
        return 0.0, 0.0, 0.0, 0.0, "未知", False
    for pat, p_in, p_out, p_cr, p_cw, tier in _PRICING_RULES:
        if pat.search(name):
            return p_in, p_out, p_cr, p_cw, tier, True
    return 0.0, 0.0, 0.0, 0.0, "未知单价", False


def estimate_usage_cost(
    model: str,
    input_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
) -> dict[str, Any]:
    """计算参考估算费用、无缓存原价与缓存省流金额（USD）。"""
    uncached, cr, cw, out_tok, _ = split_token_components(
        input_tokens, cache_read_tokens, cache_write_tokens, output_tokens
    )
    p_in, p_out, p_cr, p_cw, tier, matched = resolve_model_pricing(model)
    if not matched:
        return {
            "cost_usd": 0.0,
            "no_cache_usd": 0.0,
            "saved_usd": 0.0,
            "tier": tier,
            "matched": False,
        }
    cost = (uncached * p_in + cr * p_cr + cw * p_cw + out_tok * p_out) / 1_000_000.0
    no_cache = ((uncached + cr + cw) * p_in + out_tok * p_out) / 1_000_000.0
    saved = max(0.0, no_cache - cost)
    return {
        "cost_usd": cost,
        "no_cache_usd": no_cache,
        "saved_usd": saved,
        "tier": tier,
        "matched": True,
    }


def _compute_heatmap_levels(values: list[int]) -> list[int]:
    """按非零分位数映射 0..4 热度等级（避免极端单日峰值把其余有消耗日期压成 0）。"""
    non_zero = sorted(v for v in values if v > 0)
    if not non_zero:
        return [0] * len(values)
    n = len(non_zero)
    q1 = non_zero[int((n - 1) * 0.25)]
    q2 = non_zero[int((n - 1) * 0.50)]
    q3 = non_zero[int((n - 1) * 0.75)]
    levels: list[int] = []
    for v in values:
        if v <= 0:
            levels.append(0)
        elif v <= q1:
            levels.append(1)
        elif v <= q2:
            levels.append(2)
        elif v <= q3:
            levels.append(3)
        else:
            levels.append(4)
    return levels


def _empty_agg() -> dict[str, Any]:
    return {
        "calls": 0,
        "ok_calls": 0,
        "fail_calls": 0,
        "input_tokens": 0,
        "uncached_input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "latency_ms_sum": 0.0,
        "ttft_ms_sum": 0.0,
        "ttft_count": 0,
        "tok_s_sum": 0.0,
        "tok_s_count": 0,
        "estimated_cost_usd": 0.0,
        "no_cache_cost_usd": 0.0,
        "cache_saved_usd": 0.0,
    }


def _accumulate_row(agg: dict[str, Any], row: dict[str, Any], cost_info: dict[str, Any]) -> None:
    uncached, cr, cw, out_tok, total = split_token_components(
        row["input_tokens"],
        row["cache_read_tokens"],
        row["cache_write_tokens"],
        row["output_tokens"],
    )
    in_tok = uncached + cr + cw
    agg["calls"] += int(row["calls"] or 0)
    agg["ok_calls"] += int(row["ok_calls"] or 0)
    agg["fail_calls"] += int(row["fail_calls"] or 0)
    agg["input_tokens"] += in_tok
    agg["uncached_input_tokens"] += uncached
    agg["cache_read_tokens"] += cr
    agg["cache_write_tokens"] += cw
    agg["output_tokens"] += out_tok
    agg["total_tokens"] += total
    agg["latency_ms_sum"] += float(row["latency_ms_sum"] or 0.0)
    agg["ttft_ms_sum"] += float(row["ttft_ms_sum"] or 0.0)
    agg["ttft_count"] += int(row["ttft_count"] or 0)
    agg["tok_s_sum"] += float(row["tok_s_sum"] or 0.0)
    agg["tok_s_count"] += int(row["tok_s_count"] or 0)
    agg["estimated_cost_usd"] += float(cost_info["cost_usd"])
    agg["no_cache_cost_usd"] += float(cost_info["no_cache_usd"])
    agg["cache_saved_usd"] += float(cost_info["saved_usd"])


def _finalize_agg(agg: dict[str, Any]) -> dict[str, Any]:
    calls = int(agg["calls"])
    ok_calls = int(agg["ok_calls"])
    in_tok = int(agg["input_tokens"])
    cr = int(agg["cache_read_tokens"])
    cost = float(agg["estimated_cost_usd"])
    no_cache = float(agg["no_cache_cost_usd"])
    saved = float(agg["cache_saved_usd"])
    return {
        "calls": calls,
        "ok_calls": ok_calls,
        "fail_calls": int(agg["fail_calls"]),
        "success_rate": round(ok_calls / calls * 100.0, 1) if calls > 0 else 0.0,
        "input_tokens": in_tok,
        "uncached_input_tokens": int(agg["uncached_input_tokens"]),
        "cache_read_tokens": cr,
        "cache_write_tokens": int(agg["cache_write_tokens"]),
        "output_tokens": int(agg["output_tokens"]),
        "total_tokens": int(agg["total_tokens"]),
        "cache_hit_pct": compute_exact_cache_hit_pct(in_tok, cr),
        "avg_latency_ms": round(agg["latency_ms_sum"] / calls, 1) if calls > 0 else 0.0,
        "avg_ttft_ms": round(agg["ttft_ms_sum"] / agg["ttft_count"], 1) if agg["ttft_count"] > 0 else 0.0,
        "avg_tok_s": round(agg["tok_s_sum"] / agg["tok_s_count"], 1) if agg["tok_s_count"] > 0 else 0.0,
        "estimated_cost_usd": round(cost, 4),
        "no_cache_cost_usd": round(no_cache, 4),
        "cache_saved_usd": round(saved, 4),
        "cache_savings_pct": round(saved / no_cache * 100.0, 1) if no_cache > 0 else 0.0,
    }


def query_llm_usage(
    store: Any,
    *,
    range_key: str = "7d",
    granularity: str = "auto",
    profile_id: str = "",
    model: str = "",
    profiles_meta: dict[str, dict[str, Any]] | None = None,
    now_dt: _dt.datetime | None = None,
) -> dict[str, Any]:
    """查询并构建 LLM Token 消耗全景分析响应。"""
    rk = (range_key or "7d").strip().lower()
    if rk not in ("24h", "7d", "30d", "90d", "all"):
        rk = "7d"
    gran = (granularity or "auto").strip().lower()
    if gran not in ("auto", "hour", "day"):
        gran = "auto"

    now = now_dt or _dt.datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    if rk == "24h":
        since_dt = now - _dt.timedelta(hours=23)
        since_hour = since_dt.strftime("%Y-%m-%d %H")
        eff_gran = "hour" if gran == "auto" else gran
    elif rk == "7d":
        since_dt = (now - _dt.timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        since_hour = since_dt.strftime("%Y-%m-%d %H")
        eff_gran = "day" if gran == "auto" else gran
    elif rk == "30d":
        since_dt = (now - _dt.timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
        since_hour = since_dt.strftime("%Y-%m-%d %H")
        eff_gran = "day"
    elif rk == "90d":
        since_dt = (now - _dt.timedelta(days=89)).replace(hour=0, minute=0, second=0, microsecond=0)
        since_hour = since_dt.strftime("%Y-%m-%d %H")
        eff_gran = "day"
    else:
        since_dt = None
        since_hour = ""
        eff_gran = "day" if gran == "auto" else gran

    pmeta = profiles_meta or {}
    pid_filter = (profile_id or "").strip()
    model_filter = (model or "").strip()

    # 拉取全量小时桶（用于过滤选项 + 84 天热力图），再按条件筛选当前视图窗口
    all_rows: list[dict[str, Any]] = store.query_llm_usage_hourly()
    sess_rows: list[dict[str, Any]] = store.list_session_usage_totals()
    sess_by_id = {str(s["id"]): s for s in sess_rows}

    # 构建可用筛选选项（仅包含实际产生过调用记录的渠道与模型，避免选出空白页）
    profile_opts_map: dict[str, str] = {}
    model_opts_set: set[str] = set()
    all_hourly_tokens = 0
    for r in all_rows:
        rpid = str(r.get("profile_id") or "")
        rpname = str(r.get("profile_name") or "")
        rmodel = str(r.get("model") or "")
        all_hourly_tokens += max(0, int(r.get("input_tokens") or 0)) + max(0, int(r.get("output_tokens") or 0))
        if rpid:
            cur_name = str((pmeta.get(rpid) or {}).get("name") or "").strip()
            profile_opts_map[rpid] = cur_name or rpname or rpid
        elif rpname:
            profile_opts_map[rpname] = rpname
        if rmodel:
            model_opts_set.add(rmodel)

    def _row_profile_key(r: dict[str, Any]) -> str:
        return str(r.get("profile_id") or r.get("profile_name") or "unknown")

    def _row_profile_name(r: dict[str, Any]) -> str:
        rpid = str(r.get("profile_id") or "")
        if rpid and rpid in pmeta and str(pmeta[rpid].get("name") or "").strip():
            return str(pmeta[rpid]["name"]).strip()
        return str(r.get("profile_name") or rpid or "未命名渠道")

    def _matches_facets(r: dict[str, Any]) -> bool:
        if pid_filter and _row_profile_key(r) != pid_filter:
            return False
        if model_filter and str(r.get("model") or "") != model_filter:
            return False
        return True

    facet_rows = [r for r in all_rows if _matches_facets(r)]
    window_rows = [
        r for r in facet_rows if (not since_hour or str(r.get("bucket_hour") or "") >= since_hour)
    ]

    # 主汇总 + 趋势桶 + 维度分组
    summary_agg = _empty_agg()
    today_agg = _empty_agg()
    series_map: dict[str, dict[str, Any]] = {}
    model_map: dict[str, dict[str, Any]] = {}
    model_meta_info: dict[str, dict[str, Any]] = {}
    profile_map: dict[str, dict[str, Any]] = {}
    profile_info: dict[str, dict[str, Any]] = {}
    session_window_map: dict[str, dict[str, Any]] = {}

    for r in window_rows:
        rmodel = str(r.get("model") or "unknown")
        cost_info = estimate_usage_cost(
            rmodel,
            r["input_tokens"],
            r["cache_read_tokens"],
            r["cache_write_tokens"],
            r["output_tokens"],
        )
        _accumulate_row(summary_agg, r, cost_info)

        bhour = str(r.get("bucket_hour") or "")
        bday = bhour[:10]
        if bday == today_str:
            _accumulate_row(today_agg, r, cost_info)

        bkey = bhour if eff_gran == "hour" else bday
        if bkey:
            _accumulate_row(series_map.setdefault(bkey, _empty_agg()), r, cost_info)

        _accumulate_row(model_map.setdefault(rmodel, _empty_agg()), r, cost_info)
        if rmodel not in model_meta_info:
            model_meta_info[rmodel] = {
                "pricing_tier": cost_info["tier"],
                "pricing_matched": bool(cost_info["matched"]),
            }

        pkey = _row_profile_key(r)
        _accumulate_row(profile_map.setdefault(pkey, _empty_agg()), r, cost_info)
        if pkey not in profile_info:
            pm = pmeta.get(pkey) or {}
            profile_info[pkey] = {
                "profile_id": str(r.get("profile_id") or ""),
                "profile_name": _row_profile_name(r),
                "format": str(pm.get("format") or ""),
                "base_url": str(pm.get("base_url") or ""),
                "icon_url": str(pm.get("icon_url") or ""),
                "models": set(),
            }
        profile_info[pkey]["models"].add(rmodel)

        sid = str(r.get("session_id") or "")
        if sid:
            _accumulate_row(session_window_map.setdefault(sid, _empty_agg()), r, cost_info)

    # 构造连续无断层的时间序列
    series_keys: list[tuple[str, str]] = []
    if eff_gran == "hour":
        hours_count = 24 if rk == "24h" else (168 if rk == "7d" else 48)
        start_h = (now - _dt.timedelta(hours=hours_count - 1)).replace(minute=0, second=0, microsecond=0)
        for i in range(hours_count):
            cur_dt = start_h + _dt.timedelta(hours=i)
            key = cur_dt.strftime("%Y-%m-%d %H")
            lbl = cur_dt.strftime("%H:00") if rk == "24h" else cur_dt.strftime("%m-%d %H:00")
            series_keys.append((key, lbl))
    else:
        if rk == "7d":
            days_count = 7
            start_d = now.date() - _dt.timedelta(days=days_count - 1)
        elif rk == "30d":
            days_count = 30
            start_d = now.date() - _dt.timedelta(days=days_count - 1)
        elif rk == "90d":
            days_count = 90
            start_d = now.date() - _dt.timedelta(days=days_count - 1)
        elif rk == "24h":
            days_count = 2
            start_d = now.date() - _dt.timedelta(days=1)
        else:
            # 'all'：从最早记录日期到今天（至少显示 7 天，最多 120 天）
            valid_days = sorted(k for k in series_map if len(k) == 10)
            if valid_days:
                try:
                    earliest = _dt.date.fromisoformat(valid_days[0])
                except ValueError:
                    earliest = now.date() - _dt.timedelta(days=6)
                span = (now.date() - earliest).days + 1
                days_count = max(7, min(120, span))
                start_d = now.date() - _dt.timedelta(days=days_count - 1)
            else:
                days_count = 7
                start_d = now.date() - _dt.timedelta(days=6)
        for i in range(days_count):
            cur_d = start_d + _dt.timedelta(days=i)
            key = cur_d.isoformat()
            series_keys.append((key, cur_d.strftime("%m-%d")))

    series_out: list[dict[str, Any]] = []
    for bkey, lbl in series_keys:
        fin = _finalize_agg(series_map.get(bkey) or _empty_agg())
        series_out.append({
            "bucket": bkey,
            "label": lbl,
            **fin,
        })

    # 构造热力图（24h 展示 24 小时格，其余展示近 12 周 = 84 天 GitHub 风格热力图）
    heatmap_mode = "hourly" if rk == "24h" else "daily"
    heatmap_raw: list[dict[str, Any]] = []
    if heatmap_mode == "hourly":
        start_h = (now - _dt.timedelta(hours=23)).replace(minute=0, second=0, microsecond=0)
        hour_bucket_map: dict[str, dict[str, Any]] = {}
        cutoff_24h = start_h.strftime("%Y-%m-%d %H")
        for r in facet_rows:
            bh = str(r.get("bucket_hour") or "")
            if bh >= cutoff_24h:
                ci = estimate_usage_cost(
                    str(r.get("model") or ""),
                    r["input_tokens"], r["cache_read_tokens"], r["cache_write_tokens"], r["output_tokens"],
                )
                _accumulate_row(hour_bucket_map.setdefault(bh, _empty_agg()), r, ci)
        for i in range(24):
            cur_dt = start_h + _dt.timedelta(hours=i)
            hk = cur_dt.strftime("%Y-%m-%d %H")
            fin = _finalize_agg(hour_bucket_map.get(hk) or _empty_agg())
            heatmap_raw.append({
                "key": hk,
                "date": cur_dt.strftime("%m-%d %H:00"),
                "short_label": cur_dt.strftime("%H"),
                "total_tokens": fin["total_tokens"],
                "calls": fin["calls"],
                "cache_hit_pct": fin["cache_hit_pct"],
                "estimated_cost_usd": fin["estimated_cost_usd"],
            })
    else:
        # 对齐到 12 周（84 天），最后一天为今天，按列 7 天排布
        days_n = 84
        start_d = now.date() - _dt.timedelta(days=days_n - 1)
        day_bucket_map: dict[str, dict[str, Any]] = {}
        cutoff_day = start_d.isoformat()
        for r in facet_rows:
            bd = str(r.get("bucket_hour") or "")[:10]
            if bd >= cutoff_day:
                ci = estimate_usage_cost(
                    str(r.get("model") or ""),
                    r["input_tokens"], r["cache_read_tokens"], r["cache_write_tokens"], r["output_tokens"],
                )
                _accumulate_row(day_bucket_map.setdefault(bd, _empty_agg()), r, ci)
        for i in range(days_n):
            cur_d = start_d + _dt.timedelta(days=i)
            dk = cur_d.isoformat()
            fin = _finalize_agg(day_bucket_map.get(dk) or _empty_agg())
            heatmap_raw.append({
                "key": dk,
                "date": dk,
                "short_label": cur_d.strftime("%m-%d"),
                "weekday": cur_d.weekday(),  # 0=Mon .. 6=Sun
                "total_tokens": fin["total_tokens"],
                "calls": fin["calls"],
                "cache_hit_pct": fin["cache_hit_pct"],
                "estimated_cost_usd": fin["estimated_cost_usd"],
            })

    levels = _compute_heatmap_levels([int(item["total_tokens"]) for item in heatmap_raw])
    for item, lvl in zip(heatmap_raw, levels):
        item["level"] = lvl

    summary_fin = _finalize_agg(summary_agg)
    today_fin = _finalize_agg(today_agg)
    grand_tokens = max(1, int(summary_fin["total_tokens"]))

    # 模型排行
    by_model_out: list[dict[str, Any]] = []
    for mname, agg in model_map.items():
        fin = _finalize_agg(agg)
        meta_m = model_meta_info.get(mname) or {}
        by_model_out.append({
            "model": mname,
            "share_pct": round(fin["total_tokens"] / grand_tokens * 100.0, 1) if summary_fin["total_tokens"] > 0 else 0.0,
            "pricing_tier": str(meta_m.get("pricing_tier") or "未知"),
            "pricing_matched": bool(meta_m.get("pricing_matched")),
            **fin,
        })
    by_model_out.sort(key=lambda x: (x["total_tokens"], x["calls"]), reverse=True)

    # 渠道排行
    by_profile_out: list[dict[str, Any]] = []
    for pkey, agg in profile_map.items():
        fin = _finalize_agg(agg)
        pinfo = profile_info.get(pkey) or {}
        by_profile_out.append({
            "profile_key": pkey,
            "profile_id": str(pinfo.get("profile_id") or ""),
            "profile_name": str(pinfo.get("profile_name") or pkey),
            "format": str(pinfo.get("format") or ""),
            "base_url": str(pinfo.get("base_url") or ""),
            "icon_url": str(pinfo.get("icon_url") or ""),
            "models_count": len(pinfo.get("models") or ()),
            "share_pct": round(fin["total_tokens"] / grand_tokens * 100.0, 1) if summary_fin["total_tokens"] > 0 else 0.0,
            **fin,
        })
    by_profile_out.sort(key=lambda x: (x["total_tokens"], x["calls"]), reverse=True)

    # 会话全量排行 + lifetime 汇总
    lifetime_in = 0
    lifetime_out = 0
    lifetime_cr = 0
    lifetime_cw = 0
    lifetime_cost = 0.0
    lifetime_saved = 0.0
    by_session_out: list[dict[str, Any]] = []

    seen_sids: set[str] = set()
    for s in sess_rows:
        sid = str(s["id"])
        s_model = str(s.get("model") or s.get("llm_model") or "")
        s_pid = str(s.get("llm_profile_id") or "")
        # 仅在未加维度过滤或命中过滤时纳入会话榜
        win_agg = session_window_map.get(sid)
        if pid_filter and not win_agg and s_pid != pid_filter:
            continue
        if model_filter and not win_agg and s_model != model_filter:
            continue
        seen_sids.add(sid)

        s_unc, s_cr, s_cw, s_out, s_tot = split_token_components(
            s.get("input_tokens", 0),
            s.get("cache_read_tokens", 0),
            s.get("cache_write_tokens", 0),
            s.get("output_tokens", 0),
        )
        s_in = s_unc + s_cr + s_cw
        s_cost = estimate_usage_cost(s_model, s_in, s_cr, s_cw, s_out)
        lifetime_in += s_in
        lifetime_out += s_out
        lifetime_cr += s_cr
        lifetime_cw += s_cw
        lifetime_cost += float(s_cost["cost_usd"])
        lifetime_saved += float(s_cost["saved_usd"])

        win_fin = _finalize_agg(win_agg) if win_agg else None
        has_pruned = bool(s_tot > (win_fin["total_tokens"] if win_fin else 0) * 1.05 and s_tot > 0)
        by_session_out.append({
            "session_id": sid,
            "title": str(s.get("title") or "未命名会话"),
            "status": str(s.get("status") or "idle"),
            "target_url": str(s.get("target_url") or ""),
            "model": s_model,
            "steps_count": int(s.get("steps_count") or 0),
            "updated_at": str(s.get("updated_at") or ""),
            "created_at": str(s.get("created_at") or ""),
            "input_tokens": s_in,
            "uncached_input_tokens": s_unc,
            "cache_read_tokens": s_cr,
            "cache_write_tokens": s_cw,
            "output_tokens": s_out,
            "total_tokens": s_tot,
            "cache_hit_pct": compute_exact_cache_hit_pct(s_in, s_cr),
            "estimated_cost_usd": round(float(s_cost["cost_usd"]), 4),
            "cache_saved_usd": round(float(s_cost["saved_usd"]), 4),
            "window_calls": win_fin["calls"] if win_fin else 0,
            "window_total_tokens": win_fin["total_tokens"] if win_fin else 0,
            "has_pruned_history": has_pruned,
        })

    # 补充未在 agent_sessions 中的孤立 session_id（如已删除会话但仍有账本记录）
    for sid, agg in session_window_map.items():
        if sid in seen_sids:
            continue
        fin = _finalize_agg(agg)
        by_session_out.append({
            "session_id": sid,
            "title": f"已归档会话 ({sid[-6:]})",
            "status": "archived",
            "target_url": "",
            "model": "",
            "steps_count": 0,
            "updated_at": "",
            "created_at": "",
            "input_tokens": fin["input_tokens"],
            "uncached_input_tokens": fin["uncached_input_tokens"],
            "cache_read_tokens": fin["cache_read_tokens"],
            "cache_write_tokens": fin["cache_write_tokens"],
            "output_tokens": fin["output_tokens"],
            "total_tokens": fin["total_tokens"],
            "cache_hit_pct": fin["cache_hit_pct"],
            "estimated_cost_usd": fin["estimated_cost_usd"],
            "cache_saved_usd": fin["cache_saved_usd"],
            "window_calls": fin["calls"],
            "window_total_tokens": fin["total_tokens"],
            "has_pruned_history": False,
        })

    by_session_out.sort(key=lambda x: (x["total_tokens"], x["window_total_tokens"]), reverse=True)

    lifetime_total = lifetime_in + lifetime_out
    has_older_pruned = bool(
        not pid_filter
        and not model_filter
        and lifetime_total > all_hourly_tokens * 1.05
    )

    return {
        "range": rk,
        "granularity": eff_gran,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            **summary_fin,
            "today_tokens": today_fin["total_tokens"],
            "today_calls": today_fin["calls"],
            "today_cost_usd": today_fin["estimated_cost_usd"],
            "models_count": len(by_model_out),
            "profiles_count": len(by_profile_out),
            "sessions_count": len(by_session_out),
        },
        "session_lifetime": {
            "sessions_count": len(sess_rows),
            "input_tokens": lifetime_in,
            "output_tokens": lifetime_out,
            "cache_read_tokens": lifetime_cr,
            "cache_write_tokens": lifetime_cw,
            "total_tokens": lifetime_total,
            "cache_hit_pct": compute_exact_cache_hit_pct(lifetime_in, lifetime_cr),
            "estimated_cost_usd": round(lifetime_cost, 4),
            "cache_saved_usd": round(lifetime_saved, 4),
            "has_older_pruned": has_older_pruned,
        },
        "series": series_out,
        "heatmap": {
            "mode": heatmap_mode,
            "cells": heatmap_raw,
        },
        "by_model": by_model_out,
        "by_profile": by_profile_out,
        "by_session": by_session_out[:25],
        "filters": {
            "profiles": [
                {"id": k, "name": v}
                for k, v in sorted(profile_opts_map.items(), key=lambda kv: kv[1].lower())
            ],
            "models": sorted(model_opts_set, key=lambda s: s.lower()),
        },
        "pricing_note": "费用按各模型公开标价参考估算（免费模型计 $0，中转/自建渠道请以实际账单为准）",
    }


def get_dashboard_llm_kpi(store: Any, now_dt: _dt.datetime | None = None) -> dict[str, Any]:
    """为仪表盘 KPI 卡片提供近 7 日 + 今日 LLM Token 消耗与缓存省流轻量摘要（单次小时表扫描）。"""
    now = now_dt or _dt.datetime.now()
    since_hour = (now - _dt.timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H")
    today_str = now.strftime("%Y-%m-%d")
    rows: list[dict[str, Any]] = store.query_llm_usage_hourly(since_hour=since_hour)
    agg = _empty_agg()
    today_tokens = 0
    for r in rows:
        rmodel = str(r.get("model") or "unknown")
        ci = estimate_usage_cost(
            rmodel,
            r["input_tokens"],
            r["cache_read_tokens"],
            r["cache_write_tokens"],
            r["output_tokens"],
        )
        _accumulate_row(agg, r, ci)
        if str(r.get("bucket_hour") or "")[:10] == today_str:
            _, _, _, _, tot = split_token_components(
                r["input_tokens"], r["cache_read_tokens"], r["cache_write_tokens"], r["output_tokens"]
            )
            today_tokens += tot
    fin = _finalize_agg(agg)
    return {
        "llm_tokens_7d": fin["total_tokens"],
        "llm_tokens_today": today_tokens,
        "llm_cache_hit_pct": fin["cache_hit_pct"],
        "llm_calls_7d": fin["calls"],
        "llm_cost_7d_usd": fin["estimated_cost_usd"],
        "llm_saved_7d_usd": fin["cache_saved_usd"],
    }


def _extract_bucket_hour(entry: dict[str, Any]) -> str:
    """从调用记录的 created_at / ts 字段规范化提取小时桶 YYYY-MM-DD HH。"""
    for key in ("created_at", "ts"):
        raw = str(entry.get(key) or "").strip()
        if len(raw) >= 13 and raw[4] == "-" and raw[7] == "-" and raw[10] in (" ", "T"):
            return raw[:13].replace("T", " ")
    return _dt.datetime.now().strftime("%Y-%m-%d %H")


def _normalize_session_id(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    # 将 :compact / :btw / :share 等附属调用归并回所属主会话
    return s.split(":", 1)[0].strip()


def _normalize_session_usage_row(raw: dict[str, Any]) -> dict[str, Any]:
    """统一归一化 PRO 与社区版 agent_sessions/*.json 的 Token 与元数据字段。"""
    u = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    in_tok = int(raw.get("input_tokens") or u.get("input_tokens") or u.get("prompt_tokens") or 0)
    out_tok = int(raw.get("output_tokens") or u.get("output_tokens") or u.get("completion_tokens") or 0)
    cr_tok = int(raw.get("cache_read_tokens") or u.get("cache_read_tokens") or 0)
    cw_tok = int(raw.get("cache_write_tokens") or u.get("cache_write_tokens") or 0)
    steps = int(raw.get("steps_count") or u.get("steps") or 0)
    return {
        "id": str(raw.get("id") or ""),
        "title": str(raw.get("title") or ""),
        "status": str(raw.get("status") or "idle"),
        "target_url": str(raw.get("target_url") or ""),
        "llm_profile_id": str(raw.get("llm_profile_id") or ""),
        "llm_model": str(raw.get("llm_model") or ""),
        "model": str(raw.get("model") or raw.get("llm_model") or ""),
        "steps_count": max(0, steps),
        "input_tokens": max(0, in_tok),
        "output_tokens": max(0, out_tok),
        "cache_read_tokens": max(0, cr_tok),
        "cache_write_tokens": max(0, cw_tok),
        "created_at": str(raw.get("created_at") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
    }


def _file_lock_ctx(target: Path):
    try:
        from core.locks import interprocess_file_lock

        return interprocess_file_lock(target)
    except Exception:
        return nullcontext()


class JsonHourlyUsageStore:
    """轻量跨进程安全的永久小时级 LLM 用量账本（供 PRO / 开源社区版与纯 JSON 工程复用）。

    特性：
      - 按 (bucket_hour, profile_id, model, session_id) 永久累积小时桶，不受内存环形队列或 500 条日志裁剪影响；
      - 按 call_id 去重实现幂等写入与 JSONL 历史回填，重复写入绝不重复累加；
      - 损坏守卫（_loaded_corrupt=True）拒绝用空账本覆盖损坏文件；
      - 通过跨进程文件锁 + .tmp -> os.replace 原子落盘，存放于 results/ 满足对外发行脱敏红线。
    """

    _MAX_SEEN_IDS = 5000

    def __init__(
        self,
        ledger_path: Path | str,
        *,
        sessions_dir: Path | str | None = None,
        jsonl_calls_path: Path | str | None = None,
    ) -> None:
        self.ledger_path = Path(ledger_path)
        self.sessions_dir = Path(sessions_dir) if sessions_dir is not None else None
        self.jsonl_calls_path = Path(jsonl_calls_path) if jsonl_calls_path is not None else None
        self._lock = threading.RLock()
        self._buckets: dict[str, dict[str, Any]] = {}
        self._seen_order: list[str] = []
        self._seen_set: set[str] = set()
        self._jsonl_state: dict[str, Any] = {}
        self._loaded_sig: tuple[int, int] | None = None
        self._last_jsonl_sig: tuple[int, int] | None = None
        self._loaded_corrupt: bool = False
        with self._lock:
            with _file_lock_ctx(self.ledger_path):
                self._reload_if_changed_locked()

    def set_sessions_dir(self, sessions_dir: Path | str | None) -> None:
        with self._lock:
            self.sessions_dir = Path(sessions_dir) if sessions_dir is not None else None

    def _file_sig(self, path: Path) -> tuple[int, int] | None:
        try:
            st = path.stat()
            return (int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            return None

    def _reload_if_changed_locked(self) -> None:
        sig = self._file_sig(self.ledger_path)
        if sig is None:
            if self._loaded_sig is not None:
                self._buckets.clear()
                self._seen_order.clear()
                self._seen_set.clear()
                self._jsonl_state.clear()
                self._loaded_sig = None
                self._loaded_corrupt = False
            return
        if sig == self._loaded_sig:
            return
        try:
            text = self.ledger_path.read_text(encoding="utf-8")
        except OSError:
            # Windows 跨进程原子替换瞬间可能触发短暂 PermissionError，绝不误判为文件损坏
            return
        try:
            raw = json.loads(text)
            if not isinstance(raw, dict):
                raise ValueError("小时账本根节点必须为 JSON 对象")
            buckets = raw.get("buckets")
            if buckets is None:
                buckets = {}
            if not isinstance(buckets, dict):
                raise ValueError("小时账本 buckets 必须为对象")
            seen = raw.get("seen_ids")
            if not isinstance(seen, list):
                seen = []
            jstate = raw.get("jsonl_state")
            self._buckets = {
                str(k): dict(v)
                for k, v in buckets.items()
                if isinstance(v, dict)
            }
            trimmed = [str(x) for x in seen if x][-self._MAX_SEEN_IDS :]
            self._seen_order = trimmed
            self._seen_set = set(trimmed)
            self._jsonl_state = dict(jstate) if isinstance(jstate, dict) else {}
            self._loaded_sig = sig
            self._loaded_corrupt = False
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            self._loaded_corrupt = True
            self._loaded_sig = sig
            print(f"[ERROR] LLM 小时用量账本解析失败 ({self.ledger_path}): {exc}，已拒绝静默覆写")

    def _save_locked(self) -> None:
        if self._loaded_corrupt:
            return
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "buckets": self._buckets,
            "seen_ids": self._seen_order[-self._MAX_SEEN_IDS :],
            "jsonl_state": self._jsonl_state,
        }
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        tmp = self.ledger_path.with_name(f".{self.ledger_path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.ledger_path)
            self._loaded_sig = self._file_sig(self.ledger_path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    @staticmethod
    def _entry_dedupe_id(entry: dict[str, Any]) -> str:
        call_id = str(entry.get("id") or "").strip()
        if call_id:
            return call_id
        # 无 id 的历史记录计算确定性内容指纹，避免重扫时无限累加
        fp_src = "|".join(
            str(entry.get(k) or "")
            for k in (
                "created_at",
                "ts",
                "profile_id",
                "model",
                "session_id",
                "ok",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "latency_ms",
            )
        )
        return "anon_" + hashlib.sha1(fp_src.encode("utf-8", errors="ignore")).hexdigest()[:16]

    def _accumulate_entry_locked(self, entry: dict[str, Any]) -> bool:
        if not isinstance(entry, dict):
            return False
        call_id = self._entry_dedupe_id(entry)
        if call_id in self._seen_set:
            return False

        bh = _extract_bucket_hour(entry)
        pid = str(entry.get("profile_id") or "").strip()
        pname = str(entry.get("profile_name") or "").strip()
        model = str(entry.get("model") or "").strip()
        sid = _normalize_session_id(entry.get("session_id"))
        bkey = f"{bh}\x1f{pid}\x1f{model}\x1f{sid}"

        b = self._buckets.get(bkey)
        if b is None:
            b = {
                "bucket_hour": bh,
                "profile_id": pid,
                "profile_name": pname,
                "model": model,
                "session_id": sid,
                "calls": 0,
                "ok_calls": 0,
                "fail_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "latency_ms_sum": 0.0,
                "ttft_ms_sum": 0.0,
                "ttft_count": 0,
                "tok_s_sum": 0.0,
                "tok_s_count": 0,
            }
            self._buckets[bkey] = b
        elif pname:
            b["profile_name"] = pname

        ok_flag = bool(entry.get("ok"))
        in_tok = max(0, int(entry.get("input_tokens") or entry.get("prompt_tokens") or 0))
        out_tok = max(0, int(entry.get("output_tokens") or entry.get("completion_tokens") or 0))
        cr_tok = max(0, int(entry.get("cache_read_tokens") or 0))
        cw_tok = max(0, int(entry.get("cache_write_tokens") or 0))
        lat_ms = max(0.0, float(entry.get("latency_ms") or 0.0))
        ttft_ms = max(0.0, float(entry.get("ttft_ms") or 0.0))
        tok_s = max(0.0, float(entry.get("tok_s") or 0.0))

        b["calls"] = int(b.get("calls") or 0) + 1
        if ok_flag:
            b["ok_calls"] = int(b.get("ok_calls") or 0) + 1
        else:
            b["fail_calls"] = int(b.get("fail_calls") or 0) + 1
        b["input_tokens"] = int(b.get("input_tokens") or 0) + in_tok
        b["output_tokens"] = int(b.get("output_tokens") or 0) + out_tok
        b["cache_read_tokens"] = int(b.get("cache_read_tokens") or 0) + cr_tok
        b["cache_write_tokens"] = int(b.get("cache_write_tokens") or 0) + cw_tok
        b["latency_ms_sum"] = round(float(b.get("latency_ms_sum") or 0.0) + lat_ms, 3)
        # 对齐母港 SQLite 口径：仅成功调用计入首字延迟与生成吞吐，避免失败/超时污染性能均值
        if ok_flag and ttft_ms > 0:
            b["ttft_ms_sum"] = round(float(b.get("ttft_ms_sum") or 0.0) + ttft_ms, 3)
            b["ttft_count"] = int(b.get("ttft_count") or 0) + 1
        if ok_flag and tok_s > 0:
            b["tok_s_sum"] = round(float(b.get("tok_s_sum") or 0.0) + tok_s, 3)
            b["tok_s_count"] = int(b.get("tok_s_count") or 0) + 1

        self._seen_set.add(call_id)
        self._seen_order.append(call_id)
        if len(self._seen_order) > self._MAX_SEEN_IDS:
            overflow = len(self._seen_order) - self._MAX_SEEN_IDS
            dropped = self._seen_order[:overflow]
            self._seen_order = self._seen_order[overflow:]
            for old_id in dropped:
                self._seen_set.discard(old_id)
        return True

    def record_call(self, entry: dict[str, Any]) -> bool:
        """原子记录单次 LLM 调用到永久小时账本（按 call_id 幂等）。"""
        with self._lock:
            with _file_lock_ctx(self.ledger_path):
                self._reload_if_changed_locked()
                if self._loaded_corrupt:
                    return False
                changed = self._accumulate_entry_locked(entry)
                if changed:
                    self._save_locked()
                return changed

    def sync_from_jsonl(self, jsonl_path: Path | str | None = None) -> int:
        """增量游标回填 JSONL 调用日志（仅 seek 读取新增字节，杜绝 >5000 行级联重算）。"""
        target = Path(jsonl_path) if jsonl_path is not None else self.jsonl_calls_path
        if target is None or not target.is_file():
            return 0
        with self._lock:
            sig = self._file_sig(target)
            if sig is None or sig == self._last_jsonl_sig:
                return 0
            with _file_lock_ctx(self.ledger_path):
                self._reload_if_changed_locked()
                if self._loaded_corrupt:
                    return 0
                added = 0
                target_key = str(target.resolve())
                file_size = sig[1]
                try:
                    with target.open("rb") as f:
                        first_line = f.readline()
                        first_hash = hashlib.sha1(first_line).hexdigest()[:16] if first_line else ""
                        saved_offset = 0
                        if (
                            self._jsonl_state.get("path") == target_key
                            and self._jsonl_state.get("first_line_hash") == first_hash
                            and isinstance(self._jsonl_state.get("offset"), int)
                            and 0 < int(self._jsonl_state["offset"]) <= file_size
                        ):
                            saved_offset = int(self._jsonl_state["offset"])
                        f.seek(saved_offset)
                        new_offset = saved_offset
                        while True:
                            raw_line = f.readline()
                            if not raw_line:
                                break
                            # 仅推进完整行，防止并发追加半行被跳过
                            if not raw_line.endswith(b"\n") and f.tell() < file_size:
                                break
                            new_offset = f.tell()
                            s = raw_line.decode("utf-8", errors="ignore").strip()
                            if not s:
                                continue
                            try:
                                item = json.loads(s)
                            except Exception:
                                continue
                            if isinstance(item, dict) and self._accumulate_entry_locked(item):
                                added += 1
                except OSError:
                    return 0

                state_changed = (
                    self._jsonl_state.get("path") != target_key
                    or self._jsonl_state.get("first_line_hash") != first_hash
                    or self._jsonl_state.get("offset") != new_offset
                )
                if state_changed:
                    self._jsonl_state = {
                        "path": target_key,
                        "first_line_hash": first_hash,
                        "offset": new_offset,
                    }
                if added > 0 or state_changed:
                    self._save_locked()
                self._last_jsonl_sig = sig
                return added

    def query_llm_usage_hourly(
        self,
        *,
        since_hour: str | None = None,
        until_hour: str | None = None,
        profile_id: str = "",
        model: str = "",
    ) -> list[dict[str, Any]]:
        """返回与 SQLite AgentStore.query_llm_usage_hourly 完全同构的小时桶列表。"""
        with self._lock:
            if self.jsonl_calls_path is not None:
                self.sync_from_jsonl(self.jsonl_calls_path)
            with _file_lock_ctx(self.ledger_path):
                self._reload_if_changed_locked()
            pid_f = (profile_id or "").strip()
            model_f = (model or "").strip()
            out: list[dict[str, Any]] = []
            for b in self._buckets.values():
                bh = str(b.get("bucket_hour") or "")
                if since_hour and bh < since_hour:
                    continue
                if until_hour and bh > until_hour:
                    continue
                if pid_f and str(b.get("profile_id") or "") != pid_f:
                    continue
                if model_f and str(b.get("model") or "") != model_f:
                    continue
                out.append(dict(b))
            out.sort(key=lambda r: (str(r.get("bucket_hour") or ""), str(r.get("profile_id") or ""), str(r.get("model") or "")))
            return out

    def list_session_usage_totals(self) -> list[dict[str, Any]]:
        """扫描 Agent 会话目录，返回与 AgentStore.list_session_usage_totals 同构的非空会话台账。"""
        with self._lock:
            sdir = self.sessions_dir
        if sdir is None or not sdir.is_dir():
            return []
        try:
            files = sorted(
                (p for p in sdir.glob("*.json") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:200]
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for fp in files:
            try:
                raw = json.loads(fp.read_text(encoding="utf-8", errors="ignore"))
                if not isinstance(raw, dict) or not raw.get("id"):
                    continue
                norm = _normalize_session_usage_row(raw)
                # 对齐母港 SQLite WHERE input_tokens > 0 OR output_tokens > 0 口径，过滤 0-Token 空会话
                if norm["input_tokens"] <= 0 and norm["output_tokens"] <= 0:
                    continue
                out.append(norm)
            except Exception:
                continue
        return out
