"""开源社区版 LLM Token 消耗永久小时账本、JSONL 回填与 /api/llm/usage、/api/dashboard 回归测试。"""

from __future__ import annotations

import datetime as _dt
import json
import threading
import urllib.request
from pathlib import Path

from core import llm_client
from core.llm_usage import (
    JsonHourlyUsageStore,
    compute_exact_cache_hit_pct,
    estimate_usage_cost,
    query_llm_usage,
    split_token_components,
)
from web import server as lite_server


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get_json(url: str):
    with _opener().open(url, timeout=5) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _get_text(url: str):
    with _opener().open(url, timeout=5) as r:
        return r.status, r.read().decode("utf-8")


def test_community_token_conservation_and_pricing():
    assert compute_exact_cache_hit_pct(1000, 999) == 99.9
    assert compute_exact_cache_hit_pct(1000, 1000) == 100.0
    assert compute_exact_cache_hit_pct(0, 0) is None

    unc, cr, cw, out_t, tot = split_token_components(1000, 700, 100, 250)
    assert (unc, cr, cw, out_t, tot) == (200, 700, 100, 250, 1250)

    free = estimate_usage_cost("deepseek-v4.1-flash:free", 1_000_000, 900_000, 0, 100_000)
    assert free["matched"] is True and free["cost_usd"] == 0.0

    paid = estimate_usage_cost("deepseek-v4.1-flash-1017", 1_000_000, 900_000, 0, 100_000)
    assert paid["matched"] is True and paid["saved_usd"] > 0.20


def test_community_jsonl_backfill_idempotency_and_session_normalization(tmp_path: Path):
    calls_jsonl = tmp_path / "llm_calls.jsonl"
    ledger = tmp_path / "llm_usage_hourly.json"
    sessions_dir = tmp_path / "agent_sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # 社区版会话 JSON 格式：prompt_tokens/completion_tokens 在 usage 嵌套字典内
    (sessions_dir / "sess_comm_1.json").write_text(
        json.dumps(
            {
                "id": "sess_comm_1",
                "title": "社区版探测任务",
                "status": "done",
                "model": "deepseek-v4.1-flash-1017",
                "steps_count": 12,
                "usage": {"prompt_tokens": 5000, "completion_tokens": 600, "total_tokens": 5600},
                "cache_read_tokens": 4500,
                "cache_write_tokens": 0,
                "created_at": "2026-11-27 09:00:00",
                "updated_at": "2026-11-27 09:15:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 写入历史 JSONL（包含重复 call_id 与 :compact 附属调用）
    rows = [
        {
            "id": "call_hist_1",
            "created_at": "2026-11-27 09:05:00",
            "session_id": "sess_comm_1",
            "profile_id": "p_comm",
            "profile_name": "社区测试渠道",
            "model": "deepseek-v4.1-flash-1017",
            "ok": True,
            "latency_ms": 900.0,
            "ttft_ms": 200.0,
            "tok_s": 88.0,
            "input_tokens": 3000,
            "output_tokens": 400,
            "cache_read_tokens": 2700,
            "cache_write_tokens": 0,
        },
        {
            "id": "call_hist_1",  # 重复 ID，绝不能重复累加
            "created_at": "2026-11-27 09:05:00",
            "session_id": "sess_comm_1",
            "profile_id": "p_comm",
            "profile_name": "社区测试渠道",
            "model": "deepseek-v4.1-flash-1017",
            "ok": True,
            "latency_ms": 900.0,
            "input_tokens": 3000,
            "output_tokens": 400,
            "cache_read_tokens": 2700,
            "cache_write_tokens": 0,
        },
        {
            "id": "call_hist_2",
            "created_at": "2026-11-27 09:10:00",
            "session_id": "sess_comm_1:compact",
            "profile_id": "p_comm",
            "profile_name": "社区测试渠道",
            "model": "deepseek-v4.1-flash-1017",
            "ok": True,
            "latency_ms": 700.0,
            "ttft_ms": 180.0,
            "tok_s": 92.0,
            "input_tokens": 2000,
            "output_tokens": 200,
            "cache_read_tokens": 1800,
            "cache_write_tokens": 0,
        },
    ]
    calls_jsonl.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    store = JsonHourlyUsageStore(ledger, sessions_dir=sessions_dir, jsonl_calls_path=calls_jsonl)
    res = query_llm_usage(store, range_key="7d", now_dt=_dt.datetime(2026, 11, 27, 12, 0, 0))
    assert res["summary"]["calls"] == 2
    assert res["summary"]["total_tokens"] == 5600
    assert res["summary"]["cache_hit_pct"] == 90.0
    assert res["by_session"][0]["title"] == "社区版探测任务"
    assert res["by_session"][0]["window_calls"] == 2

    # 增量字节游标防 O(N²) 重复累加回归（模拟超 _MAX_SEEN_IDS 滚动上限 + 无 id 历史行）
    store._MAX_SEEN_IDS = 3
    extra_lines = [
        json.dumps(
            {
                "id": f"call_extra_{i}",
                "created_at": "2026-11-27 10:00:00",
                "profile_id": "p_comm",
                "profile_name": "社区测试渠道",
                "model": "deepseek-v4.1-flash-1017",
                "ok": True,
                "latency_ms": 500.0,
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            }
        )
        for i in range(6)
    ]
    # 无 id 历史行也不得重复累加
    extra_lines.append(
        json.dumps(
            {
                "created_at": "2026-11-27 10:05:00",
                "profile_id": "p_comm",
                "profile_name": "社区测试渠道",
                "model": "deepseek-v4.1-flash-1017",
                "ok": True,
                "latency_ms": 400.0,
                "input_tokens": 50,
                "output_tokens": 5,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            }
        )
    )
    with calls_jsonl.open("a", encoding="utf-8") as f:
        f.write("\n".join(extra_lines) + "\n")
    res2 = query_llm_usage(store, range_key="7d", now_dt=_dt.datetime(2026, 11, 27, 12, 0, 0))
    assert res2["summary"]["calls"] == 9
    # 再追加 1 行触发新一轮 sync_from_jsonl，绝不能把前面被挤出 seen_set 的 9 行重算
    with calls_jsonl.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "id": "call_extra_last",
                    "created_at": "2026-11-27 10:10:00",
                    "profile_id": "p_comm",
                    "profile_name": "社区测试渠道",
                    "model": "deepseek-v4.1-flash-1017",
                    "ok": True,
                    "latency_ms": 400.0,
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                }
            )
            + "\n"
        )
    res3 = query_llm_usage(store, range_key="7d", now_dt=_dt.datetime(2026, 11, 27, 12, 0, 0))
    assert res3["summary"]["calls"] == 10


