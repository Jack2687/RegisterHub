"""Tests for multi-profile LLM persistence, Anthropic/Responses protocols, Agent config, uploads, /btw, and Studio parity."""

from __future__ import annotations

import base64
import json
import threading
import urllib.request

from core import ai_agent, llm_client, llm_profiles
from web import server as lite_server


def _req(method: str, url: str, payload: dict | None = None, extra_headers: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=8) as r:
        raw = r.read().decode("utf-8")
        return r.status, raw, r.headers.get("Content-Type", "")


class _FakeResp:
    def __init__(self, body: dict, status: int = 200):
        self._raw = json.dumps(body).encode("utf-8")
        self.status = status
        self.headers = {"Content-Type": "application/json"}

    def read(self, n: int = -1) -> bytes:
        return self._raw if n < 0 else self._raw[:n]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_llm_profiles_crud_keys_models_and_session_override(tmp_path, monkeypatch):
    res_dir = tmp_path / "results"
    res_dir.mkdir()
    cfg_file = res_dir / "llm_config.json"
    acc_file = res_dir / "accounts.txt"
    acc_file.write_text("u@example.com----Pw123----u----sk-vault-key-111122223333\n", encoding="utf-8")
    sess_dir = res_dir / "agent_sessions"
    sess_dir.mkdir()

    monkeypatch.setattr(llm_client, "LLM_CONFIG_FILE", cfg_file)
    monkeypatch.setattr(lite_server, "RESULTS_DIR", res_dir)
    monkeypatch.setattr(lite_server, "ACCOUNTS_FILE", acc_file)
    monkeypatch.setattr(lite_server.agent_manager, "_sessions", {})
    monkeypatch.setattr(ai_agent, "RESULTS_DIR", res_dir)
    monkeypatch.setattr(ai_agent, "AGENT_SESSIONS_DIR", sess_dir)

    srv = lite_server.create_server(host="127.0.0.1", port=0)
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        # 1. Create a new provider with pinned & catalog models + API key
        st, raw, _ = _req(
            "POST",
            f"{base}/api/llm/profiles",
            {
                "id": "",
                "name": "DeepSeek Studio",
                "format": "openai",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "api_keys": ["sk-ds-primary-1234567890"],
                "pinned_models": ["deepseek-chat", "deepseek-reasoner"],
                "catalog_models": ["deepseek-chat", "deepseek-reasoner", "deepseek-coder"],
                "max_tokens": 0,
            },
        )
        assert st == 200
        created = json.loads(raw)
        assert created["ok"] is True
        prof = created["profile"]
        pid = prof["id"]
        assert prof["name"] == "DeepSeek Studio"
        assert prof["model"] == "deepseek-chat"
        assert prof["pinned_models"] == ["deepseek-chat", "deepseek-reasoner"]
        assert prof["catalog_models"] == ["deepseek-chat", "deepseek-reasoner", "deepseek-coder"]
        assert prof["key_count"] == 1
        assert prof["has_key"] is True

        # 2. Verify persistence across GET /api/llm/profiles and legacy sync
        _, raw_list, _ = _req("GET", f"{base}/api/llm/profiles")
        listed = json.loads(raw_list)
        assert listed["active_id"] == pid
        found = next(p for p in listed["profiles"] if p["id"] == pid)
        assert found["name"] == "DeepSeek Studio"
        assert found["pinned_models"] == ["deepseek-chat", "deepseek-reasoner"]
        legacy = llm_client.load_raw_llm_config()
        assert legacy["base_url"] == "https://api.deepseek.com/v1"
        assert legacy["model"] == "deepseek-chat"
        assert legacy["max_tokens"] == llm_client.DEFAULT_MAX_TOKENS
        assert legacy["api_key"] == "sk-ds-primary-1234567890"

        # 3. Append a 2nd key & remove the 1st key via /keys endpoint
        st_k, raw_k, _ = _req(
            "POST",
            f"{base}/api/llm/profiles/{pid}/keys",
            {"action": "add", "keys": ["sk-ds-second-9999888877"]},
        )
        assert st_k == 200
        k_res = json.loads(raw_k)
        assert k_res["added"] == 1
        assert k_res["profile"]["key_count"] == 2
        first_kid = k_res["profile"]["key_pool"][0]["id"]

        st_rm, raw_rm, _ = _req(
            "POST",
            f"{base}/api/llm/profiles/{pid}/keys",
            {"action": "remove", "key_id": first_kid},
        )
        assert st_rm == 200
        assert json.loads(raw_rm)["profile"]["key_count"] == 1

        # 4. Import from vault works (append AND replace_keys without duplicating profile)
        st_v, raw_v, _ = _req(
            "POST",
            f"{base}/api/llm/import-vault",
            {"project": "demo", "profile_id": pid, "replace_keys": False},
        )
        assert st_v == 200
        v_res = json.loads(raw_v)
        assert v_res["ok"] is True and v_res["imported_keys"] == 1 and v_res["profile"]["key_count"] == 2

        st_vr, raw_vr, _ = _req(
            "POST",
            f"{base}/api/llm/import-vault",
            {"project": "demo", "profile_id": pid, "replace_keys": True},
        )
        assert st_vr == 200
        vr_res = json.loads(raw_vr)
        assert vr_res["profile"]["id"] == pid and vr_res["profile"]["key_count"] == 1

        # Restore second key for subsequent test
        _req("POST", f"{base}/api/llm/profiles/{pid}/keys", {"action": "add", "keys": ["sk-ds-second-9999888877"]})

        # 5. POST /api/llm/models uses saved profile key & persists catalog + context windows when save_catalog=True
        monkeypatch.setattr(
            llm_client,
            "discover_models",
            lambda overrides=None, **kw: {
                "ok": True,
                "models": ["deepseek-chat", "deepseek-v4"],
                "model_context_windows": {"deepseek-v4": 256},
                "count": 2,
                "error": "",
                "seen_key": (overrides or {}).get("api_key", ""),
            },
        )
        st_m, raw_m, _ = _req(
            "POST",
            f"{base}/api/llm/models",
            {"profile_id": pid, "base_url": "https://api.deepseek.com/v1", "api_key": "", "save_catalog": True},
        )
        assert st_m == 200
        m_res = json.loads(raw_m)
        assert m_res["saved"] is True
        assert m_res["seen_key"] == "sk-vault-key-111122223333"
        _, raw_list2, _ = _req("GET", f"{base}/api/llm/profiles")
        found2 = next(p for p in json.loads(raw_list2)["profiles"] if p["id"] == pid)
        assert found2["catalog_models"] == ["deepseek-chat", "deepseek-v4"]
        assert found2["model_context_windows"].get("deepseek-v4") == 256

        # 6. Per-session model & profile persisted on CREATE and PATCH + upload + /btw
        _, raw_s, _ = _req(
            "POST",
            f"{base}/api/agent/sessions",
            {"title": "Test Model Switch", "llm_profile_id": pid, "llm_model": "deepseek-chat"},
        )
        created_s = json.loads(raw_s)["session"]
        sid = created_s["id"]
        assert created_s["llm_profile_id"] == pid
        assert created_s["llm_model"] == "deepseek-chat"

        st_p, raw_p, _ = _req(
            "PATCH",
            f"{base}/api/agent/sessions/{sid}",
            {"llm_profile_id": pid, "llm_model": "deepseek-reasoner"},
        )
        assert st_p == 200
        p_sess = json.loads(raw_p)["session"]
        assert p_sess["llm_profile_id"] == pid
        assert p_sess["llm_model"] == "deepseek-reasoner"

        # Upload attachment
        st_u, raw_u, _ = _req(
            "POST",
            f"{base}/api/agent/sessions/{sid}/upload",
            {"filename": "sample.txt", "content_b64": base64.b64encode(b"hello registerhub").decode("ascii")},
        )
        assert st_u == 200
        u_res = json.loads(raw_u)
        assert u_res["ok"] is True and u_res["file"]["name"] == "sample.txt"
        assert (res_dir / "agent_uploads" / sid / "sample.txt").read_bytes() == b"hello registerhub"

        # /btw side-question SSE emits item in done payload
        monkeypatch.setattr(
            llm_client,
            "chat_completion",
            lambda *a, **kw: {
                "content": "这是旁路快速回答",
                "thinking": "",
                "tool_calls": [],
                "usage": {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13},
                "model": "deepseek-reasoner",
                "finish_reason": "stop",
            },
        )
        st_btw, raw_btw, ctype_btw = _req(
            "POST",
            f"{base}/api/agent/sessions/{sid}/side-question/stream",
            {"question": "刚才改了哪行？"},
        )
        assert st_btw == 200 and "text/event-stream" in ctype_btw
        assert '"type": "start"' in raw_btw and '"item":' in raw_btw and "这是旁路快速回答" in raw_btw

        # 7. Persistent Agent Config GET/POST
        st_ac, raw_ac, _ = _req(
            "POST",
            f"{base}/api/agent/config",
            {"name": "自定义社区 Agent", "max_steps": 24, "llm_profile_id": pid, "enabled_tools": ["probe_url", "list_adapters"]},
        )
        assert st_ac == 200 and json.loads(raw_ac)["ok"] is True
        _, raw_ac_get, _ = _req("GET", f"{base}/api/agent/config")
        ac_get = json.loads(raw_ac_get)["agent"]
        assert ac_get["name"] == "自定义社区 Agent"
        assert ac_get["max_steps"] == 24
        assert ac_get["llm_profile_id"] == pid
        assert ac_get["enabled_tools"] == ["probe_url", "list_adapters"]

        # 8. /api/keys/verify returns SSE stream matching KeysVaultPage streamPostSSE
        st_kv, raw_kv, ctype = _req(
            "POST",
            f"{base}/api/keys/verify",
            {"keys": [{"key": "sk-vault-key-111122223333", "project": "demo"}], "base_url": ""},
        )
        assert st_kv == 200
        assert "text/event-stream" in ctype
        assert '"type": "item"' in raw_kv and '"type": "done"' in raw_kv

        # 9. Activate default & delete custom profile
        st_act, _, _ = _req("POST", f"{base}/api/llm/profiles/community_default/activate", {})
        assert st_act == 200
        st_del, raw_del, _ = _req("DELETE", f"{base}/api/llm/profiles/{pid}")
        assert st_del == 200
        assert json.loads(raw_del)["deleted"] == pid
    finally:
        srv.shutdown()
        srv.server_close()


