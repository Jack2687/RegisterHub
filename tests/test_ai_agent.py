"""Unit tests for Community Lite LLM Client, Single Autonomous Registration Agent & Web API."""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.request

from core import ai_agent, llm_client
from web import server as lite_server


class _FakeHTTPResponse:
    def __init__(self, body: bytes, status: int = 200, url: str = "https://api.openai.com/v1/chat/completions", headers=None):
        self._body = body
        self.status = status
        self._url = url
        self.headers = headers or {"Content-Type": "application/json"}

    def read(self, n: int = -1) -> bytes:
        return self._body if n < 0 else self._body[:n]

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeOpener:
    def __init__(self, handler):
        self._handler = handler
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append((req, timeout))
        return self._handler(req)


def _start_ephemeral():
    srv = lite_server.create_server(host="127.0.0.1", port=0)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, f"http://127.0.0.1:{port}"


def _req_json(method: str, url: str, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def test_llm_config_atomic_save_masking_and_preserve(tmp_path, monkeypatch):
    cfg_file = tmp_path / "results" / "llm_config.json"
    monkeypatch.setattr(llm_client, "LLM_CONFIG_FILE", cfg_file)

    # 1. Defaults when config file does not exist yet
    pub0 = llm_client.get_public_llm_config()
    assert pub0["base_url"] == "https://api.openai.com/v1"
    assert pub0["model"] == "gpt-4o-mini"
    assert pub0["has_key"] is False
    assert pub0["api_key_hint"] == ""
    assert "api_key" not in pub0

    # 2. Save real API key -> masked in response, stored raw on disk
    pub1 = llm_client.save_llm_config({
        "base_url": "https://api.example.com/v1/",
        "model": "gpt-4.1-mini",
        "api_key": "sk-proj-secret1234567890abcd",
        "proxy_url": "http://127.0.0.1:7897",
        "max_tokens": 2048,
        "max_tokens_field": "max_completion_tokens",
        "temperature": 0.4,
    })
    assert pub1["base_url"] == "https://api.example.com/v1"
    assert pub1["has_key"] is True
    assert pub1["api_key_hint"] == "sk-pr...abcd"
    assert "api_key" not in pub1
    assert pub1["max_tokens_field"] == "max_completion_tokens"
    assert cfg_file.exists()
    raw_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw_disk["api_key"] == "sk-proj-secret1234567890abcd"

    # 3. Blank API key on subsequent save preserves existing key
    pub2 = llm_client.save_llm_config({
        "model": "o4-mini",
        "api_key": "",
        "temperature": 0.1,
    })
    assert pub2["model"] == "o4-mini"
    assert pub2["has_key"] is True
    assert pub2["api_key_hint"] == "sk-pr...abcd"
    raw_disk2 = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw_disk2["api_key"] == "sk-proj-secret1234567890abcd"


def test_llm_chat_completion_and_test_connection_mocked(tmp_path, monkeypatch):
    cfg_file = tmp_path / "results" / "llm_config.json"
    monkeypatch.setattr(llm_client, "LLM_CONFIG_FILE", cfg_file)
    llm_client.save_llm_config({
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key": "sk-test-key-00001111",
        "max_tokens_field": "max_completion_tokens",
    })

    captured_payloads = []

    def _fake_handler(req):
        captured_payloads.append(json.loads(req.data.decode("utf-8")))
        assert req.headers.get("Authorization") == "Bearer sk-test-key-00001111"
        resp_obj = {
            "model": "gpt-4o-mini-2026",
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "OK"},
            }],
            "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
        }
        return _FakeHTTPResponse(json.dumps(resp_obj).encode("utf-8"))

    opener = _FakeOpener(_fake_handler)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **kw: opener)

    res = llm_client.test_connection()
    assert res["ok"] is True
    assert res["reply"] == "OK"
    assert res["model"] == "gpt-4o-mini-2026"
    assert res["latency_ms"] >= 0
    assert "max_completion_tokens" in captured_payloads[0]
    assert "max_tokens" not in captured_payloads[0]

    # HTTP 401 failure surfaces clean error without crashing
    def _fail_401(req):
        raise urllib.error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"error":{"message":"Incorrect API key provided"}}'),
        )

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **kw: _FakeOpener(_fail_401))
    res_err = llm_client.test_connection()
    assert res_err["ok"] is False
    assert "401" in res_err["error"]
    assert "Incorrect API key" in res_err["error"]


