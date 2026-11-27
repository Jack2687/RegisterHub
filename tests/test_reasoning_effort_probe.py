"""社区版：按厂商原生思考能力矩阵、三协议发包映射、自动探测与会话级覆盖单元测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core import llm_client, llm_profiles
from core.ai_agent import SingleAgentManager
from core.llm_context import get_model_reasoning_spec


def test_community_payloads_and_probe(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(llm_client, "LLM_CONFIG_FILE", tmp_path / "llm_config.json")
    assert get_model_reasoning_spec("gpt-5.4-mini", "openai")["levels"] == ["off", "low", "medium", "high", "xhigh"]
    assert get_model_reasoning_spec("deepseek-v4.1-flash-1017", "openai")["control_type"] == "toggle"

    prof = llm_profiles.upsert_profile({
        "name": "comm-relay",
        "format": "openai",
        "model": "gpt-5.4-mini",
        "base_url": "https://relay.example.com/v1",
        "api_key": "sk-comm-test",
    })
    pid = prof["id"]

    # Runtime override applies reasoning_effort & Anthropic mapping cleanly
    ov = llm_profiles.resolve_runtime_override(profile_id=pid, reasoning_effort_override="xhigh")
    assert ov["reasoning_effort"] == "xhigh"
    _, _, _, payload_oai = llm_client._build_request_spec(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        system_prompt="",
        cfg=ov,
        max_tok=2048,
        stream=False,
    )
    assert payload_oai["reasoning_effort"] == "xhigh"

    ov_ant = {**ov, "format": "anthropic", "model": "claude-opus-4-6", "thinking_mode": "none", "reasoning_effort": "low"}
    _, _, _, payload_ant = llm_client._build_request_spec(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        system_prompt="",
        cfg=ov_ant,
        max_tok=16384,
        stream=False,
    )
    assert payload_ant["thinking"]["type"] == "enabled"

    def fake_chat(messages, tools=None, system_prompt="", overrides=None, timeout=18):
        eff = str((overrides or {}).get("reasoning_effort") or "none")
        tok_map = {"low": 22, "high": 46, "xhigh": 62, "max": 72}
        return {
            "ok": True,
            "content": "2028",
            "thinking": "y" * tok_map.get(eff, 10),
            "reasoning_tokens": tok_map.get(eff, 10),
            "output_tokens": tok_map.get(eff, 10) + 5,
        }

    monkeypatch.setattr(llm_client, "chat_completion", fake_chat)
    res = llm_profiles.probe_reasoning_capability(pid, model="gpt-5.4-mini", force=False)
    assert res["ok"] is True
    assert res["capability"]["status"] == "verified"
    assert res["capability"]["mode"] == "graded"
    assert "max" in res["capability"]["supported_levels"]

    res_cached = llm_profiles.probe_reasoning_capability(pid, model="gpt-5.4-mini", force=False)
    assert res_cached["cached"] is True

    mgr = SingleAgentManager()
    monkeypatch.setattr("core.ai_agent.AGENT_SESSIONS_DIR", tmp_path / "sessions")
    sess = mgr.create_session(title="c-test", llm_profile_id=pid, llm_model="gpt-5.4-mini", llm_reasoning_effort="high")
    assert sess.llm_reasoning_effort == "high"
    assert sess.to_detail()["llm_reasoning_effort"] == "high"