def test_anthropic_and_openai_responses_protocols_and_call_logs(tmp_path, monkeypatch):
    cfg_file = tmp_path / "results" / "llm_config.json"
    monkeypatch.setattr(llm_client, "LLM_CONFIG_FILE", cfg_file)

    captured = []

    class _Opener:
        def open(self, req, timeout=None):
            captured.append((req.full_url, dict(req.header_items()), json.loads(req.data.decode("utf-8")) if req.data else None))
            if req.full_url.endswith("/v1/messages"):
                return _FakeResp({
                    "model": "claude-sonnet-4-20250514",
                    "stop_reason": "tool_use",
                    "content": [
                        {"type": "thinking", "thinking": "checking tools"},
                        {"type": "text", "text": "好的，马上探测。"},
                        {"type": "tool_use", "id": "toolu_1", "name": "probe_url", "input": {"url": "https://example.com"}},
                    ],
                    "usage": {"input_tokens": 42, "output_tokens": 18},
                })
            if req.full_url.endswith("/v1/responses"):
                return _FakeResp({
                    "model": "gpt-5",
                    "status": "completed",
                    "output_text": "响应完成",
                    "output": [
                        {"type": "function_call", "call_id": "fc_1", "name": "list_adapters", "arguments": "{}"},
                    ],
                    "usage": {"input_tokens": 30, "output_tokens": 12},
                })
            return _FakeResp({"data": [{"id": "claude-sonnet-4-20250514", "context_length": 204800}]})

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **kw: _Opener())

    # 1. Anthropic discover_models + chat_completion
    dm = llm_client.discover_models(overrides={
        "format": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key": "sk-ant-test-1234",
    })
    assert dm["ok"] is True
    assert dm["models"] == ["claude-sonnet-4-20250514"]
    assert dm["model_context_windows"].get("claude-sonnet-4-20250514") == 200
    assert captured[-1][1].get("X-api-key") == "sk-ant-test-1234"
    assert captured[-1][1].get("Anthropic-version") == "2023-06-01"

    anth_res = llm_client.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        tools=ai_agent.TOOL_SCHEMAS[:1],
        system_prompt="You are helpful",
        config_override={
            "format": "anthropic",
            "base_url": "https://api.anthropic.com",
            "model": "claude-sonnet-4-20250514",
            "api_key": "sk-ant-test-1234",
            "profile_name": "Claude Direct",
        },
    )
    assert anth_res["content"] == "好的，马上探测。"
    assert anth_res["thinking"] == "checking tools"
    assert anth_res["tool_calls"][0]["function"]["name"] == "probe_url"
    assert captured[-1][0] == "https://api.anthropic.com/v1/messages"

    # 2. OpenAI Responses API chat_completion
    resp_res = llm_client.chat_completion(
        messages=[{"role": "user", "content": "list"}],
        tools=ai_agent.TOOL_SCHEMAS[:1],
        system_prompt="Be concise",
        config_override={
            "format": "openai-responses",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-5",
            "api_key": "sk-oa-test-1234",
            "profile_name": "GPT-5 Responses",
        },
    )
    assert resp_res["content"] == "响应完成"
    assert resp_res["tool_calls"][0]["function"]["name"] == "list_adapters"
    assert captured[-1][0] == "https://api.openai.com/v1/responses"

    # 3. Call telemetry recorded without leaking secrets
    calls = llm_client.list_llm_calls(limit=10)
    assert len(calls) == 2
    assert calls[0]["model"] == "gpt-5"
    assert calls[1]["model"] == "claude-sonnet-4-20250514"
    assert llm_client.get_llm_call(calls[0]["id"])["profile_name"] == "GPT-5 Responses"
    assert "sk-ant-test" not in json.dumps(calls)
