"""Tests for Community Edition Built-in Mail Pool & Built-in Proxy Pool (Zero-Dependency)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from core import ai_agent
from core.imap_mail import BuiltinPoolMailClient
from core.mail_builtin import BuiltinMailStore
from core.proxy_builtin import BuiltinProxyStore
from core.runner import StandardRunner
from web import server as web_server
from web import studio_bridge


def _start_ephemeral():
    srv = web_server.create_server("127.0.0.1", 0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{port}"


def _req_json(method: str, url: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_console_meta_enables_builtin_mail_and_proxies(tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(web_server, "CONFIG_FILE", tmp_path / "config.json")
    studio_bridge._BUILTIN_MAIL_STORES.clear()
    studio_bridge._BUILTIN_PROXY_STORES.clear()

    srv, base = _start_ephemeral()
    try:
        st, meta = _req_json("GET", f"{base}/api/console/meta")
        assert st == 200
        assert meta["edition"] == "community"
        assert meta["features"]["builtin_mail"] is True
        assert meta["features"]["builtin_proxies"] is True
        assert meta["features"]["proxy_import"] is True
        assert meta["features"]["single_thread_only"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_builtin_mail_pool_http_lifecycle_and_otp_client(tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(web_server, "CONFIG_FILE", tmp_path / "config.json")
    studio_bridge._BUILTIN_MAIL_STORES.clear()

    srv, base = _start_ephemeral()
    try:
        # 1. Formats
        st, fmts = _req_json("GET", f"{base}/api/mail/builtin/formats")
        assert st == 200 and len(fmts["formats"]) >= 5

        sample_text = "\n".join([
            "alice@outlook.com----Pass123!----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C519_BAY.mock_refresh_token_001",
            "bob@icloud.com----https://mail-link.example.com/inbox/abc123token",
            "carol@example.com----app_secret_99----imap.example.com:993",
        ])

        # 2. Preview auto-detects all 3 formats & masks secrets
        st, prev = _req_json("POST", f"{base}/api/mail/builtin/preview", {"text": sample_text, "format": "auto"})
        assert st == 200
        assert prev["valid_count"] == 3
        assert prev["error_count"] == 0
        raw_prev = json.dumps(prev)
        assert "Pass123!" not in raw_prev
        assert "app_secret_99" not in raw_prev
        assert "mock_refresh_token_001" not in raw_prev

        # 3. Import into results/builtin_mail.db (DPAPI encrypted)
        st, imp = _req_json("POST", f"{base}/api/mail/builtin/import", {"text": sample_text, "format": "auto"})
        assert st == 200
        assert imp["added"] == 3
        assert (tmp_path / "builtin_mail.db").is_file()

        # 4. List & filter & search
        st, listed = _req_json("GET", f"{base}/api/mail/builtin/accounts?query=outlook")
        assert st == 200
        assert listed["total"] == 1
        assert listed["items"][0]["email"] == "alice@outlook.com"
        assert listed["items"][0]["has_refresh_token"] is True
        assert "Pass123!" not in json.dumps(listed)

        # 5. Lease -> Release(mark_status=dead) -> Cleanup dead
        st, leased = _req_json("POST", f"{base}/api/mail/builtin/lease", {"owner": "test_worker", "include_unknown": True})
        assert st == 200 and leased["account"] is not None
        leased_email = leased["account"]["email"]

        st, rel = _req_json(
            "POST",
            f"{base}/api/mail/builtin/release",
            {"email": leased_email, "owner": "test_worker", "mark_status": "dead"},
        )
        assert st == 200 and rel["released"] is True
        assert rel["stats"]["dead"] == 1

        st, clean = _req_json("POST", f"{base}/api/mail/builtin/cleanup-dead", {})
        assert st == 200 and clean["deleted"] == 1
        assert clean["stats"]["total"] == 2

        # 6. Reset & Delete remaining
        st, rst = _req_json("POST", f"{base}/api/mail/builtin/reset", {"emails": ["bob@icloud.com"]})
        assert st == 200 and rst["reset"] == 1

        # 7. BuiltinPoolMailClient end-to-end lease + OTP extraction + release
        client = BuiltinPoolMailClient(db_path=tmp_path / "builtin_mail.db", owner="unit_runner", include_unknown=True)
        em = client.create_mailbox()
        assert em in ("bob@icloud.com", "carol@example.com")
        monkeypatch.setattr(
            client.store,
            "list_messages",
            lambda email, top=10, since=0.0: [
                {"id": "m1", "subject": "Verify your account", "text": "Your code is 849201", "html": ""}
            ],
        )
        code = client.poll_for_verification_code(timeout_secs=6)
        assert code == "849201"
        assert client.release(mark_status="valid") is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_builtin_proxy_pool_http_lifecycle_and_runner_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(web_server, "CONFIG_FILE", tmp_path / "config.json")
    studio_bridge._BUILTIN_PROXY_STORES.clear()

    srv, base = _start_ephemeral()
    try:
        # 1. Presets
        st, presets = _req_json("GET", f"{base}/api/proxies/builtin/presets")
        assert st == 200 and len(presets["presets"]) >= 4

        # 2. Import static HTTP + auth SOCKS5 (Verify masking + browser_compatible flag)
        proxy_text = "\n".join([
            "http://demo_user:secret_pass_1@proxy-a.example.com:8080",
            "socks5://sock_user:secret_pass_2@proxy-b.example.com:1080",
        ])
        st, imp = _req_json("POST", f"{base}/api/proxies/builtin/import", {"text": proxy_text, "default_scheme": "http"})
        assert st == 200
        assert imp["added"] == 2
        assert (tmp_path / "builtin_proxies.db").is_file()

        st, entries = _req_json("GET", f"{base}/api/proxies/builtin/entries")
        assert st == 200
        assert len(entries["items"]) == 2
        raw_entries = json.dumps(entries)
        assert "secret_pass_1" not in raw_entries
        assert "secret_pass_2" not in raw_entries
        by_scheme = {it["scheme"]: it for it in entries["items"]}
        assert by_scheme["http"]["browser_compatible"] is True
        assert by_scheme["socks5"]["browser_compatible"] is False

        # 3. Add Residential Gateway + materialize fresh Sticky Session on loopback
        st, gw = _req_json(
            "POST",
            f"{base}/api/proxies/builtin/gateway",
            {
                "gateway_url": "http://res_user:res_secret@gate.smartproxy.example:7000",
                "label": "US-Res",
                "preset": "smartproxy",
                "country": "us",
            },
        )
        assert st == 200 and gw["entry"]["kind"] == "residential_gateway"
        gw_id = gw["entry"]["id"]

        st, mat = _req_json(
            "POST",
            f"{base}/api/proxies/builtin/materialize-gateway",
            {"id": gw_id, "session_id": "sess999"},
        )
        assert st == 200
        pw = mat["entry"]["playwright_proxy"]
        assert pw["server"] == "http://gate.smartproxy.example:7000"
        assert "sess999" in pw["username"]
        assert pw["password"] == "res_secret"

        # 4. Runtime entries loopback broker returns browser-compatible items only when requested
        st, rt = _req_json("GET", f"{base}/api/proxies/builtin/runtime-entries?browser_only=1")
        assert st == 200
        assert len(rt["items"]) == 2  # HTTP static + HTTP residential gateway (excludes auth SOCKS5)

        # 5. API extract source CRUD (add -> toggle -> delete)
        st, src = _req_json(
            "POST",
            f"{base}/api/proxies/builtin/sources",
            {
                "kind": "api_extract",
                "url": "https://api.proxy-vendor.example.com/get?token=topsecret99",
                "label": "Vendor-API",
                "pull_now": False,
            },
        )
        assert st == 200 and src["source"]["id"] >= 1
        assert "topsecret99" not in json.dumps(src)
        sid = src["source"]["id"]

        st, tog_src = _req_json("POST", f"{base}/api/proxies/builtin/sources/{sid}/toggle", {"enabled": False})
        assert st == 200 and tog_src["ok"] is True

        st, del_src = _req_json("DELETE", f"{base}/api/proxies/builtin/sources/{sid}")
        assert st == 200 and del_src["deleted"] is True

        # 6. Toggle & delete entry
        http_id = by_scheme["http"]["id"]
        st, tog = _req_json("POST", f"{base}/api/proxies/builtin/toggle", {"ids": [http_id], "enabled": False})
        assert st == 200 and tog["updated"] == 1

        # 7. StandardRunner resolves built-in residential gateway with fresh sticky session & masked label
        runner = StandardRunner({"browser": {"proxy_mode": "builtin", "headless": True}, "output_dir": str(tmp_path)})
        pw_proxy, label = runner._resolve_launch_proxy()
        assert pw_proxy is not None
        assert pw_proxy["server"] == "http://gate.smartproxy.example:7000"
        assert pw_proxy["password"] == "res_secret"
        assert "res_secret" not in label

        # 8. Kernel endpoints respond gracefully without crashing in zero-dep Community Edition
        st, kst = _req_json("GET", f"{base}/api/proxies/builtin/kernel/status")
        assert st == 200 and kst["kernel"]["running"] is False
        st, kstart = _req_json("POST", f"{base}/api/proxies/builtin/kernel/start", {})
        assert st == 200 and kstart["ok"] is False and "零外部依赖" in kstart["error"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_settings_config_and_agent_builtin_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(web_server, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(ai_agent, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(ai_agent, "CONFIG_FILE", tmp_path / "config.json")
    studio_bridge._BUILTIN_MAIL_STORES.clear()
    studio_bridge._BUILTIN_PROXY_STORES.clear()

    srv, base = _start_ephemeral()
    try:
        # 1. Switch Settings to builtin mail + builtin proxy (preserves custom proxy URL)
        st, s1 = _req_json(
            "POST",
            f"{base}/api/settings/config",
            {"proxy_mode": "proxy", "proxy_residential_url": "http://127.0.0.1:7897"},
        )
        assert st == 200 and s1["proxy_mode"] == "proxy" and s1["proxy_residential_url"] == "http://127.0.0.1:7897"

        st, s2 = _req_json(
            "POST",
            f"{base}/api/settings/config",
            {"proxy_mode": "builtin", "mail_provider": "builtin"},
        )
        assert st == 200
        assert s2["proxy_mode"] == "builtin"
        assert s2["mail_provider"] == "builtin"
        assert s2["proxy_residential_url"] == "http://127.0.0.1:7897"

        # 2. Agent config exposes all 16 tools including built-in mail & proxy managers
        st, acfg = _req_json("GET", f"{base}/api/agent/config")
        assert st == 200
        tool_names = {t["name"] for t in acfg["available_tools"]}
        assert len(tool_names) == 16
        assert "manage_builtin_mail" in tool_names
        assert "manage_builtin_proxies" in tool_names

        # 3. Agent tools manage built-in mail & proxies and check_infra reports live stats
        r_m = ai_agent.tool_manage_builtin_mail(
            "import",
            text="agent@example.com----app_pw----imap.example.com:993",
        )
        assert r_m["ok"] is True and r_m["added"] == 1

        r_p = ai_agent.tool_manage_builtin_proxies(
            "import",
            text="http://198.51.100.10:8080",
        )
        assert r_p["ok"] is True and r_p["added"] == 1

        infra = ai_agent.tool_check_infra(check_ip_score=False)
        assert infra["ok"] is True
        assert infra["config"]["proxy_mode"] == "builtin"
        assert infra["config"]["mail_mode"] == "builtin"
        assert infra["builtin_mail"]["total"] == 1
        assert infra["builtin_proxies"]["total"] == 1
    finally:
        srv.shutdown()
        srv.server_close()
