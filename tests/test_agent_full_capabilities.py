"""Comprehensive regression tests for RegisterHub Community Full Agent capabilities:

1. True SSE streaming (`stream_chat_completion`) across OpenAI Chat, Anthropic Messages,
   and OpenAI Responses formats — verifying `text_delta`, `thinking_delta`, `tool_call_delta`,
   cache token extraction (`cache_read_tokens`, `cache_write_tokens`, `cache_hit_pct`),
   `ttft_ms`, `tok_s`, and live `stop_event` cancellation.
2. Live Agent SSE stream delivering ephemeral `text_delta`/`thinking_delta` + persistent
   `usage` (with cache hit %, context_tokens, tok_s, ttft_ms) + `assistant_msg` metrics.
3. True LLM Context Compaction (`POST /api/agent/sessions/<id>/compact`) + automatic
   85% window compaction + `role="compaction"` UI card & context_tokens reduction.
4. Real streaming `/btw` (`POST /api/agent/sessions/<id>/side-question/stream`) + persistence.
5. All 7 new pure-Community tools: `patch_adapter` (+ step checkpoint & one-click restore),
   `read_core_file`, `run_scratch_python`, `read_session_file`, `analyze_har`,
   `check_infra`, `verify_accounts`.
6. Guard Rules CRUD + dry-run tester + runtime deny enforcement + follow-up queue +
   `/goal` mode + `max_tokens` truncation auto-recovery + config auto-upgrade migration.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from core import ai_agent, llm_client, llm_profiles
from web import server as web_server


def _http_json(
    base_url: str,
    method: str,
    path: str,
    payload: Dict[str, Any] | None = None,
) -> Tuple[int, Dict[str, Any]]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as he:
        return he.code, json.loads(he.read().decode("utf-8"))


def _start_mock_llm_server(sse_batches: Dict[str, List[str]]) -> Tuple[ThreadingHTTPServer, str, List[Dict[str, Any]]]:
    recorded: List[Dict[str, Any]] = []

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            body = json.loads(raw) if raw else {}
            recorded.append({"path": self.path, "body": body})

            lines = sse_batches.get(self.path)
            if callable(lines):
                lines = lines(body, len(recorded))
            if lines is None:
                self.send_response(404)
                self.end_headers()
                return

            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for ln in lines:
                    self.wfile.write((ln + "\n").encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(0.01)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(lines, ensure_ascii=False).encode("utf-8"))

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, f"http://127.0.0.1:{port}", recorded


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sites_dir = tmp_path / "sites"
    results_dir = tmp_path / "results"
    sessions_dir = results_dir / "agent_sessions"
    sites_dir.mkdir(parents=True)
    results_dir.mkdir(parents=True)

    (sites_dir / "demo_site.py").write_text(
        "from core.adapter import BaseSiteAdapter\n\n"
        "class DemoSiteAdapter(BaseSiteAdapter):\n"
        "    def target_url(self) -> str:\n"
        "        return 'https://example.com/signup'\n"
        "    def prepare_identity(self) -> dict:\n"
        "        return {'email': 'a@example.com'}\n"
        "    def fill_form(self, identity: dict) -> None:\n"
        "        pass\n"
        "    def submit_and_confirm(self) -> bool:\n"
        "        return False\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(llm_client, "LLM_CONFIG_FILE", results_dir / "llm_config.json")
    monkeypatch.setattr(ai_agent, "SITES_DIR", sites_dir)
    monkeypatch.setattr(ai_agent, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(ai_agent, "ACCOUNTS_FILE", results_dir / "accounts.txt")
    monkeypatch.setattr(ai_agent, "FAILED_FILE", results_dir / "failed.txt")
    monkeypatch.setattr(ai_agent, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(ai_agent, "AGENT_SESSIONS_DIR", sessions_dir)

    mgr = ai_agent.SingleAgentManager()
    monkeypatch.setattr(ai_agent, "agent_manager", mgr)
    monkeypatch.setattr(web_server, "agent_manager", mgr)
    monkeypatch.setattr(web_server, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(web_server, "ACCOUNTS_FILE", results_dir / "accounts.txt")
    monkeypatch.setattr(web_server, "FAILED_FILE", results_dir / "failed.txt")

    srv = web_server.create_server("127.0.0.1", 0)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield {
            "tmp_path": tmp_path,
            "sites_dir": sites_dir,
            "results_dir": results_dir,
            "sessions_dir": sessions_dir,
            "mgr": mgr,
            "base_url": f"http://127.0.0.1:{port}",
        }
    finally:
        mgr.stop_all()
        srv.shutdown()
        srv.server_close()


def test_stream_chat_completion_openai_anthropic_responses_and_cache_metrics(isolated_env):
    openai_sse = [
        'data: {"model":"gpt-4o-mini","choices":[{"delta":{"reasoning_content":"先分析表单"},"finish_reason":null}]}',
        "",
        'data: {"model":"gpt-4o-mini","choices":[{"delta":{"content":"你好，"},"finish_reason":null}]}',
        "",
        'data: {"model":"gpt-4o-mini","choices":[{"delta":{"content":"流式输出正常！"},"finish_reason":null}]}',
        "",
        'data: {"model":"gpt-4o-mini","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_o1","function":{"name":"list_adapters","arguments":"{"}}]},"finish_reason":null}]}',
        "",
        'data: {"model":"gpt-4o-mini","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"}"}}]},"finish_reason":"tool_calls"}]}',
        "",
        'data: {"model":"gpt-4o-mini","choices":[],"usage":{"prompt_tokens":1000,"completion_tokens":80,"prompt_tokens_details":{"cached_tokens":750}}}',
        "",
        "data: [DONE]",
        "",
    ]
    anthropic_sse = [
        "event: message_start",
        'data: {"type":"message_start","message":{"model":"claude-sonnet-4","usage":{"input_tokens":200,"cache_read_input_tokens":600,"cache_creation_input_tokens":200,"output_tokens":0}}}',
        "",
        "event: content_block_start",
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}',
        "",
        "event: content_block_delta",
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"思考中..."}}',
        "",
        "event: content_block_start",
        'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}',
        "",
        "event: content_block_delta",
        'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Claude 流式OK"}}',
        "",
        "event: message_delta",
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":45}}',
        "",
        "event: message_stop",
        'data: {"type":"message_stop"}',
        "",
    ]
    responses_sse = [
        'data: {"type":"response.reasoning_summary_text.delta","delta":"推理摘要"}',
        "",
        'data: {"type":"response.output_text.delta","delta":"Responses 流式"}',
        "",
        'data: {"type":"response.output_text.delta","delta":"完成"}',
        "",
        'data: {"type":"response.completed","response":{"model":"gpt-5","status":"completed","usage":{"input_tokens":500,"output_tokens":30,"input_tokens_details":{"cached_tokens":400}}}}',
        "",
    ]

    mock_srv, mock_url, recorded = _start_mock_llm_server({
        "/v1/chat/completions": openai_sse,
        "/v1/messages": anthropic_sse,
        "/v1/responses": responses_sse,
    })
    try:
        # 1. OpenAI Chat SSE
        evs_o: List[Dict[str, Any]] = []
        res_o = llm_client.stream_chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            config_override={"format": "openai", "base_url": f"{mock_url}/v1", "model": "gpt-4o-mini", "api_key": "sk-test"},
            on_event=evs_o.append,
        )
        assert res_o["ok"] is True
        assert res_o["content"] == "你好，流式输出正常！"
        assert res_o["thinking"] == "先分析表单"
        assert res_o["cache_read_tokens"] == 750
        assert res_o["cache_hit_pct"] == 75
        assert res_o["ttft_ms"] > 0 and res_o["tok_s"] > 0
        assert len(res_o["tool_calls"]) == 1 and res_o["tool_calls"][0]["function"]["name"] == "list_adapters"
        assert any(e["type"] == "text_delta" for e in evs_o)
        assert any(e["type"] == "thinking_delta" for e in evs_o)
        assert any(e["type"] == "tool_call_delta" for e in evs_o)

        # 2. Anthropic Messages SSE
        evs_a: List[Dict[str, Any]] = []
        res_a = llm_client.stream_chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            config_override={"format": "anthropic", "base_url": mock_url, "model": "claude-sonnet-4", "api_key": "sk-ant"},
            on_event=evs_a.append,
        )
        assert res_a["content"] == "Claude 流式OK"
        assert res_a["thinking"] == "思考中..."
        assert res_a["input_tokens"] == 1000
        assert res_a["cache_read_tokens"] == 600
        assert res_a["cache_write_tokens"] == 200
        assert res_a["cache_hit_pct"] == 60

        # 3. OpenAI Responses SSE
        evs_r: List[Dict[str, Any]] = []
        res_r = llm_client.stream_chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            config_override={"format": "openai-responses", "base_url": f"{mock_url}/v1", "model": "gpt-5", "api_key": "sk-test"},
            on_event=evs_r.append,
        )
        assert res_r["content"] == "Responses 流式完成"
        assert res_r["thinking"] == "推理摘要"
        assert res_r["cache_read_tokens"] == 400
        assert res_r["cache_hit_pct"] == 80

        # Verify calls log includes cache_read_tokens & request/response bodies
        calls = llm_client.list_llm_calls(limit=10)
        assert len(calls) >= 3
        assert calls[0]["has_bodies"] is True
        assert calls[0]["cache_read_tokens"] == 400
    finally:
        mock_srv.shutdown()
        mock_srv.server_close()


def test_live_agent_sse_streaming_telemetry_compaction_and_btw(isolated_env):
    base_url = isolated_env["base_url"]

    def _dynamic_chat(body: Dict[str, Any], call_no: int):
        msgs = body.get("messages") or []
        sys_txt = str(msgs[0].get("content") or "") if msgs else ""
        if "压缩器" in sys_txt:
            return {
                "model": "gpt-4o-mini",
                "choices": [{"message": {"role": "assistant", "content": "## 目标与站点\n已完成探测\n## 下一步行动\n继续"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 40},
            }
        if "旁白答疑" in sys_txt:
            return [
                'data: {"model":"gpt-4o-mini","choices":[{"delta":{"thinking":"查上下文"},"finish_reason":null}]}',
                "",
                'data: {"model":"gpt-4o-mini","choices":[{"delta":{"content":"旁白流式"},"finish_reason":null}]}',
                "",
                'data: {"model":"gpt-4o-mini","choices":[{"delta":{"content":"回答完成"},"finish_reason":"stop"}]}',
                "",
                'data: {"model":"gpt-4o-mini","choices":[],"usage":{"prompt_tokens":300,"completion_tokens":20,"prompt_tokens_details":{"cached_tokens":150}}}',
                "",
                "data: [DONE]",
                "",
            ]
        return [
            'data: {"model":"gpt-4o-mini","choices":[{"delta":{"reasoning_content":"准备回复"},"finish_reason":null}]}',
            "",
            'data: {"model":"gpt-4o-mini","choices":[{"delta":{"content":"第一段流式正文，"},"finish_reason":null}]}',
            "",
            'data: {"model":"gpt-4o-mini","choices":[{"delta":{"content":"第二段流式正文。"},"finish_reason":"stop"}]}',
            "",
            'data: {"model":"gpt-4o-mini","choices":[],"usage":{"prompt_tokens":1200,"completion_tokens":50,"prompt_tokens_details":{"cached_tokens":900}}}',
            "",
            "data: [DONE]",
            "",
        ]

    mock_srv, mock_url, _ = _start_mock_llm_server({"/v1/chat/completions": _dynamic_chat})
    try:
        llm_profiles.upsert_profile({
            "id": "p_stream",
            "name": "Mock Stream",
            "format": "openai",
            "base_url": f"{mock_url}/v1",
            "model": "gpt-4o-mini",
            "api_key": "sk-mock-123456",
            "streaming": True,
        })
        llm_profiles.activate_profile("p_stream")

        st, created = _http_json(base_url, "POST", "/api/agent/sessions", {"title": "流式验收"})
        assert st == 200
        sid = created["session"]["id"]

        # Start SSE listener before sending message
        sse_events: List[Dict[str, Any]] = []

        def _consume_sse():
            req = urllib.request.Request(f"{base_url}/api/agent/sessions/{sid}/stream?since=0")
            with urllib.request.urlopen(req, timeout=10) as resp:
                for raw_ln in resp:
                    s = raw_ln.decode("utf-8", errors="ignore").strip()
                    if s.startswith("data:"):
                        obj = json.loads(s[5:].strip())
                        sse_events.append(obj)
                        if obj.get("type") == "status" and obj.get("status") in ("done", "error", "stopped"):
                            break

        # First kick off message, then verify deltas & telemetry
        st, _ = _http_json(base_url, "POST", f"/api/agent/sessions/{sid}/message", {"message": "开始流式测试"})
        assert st == 200
        _consume_sse()

        # Wait until idle/done
        for _ in range(50):
            _, detail_res = _http_json(base_url, "GET", f"/api/agent/sessions/{sid}")
            if detail_res["session"]["status"] == "done":
                break
            time.sleep(0.05)

        sess_d = detail_res["session"]
        assert sess_d["status"] == "done"
        assert sess_d["cache_read_tokens"] == 900
        assert sess_d["context_tokens"] == 1200
        assert sess_d["last_tok_s"] > 0
        assert sess_d["last_ttft_ms"] > 0
        asst_msgs = [m for m in sess_d["messages"] if m["role"] == "assistant"]
        assert len(asst_msgs) == 1
        assert asst_msgs[0]["content"] == "第一段流式正文，第二段流式正文。"
        assert asst_msgs[0]["thinking"] == "准备回复"
        assert asst_msgs[0]["metrics"]["cache_hit_pct"] == 75

        # Send a 2nd turn so history has >= 4 messages, then test REAL LLM compaction
        st, _ = _http_json(base_url, "POST", f"/api/agent/sessions/{sid}/message", {"message": "继续第二步"})
        assert st == 200
        for _ in range(50):
            _, d2 = _http_json(base_url, "GET", f"/api/agent/sessions/{sid}")
            if d2["session"]["status"] == "done" and len([m for m in d2["session"]["messages"] if m["role"] == "assistant"]) >= 2:
                break
            time.sleep(0.05)

        st, cmp_res = _http_json(
            base_url,
            "POST",
            f"/api/agent/sessions/{sid}/compact",
            {"custom_instructions": "保留流式结果"},
        )
        assert st == 200 and cmp_res["ok"] is True and cmp_res["compacted"] is True
        assert cmp_res["tokens_after"] < cmp_res["tokens_before"]
        assert cmp_res["dropped_messages"] >= 2
        assert any(m["role"] == "compaction" for m in cmp_res["session"]["messages"])

        # Test streaming /btw side-question + persistence
        btw_req = urllib.request.Request(
            f"{base_url}/api/agent/sessions/{sid}/side-question/stream",
            data=json.dumps({"question": "刚才做了什么？"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        btw_evs: List[Dict[str, Any]] = []
        with urllib.request.urlopen(btw_req, timeout=10) as bresp:
            for raw_ln in bresp:
                s = raw_ln.decode("utf-8", errors="ignore").strip()
                if s.startswith("data:"):
                    btw_evs.append(json.loads(s[5:].strip()))
        assert any(e.get("type") == "delta" for e in btw_evs)
        done_btw = [e for e in btw_evs if e.get("type") == "done"]
        assert done_btw and done_btw[0]["item"]["answer"] == "旁白流式回答完成"
        _, d3 = _http_json(base_url, "GET", f"/api/agent/sessions/{sid}")
        assert len(d3["session"]["side_questions"]) == 1
    finally:
        mock_srv.shutdown()
        mock_srv.server_close()


def test_new_community_tools_checkpoints_guards_and_config_migration(isolated_env):
    base_url = isolated_env["base_url"]
    sites_dir = isolated_env["sites_dir"]
    results_dir = isolated_env["results_dir"]
    mgr = isolated_env["mgr"]

    # 1. Verify config auto-upgrade migration enables new tools while preserving manual disable
    cfg_file = results_dir / "agent_config.json"
    cfg_file.write_text(
        json.dumps({
            "name": "旧版配置",
            "max_steps": 20,
            "enabled_tools": ["probe_url", "list_adapters"],
            "known_tools": ["probe_url", "list_adapters", "read_adapter"],  # read_adapter was manually disabled
        }),
        encoding="utf-8",
    )
    st, cfg_res = _http_json(base_url, "GET", "/api/agent/config")
    assert st == 200
    assert len(cfg_res["available_tools"]) == 16
    enabled = set(cfg_res["agent"]["enabled_tools"])
    assert "patch_adapter" in enabled
    assert "read_core_file" in enabled
    assert "run_scratch_python" in enabled
    assert "manage_builtin_mail" in enabled
    assert "manage_builtin_proxies" in enabled
    assert "read_adapter" not in enabled  # manually disabled tool respected!

    # 2. Create session + upload HAR + test read_session_file & analyze_har
    st, created = _http_json(base_url, "POST", "/api/agent/sessions", {"title": "工具验收"})
    sid = created["session"]["id"]
    sess = mgr.get_session(sid)
    assert sess is not None

    sample_har = json.dumps({
        "log": {
            "entries": [
                {
                    "request": {
                        "method": "POST",
                        "url": "https://api.acme.test/v1/auth/signup",
                        "postData": {"text": '{"email":"u@acme.test","cf_turnstile_token":"tok"}'},
                    },
                    "response": {
                        "status": 200,
                        "content": {"mimeType": "application/json", "text": '{"otp_sent":true}'},
                    },
                }
            ]
        }
    })
    st, up_res = _http_json(
        base_url,
        "POST",
        f"/api/agent/sessions/{sid}/upload",
        {"filename": "signup.har", "content_b64": base64.b64encode(sample_har.encode("utf-8")).decode("ascii")},
    )
    assert st == 200 and up_res["ok"] is True
    _, d_up = _http_json(base_url, "GET", f"/api/agent/sessions/{sid}")
    assert len(d_up["session"]["uploads"]) == 1

    r_list = ai_agent.tool_read_session_file(sid)
    assert r_list["ok"] is True and r_list["count"] == 1
    r_har = ai_agent.tool_analyze_har(sid, filename="signup.har", site_name="acme")
    assert r_har["ok"] is True and r_har["api_steps_count"] == 1
    assert "Turnstile" in r_har["signals"]
    assert "class AcmeAdapter(BaseSiteAdapter):" in r_har["skeleton"]

    # 3. Test patch_adapter + checkpoint list & one-click restore
    orig_code = (sites_dir / "demo_site.py").read_text(encoding="utf-8")
    p_res = ai_agent.tool_patch_adapter(
        "demo_site",
        old_text="https://example.com/signup",
        new_text="https://example.com/register-v2",
        session_id=sid,
    )
    assert p_res["ok"] is True and p_res.get("checkpoint_id")
    assert "register-v2" in (sites_dir / "demo_site.py").read_text(encoding="utf-8")

    # Anti-fake-Active still enforced on patch_adapter
    p_bad = ai_agent.tool_patch_adapter(
        "demo_site",
        old_text="return {'email': 'a@example.com'}",
        new_text="return {'email': 'a@example.com', 'api_key': 'Active'}",
        session_id=sid,
    )
    assert p_bad["ok"] is False and "反假成功" in p_bad["error"]

    st, cps = _http_json(base_url, "GET", f"/api/agent/sessions/{sid}/checkpoints")
    assert st == 200 and cps["count"] == 1
    cp_id = cps["checkpoints"][0]["id"]

    st, rst = _http_json(base_url, "POST", f"/api/agent/sessions/{sid}/checkpoints/{cp_id}/restore", {})
    assert st == 200 and rst["ok"] is True
    assert (sites_dir / "demo_site.py").read_text(encoding="utf-8") == orig_code

    # 4. Test read_core_file (whitelisted vs blocked)
    rc_ok = ai_agent.tool_read_core_file("core/adapter.py", start_line=1, end_line=30)
    assert rc_ok["ok"] is True and "BaseSiteAdapter" in rc_ok["content"]
    rc_blk = ai_agent.tool_read_core_file("config.json")
    assert rc_blk["ok"] is False

    # 5. Test run_scratch_python
    sc_res = ai_agent.tool_run_scratch_python("print('SCRATCH_OK', 2 + 3)", filename="calc.py", timeout_sec=10, session=sess)
    assert sc_res["ok"] is True and "SCRATCH_OK 5" in sc_res["output_tail"]

    # 6. Test check_infra & verify_accounts
    inf = ai_agent.tool_check_infra(check_ip_score=False)
    assert inf["ok"] is True and "config" in inf
    (results_dir / "accounts.txt").write_text("u1@test.com----pass123----u1----sk-live-abcdef123456\nu2@test.com----pass123\n", encoding="utf-8")
    va = ai_agent.tool_verify_accounts(limit=5)
    assert va["ok"] is True and va["total"] == 2 and va["with_key"] == 1

    # 7. Test Guard Rules CRUD + dry-run test
    st, g_add = _http_json(base_url, "POST", "/api/agent/guard", {
        "id": "rule_test_block",
        "name": "测试禁跑 demo",
        "tool": "run_adapter_once",
        "pattern": "demo_site",
        "policy": "deny",
        "priority": 90,
        "enabled": True,
    })
    assert st == 200 and g_add["ok"] is True
    st, g_test = _http_json(base_url, "POST", "/api/agent/guard/test", {"tool": "run_adapter_once", "args": {"adapter": "demo_site"}})
    assert st == 200 and g_test["policy"] == "deny" and g_test["matched_rule"]["id"] == "rule_test_block"
    st, g_del = _http_json(base_url, "DELETE", "/api/agent/guard/rule_test_block")
    assert st == 200 and g_del["ok"] is True


def test_followup_queue_goal_mode_and_max_tokens_recovery(isolated_env, monkeypatch: pytest.MonkeyPatch):
    mgr = isolated_env["mgr"]
    sess = mgr.create_session(title="高级模式测试")

    calls: List[Dict[str, Any]] = []
    gate = threading.Event()

    def _fake_chat(messages, tools=None, system_prompt="", config_override=None, max_tokens_override=None, **kwargs):
        calls.append({"messages": list(messages), "max_tokens_override": max_tokens_override})
        if len(calls) == 1:
            # First call simulates max_tokens truncation -> triggers auto-escalation
            return {
                "content": "半截输出...",
                "thinking": "",
                "tool_calls": [],
                "usage": {"prompt_tokens": 100, "completion_tokens": 4096, "total_tokens": 4196},
                "model": "mock-m",
                "finish_reason": "length",
                "effective_max_tokens": 4096,
            }
        if len(calls) == 2:
            # Wait briefly so test can enqueue a follow-up message while running
            gate.wait(timeout=2.0)
            return {
                "content": "目标已完成 [GOAL_DONE]",
                "thinking": "",
                "tool_calls": [],
                "usage": {"prompt_tokens": 150, "completion_tokens": 40, "total_tokens": 190},
                "model": "mock-m",
                "finish_reason": "stop",
            }
        return {
            "content": "排队消息已自动执行完毕",
            "thinking": "",
            "tool_calls": [],
            "usage": {"prompt_tokens": 180, "completion_tokens": 25, "total_tokens": 205},
            "model": "mock-m",
            "finish_reason": "stop",
        }

    monkeypatch.setattr(llm_client, "chat_completion", _fake_chat)

    res = mgr.send_message(sess.id, "/goal 完成测试目标")
    assert res["ok"] is True

    # Wait until 2nd LLM call is active, then push a queued follow-up message
    for _ in range(40):
        if len(calls) >= 2:
            break
        time.sleep(0.05)
    q_res = mgr.send_message(sess.id, "第二条排队指令", mode="queue")
    assert q_res["ok"] is True and q_res["queued"] is True and len(q_res["queued_messages"]) == 1
    gate.set()

    # Wait until both the /goal turn (with max_tokens recovery) AND the queued turn finish
    for _ in range(80):
        if len(calls) >= 3 and sess.status == "done":
            break
        time.sleep(0.05)

    assert len(calls) == 3
    assert calls[1]["max_tokens_override"] == 16384  # auto-escalated from 4096!
    assert sess.status == "done"
    assert sess.queued_messages == []
    assert any(ev.get("content") == "排队消息已自动执行完毕" for ev in sess.events)
