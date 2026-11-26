"""开源社区版不限步数默认配置、防死循环熔断、统一上下文窗口识别与 85% 自动压缩回归测试。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from core import ai_agent, llm_client, llm_context, llm_profiles


def test_community_unified_context_window_detection_and_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_client, "LLM_CONFIG_FILE", tmp_path / "llm_config.json")
    prof_file = llm_profiles._profiles_file()

    # 1) 即使上游未返回元数据，创建/保存 DeepSeek-V4.1-Flash-1017 也会自动识别为 1M (1000K)
    saved = llm_profiles.upsert_profile(
        {
            "name": "River DeepSeek",
            "format": "openai",
            "base_url": "https://api.river.ai/v1",
            "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
            "api_key": "sk-river-community-123456",
        }
    )
    assert saved["context_window_k"] == 1000
    assert llm_profiles.resolve_context_window(saved["id"], "deepseek-ai/DeepSeek-V4.1-Flash-1017") == 1_000_000

    # 2) 旧档案若残留默认 context_window_k=128，也会在解析时自动修正为 1M
    raw = json.loads(prof_file.read_text(encoding="utf-8"))
    raw["profiles"][0]["context_window_k"] = 128
    prof_file.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    assert llm_profiles.resolve_context_window(saved["id"]) == 1_000_000

    # 3) _extract_model_ids_and_ctx 兼容多种上游元数据字段并归一化
    ids, ctx_map = llm_client._extract_model_ids_and_ctx(
        {
            "data": [
                {"id": "deepseek-ai/DeepSeek-V4.1-Flash-1017"},
                {"id": "vendor/foo-256k", "context_length": 262144},
                {"id": "vendor/bar-1m", "limits": {"context": 1000000}},
                {"id": "vendor/baz-200k", "top_provider": {"context_length": 200000}},
            ]
        }
    )
    assert len(ids) == 4
    assert ctx_map == {
        "vendor/foo-256k": 256,
        "vendor/bar-1m": 1000,
        "vendor/baz-200k": 200,
    }
    assert llm_context.infer_model_context_window_k("deepseek-ai/DeepSeek-V4.1-Flash-1017", ctx_map) == 1000


def test_community_unlimited_steps_default_and_duplicate_failure_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai_agent, "AGENT_SESSIONS_DIR", tmp_path / "agent_sessions")
    monkeypatch.setattr(ai_agent, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(ai_agent.agent_manager, "_sessions", {})

    # 1) 默认 max_steps == 0（不限步数），且保存 0 不会被篡改为 1 或 16
    assert ai_agent.MAX_STEPS_PER_TURN == 0
    cfg0 = ai_agent.load_agent_config()
    assert cfg0["max_steps"] == 0
    saved_cfg = ai_agent.save_agent_config({"max_steps": 0})
    assert saved_cfg["max_steps"] == 0
    assert ai_agent.load_agent_config()["max_steps"] == 0

    # 2) 单轮连续执行 22 次工具调用（远超旧 16 步限制），完整结束且不出现暂停提示
    call_n = {"n": 0}
    monkeypatch.setattr(ai_agent, "tool_list_adapters", lambda: {"ok": True, "count": 0, "adapters": []})

    def _chat_22(messages, tools=None, system_prompt="", **kwargs):
        call_n["n"] += 1
        if call_n["n"] <= 22:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"c_{call_n['n']}",
                        "type": "function",
                        "function": {"name": "list_adapters", "arguments": "{}"},
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
                "finish_reason": "tool_calls",
            }
        return {
            "content": "22 步任务一气呵成完成。",
            "tool_calls": [],
            "usage": {"prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220},
            "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
            "finish_reason": "stop",
        }

    monkeypatch.setattr(llm_client, "chat_completion", _chat_22)
    sess = ai_agent.agent_manager.create_session(title="Unlimited 22")
    ai_agent.agent_manager.send_message(sess.id, "连续跑 22 步")
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and sess.status == "running":
        time.sleep(0.05)

    assert sess.status == "done"
    assert call_n["n"] == 23
    assert any("22 步任务一气呵成完成" in str(ev.get("content") or "") for ev in sess.events)
    assert not any("步数上限" in str(ev.get("content") or "") for ev in sess.events)

    # 3) 相同参数连续失败 2 次后，第 3 次触发防死循环熔断拦截
    exec_count = {"n": 0}

    def _always_fail():
        exec_count["n"] += 1
        return {"ok": False, "error": "boom"}

    monkeypatch.setattr(ai_agent, "tool_list_adapters", _always_fail)
    loop_turns = {"n": 0}
    saw_loop_block = {"hit": False}

    def _chat_loop(messages, tools=None, system_prompt="", **kwargs):
        loop_turns["n"] += 1
        for m in messages:
            if m.get("role") == "tool" and "防死循环熔断" in str(m.get("content") or ""):
                saw_loop_block["hit"] = True
        if loop_turns["n"] <= 3:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"f_{loop_turns['n']}",
                        "type": "function",
                        "function": {"name": "list_adapters", "arguments": "{}"},
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
                "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
                "finish_reason": "tool_calls",
            }
        return {
            "content": "已收到防死循环提示并停止重复调用。",
            "tool_calls": [],
            "usage": {"prompt_tokens": 60, "completion_tokens": 10, "total_tokens": 70},
            "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
            "finish_reason": "stop",
        }

    monkeypatch.setattr(llm_client, "chat_completion", _chat_loop)
    s_loop = ai_agent.agent_manager.create_session(title="Loop Guard")
    ai_agent.agent_manager.send_message(s_loop.id, "验证熔断")
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and s_loop.status == "running":
        time.sleep(0.05)

    assert s_loop.status == "done"
    assert exec_count["n"] == 2
    assert saw_loop_block["hit"] is True
