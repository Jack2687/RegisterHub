"""Release readiness tests for RegisterHub Community Edition."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from core import runner as runner_mod

ROOT = Path(__file__).resolve().parent.parent


def test_resolve_browser_engine_respects_monkeypatch_and_flags(monkeypatch):
    def _fake_pw():
        return None

    # Monkeypatched sync_playwright must always win
    monkeypatch.setattr(runner_mod, "sync_playwright", _fake_pw)
    name, fn = runner_mod.resolve_browser_engine("auto", use_chrome_channel=True)
    assert name == "playwright"
    assert fn is _fake_pw

    # Restore original reference to test explicit flags
    monkeypatch.setattr(runner_mod, "sync_playwright", runner_mod._ORIG_PLAYWRIGHT_SYNC)
    name_pw, _ = runner_mod.resolve_browser_engine("playwright", use_chrome_channel=True)
    assert name_pw == "playwright"

    # External CDP always uses standard playwright
    name_cdp, _ = runner_mod.resolve_browser_engine("auto", use_chrome_channel=True, cdp_endpoint="http://127.0.0.1:9222")
    assert name_cdp == "playwright"


def test_cli_accepts_dash_dash_doctor():
    r = subprocess.run(
        [sys.executable, "main.py", "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert r.returncode == 0
    assert "--doctor" in r.stdout
