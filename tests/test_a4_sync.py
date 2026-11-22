"""Stage A4 Sync, 3-Col Account Delete, Trend Dedup & SSRF Guard Tests (Community)."""

from __future__ import annotations

import datetime
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from core.ai_agent import tool_probe_url
import web.server as comm_server


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as he:
        return he.code, json.loads(he.read().decode("utf-8"))


def test_3col_apikey_delete_atomic_clear_and_ssrf(tmp_path: Path, monkeypatch) -> None:
    acc_file = tmp_path / "accounts.txt"
    fail_file = tmp_path / "failed.txt"
    acc_file.write_text(
        "alice@example.com----sk-3col-1234567890abcd----alice_user\n"
        "bob@example.com----SecretPass!1----bob_user----sk-4col-9999999999zzzz\n",
        encoding="utf-8",
    )
    today = datetime.date.today().isoformat()
    fail_file.write_text(f"{today} 10:00:00 | timeout | u@example.com\n", encoding="utf-8")

    monkeypatch.setattr(comm_server, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(comm_server, "ACCOUNTS_FILE", acc_file)
    monkeypatch.setattr(comm_server, "FAILED_FILE", fail_file)
    monkeypatch.setattr(
        comm_server.job_manager,
        "list_jobs",
        lambda: [{"id": "j1", "adapter": "demo", "status": "failed", "count": 1, "started_at": f"{today} 10:00:01"}],
    )

    # 1) 7-day trend deduplicates 1 failed job + 1 failed.txt line into 1 (not 2)
    ov = comm_server.build_overview_payload()
    today_row = [d for d in ov["daily_trend"] if d["full_date"] == today][0]
    assert today_row["failed"] == 1

    # 2) HTTP server endpoints: 3-col delete, atomic clear, SSRF block
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), comm_server.LiteRequestHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        code, body = _post_json(
            f"{base}/api/accounts/delete",
            {"email": "alice@example.com", "api_key": "sk-3col-1234567890abcd"},
        )
        assert code == 200
        assert body["ok"] is True
        assert body["removed"] == 1
        rem_txt = acc_file.read_text(encoding="utf-8")
        assert "alice@example.com" not in rem_txt
        assert "bob@example.com" in rem_txt

        # Atomic clear creates .bak and empties accounts.txt
        c_code, c_body = _post_json(f"{base}/api/accounts/clear", {})
        assert c_code == 200 and c_body["ok"] is True
        assert acc_file.read_text(encoding="utf-8") == ""
        assert "bob@example.com" in acc_file.with_suffix(".txt.bak").read_text(encoding="utf-8")

        # SSRF blocked on test-proxy and test-key
        p_code, _ = _post_json(f"{base}/api/config/test-proxy", {"target_url": "http://169.254.169.254/latest/meta-data/"})
        assert p_code == 400
        k_code, _ = _post_json(
            f"{base}/api/accounts/test-key",
            {"key": "sk-valid-length-key", "base_url": "http://127.0.0.1:9999/v1"},
        )
        assert k_code == 400
    finally:
        httpd.shutdown()
        httpd.server_close()

    # 3) Agent tool_probe_url blocks loopback/metadata SSRF
    res = tool_probe_url("http://127.0.0.1:8080/internal")
    assert res["ok"] is False


def test_app_js_checks_removed_and_reconnects_sse() -> None:
    app_js = (Path(comm_server.__file__).resolve().parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "Number(d.removed || 0) > 0" in app_js
    assert "?since=" in app_js
    assert "agentReconnectAttempts" in app_js