def test_write_adapter_ast_security_anti_fake_active_and_backup(tmp_path, monkeypatch):
    sites_dir = tmp_path / "sites"
    sites_dir.mkdir()
    monkeypatch.setattr(ai_agent, "SITES_DIR", sites_dir)

    valid_v1 = '''"""Sample Adapter v1."""
from core.adapter import BaseSiteAdapter


class AcmeAdapter(BaseSiteAdapter):
    def target_url(self) -> str:
        return "https://acme.example.com/signup"

    def fill_form(self, identity):
        return True

    def submit_and_confirm(self) -> bool:
        return True
'''

    # 1. Unsafe identifiers / path traversal rejected
    for bad_name in ("../escape", "sub/site", "acme.py", "_private", "class", "123bad", ""):
        r = ai_agent.tool_write_adapter(bad_name, valid_v1)
        assert r["ok"] is False, f"Expected rejection for {bad_name!r}"

    # 2. Invalid Python syntax rejected
    r_syn = ai_agent.tool_write_adapter("broken_site", "class Broken(BaseSiteAdapter):\n    def x(:\n        pass\n")
    assert r_syn["ok"] is False
    assert "语法" in r_syn["error"]

    # 3. Missing BaseSiteAdapter subclass rejected
    r_nocls = ai_agent.tool_write_adapter("plain_mod", "x = 42\n")
    assert r_nocls["ok"] is False
    assert "BaseSiteAdapter" in r_nocls["error"]

    # 4. Fake 'Active' placeholder rejected across assignment / dict / ledger string
    fake_variants = [
        valid_v1.replace("return True\n", 'self.account_data["api_key"] = "Active"\n        return True\n', 1),
        valid_v1.replace("return True\n", 'payload = {"api_key": "Active"}\n        return bool(payload)\n', 1),
        valid_v1.replace("return True\n", 'line = "u@e.com----pw----u----Active"\n        return bool(line)\n', 1),
    ]
    for code_bad in fake_variants:
        r_fake = ai_agent.tool_write_adapter("fake_site", code_bad)
        assert r_fake["ok"] is False
        assert "占位" in r_fake["error"]

    # 5. Valid write succeeds atomically (no backup on first write)
    r_ok1 = ai_agent.tool_write_adapter("acme-demo", valid_v1)
    assert r_ok1["ok"] is True
    assert r_ok1["name"] == "acme_demo"
    assert r_ok1["backup"] == ""
    assert (sites_dir / "acme_demo.py").is_file()

    # 6. Overwrite creates .bak backup and replaces atomically
    valid_v2 = valid_v1.replace("Sample Adapter v1.", "Sample Adapter v2.")
    r_ok2 = ai_agent.tool_write_adapter("acme_demo", valid_v2)
    assert r_ok2["ok"] is True
    assert r_ok2["backup"] == "acme_demo.py.bak"
    assert "v1" in (sites_dir / "acme_demo.py.bak").read_text(encoding="utf-8")
    assert "v2" in (sites_dir / "acme_demo.py").read_text(encoding="utf-8")

    # 7. list_adapters and read_adapter reflect newly written file
    listed = ai_agent.tool_list_adapters()
    assert listed["count"] == 1
    assert listed["adapters"][0]["name"] == "acme_demo"
    assert listed["adapters"][0]["target_url"] == "https://acme.example.com/signup"

    read_res = ai_agent.tool_read_adapter("acme_demo")
    assert read_res["ok"] is True
    assert "Sample Adapter v2." in read_res["code"]


def test_probe_url_and_read_failures(tmp_path, monkeypatch):
    sample_html = b"""<!doctype html><html><head><title>Demo Signup</title></head><body>
    <form id="reg" action="/api/register" method="POST">
      <input type="email" name="email" placeholder="Work email" />
      <input type="password" name="password" id="pwd" />
      <button type="submit">Create Account</button>
    </form>
    <script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>
    </body></html>"""

    opener = _FakeOpener(lambda req: _FakeHTTPResponse(sample_html, status=200, url="https://example.com/signup", headers={"Server": "cloudflare"}))
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **kw: opener)
    monkeypatch.setattr(ai_agent, "CONFIG_FILE", tmp_path / "missing_config.json")

    res = ai_agent.tool_probe_url("https://example.com/signup")
    assert res["ok"] is True
    assert res["title"] == "Demo Signup"
    assert res["forms_count"] == 1
    assert len(res["inputs"]) == 2
    assert "Cloudflare Turnstile" in res["signals"]

    failed_file = tmp_path / "failed.txt"
    failed_file.write_text("2026-11-19 | timeout waiting OTP | a@b.com\n2026-11-19 | 429 rate limit | c@d.com\n", encoding="utf-8")
    monkeypatch.setattr(ai_agent, "FAILED_FILE", failed_file)
    f_res = ai_agent.tool_read_failures(limit=5)
    assert f_res["ok"] is True
    assert f_res["total_lines"] == 2
    assert "429 rate limit" in f_res["recent"][-1]


