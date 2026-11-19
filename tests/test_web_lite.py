"""RegisterHub Lite Web Workbench unit tests (stdlib ThreadingHTTPServer)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from web import server as lite_server


def _start_ephemeral():
    srv = lite_server.create_server(host="127.0.0.1", port=0)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, f"http://127.0.0.1:{port}"


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _post_json(url: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def test_lite_meta_static_adapters_doctor_and_export(tmp_path, monkeypatch):
    res_dir = tmp_path / "results"
    res_dir.mkdir()
    acc = res_dir / "accounts.txt"
    acc.write_text("alice@example.com----Secret123----alice_dev\n", encoding="utf-8")

    monkeypatch.setattr(lite_server, "RESULTS_DIR", res_dir)
    monkeypatch.setattr(lite_server, "ACCOUNTS_FILE", acc)
    monkeypatch.setattr(
        lite_server,
        "collect_doctor_report",
        lambda include_network=True: {
            "checked_at": "2026-11-19 12:00:00",
            "ready": True,
            "blockers": 0,
            "warnings": 0,
            "checks": [{"name": "Python Interpreter", "state": "pass", "detail": "v3.14", "hint": ""}],
        },
    )

    srv, base = _start_ephemeral()
    try:
        # 1. Index HTML & CSS assets serve cleanly
        with urllib.request.urlopen(f"{base}/", timeout=5) as r:
            html = r.read().decode("utf-8")
            assert r.status == 200
            assert "RegisterHub" in html
            assert "Lite" in html

        with urllib.request.urlopen(f"{base}/static/app.css", timeout=5) as r:
            assert r.status == 200
            assert "--teal:" in r.read().decode("utf-8")

        # 2. Meta enforces community single-thread edition
        st, meta = _get_json(f"{base}/api/meta")
        assert st == 200
        assert meta["edition"] == "community"
        assert meta["concurrency_mode"] == "single-thread"

        # 3. Doctor + Adapters
        _, doc = _get_json(f"{base}/api/doctor")
        assert doc["ready"] is True
        _, ad = _get_json(f"{base}/api/adapters")
        assert len(ad["adapters"]) >= 1

        # 4. Accounts + CSV / Bitwarden Export
        _, accs = _get_json(f"{base}/api/accounts")
        assert accs["total"] == 1
        assert accs["accounts"][0]["email"] == "alice@example.com"

        _, exp = _post_json(f"{base}/api/export/bitwarden", {})
        assert exp["ok"] is True
        assert exp["filename"] == "bitwarden_import.csv"
        assert "alice@example.com" in exp["content"]

        # 5. Unknown adapter rejected cleanly with 400
        try:
            _post_json(f"{base}/api/run", {"adapter": "nonexistent_site_xyz", "count": 1})
            assert False, "Expected HTTP 400 for unknown adapter"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        srv.shutdown()
        srv.server_close()
