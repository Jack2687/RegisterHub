"""Tests for Community Edition polish features:
- Online AST-checked Adapter Code Editor (`adapter_editor: True`)
- Multi-country ToolsPage Profile & 4-in-1 Extras generators (`tools_extras: True`)
- Built-in editable Agent Skills (`skills/*.md`) & system prompt injection
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from core import ai_agent
from web import server as web_server


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


def test_online_ast_adapter_editor_validates_saves_and_backs_up(tmp_path, monkeypatch):
    sites_dir = tmp_path / "sites"
    sites_dir.mkdir()
    orig = (
        "from core.adapter import BaseSiteAdapter\n\n"
        "class SampleAdapter(BaseSiteAdapter):\n"
        "    def target_url(self) -> str:\n"
        "        return 'https://example.com/signup'\n"
        "    def fill_form(self, identity):\n"
        "        return True\n"
        "    def submit_and_confirm(self) -> bool:\n"
        "        return False\n"
    )
    (sites_dir / "sample_site.py").write_text(orig, encoding="utf-8")
    monkeypatch.setattr(ai_agent, "SITES_DIR", sites_dir)
    monkeypatch.setattr(ai_agent, "RESULTS_DIR", tmp_path / "results")

    srv, base = _start_ephemeral()
    try:
        st, meta = _req_json("GET", f"{base}/api/console/meta")
        assert st == 200
        assert meta["features"]["adapter_editor"] is True
        assert meta["features"]["tools_extras"] is True

        # 1. Valid edit passes AST & creates .bak backup
        updated = orig.replace("https://example.com/signup", "https://example.org/register")
        st, res = _req_json("POST", f"{base}/api/studio/adapters/sample_site/code", {"code": updated})
        assert st == 200 and res["ok"] is True
        assert res["lines"] >= 8
        assert (sites_dir / "sample_site.py.bak").read_text(encoding="utf-8") == orig
        assert "https://example.org/register" in (sites_dir / "sample_site.py").read_text(encoding="utf-8")

        # 2. Syntax error is rejected with 400
        try:
            _req_json("POST", f"{base}/api/studio/adapters/sample_site/code", {"code": "def broken(:\n  pass"})
            assert False, "Expected 400 for syntax error"
        except urllib.error.HTTPError as he:
            assert he.code == 400

        # 3. Fake placeholder credential is rejected with 400
        bad_fake = updated.replace("return False", 'self.account_data["api_key"] = "Active"\n        return True')
        try:
            _req_json("POST", f"{base}/api/studio/adapters/sample_site/code", {"code": bad_fake})
            assert False, "Expected 400 for fake placeholder credential"
        except urllib.error.HTTPError as he:
            assert he.code == 400
    finally:
        srv.shutdown()
        srv.server_close()


def test_tools_profile_batch_and_extras_four_in_one():
    srv, base = _start_ephemeral()
    try:
        # 1. Profile endpoint returns profiles list + us_states matching ToolsPage contract
        st, prof = _req_json(
            "POST",
            f"{base}/api/tools/profile",
            {"country": "US", "state": "DE", "count": 4, "email": "custom@example.test"},
        )
        assert st == 200 and prof["ok"] is True
        assert prof["count"] == 4
        assert len(prof["profiles"]) == 4
        assert "DE" in prof["us_states"]
        for p in prof["profiles"]:
            assert p["country_code"] == "US"
            assert p["state_code"] == "DE"
            assert p["email"] == "custom@example.test"
            assert p["full_name"] and p["address"] and p["zip_code"] and p["phone"]

        # Non-US country supported cleanly
        st, jp = _req_json("POST", f"{base}/api/tools/profile", {"country": "JP", "count": 2})
        assert st == 200 and len(jp["profiles"]) == 2
        assert jp["profiles"][0]["country_code"] == "JP"

        # 2. All 4 extras kinds return complete non-empty fields
        st, tax = _req_json("POST", f"{base}/api/tools/extras", {"kind": "tax", "country": "US", "state": "DE"})
        assert st == 200 and tax["kind"] == "tax"
        assert tax["data"]["tax_id_formatted"] and tax["data"]["duns_number"]

        st, phone = _req_json("POST", f"{base}/api/tools/extras", {"kind": "phone", "country": "UK"})
        assert st == 200 and phone["data"]["e164"].startswith("+44")

        st, idc = _req_json("POST", f"{base}/api/tools/extras", {"kind": "idcard", "country": "US", "state": "CA"})
        assert st == 200 and idc["data"]["passport_number"] and idc["data"]["driver_license"].startswith("CA-")

        st, hw = _req_json("POST", f"{base}/api/tools/extras", {"kind": "hardware", "platform": "mac_apple_silicon"})
        assert st == 200 and hw["data"]["os_platform"] == "MacIntel" and "Apple" in hw["data"]["webgl_renderer"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_agent_builtin_skills_list_read_write_and_prompt_injection(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "site-onboarding.md").write_text(
        "---\nname: site-onboarding\ndescription: 新站探测测试技能\n---\n# 流程\n先调用 probe_url。\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ai_agent, "AGENT_SKILLS_DIR", skills_dir)
    monkeypatch.setattr(ai_agent, "RESULTS_DIR", tmp_path / "results")

    srv, base = _start_ephemeral()
    try:
        st, cfg = _req_json("GET", f"{base}/api/agent/config")
        assert st == 200
        assert len(cfg["skills"]) == 1
        assert cfg["skills"][0]["name"] == "site-onboarding"
        assert cfg["skills"][0]["enabled"] is True
        assert "site-onboarding" in cfg["agent"]["enabled_skills"]

        # Read skill content
        st, sk = _req_json("GET", f"{base}/api/agent/skills/site-onboarding")
        assert st == 200 and "probe_url" in sk["content"]

        # Edit skill content + create custom skill
        st, wr = _req_json(
            "POST",
            f"{base}/api/agent/skills/custom-recipe",
            {"content": "---\ndescription: 自定义技能\n---\n优先检查内置邮箱池。\n"},
        )
        assert st == 200 and wr["ok"] is True

        # Prompt rendering includes enabled skills cleanly
        rendered = ai_agent._render_enabled_skills_prompt(["site-onboarding", "custom-recipe"])
        assert "site-onboarding" in rendered and "优先检查内置邮箱池" in rendered

        # Disable one skill via /api/agent/config and verify persistence
        st, saved = _req_json("POST", f"{base}/api/agent/config", {"enabled_skills": ["site-onboarding"]})
        assert st == 200 and saved["agent"]["enabled_skills"] == ["site-onboarding"]
        by_name = {s["name"]: s["enabled"] for s in saved["skills"]}
        assert by_name["site-onboarding"] is True
        assert by_name["custom-recipe"] is False
    finally:
        srv.shutdown()
        srv.server_close()