def test_server_llm_and_agent_endpoints_with_sse_and_lifecycle(tmp_path, monkeypatch):
    res_dir = tmp_path / "results"
    sessions_dir = res_dir / "agent_sessions"
    sites_dir = tmp_path / "sites"
    sites_dir.mkdir(parents=True)
    res_dir.mkdir(parents=True)
    llm_cfg_file = res_dir / "llm_config.json"

    monkeypatch.setattr(llm_client, "LLM_CONFIG_FILE", llm_cfg_file)
    monkeypatch.setattr(ai_agent, "RESULTS_DIR", res_dir)
    monkeypatch.setattr(ai_agent, "AGENT_SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(ai_agent, "SITES_DIR", sites_dir)
    monkeypatch.setattr(ai_agent, "FAILED_FILE", res_dir / "failed.txt")
    monkeypatch.setattr(ai_agent, "ACCOUNTS_FILE", res_dir / "accounts.txt")
    monkeypatch.setattr(ai_agent.agent_manager, "_sessions", {})

    # Mock 2-step autonomous agent turn: Step 1 calls update_plan + list_adapters, Step 2 finishes
    call_counter = {"n": 0}

    def _mock_chat(messages, tools=None, system_prompt="", **kwargs):
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            return {
                "content": "先制定执行计划并盘点当前适配器。",
                "tool_calls": [
                    {
                        "id": "call_plan_1",
                        "type": "function",
                        "function": {
                            "name": "update_plan",
                            "arguments": json.dumps({
                                "steps": [
                                    {"title": "盘点已有适配器", "status": "in_progress"},
                                    {"title": "汇报检查结论", "status": "pending"},
                                ]
                            }),
                        },
                    },
                    {
                        "id": "call_list_2",
                        "type": "function",
                        "function": {"name": "list_adapters", "arguments": "{}"},
                    },
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
                "model": "gpt-4o-mini",
                "finish_reason": "tool_calls",
            }
        return {
            "content": "盘点完成，当前工作区干净可开始新建适配器。",
            "tool_calls": [],
            "usage": {"prompt_tokens": 150, "completion_tokens": 30, "total_tokens": 180},
            "model": "gpt-4o-mini",
            "finish_reason": "stop",
        }

    monkeypatch.setattr(llm_client, "chat_completion", _mock_chat)
    monkeypatch.setattr(
        llm_client,
        "test_connection",
        lambda overrides=None, **kw: {
            "ok": True,
            "latency_ms": 42.5,
            "reply": "OK",
            "model": "gpt-4o-mini",
            "error": "",
        },
    )

    srv, base = _start_ephemeral()
    try:
        # 1. UI HTML includes new AI Direct-Run & LLM Config views, models & export buttons
        with urllib.request.urlopen(f"{base}/", timeout=5) as r:
            html = r.read().decode("utf-8")
            assert 'id="view-agent"' in html
            assert 'id="view-llm"' in html
            assert 'id="nav-badge-agent"' in html
            assert 'id="btn-llm-models"' in html
            assert 'id="btn-agent-export"' in html
            assert "一键 AI 直跑" in html

        # 2. LLM config GET / POST / key masking / test endpoint
        st, cfg0 = _req_json("GET", f"{base}/api/llm/config")
        assert st == 200 and cfg0["has_key"] is False

        st, saved = _req_json("POST", f"{base}/api/llm/config", {
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "api_key": "sk-live-unit-test-99887766",
            "temperature": 0.3,
        })
        assert st == 200 and saved["ok"] is True
        assert saved["config"]["has_key"] is True
        assert saved["config"]["api_key_hint"] == "sk-li...7766"
        assert "api_key" not in saved["config"]

        st, t_res = _req_json("POST", f"{base}/api/llm/test", {})
        assert st == 200 and t_res["ok"] is True and t_res["reply"] == "OK"

        # 3. Create agent session & list
        st, cr = _req_json("POST", f"{base}/api/agent/sessions", {
            "title": "测试直跑会话",
            "target_url": "https://example.com/signup",
        })
        assert st == 200 and cr["ok"] is True
        sid = cr["session"]["id"]

        st, lst = _req_json("GET", f"{base}/api/agent/sessions")
        assert st == 200 and any(s["id"] == sid for s in lst["sessions"])

        # 4. Send message & read SSE stream
        st, msg_res = _req_json("POST", f"{base}/api/agent/sessions/{sid}/message", {
            "message": "请盘点当前适配器",
            "target_url": "https://example.com/signup",
        })
        assert st == 200 and msg_res["ok"] is True

        sse_events = []
        with urllib.request.urlopen(f"{base}/api/agent/sessions/{sid}/stream", timeout=10) as stream_r:
            assert "text/event-stream" in (stream_r.headers.get("Content-Type") or "")
            for raw_line in stream_r:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if line.startswith("data:"):
                    ev = json.loads(line[5:].strip())
                    sse_events.append(ev)
                    if ev.get("type") == "status" and not ev.get("replay") and ev.get("status") in ("done", "error", "stopped"):
                        break

        ev_types = [e.get("type") for e in sse_events]
        assert "plan" in ev_types
        assert "tool_start" in ev_types
        assert "tool_end" in ev_types
        assert "usage" in ev_types
        assert "assistant_msg" in ev_types
        assert "status" in ev_types

        # 5. Session detail & disk persistence verified
        st, detail = _req_json("GET", f"{base}/api/agent/sessions/{sid}")
        assert st == 200 and detail["ok"] is True
        assert detail["session"]["status"] == "done"
        assert detail["session"]["usage"]["total_tokens"] == 320
        assert len(detail["session"]["plan"]) == 2
        assert (sessions_dir / f"{sid}.json").is_file()

        # 6. Stop endpoint & DELETE cleanup
        st, stop_res = _req_json("POST", f"{base}/api/agent/sessions/{sid}/stop", {})
        assert st == 200 and stop_res["ok"] is True

        st, del_res = _req_json("DELETE", f"{base}/api/agent/sessions/{sid}")
        assert st == 200 and del_res["ok"] is True and del_res["deleted"] == sid
        assert not (sessions_dir / f"{sid}.json").exists()

        try:
            _req_json("GET", f"{base}/api/agent/sessions/{sid}")
            assert False, "Expected 404 after delete"
        except urllib.error.HTTPError as he:
            assert he.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_agent_stop_cancels_active_turn(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "agent_sessions"
    monkeypatch.setattr(ai_agent, "AGENT_SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(ai_agent.agent_manager, "_sessions", {})

    entered = threading.Event()
    release = threading.Event()

    def _slow_chat(messages, tools=None, system_prompt="", **kwargs):
        entered.set()
        release.wait(timeout=3.0)
        return {
            "content": "should not be emitted after stop",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "model": "gpt-4o-mini",
            "finish_reason": "stop",
        }

    monkeypatch.setattr(llm_client, "chat_completion", _slow_chat)

    sess = ai_agent.agent_manager.create_session(title="Cancel Test")
    ai_agent.agent_manager.send_message(sess.id, "开始长任务")
    assert entered.wait(timeout=2.0)

    # Second concurrent turn must be blocked by single-thread safety
    sess2 = ai_agent.agent_manager.create_session(title="Concurrent Blocked")
    try:
        ai_agent.agent_manager.send_message(sess2.id, "同时跑第二个")
        assert False, "Expected RuntimeError for concurrent agent run"
    except RuntimeError:
        pass

    res = ai_agent.agent_manager.stop_session(sess.id)
    assert res is not None and res["status"] == "stopped"
    release.set()
    time.sleep(0.1)
    assert sess.status == "stopped"
    assert not any(ev.get("content") == "should not be emitted after stop" for ev in sess.events)


def test_llm_url_normalization_reasoning_honest_probe_proxy_and_models(tmp_path, monkeypatch):
    cfg_file = tmp_path / "results" / "llm_config.json"
    monkeypatch.setattr(llm_client, "LLM_CONFIG_FILE", cfg_file)

    # 1. Base URL normalization
    assert llm_client._build_chat_url("https://api.deepseek.com/") == "https://api.deepseek.com/v1/chat/completions"
    assert llm_client._build_chat_url("https://api.openai.com/v1/responses/") == "https://api.openai.com/v1/chat/completions"
    assert llm_client._build_chat_url("https://api.openai.com/v1/chat/completions") == "https://api.openai.com/v1/chat/completions"
    assert llm_client._build_chat_url("https://api.groq.com/openai") == "https://api.groq.com/openai/chat/completions"
    assert llm_client._build_chat_url("https://open.bigmodel.cn/api/paas/v4/") == "https://open.bigmodel.cn/api/paas/v4/chat/completions"

    # 2. SOCKS5 / SOCKS5H proxy explicitly rejected on save AND runtime
    for bad_px in ("socks5://127.0.0.1:7897", "socks5h://127.0.0.1:1080"):
        try:
            llm_client.save_llm_config({"proxy_url": bad_px})
            assert False, f"Expected ValueError for {bad_px}"
        except ValueError as ve:
            assert "http:// 或 https://" in str(ve)

    llm_client.save_llm_config({
        "base_url": "https://api.example.com",
        "model": "gpt-4o-mini",
        "api_key": "sk-test-1234567890",
    })

    socks_test = llm_client.test_connection(overrides={"proxy_url": "socks5://127.0.0.1:7897"})
    assert socks_test["ok"] is False
    assert "http:// 或 https://" in socks_test["error"]

    # 3. Invalid base_url without http(s):// must NOT silently fall back to saved URL
    bad_url_res = llm_client.test_connection(overrides={"base_url": "api.openai.com/v1"})
    assert bad_url_res["ok"] is False
    assert "http:// 或 https://" in bad_url_res["error"]

    # 4. Reasoning models (o1/o3/o4/gpt-5 or max_completion_tokens) omit temperature + extract reasoning_content
    captured = []

    def _reasoning_handler(req):
        body = json.loads(req.data.decode("utf-8"))
        captured.append((req.full_url, body))
        if body["model"] == "empty-model":
            resp_obj = {
                "model": "empty-model",
                "choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": "", "reasoning_content": "only thought"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 50, "total_tokens": 55},
            }
        else:
            resp_obj = {
                "model": body["model"],
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "OK", "reasoning_content": "step-by-step"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15},
            }
        return _FakeHTTPResponse(json.dumps(resp_obj).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **kw: _FakeOpener(_reasoning_handler))

    # Standard model -> max_tokens=512, includes temperature
    r_std = llm_client.test_connection(overrides={"model": "gpt-4o-mini", "max_tokens_field": "max_tokens"})
    assert r_std["ok"] is True
    assert captured[-1][0] == "https://api.example.com/v1/chat/completions"
    assert captured[-1][1]["max_tokens"] == 512
    assert "temperature" in captured[-1][1]

    # o3-mini / gpt-5 -> omits temperature, max_tokens_override = 1024
    for r_model in ("o3-mini", "gpt-5", "o1-preview", "o4-mini"):
        r_m = llm_client.test_connection(overrides={"model": r_model, "max_tokens_field": "max_tokens"})
        assert r_m["ok"] is True
        assert "temperature" not in captured[-1][1]
        assert captured[-1][1]["max_tokens"] == 1024

    # reasoning_content extracted as "thinking"
    comp = llm_client.chat_completion([{"role": "user", "content": "hi"}], config_override={"model": "deepseek-reasoner"})
    assert comp["thinking"] == "step-by-step"
    assert comp["content"] == "OK"

    # Empty visible reply rejected honestly
    r_empty = llm_client.test_connection(overrides={"model": "empty-model"})
    assert r_empty["ok"] is False
    assert r_empty["error"] == "模型返回空白回复"

    # 5. Pure-stdlib discover_models probes <base>/models then falls back to <root>/v1/models
    probed_urls = []

    def _models_handler(req):
        probed_urls.append(req.full_url)
        if req.full_url.endswith("/custom/models"):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"not found"))
        if req.full_url.endswith("/v1/models"):
            payload = {"data": [{"id": "gpt-4o"}, {"id": "deepseek-chat"}, {"id": "gpt-4o"}]}
            return _FakeHTTPResponse(json.dumps(payload).encode("utf-8"), url=req.full_url)
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"not found"))

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **kw: _FakeOpener(_models_handler))
    disc = llm_client.discover_models(overrides={"base_url": "https://relay.example.com/custom"})
    assert disc["ok"] is True
    assert disc["models"] == ["deepseek-chat", "gpt-4o"]
    assert disc["count"] == 2
    assert disc["source_url"].endswith("/v1/models")
    assert any(u == "https://relay.example.com/custom/models" for u in probed_urls)


