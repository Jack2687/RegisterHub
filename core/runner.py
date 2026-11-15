"""
Standard Single-Thread Runner.
Manages browser lifecycle, logging, and account ledger exports.
"""
import os
import sys
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Type, Optional
from playwright.sync_api import sync_playwright

from core.adapter import BaseSiteAdapter
from core.stealth import apply_stealth_to_context, connect_fingerprint_browser


class StandardRunner:
    """Manages automation execution and ledger recording."""

    def __init__(self, config: Dict[str, Any], cdp_endpoint: Optional[str] = None):
        self.config = config
        self.headless = config.get("browser", {}).get("headless", False)
        self.enable_stealth = config.get("browser", {}).get("enable_stealth", True)
        self.use_chrome_channel = config.get("browser", {}).get("use_chrome_channel", True)
        self.proxy_server = config.get("browser", {}).get("proxy") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
        self.cdp_endpoint = cdp_endpoint or config.get("browser", {}).get("cdp_endpoint")
        self.output_dir = Path(config.get("output_dir", "./results"))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.success_file = self.output_dir / "accounts.txt"
        self.failed_file = self.output_dir / "failed.txt"

    def log(self, level: str, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] [{level.upper()}] {msg}")

    def save_success(self, account: Dict[str, Any]):
        line = f"{account.get('email', '')}----{account.get('password', '')}----{account.get('username', '')}\n"
        with open(self.success_file, "a", encoding="utf-8") as f:
            f.write(line)
        self.log("info", f"Credential saved to {self.success_file}")

    def save_failed(self, reason: str, account: Dict[str, Any]):
        line = f"{datetime.now().isoformat()} | {reason} | {account}\n"
        with open(self.failed_file, "a", encoding="utf-8") as f:
            f.write(line)

    def _launch_browser(self, p):
        """Launch browser with robust maximization, stealth and genuine Chrome channel."""
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check"
        ]
        
        launch_kwargs = {
            "headless": self.headless,
            "args": launch_args,
            "ignore_default_args": ["--enable-automation"]
        }

        if self.proxy_server:
            self.log("info", f"Using proxy server: {self.proxy_server}")
            launch_kwargs["proxy"] = {"server": self.proxy_server}

        # Try launching official Google Chrome first if available
        if self.use_chrome_channel:
            try:
                self.log("info", "Attempting to launch official Google Chrome (channel='chrome')...")
                return p.chromium.launch(channel="chrome", **launch_kwargs)
            except Exception as e:
                self.log("warn", f"Official Google Chrome not available ({e}), falling back to bundled Chromium.")

        return p.chromium.launch(**launch_kwargs)

    def execute_adapter(self, adapter_cls: Type[BaseSiteAdapter], count: int = 1) -> int:
        success_count = 0
        mode_desc = f"external CDP ({self.cdp_endpoint})" if self.cdp_endpoint else f"desktop browser (headless={self.headless})"
        self.log("info", f"Starting automation session (target count: {count}, mode: {mode_desc}, stealth: {self.enable_stealth})...")

        with sync_playwright() as p:
            if self.cdp_endpoint:
                self.log("info", f"Connecting to external fingerprint browser at {self.cdp_endpoint}...")
                browser = connect_fingerprint_browser(p, self.cdp_endpoint)
                context = browser.contexts[0] if browser.contexts else browser.new_context(viewport={"width": 1280, "height": 800})
            else:
                browser = self._launch_browser(p)
                context = browser.new_context(
                    no_viewport=True,
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                )

            if self.enable_stealth:
                apply_stealth_to_context(context)
                self.log("info", "Anti-fingerprint masking scripts injected into context.")
            
            for idx in range(1, count + 1):
                self.log("info", f"--- Processing task [{idx}/{count}] ---")
                page = context.new_page()
                adapter = adapter_cls(page, context, self.config)
                
                try:
                    ok = adapter.run()
                    if ok:
                        self.log("success", f"Task [{idx}/{count}] finished successfully!")
                        self.save_success(adapter.account_data)
                        success_count += 1
                        if not self.headless:
                            time.sleep(2)
                    else:
                        self.log("warn", f"Task [{idx}/{count}] returned failure.")
                        self.save_failed("Form submission or verification returned False", adapter.account_data)
                        if not self.headless:
                            self.log("info", "Keeping window visible for 5 seconds for observation...")
                            time.sleep(5)
                except Exception as e:
                    self.log("error", f"Task [{idx}/{count}] encountered exception: {e}")
                    self.save_failed(str(e), adapter.account_data)
                    if not self.headless:
                        self.log("info", "Task aborted. Keeping browser open for 6 seconds for review...")
                        time.sleep(6)
                finally:
                    page.close()

            context.close()
            browser.close()

        self.log("info", f"Session completed. Success: {success_count}/{count}")
        return success_count
