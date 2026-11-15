"""
Standard Single-Thread Runner.
Manages browser lifecycle, logging, and account ledger exports.
"""
import os
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Type
from playwright.sync_api import sync_playwright

from core.adapter import BaseSiteAdapter


class StandardRunner:
    """Manages automation execution and ledger recording."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.headless = config.get("browser", {}).get("headless", False)
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

    def execute_adapter(self, adapter_cls: Type[BaseSiteAdapter], count: int = 1) -> int:
        success_count = 0
        self.log("info", f"Starting automation session (target count: {count}, headless: {self.headless})...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            
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
                    else:
                        self.log("warn", f"Task [{idx}/{count}] returned failure.")
                        self.save_failed("Form submission or verification returned False", adapter.account_data)
                except Exception as e:
                    self.log("error", f"Task [{idx}/{count}] encountered exception: {e}")
                    self.save_failed(str(e), adapter.account_data)
                finally:
                    page.close()

            context.close()
            browser.close()

        self.log("info", f"Session completed. Success: {success_count}/{count}")
        return success_count