def test_write_adapter_blocks_boolop_ifexp_dict_bypass(tmp_path, monkeypatch):
    sites_dir = tmp_path / "sites"
    sites_dir.mkdir()
    monkeypatch.setattr(ai_agent, "SITES_DIR", sites_dir)

    base_tpl = '''"""Bypass test."""
from core.adapter import BaseSiteAdapter


class DemoAdapter(BaseSiteAdapter):
    def target_url(self) -> str:
        return "https://demo.example.com/signup"

    def fill_form(self, identity):
        return True

    def submit_and_confirm(self) -> bool:
        key = ""
        __INJECT__
        return True
'''
    bypass_snippets = [
        'self.account_data["api_key"] = key or "Active"',
        'self.account_data["api_key"] = key if key else "placeholder"',
        'data = {"api_key": key or "Active"}',
        'data = {"token": "OK"}',
        'self.account_data.setdefault("api_key", "Active")',
        'self.account_data["api_key"] = self.account_data.get("k", "Active")',
    ]
    for snip in bypass_snippets:
        code = base_tpl.replace("__INJECT__", snip)
        res = ai_agent.tool_write_adapter("bypass_test", code)
        assert res["ok"] is False, f"Expected AST block for: {snip}"
        assert "占位" in res["error"]

    # Benign real code (empty fallback or non-credential status check) must still pass
    benign_code = base_tpl.replace(
        "__INJECT__",
        'status = "Active"\n        self.account_data["api_key"] = key or ""\n        assert status == "Active"',
    )
    ok_res = ai_agent.tool_write_adapter("benign_test", benign_code)
    assert ok_res["ok"] is True


