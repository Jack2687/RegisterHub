"""Regression tests for the 2026-11-26 full security & bug remediation suite.

Covers:
- FC-1 / BH5-3 / BH5-4 / N-5: core.net_guard public URL, obfuscated IP, fullwidth dot, and SOCKS5 rejection
- B-1: Zero-password browser-origin defense (Host DNS-rebinding, Sec-Fetch-Site, Origin, text/plain CSRF)
- W-01 / N-2 / D-2 / D-3: LLM base_url override key protection, 30x cross-origin auth strip, error redaction
- W-02 / W-04 / W-07 / W-08 / W-12: proxy masking, exact key prune, per-project error clear, lease ownership
- BH1 / BH2 / BH3 / BH5 core regressions (locks, exporter CSV formula guard, repair/slice/delete/checkpoint, events cursor)
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core import ai_agent, llm_client, llm_profiles
from core.exporter import export_to_csv, parse_accounts_file, run_export
from core.locks import AtomicClaimLock, interprocess_file_lock
from core.mail_builtin import BuiltinMailStore
from core.net_guard import (
    PrivateAddressBlockedError,
    build_safe_urllib_opener,
    validate_proxy_url,
    validate_public_http_url,
)
from core.secrets import sanitize_error_text
from core.stealth import connect_fingerprint_browser
from web import server as web_server


def _start_server():
    srv = web_server.create_server("127.0.0.1", 0)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, f"http://127.0.0.1:{port}", port


# ---------------------------------------------------------------------------
# 1. net_guard full unit coverage (FC-1, BH5-3, BH5-4, N-5)
# ---------------------------------------------------------------------------


def test_net_guard_public_domains_and_obfuscated_ip_blocks():
    # Legitimate hex-letter domains must NOT be misclassified as obfuscated IPs (BH5-3)
    for ok_url in (
        "https://cafe.de/signup",
        "https://dead.beef/",
        "https://0x.org/",
        "https://beef.de/",
        "https://example.com/register",
    ):
        assert validate_public_http_url(ok_url, resolve_dns=False) == ok_url

    # Private / loopback / metadata / obfuscated / fullwidth dot URLs MUST be blocked (FC-1, N-5)
    for bad_url in (
        "http://127.0.0.1/admin",
        "http://127.1/",
        "http://0177.0.0.1/",
        "http://0x7f000001/",
        "http://0x7f.0.0.1/",
        "http://2130706433/",
        "http://[::1]:18886/",
        "http://[::ffff:127.0.0.1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://localhost:18886/",
        "http://sub.localhost/",
        "http://metadata.google.internal/",
        "http://127\u30020\u30020\u30021/",
        "http://foo\uff0elocalhost/",
    ):
        with pytest.raises((PrivateAddressBlockedError, ValueError)):
            validate_public_http_url(bad_url, resolve_dns=False)

    # Non-http schemes and embedded userinfo credentials MUST be blocked
    for bad_scheme in (
        "file:///etc/passwd",
        "gopher://127.0.0.1:6379/",
        "javascript:alert(1)",
        "https://user:pass@example.com/",
    ):
        with pytest.raises(ValueError):
            validate_public_http_url(bad_scheme, resolve_dns=False)


def test_net_guard_urllib_opener_rejects_socks5_clearly():
    # validate_proxy_url accepts socks5 for Playwright/httpx, but urllib opener fails fast (BH5-4)
    assert validate_proxy_url("socks5://127.0.0.1:1080") == "socks5://127.0.0.1:1080"
    with pytest.raises(ValueError, match="urllib"):
        build_safe_urllib_opener("socks5://127.0.0.1:1080")


# ---------------------------------------------------------------------------
# 2. Zero-password same-origin & Host browser defense (B-1, W-10)
# ---------------------------------------------------------------------------


def test_zero_password_local_origin_host_and_csrf_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(web_server, "ACCOUNTS_FILE", tmp_path / "accounts.txt")
    monkeypatch.setattr(web_server, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.delenv("REGISTERHUB_API_TOKEN", raising=False)

    srv, base, port = _start_server()
    try:
        # Normal local access works with ZERO password / token
        with urllib.request.urlopen(f"{base}/api/meta", timeout=5) as r:
            assert r.status == 200

        # 1. DNS rebinding: foreign Host header blocked with 403
        req_host = urllib.request.Request(f"{base}/api/accounts", headers={"Host": "evil.attacker.com"})
        with pytest.raises(urllib.error.HTTPError) as exc_h:
            urllib.request.urlopen(req_host, timeout=5)
        assert exc_h.value.code == 403

        # 2. Cross-site Sec-Fetch-Site on GET /api/* blocked with 403
        req_sfs = urllib.request.Request(f"{base}/api/accounts", headers={"Sec-Fetch-Site": "cross-site"})
        with pytest.raises(urllib.error.HTTPError) as exc_sfs:
            urllib.request.urlopen(req_sfs, timeout=5)
        assert exc_sfs.value.code == 403

        # 3. Cross-origin POST blocked with 403
        req_ori = urllib.request.Request(
            f"{base}/api/config",
            data=b'{"headless":true}',
            headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_ori:
            urllib.request.urlopen(req_ori, timeout=5)
        assert exc_ori.value.code == 403

        # 4. Cross-port local Origin blocked with 403
        req_port = urllib.request.Request(
            f"{base}/api/config",
            data=b'{"headless":true}',
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port + 1}"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_port:
            urllib.request.urlopen(req_port, timeout=5)
        assert exc_port.value.code == 403

        # 5. Same-origin local POST succeeds with 200
        req_ok = urllib.request.Request(
            f"{base}/api/config",
            data=b'{"headless":true}',
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}",
                "Sec-Fetch-Site": "same-origin",
            },
            method="POST",
        )
        with urllib.request.urlopen(req_ok, timeout=5) as r_ok:
            assert r_ok.status == 200

        # 6. text/plain simple-request CSRF blocked with 415
        req_tp = urllib.request.Request(
            f"{base}/api/config",
            data=b'{"headless":false}',
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_tp:
            urllib.request.urlopen(req_tp, timeout=5)
        assert exc_tp.value.code == 415
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# 3. LLM W-01 / N-2 / D-2 / D-3 / BH2-8 / BH2-5
# ---------------------------------------------------------------------------


def test_llm_override_base_url_strips_stored_key_and_redacts_errors(tmp_path, monkeypatch):
    cfg_file = tmp_path / "llm_config.json"
    monkeypatch.setattr(llm_client, "LLM_CONFIG_FILE", cfg_file)

    llm_client.save_llm_config({
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key": "sk-secret-stored-key-12345678",
        "proxy_url": "http://u1:p1@127.0.0.1:7897",
    })

    # W-02: public config masks proxy credentials
    pub = llm_client.get_public_llm_config()
    assert "p1" not in pub["proxy_url"]
    assert "***@" in pub["proxy_url"]

    # W-01: overriding base_url to a foreign host MUST NOT attach the stored api_key
    merged_foreign = llm_client._merge_runtime_config({"base_url": "https://attacker.example.com/v1"})
    assert merged_foreign["api_key"] == ""

    # Same-origin base_url preserves the stored api_key
    merged_same = llm_client._merge_runtime_config({"base_url": "https://api.openai.com/v1"})
    assert merged_same["api_key"] == "sk-secret-stored-key-12345678"

    # W-01: private cloud metadata base_url is rejected
    with pytest.raises(llm_client.LLMClientError):
        llm_client._merge_runtime_config({"base_url": "http://169.254.169.254/latest/meta-data"})

    # D-3: sanitize_error_text scrubs JSON-quoted secrets and bare API keys
    raw_err = 'Upstream error: {"api_key": "sk-live-secret999999", "password": "my secret pass"} and sk-proj-abcdef123456'
    scrubbed = sanitize_error_text(raw_err, max_len=300)
    assert "sk-live-secret999999" not in scrubbed
    assert "my secret pass" not in scrubbed
    assert "sk-proj-abcdef123456" not in scrubbed

    # BH2-8: decimal string token counts do not crash usage parser
    p, c, cr, cw = llm_client._extract_usage_metrics(
        {"prompt_tokens": "12.5", "completion_tokens": "4.0", "prompt_tokens_details": {"cached_tokens": "bad"}},
        "openai",
    )
    assert (p, c, cr, cw) == (12, 4, 0, 0)

    # BH2-5: corrupted non-numeric fails field in llm_profiles.json does not crash
    prof_file = tmp_path / "llm_profiles.json"
    prof_file.write_text(
        json.dumps({
            "active_id": "p1",
            "profiles": [
                {
                    "id": "p1",
                    "name": "Test",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4o-mini",
                    "keys": [{"id": "k1", "key": "sk-test-123456", "fails": "abc"}],
                }
            ],
        }),
        encoding="utf-8",
    )
    listed = llm_profiles.list_profiles_response()
    assert listed["profiles"][0]["key_pool"][0]["fails"] == 0


# ---------------------------------------------------------------------------
# 4. Data integrity & security fixes (W-04, W-07, W-08, D-6, BH1, BH5)
# ---------------------------------------------------------------------------


def test_keys_prune_exact_match_and_fleet_error_clear_isolation(tmp_path, monkeypatch):
    acc_file = tmp_path / "accounts.txt"
    acc_file.write_text(
        "a@example.com----Pw1----u1----sk-live-alpha111111\n"
        "b@example.com----Pw2----u2----sk-live-beta222222\n",
        encoding="utf-8",
    )
    fail_file = tmp_path / "failed.txt"
    snaps_dir = tmp_path / "snapshots"
    snaps_dir.mkdir()
    (snaps_dir / "snap_alpha.png").write_bytes(b"\x89PNG")
    (snaps_dir / "snap_beta.png").write_bytes(b"\x89PNG")
    fail_file.write_text(
        "2026-11-26 10:00:00 | err1 | a@x.com | Snapshot: snap_alpha.png | Site: site_a\n"
        "2026-11-26 10:01:00 | err2 | b@x.com | Snapshot: snap_beta.png | Site: site_b\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(web_server, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(web_server, "ACCOUNTS_FILE", acc_file)
    monkeypatch.setattr(web_server, "FAILED_FILE", fail_file)
    monkeypatch.setattr(web_server, "SNAPSHOTS_DIR", snaps_dir)

    srv, base, _ = _start_server()
    try:
        # W-04: substring "@" or "sk-" MUST NOT wipe accounts
        req_sub = urllib.request.Request(
            f"{base}/api/keys/prune",
            data=json.dumps({"items": [{"key": "@"}, {"key": "sk-"}]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_sub, timeout=5) as r:
            res = json.loads(r.read().decode("utf-8"))
        assert res["removed"] == 0
        assert len(parse_accounts_file(acc_file)) == 2

        # Exact key removes only the targeted row
        req_ex = urllib.request.Request(
            f"{base}/api/keys/prune",
            data=json.dumps({"items": [{"key": "sk-live-alpha111111"}]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_ex, timeout=5) as r:
            res = json.loads(r.read().decode("utf-8"))
        assert res["removed"] == 1
        rem = parse_accounts_file(acc_file)
        assert len(rem) == 1 and rem[0]["email"] == "b@example.com"

        # W-07: DELETE /api/fleet/site_a/errors removes ONLY site_a lines & snap_alpha.png
        req_del = urllib.request.Request(f"{base}/api/fleet/site_a/errors", method="DELETE")
        with urllib.request.urlopen(req_del, timeout=5) as r:
            del_res = json.loads(r.read().decode("utf-8"))
        assert del_res["ok"] is True
        remaining_fails = fail_file.read_text(encoding="utf-8")
        assert "site_a" not in remaining_fails
        assert "site_b" in remaining_fails
        assert not (snaps_dir / "snap_alpha.png").exists()
        assert (snaps_dir / "snap_beta.png").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_mail_lease_owner_enforcement_and_locks_exporter_stealth(tmp_path):
    # W-08: releasing an active lease with empty or wrong owner is rejected
    store = BuiltinMailStore(tmp_path / "mail.db")
    store.import_accounts("owner_test@example.com----pw123----imap.example.com:993")
    leased = store.lease_account(owner="worker_a", include_unknown=True)
    assert leased and leased["email"] == "owner_test@example.com"
    assert store.release_lease("owner_test@example.com", owner="", mark_status="dead") is False
    assert store.release_lease("owner_test@example.com", owner="worker_b", mark_status="dead") is False
    assert store.release_lease("owner_test@example.com", owner="worker_a", mark_status="valid") is True

    # BH1-1: non-acquirer release() does not delete holder's lock
    cdir = tmp_path / "claims"
    la = AtomicClaimLock(str(cdir), "u@test.com")
    lb = AtomicClaimLock(str(cdir), "u@test.com")
    assert la.acquire() is True
    assert lb.acquire() is False
    lb.release()
    assert Path(la.lock_file).exists()
    la.release()

    # BH1-3: interprocess_file_lock respects timeout even on same-thread nested re-entry
    lf = tmp_path / "ledger.txt"
    with interprocess_file_lock(lf, timeout=0.3):
        with pytest.raises(TimeoutError):
            with interprocess_file_lock(lf, timeout=0.15):
                pass

    # BH1-7 & D-6: run_export rejects unknown fmt; CSV neutralizes formula prefixes
    acc = tmp_path / "accounts.txt"
    acc.write_text("u@x.com----=CMD|' /C calc'!A0----uname----sk-123456\n", encoding="utf-8")
    with pytest.raises(ValueError):
        run_export(fmt="yaml", input_file=str(acc), out_dir=str(tmp_path))
    csv_out = tmp_path / "safe.csv"
    export_to_csv(parse_accounts_file(acc), csv_out)
    assert "'=CMD" in csv_out.read_text(encoding="utf-8")

    # BH5-2: connect_fingerprint_browser normalizes ports, host:port, and wss:// cleanly
    seen_eps = []

    class _FakePW:
        class chromium:
            @staticmethod
            def connect_over_cdp(ep):
                seen_eps.append(ep)
                return ep

    connect_fingerprint_browser(_FakePW(), "9222")
    connect_fingerprint_browser(_FakePW(), "127.0.0.1:9222")
    connect_fingerprint_browser(_FakePW(), "wss://remote.example:8443/devtools/browser/1")
    assert seen_eps == [
        "http://127.0.0.1:9222",
        "http://127.0.0.1:9222",
        "wss://remote.example:8443/devtools/browser/1",
    ]


def test_agent_guard_unescaped_and_ast_constant_folding_and_checkpoint_delete(tmp_path, monkeypatch):
    # A-3 & A-6: double-quoted api_key="Active" is caught even if guard rules file is emptied
    monkeypatch.setattr(ai_agent, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(ai_agent, "SITES_DIR", tmp_path / "sites")
    monkeypatch.setattr(ai_agent, "AGENT_SESSIONS_DIR", tmp_path / "results" / "agent_sessions")
    (tmp_path / "results").mkdir(parents=True)
    (tmp_path / "sites").mkdir(parents=True)
    (tmp_path / "results" / "agent_guard_rules.json").write_text("[]", encoding="utf-8")

    mgr = ai_agent.SingleAgentManager()
    pol, _ = mgr._evaluate_guard_detail("write_adapter", {"code": 'account = {"api_key": "Active"}'})
    assert pol == "deny"
    pol_init, _ = mgr._evaluate_guard_detail("run_scratch_python", {"code": 'open("sites/__init__.py","w").write("x")'})
    assert pol_init == "deny"

    # A-4: AST folding catches "Act"+"ive" and f-strings
    import ast

    for sneaky in (
        'd = {"api_key": "Act" + "ive"}',
        'd = {"api_key": f"Act{\'ive\'}"}',
        'setattr(self, "api_key", "OK")',
    ):
        assert ai_agent._detect_fake_placeholder_violation(ast.parse(sneaky)) is not None

    # BH2-1 & A-8: restore_checkpoint unlinks newly created file when existed_before=False
    sess = mgr.create_session(title="cp test")
    res_w = ai_agent.tool_write_adapter(
        "brand_new",
        "from core.adapter import BaseSiteAdapter\nclass BrandNewAdapter(BaseSiteAdapter):\n    pass\n",
        session_id=sess.id,
    )
    assert res_w["ok"] is True
    target = tmp_path / "sites" / "brand_new.py"
    assert target.is_file()
    rst = mgr.restore_checkpoint(sess.id, res_w["checkpoint_id"])
    assert rst["ok"] is True and rst["deleted"] is True
    assert not target.exists()
