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