def test_run_adapter_once_hyphen_normalization_and_unknown_detection(tmp_path, monkeypatch):
    import subprocess
    import sys

    sites_dir = tmp_path / "sites"
    sites_dir.mkdir()
    (sites_dir / "acme_demo.py").write_text("# dummy\n", encoding="utf-8")
    monkeypatch.setattr(ai_agent, "SITES_DIR", sites_dir)
    monkeypatch.setattr(ai_agent, "ACCOUNTS_FILE", tmp_path / "accounts.txt")

    seen_cmds = []

    class _FakeProc:
        def __init__(self, out_text: str, rc: int = 0):
            self.pid = 99999
            self.stdout = io.StringIO(out_text)
            self.returncode = rc

        def poll(self):
            return self.returncode

    def _fake_popen(cmd, **kwargs):
        seen_cmds.append(list(cmd))
        if len(seen_cmds) == 1:
            return _FakeProc("[+] Done acme_demo\n", rc=0)
        return _FakeProc("[-] Unknown adapter 'acme_demo'. Available: demo\n", rc=0)

    with monkeypatch.context() as mp:
        mp.setattr(subprocess, "Popen", _fake_popen)

        r1 = ai_agent.tool_run_adapter_once("acme-demo")
        assert r1["ok"] is True
        assert r1["adapter"] == "acme_demo"
        assert "--run" in seen_cmds[0]
        run_idx = seen_cmds[0].index("--run")
        assert seen_cmds[0][run_idx + 1] == "acme_demo"

        # Even if a child exited 0, "[-] Unknown adapter" output must be marked ok=False
        r2 = ai_agent.tool_run_adapter_once("acme-demo")
        assert r2["ok"] is False
        assert "未识别" in r2.get("error", "")

    # Verify real main.py exits non-zero on unknown adapter
    proc_real = subprocess.run(
        [sys.executable, str(ai_agent.ROOT_DIR / "main.py"), "--run", "definitely_not_real_adapter_xyz"],
        cwd=str(ai_agent.ROOT_DIR),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc_real.returncode != 0
    assert "Unknown adapter" in proc_real.stdout


def test_dangling_multi_tool_repair_safe_slicing_and_stop_resend_race(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "agent_sessions"
    monkeypatch.setattr(ai_agent, "AGENT_SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(ai_agent.agent_manager, "_sessions", {})

    # 1. Dangling multi-tool repair (tool 1 of 2 answered before Stop)
    broken_msgs = [
        {"role": "user", "content": "start"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "list_adapters", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "read_failures", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "list_adapters", "content": '{"ok": true}'},
    ]
    repaired = ai_agent._repair_dangling_tool_calls(broken_msgs)
    assert len(repaired) == 4
    assert repaired[2]["tool_call_id"] == "c1"
    assert repaired[3]["role"] == "tool"
    assert repaired[3]["tool_call_id"] == "c2"
    assert '"stopped": true' in repaired[3]["content"]

    # 2. Safe message slicing never cuts between assistant(tool_calls) and tool responses
    long_msgs = [
        {"role": "user", "content": "u0"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "tc1", "type": "function", "function": {"name": "list_adapters", "arguments": "{}"}},
                {"id": "tc2", "type": "function", "function": {"name": "read_failures", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "tc1", "name": "list_adapters", "content": "{}"},
        {"role": "tool", "tool_call_id": "tc2", "name": "read_failures", "content": "{}"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "done"},
    ]
    sliced = ai_agent._slice_messages_safely(long_msgs, max_count=4)
    assert sliced[0]["role"] != "tool"
    assert sliced == [{"role": "user", "content": "u1"}, {"role": "assistant", "content": "done"}]

    # 3. Stop-then-resend race: Turn 1 HTTP finishes AFTER Turn 2 starts -> Turn 1 must NOT corrupt Turn 2
    t1_entered = threading.Event()
    t1_release = threading.Event()
    t2_done = threading.Event()
    call_idx = {"n": 0}

    def _racy_chat(messages, tools=None, system_prompt="", **kwargs):
        call_idx["n"] += 1
        my_n = call_idx["n"]
        if my_n == 1:
            t1_entered.set()
            t1_release.wait(timeout=3.0)
            return {
                "content": "STALE_TURN_1_GHOST",
                "tool_calls": [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                "model": "gpt-4o-mini",
                "finish_reason": "stop",
            }
        # Turn 2 waits until Turn 1 finishes so we prove Turn 1 didn't clobber Turn 2
        t1_release.set()
        time.sleep(0.08)
        t2_done.set()
        return {
            "content": "CLEAN_TURN_2_RESULT",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "model": "gpt-4o-mini",
            "finish_reason": "stop",
        }

    monkeypatch.setattr(llm_client, "chat_completion", _racy_chat)
    sess = ai_agent.agent_manager.create_session(title="Race Test")
    ai_agent.agent_manager.send_message(sess.id, "turn 1")
    assert t1_entered.wait(timeout=2.0)

    ai_agent.agent_manager.stop_session(sess.id)
    assert sess.status == "stopped"

    r_t2 = ai_agent.agent_manager.send_message(sess.id, "turn 2")
    assert r_t2["ok"] is True and r_t2["steered"] is False
    assert t2_done.wait(timeout=3.0)
    for _ in range(30):
        if sess.status == "done":
            break
        time.sleep(0.05)

    assert sess.status == "done"
    contents = [ev.get("content") for ev in sess.events if ev.get("type") == "assistant_msg"]
    assert "CLEAN_TURN_2_RESULT" in contents
    assert "STALE_TURN_1_GHOST" not in contents


def test_agent_empty_nudge_budget_wrapup_steering_and_markdown_export(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "agent_sessions"
    monkeypatch.setattr(ai_agent, "AGENT_SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(ai_agent.agent_manager, "_sessions", {})

    # --- A. Thinking-only empty-turn nudge (recovers after 2 empty thinking-only turns) ---
    nudge_calls = {"n": 0}

    def _nudge_chat(messages, tools=None, system_prompt="", **kwargs):
        nudge_calls["n"] += 1
        if nudge_calls["n"] <= 2:
            return {
                "content": "",
                "thinking": "internal chain of thought",
                "tool_calls": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                "model": "deepseek-reasoner",
                "finish_reason": "stop",
            }
        return {
            "content": "经过提醒后给出了正式中文结论。",
            "thinking": "",
            "tool_calls": [],
            "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35},
            "model": "deepseek-reasoner",
            "finish_reason": "stop",
        }

    monkeypatch.setattr(llm_client, "chat_completion", _nudge_chat)
    s_nudge = ai_agent.agent_manager.create_session(title="Nudge Test")
    ai_agent.agent_manager.send_message(s_nudge.id, "开始分析")
    for _ in range(40):
        if s_nudge.status == "done":
            break
        time.sleep(0.05)
    assert s_nudge.status == "done"
    assert nudge_calls["n"] == 3
    assert any(ev.get("content") == "经过提醒后给出了正式中文结论。" for ev in s_nudge.events)

    # --- B. Graceful budget settlement (wrap-up with tools=[]) ---
    monkeypatch.setattr(ai_agent, "MAX_STEPS_PER_TURN", 2)
    wrap_calls = []

    def _budget_chat(messages, tools=None, system_prompt="", **kwargs):
        wrap_calls.append({"tools": tools, "msg_count": len(messages)})
        if tools:
            return {
                "content": "",
                "tool_calls": [{"id": f"c_{len(wrap_calls)}", "type": "function", "function": {"name": "list_adapters", "arguments": "{}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                "model": "gpt-4o-mini",
                "finish_reason": "tool_calls",
            }
        return {
            "content": "收尾总结：已完成 2 次适配器盘点，暂无新增文件。",
            "tool_calls": [],
            "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
            "model": "gpt-4o-mini",
            "finish_reason": "stop",
        }

    monkeypatch.setattr(llm_client, "chat_completion", _budget_chat)
    s_wrap = ai_agent.agent_manager.create_session(title="Wrap-Up Test")
    ai_agent.agent_manager.send_message(s_wrap.id, "持续巡检")
    for _ in range(40):
        if s_wrap.status == "done":
            break
        time.sleep(0.05)
    assert s_wrap.status == "done"
    assert len(wrap_calls) == 3
    assert wrap_calls[-1]["tools"] == []
    assert any(ev.get("wrap_up") is True and "收尾总结" in str(ev.get("content")) for ev in s_wrap.events)

    # --- C. Mid-run non-destructive steering ---
    monkeypatch.setattr(ai_agent, "MAX_STEPS_PER_TURN", 6)
    tool_entered = threading.Event()
    tool_release = threading.Event()
    seen_steered_in_llm = {"ok": False}

    def _slow_tool_list():
        tool_entered.set()
        tool_release.wait(timeout=3.0)
        return {"ok": True, "count": 0, "adapters": []}

    monkeypatch.setattr(ai_agent, "tool_list_adapters", _slow_tool_list)
    steer_turn = {"n": 0}

    def _steer_chat(messages, tools=None, system_prompt="", **kwargs):
        steer_turn["n"] += 1
        if steer_turn["n"] == 1:
            return {
                "content": "开始列出适配器。",
                "tool_calls": [{"id": "steer_c1", "type": "function", "function": {"name": "list_adapters", "arguments": "{}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                "model": "gpt-4o-mini",
                "finish_reason": "tool_calls",
            }
        if any("改用 IMAP 接码" in str(m.get("content") or "") for m in messages):
            seen_steered_in_llm["ok"] = True
        return {
            "content": "已按你的实时纠偏改用 IMAP 接码方案。",
            "tool_calls": [],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            "model": "gpt-4o-mini",
            "finish_reason": "stop",
        }

    monkeypatch.setattr(llm_client, "chat_completion", _steer_chat)
    s_steer = ai_agent.agent_manager.create_session(title="Steering Test", target_url="https://example.com/signup")
    ai_agent.agent_manager.send_message(s_steer.id, "探测并写适配器")
    assert tool_entered.wait(timeout=2.0)

    steer_res = ai_agent.agent_manager.send_message(s_steer.id, "改用 IMAP 接码")
    assert steer_res["ok"] is True
    assert steer_res["steered"] is True
    assert steer_res["session"]["id"] == s_steer.id
    tool_release.set()

    for _ in range(40):
        if s_steer.status == "done":
            break
        time.sleep(0.05)
    assert s_steer.status == "done"
    assert seen_steered_in_llm["ok"] is True
    assert any(ev.get("type") == "user_msg" and ev.get("steered") is True for ev in s_steer.events)

    # --- D. Markdown Export (direct + HTTP endpoint) ---
    md_res = ai_agent.export_session_markdown(s_steer.id)
    assert md_res is not None and md_res["ok"] is True
    assert md_res["filename"] == f"{s_steer.id}.md"
    assert "# Steering Test" in md_res["markdown"]
    assert "实时纠偏" in md_res["markdown"]
    assert "list_adapters" in md_res["markdown"]

    monkeypatch.setattr(
        llm_client,
        "discover_models",
        lambda overrides=None, **kw: {
            "ok": True,
            "models": ["deepseek-chat", "gpt-4o-mini"],
            "source_url": "https://api.openai.com/v1/models",
            "count": 2,
            "error": "",
        },
    )

    srv, base = _start_ephemeral()
    try:
        st, m_res = _req_json("POST", f"{base}/api/llm/models", {})
        assert st == 200 and m_res["ok"] is True and m_res["count"] == 2

        st, e_res = _req_json("GET", f"{base}/api/agent/sessions/{s_steer.id}/export")
        assert st == 200 and e_res["ok"] is True and e_res["filename"].endswith(".md")
        assert "Steering Test" in e_res["markdown"]

        try:
            _req_json("GET", f"{base}/api/agent/sessions/missing_session_999/export")
            assert False, "Expected 404 for unknown session export"
        except urllib.error.HTTPError as he:
            assert he.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_agent_session_rename_patch_and_validation(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "agent_sessions"
    monkeypatch.setattr(ai_agent, "AGENT_SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(ai_agent.agent_manager, "_sessions", {})

    srv, base = _start_ephemeral()
    try:
        # Verify Layout Workbench & Agent UI polish hooks exist in served HTML
        with urllib.request.urlopen(f"{base}/", timeout=5) as r:
            html = r.read().decode("utf-8")
            assert 'id="layout-summary" class="layout-summary-grid"' in html
            assert 'id="layout-zoom-range"' in html
            assert 'id="agent-session-search"' in html

        st, cr = _req_json("POST", f"{base}/api/agent/sessions", {
            "title": "初始会话名",
            "target_url": "https://demo.example.com/register",
        })
        assert st == 200 and cr["ok"] is True
        sid = cr["session"]["id"]

        # 1. Valid PATCH rename with surrounding spaces & newline collapsed
        st, rn = _req_json("PATCH", f"{base}/api/agent/sessions/{sid}", {
            "title": "  自定义   新标题\n测试  ",
        })
        assert st == 200 and rn["ok"] is True
        assert rn["session"]["id"] == sid
        assert rn["session"]["title"] == "自定义 新标题 测试"

        # Persisted atomically on disk and visible via GET
        disk_obj = json.loads((sessions_dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert disk_obj["title"] == "自定义 新标题 测试"
        st, detail = _req_json("GET", f"{base}/api/agent/sessions/{sid}")
        assert st == 200 and detail["session"]["title"] == "自定义 新标题 测试"

        # 2. >80 chars title trimmed cleanly to 80 chars
        long_title = "A" * 120
        st, rn_long = _req_json("PATCH", f"{base}/api/agent/sessions/{sid}", {"title": long_title})
        assert st == 200 and rn_long["ok"] is True
        assert len(rn_long["session"]["title"]) == 80
        assert rn_long["session"]["title"] == "A" * 80

        # 3. Empty / whitespace / non-string titles rejected with 400 without mutating title
        for bad_payload in ({"title": ""}, {"title": "   \t\n  "}, {}, {"title": 12345}):
            try:
                _req_json("PATCH", f"{base}/api/agent/sessions/{sid}", bad_payload)
                assert False, f"Expected 400 for payload {bad_payload!r}"
            except urllib.error.HTTPError as he:
                assert he.code == 400
                err_body = json.loads(he.read().decode("utf-8"))
                assert err_body["ok"] is False

        st, detail_after = _req_json("GET", f"{base}/api/agent/sessions/{sid}")
        assert st == 200 and detail_after["session"]["title"] == "A" * 80

        # 4. Unknown session id returns 404
        try:
            _req_json("PATCH", f"{base}/api/agent/sessions/missing_sid_404", {"title": "Valid"})
            assert False, "Expected 404 for unknown session"
        except urllib.error.HTTPError as he:
            assert he.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_agent_stop_mid_tool_records_diagnostic_note(tmp_path, monkeypatch):
    import re

    sessions_dir = tmp_path / "agent_sessions"
    monkeypatch.setattr(ai_agent, "AGENT_SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(ai_agent.agent_manager, "_sessions", {})

    tool_entered = threading.Event()
    tool_release = threading.Event()

    def _blocking_list_adapters():
        tool_entered.set()
        tool_release.wait(timeout=3.0)
        return {"ok": True, "count": 0, "adapters": []}

    call_n = {"n": 0}

    def _chat_calling_tool(messages, tools=None, system_prompt="", **kwargs):
        call_n["n"] += 1
        if call_n["n"] == 1:
            return {
                "content": "开始扫描适配器。",
                "tool_calls": [
                    {
                        "id": "call_mid_abort_1",
                        "type": "function",
                        "function": {"name": "list_adapters", "arguments": "{}"},
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
                "model": "gpt-4o-mini",
                "finish_reason": "tool_calls",
            }
        return {
            "content": "新回合已恢复完成。",
            "tool_calls": [],
            "usage": {"prompt_tokens": 15, "completion_tokens": 6, "total_tokens": 21},
            "model": "gpt-4o-mini",
            "finish_reason": "stop",
        }

    monkeypatch.setattr(ai_agent, "tool_list_adapters", _blocking_list_adapters)
    monkeypatch.setattr(llm_client, "chat_completion", _chat_calling_tool)

    srv, base = _start_ephemeral()
    try:
        st, cr = _req_json("POST", f"{base}/api/agent/sessions", {"title": "Mid-Tool Abort"})
        assert st == 200
        sid = cr["session"]["id"]

        st, _ = _req_json("POST", f"{base}/api/agent/sessions/{sid}/message", {"message": "开始扫描"})
        assert st == 200
        assert tool_entered.wait(timeout=2.0)
        time.sleep(0.06)

        st, stop_res = _req_json("POST", f"{base}/api/agent/sessions/{sid}/stop", {})
        assert st == 200 and stop_res["ok"] is True
        assert stop_res["status"] == "stopped"
        diag_re = re.compile(r"^已手动停止（中断时正在执行工具 list_adapters，已耗时 \d+\.\ds）$")
        assert diag_re.match(stop_res.get("error", "")), f"Unexpected abort note: {stop_res!r}"

        tool_release.set()
        time.sleep(0.08)

        # Persisted in detail, status event, disk JSON, and Markdown export
        st, detail = _req_json("GET", f"{base}/api/agent/sessions/{sid}")
        assert st == 200
        assert detail["session"]["status"] == "stopped"
        assert diag_re.match(detail["session"]["error"])
        stop_evs = [ev for ev in detail["session"]["events"] if ev.get("type") == "status" and ev.get("status") == "stopped"]
        assert stop_evs and diag_re.match(stop_evs[-1].get("error", ""))

        disk_obj = json.loads((sessions_dir / f"{sid}.json").read_text(encoding="utf-8"))
        assert diag_re.match(disk_obj.get("error", ""))

        st, exp = _req_json("GET", f"{base}/api/agent/sessions/{sid}/export")
        assert st == 200 and "list_adapters" in exp["markdown"] and "手动停止" in exp["markdown"]

        # Starting a fresh turn clears the previous abort diagnostic error cleanly
        st, r2 = _req_json("POST", f"{base}/api/agent/sessions/{sid}/message", {"message": "继续执行"})
        assert st == 200 and r2["ok"] is True
        for _ in range(40):
            _, d2 = _req_json("GET", f"{base}/api/agent/sessions/{sid}")
            if d2["session"]["status"] == "done":
                break
            time.sleep(0.05)
        assert d2["session"]["status"] == "done"
        assert d2["session"]["error"] == ""
    finally:
        tool_release.set()
        srv.shutdown()
        srv.server_close()


def test_stdlib_only_imports_in_llm_and_agent():
    import ast
    import sys

    stdlib_names = set(getattr(sys, "stdlib_module_names", ()))
    allowed_local = {"core"}
    for rel in ("core/llm_client.py", "core/ai_agent.py"):
        src = (ai_agent.ROOT_DIR / rel).read_text(encoding="utf-8")
        tree = ast.parse(src, filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    assert top in stdlib_names or top in allowed_local, f"Non-stdlib import {top!r} in {rel}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                assert top in stdlib_names or top in allowed_local, f"Non-stdlib from-import {top!r} in {rel}"