def test_community_http_llm_usage_dashboard_and_spa(tmp_path: Path, monkeypatch):
    res_dir = tmp_path / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(lite_server, "RESULTS_DIR", res_dir)
    monkeypatch.setattr(lite_server, "ACCOUNTS_FILE", res_dir / "accounts.txt")
    if hasattr(lite_server, "FAILED_FILE"):
        monkeypatch.setattr(lite_server, "FAILED_FILE", res_dir / "failed.txt")
    monkeypatch.setattr(llm_client, "LLM_CONFIG_FILE", res_dir / "llm_config.json")

    now_str = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    llm_client._record_llm_call(
        {
            "id": "call_live_comm_1",
            "ts": now_str[11:],
            "created_at": now_str,
            "session_id": "sess_live_1",
            "profile_id": "prof_c1",
            "profile_name": "开源渠道",
            "model": "deepseek-v4.1-flash-1017",
            "ok": True,
            "latency_ms": 500.0,
            "ttft_ms": 120.0,
            "tok_s": 110.0,
            "input_tokens": 2000,
            "output_tokens": 300,
            "cache_read_tokens": 1800,
            "cache_write_tokens": 0,
        }
    )

    srv = lite_server.create_server(host="127.0.0.1", port=0)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    try:
        st, d7 = _get_json(f"{base}/api/llm/usage?range=7d")
        assert st == 200
        for k in ("summary", "session_lifetime", "series", "heatmap", "by_model", "by_profile", "by_session", "filters"):
            assert k in d7
        assert d7["summary"]["calls"] == 1
        assert d7["summary"]["total_tokens"] == 2300
        assert len(d7["series"]) == 7

        st24, d24 = _get_json(f"{base}/api/llm/usage?range=24h")
        assert st24 == 200
        assert d24["heatmap"]["mode"] == "hourly"
        assert len(d24["series"]) == 24

        std, dash = _get_json(f"{base}/api/dashboard")
        assert std == 200
        kpi = dash.get("kpi") or {}
        for k in ("llm_tokens_7d", "llm_tokens_today", "llm_cache_hit_pct", "llm_calls_7d", "llm_cost_7d_usd", "llm_saved_7d_usd"):
            assert k in kpi
        assert kpi["llm_tokens_7d"] == 2300
        assert kpi["llm_calls_7d"] == 1

        # 校验 SPA 路由回落与社区版品牌/Apache 协议保留
        for route in ("/usage", "/token-usage"):
            sts, html = _get_text(f"{base}{route}")
            assert sts == 200
            assert "RegisterHub" in html
            assert "Lite" in html
            assert "Apache License 2.0" in html
    finally:
        srv.shutdown()
        srv.server_close()
