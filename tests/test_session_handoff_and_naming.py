"""开源社区版会话脱敏接力单（*.handoff.md）、导入续跑与分阶段自动命名回归测试。"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from core import ai_agent, llm_client
from core.session_handoff import compute_staged_session_title
from web import server as web_server


def _start_server():
    srv = web_server.create_server("127.0.0.1", 0)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, f"http://127.0.0.1:{port}", port


def test_community_staged_naming_share_and_import_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sites_dir = tmp_path / "sites"
    sites_dir.mkdir(parents=True, exist_ok=True)
    (sites_dir / "api_ninjas.py").write_text(
        'API_TOKEN = "sk-communitysecret1234567890abcd"\n'
        'PROXY = "http://user1:pass1@1.2.3.4:7897"\n'
        'def run():\n'
        '    return True\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(ai_agent, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(ai_agent, "SITES_DIR", sites_dir)
    monkeypatch.setattr(ai_agent, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(ai_agent, "AGENT_SESSIONS_DIR", tmp_path / "agent_sessions")
    monkeypatch.setattr(ai_agent.agent_manager, "_sessions", {})
    monkeypatch.delenv("REGISTERHUB_API_TOKEN", raising=False)

    monkeypatch.setattr(
        ai_agent,
        "_execute_tool_call",
        lambda name, args, session, stop_ev=None: (
            {"ok": False, "error": "USER_SRP_AUTH challenge not supported yet"}
            if name == "run_adapter_once"
            else {"ok": True}
        ),
    )

    turn = {"n": 0}

    def _fake_chat(messages, tools=None, system_prompt="", **kwargs):
        turn["n"] += 1
        if turn["n"] == 1:
            return {
                "content": "开始探测并试跑 api_ninjas",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "write_adapter", "arguments": json.dumps({"name": "api_ninjas"})},
                    },
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "run_adapter_once", "arguments": json.dumps({"adapter": "api_ninjas"})},
                    },
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
                "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
                "finish_reason": "tool_calls",
            }
        return {
            "content": "目前卡在 Cognito USER_SRP_AUTH 鉴权，已定位根因。",
            "tool_calls": [],
            "usage": {"prompt_tokens": 80, "completion_tokens": 25, "total_tokens": 105},
            "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
            "finish_reason": "stop",
        }

    monkeypatch.setattr(llm_client, "chat_completion", _fake_chat)

    sess = ai_agent.agent_manager.create_session(title="新 AI 直跑会话")
    ai_agent.agent_manager.send_message(sess.id, "帮我跑一下 https://api-ninjas.com/register")

    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline and sess.status == "running":
        time.sleep(0.05)

    assert sess.status == "done"
    assert sess.title == "api-ninjas · 卡在SRP鉴权"
    assert any(ev.get("type") == "session_renamed" for ev in sess.events)

    # 通过真实 HTTP 路由验证 /share 与 /import-handoff
    srv, base, port = _start_server()
    try:
        headers = {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{port}",
            "Sec-Fetch-Site": "same-origin",
        }
        req_share = urllib.request.Request(
            f"{base}/api/agent/sessions/{sess.id}/share",
            data=json.dumps({"include_code": True, "use_llm_refine": False}).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req_share, timeout=5) as resp:
            assert resp.status == 200
            sh = json.loads(resp.read().decode("utf-8"))
        assert sh["ok"] is True
        assert sh["status"] == "blocked"
        assert sh["has_adapter_code"] is True
        assert "sk-communitysecret" not in sh["markdown"]
        assert "user1:pass1@" not in sh["markdown"]
        assert "registerhub_handoff: v1" in sh["markdown"]

        req_imp = urllib.request.Request(
            f"{base}/api/agent/sessions/import-handoff",
            data=json.dumps({"markdown": sh["markdown"], "auto_start": False}).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req_imp, timeout=5) as resp:
            assert resp.status == 200
            imp = json.loads(resp.read().decode("utf-8"))
        assert imp["ok"] is True
        assert imp["session"]["id"] != sess.id
        assert "接力" in imp["session"]["title"]

        # 手动重命名后不再被自动改名覆盖
        ai_agent.agent_manager.rename_session(sess.id, "自定义固定名称")
        assert sess.title_source == "user"
        assert compute_staged_session_title({"title": sess.title, "title_source": sess.title_source, "status": "done", "messages": sess.messages}) is None
    finally:
        srv.shutdown()
        srv.server_close()


def test_community_passed_list_accounts_and_llm_refine_config_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sites_dir = tmp_path / "sites"
    sites_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ai_agent, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(ai_agent, "SITES_DIR", sites_dir)
    monkeypatch.setattr(ai_agent, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(ai_agent, "AGENT_SESSIONS_DIR", tmp_path / "agent_sessions")
    monkeypatch.setattr(ai_agent.agent_manager, "_sessions", {})

    sess = ai_agent.agent_manager.create_session(title="新 AI 直跑会话")
    sess.target_url = "https://acme.example.com/signup"
    sess.status = "done"
    sess.messages.append(
        {
            "role": "tool",
            "tool_name": "run_adapter_once",
            "tool_args": {"adapter": "acme"},
            "ok": True,
            "tool_result": json.dumps({"ok": True, "accounts_delta": 1, "new_accounts": ["u1@outlook.com----p"]}),
        }
    )

    called_kw: dict = {}

    def _fake_compat(messages, tools=None, system_prompt="", config_override=None, **kw):
        called_kw["config_override"] = config_override
        return {"content": "- 精炼结论：acme 已跑通", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(ai_agent, "_call_compat_chat_completion", _fake_compat)
    shared = ai_agent.agent_manager.export_session_handoff(sess.id, include_code=False, use_llm_refine=True)
    assert shared["ok"] is True
    assert shared["status"] == "passed"
    assert shared["llm_refined"] is True
    assert "精炼结论：acme 已跑通" in shared["markdown"]
    assert "config_override" in called_kw

    # 开源版供应商明文 URL/Key 与单把 Key 导出验证
    from core import llm_profiles

    monkeypatch.setattr(llm_client, "LLM_CONFIG_FILE", tmp_path / "llm_config.json")
    prof = llm_profiles.upsert_profile(
        {
            "name": "CommExp",
            "format": "openai",
            "base_url": "https://sub2.example.cc/v1",
            "model": "deepseek-v4.1-flash-1017",
            "api_key": "sk-comm-export-987654",
        }
    )
    exp_all = llm_profiles.export_profile_credentials(prof["id"])
    assert exp_all is not None and exp_all["base_url"] == "https://sub2.example.cc/v1"
    assert exp_all["keys"] == ["sk-comm-export-987654"]
    kid = prof["key_pool"][0]["id"]
    exp_one = llm_profiles.export_profile_credentials(prof["id"], key_id=kid)
    assert exp_one is not None and exp_one["key"] == "sk-comm-export-987654"
