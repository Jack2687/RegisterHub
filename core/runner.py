"""
Standard Single-Thread Runner.
Manages browser lifecycle, logging, failure snapshot capture, and account ledger exports.
"""
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Type, Optional, Tuple
from playwright.sync_api import sync_playwright, Page
from core.locks import atomic_write_text_safe, interprocess_file_lock

_ORIG_PLAYWRIGHT_SYNC = sync_playwright

try:
    from patchright.sync_api import sync_playwright as _patchright_sync_playwright
    HAS_PATCHRIGHT = True
except ImportError:
    _patchright_sync_playwright = None
    HAS_PATCHRIGHT = False


def resolve_browser_engine(stealth_backend: str = "auto", use_chrome_channel: bool = True, cdp_endpoint: Optional[str] = None):
    """Resolve sync_playwright factory, preferring Patchright (zero Runtime.enable) when installed."""
    # Respect unit-test monkeypatches on core.runner.sync_playwright
    if sync_playwright is not _ORIG_PLAYWRIGHT_SYNC:
        return "playwright", sync_playwright
    eff = (os.environ.get("REGISTERHUB_BROWSER_BACKEND") or stealth_backend or "auto").strip().lower()
    if eff == "playwright" or cdp_endpoint:
        return "playwright", sync_playwright
    if eff in ("auto", "patchright") and HAS_PATCHRIGHT and _patchright_sync_playwright is not None:
        if eff == "patchright" or use_chrome_channel:
            return "patchright", _patchright_sync_playwright
    return "playwright", sync_playwright

from core.adapter import BaseSiteAdapter
from core.stealth import apply_stealth_to_context, connect_fingerprint_browser


# ANSI Color formatting
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_RED = "\033[91m"
C_CYAN = "\033[96m"
C_RESET = "\033[0m"


def _resolve_adapter_site_name(adapter_cls: Type[BaseSiteAdapter]) -> str:
    """Resolve canonical site adapter slug for ledger attribution."""
    explicit = str(getattr(adapter_cls, "site_name", "") or "").strip().lower()
    if explicit:
        return explicit.replace("-", "_")
    mod = str(getattr(adapter_cls, "__module__", "") or "").strip()
    if mod:
        stem = mod.rsplit(".", 1)[-1].strip().lower()
        if stem and stem not in ("__main__", "adapter", "runner"):
            return stem.replace("-", "_")
    return str(getattr(adapter_cls, "__name__", "") or "unknown").strip().lower()


class StandardRunner:
    """Manages automation execution, failure snapshots, and ledger recording."""

    def __init__(self, config: Dict[str, Any], cdp_endpoint: Optional[str] = None):
        self.config = config
        browser_cfg = config.get("browser", {}) or {}
        env_headless = os.environ.get("RH_OVERRIDE_HEADLESS", "").strip().lower()
        if env_headless in ("1", "true", "yes"):
            self.headless = True
        elif env_headless in ("0", "false", "no"):
            self.headless = False
        else:
            self.headless = browser_cfg.get("headless", False)
        env_email = os.environ.get("RH_OVERRIDE_EMAIL", "").strip()
        if env_email:
            self.config = dict(self.config)
            self.config["email"] = env_email
        self.slow_mo = max(0, int(browser_cfg.get("slow_mo", 0) or 0))
        self.timeout_ms = max(5000, int(browser_cfg.get("timeout", 30000) or 30000))
        self.enable_stealth = browser_cfg.get("enable_stealth", True)
        self.use_chrome_channel = browser_cfg.get("use_chrome_channel", True)
        self.stealth_backend = str(browser_cfg.get("stealth_backend", "auto") or "auto")
        self.proxy_mode = str(
            browser_cfg.get("proxy_mode") or ("proxy" if browser_cfg.get("proxy") else "direct")
        ).strip().lower()
        self.proxy_server = browser_cfg.get("proxy") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
        self.cdp_endpoint = cdp_endpoint or browser_cfg.get("cdp_endpoint")
        self.output_dir = Path(config.get("output_dir", "./results"))
        self.snapshot_dir = self.output_dir / "snapshots"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.success_file = self.output_dir / "accounts.txt"
        self.meta_file = self.output_dir / "accounts_meta.json"
        self.failed_file = self.output_dir / "failed.txt"

    def log(self, level: str, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        lvl = level.lower()
        if lvl == "success":
            tag = f"{C_GREEN}[SUCCESS]{C_RESET}"
        elif lvl == "warn":
            tag = f"{C_YELLOW}[WARN]{C_RESET}"
        elif lvl == "error":
            tag = f"{C_RED}[ERROR]{C_RESET}"
        else:
            tag = f"{C_CYAN}[INFO]{C_RESET}"
        print(f"[{timestamp}] {tag} {msg}")

    def capture_snapshot(self, page: Page, name_prefix: str = "failure") -> Optional[Path]:
        """Capture high-resolution screenshot for blackbox post-mortem review."""
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            shot_path = self.snapshot_dir / f"{name_prefix}_{ts}.png"
            page.screenshot(path=str(shot_path), full_page=False)
            self.log("info", f"Visual snapshot preserved: {shot_path.resolve()}")
            return shot_path
        except Exception as e:
            self.log("warn", f"Could not capture snapshot: {e}")
            return None

    def _record_account_meta(self, email: str, api_key: str, site_name: str) -> None:
        clean_site = str(site_name or "").strip().lower()
        clean_email = str(email or "").strip().lower()
        if not clean_site or not clean_email:
            return
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with interprocess_file_lock(self.meta_file):
            data: Dict[str, Any] = {"by_key": {}, "by_email": {}}
            if self.meta_file.exists():
                try:
                    raw = json.loads(self.meta_file.read_text(encoding="utf-8"))
                    if not isinstance(raw, dict):
                        self.log("error", f"Corrupt accounts_meta.json (non-dict), skipping overwrite: {self.meta_file}")
                        return
                    data["by_key"] = dict(raw.get("by_key") or {}) if isinstance(raw.get("by_key"), dict) else {}
                    data["by_email"] = dict(raw.get("by_email") or {}) if isinstance(raw.get("by_email"), dict) else {}
                except Exception as exc:
                    self.log("error", f"Corrupt accounts_meta.json, refusing to overwrite ({exc})")
                    return
            entry = {"site": clean_site, "created_at": now_ts}
            data["by_email"][clean_email] = entry
            if api_key:
                data["by_key"][f"{clean_email}|{api_key}"] = entry
            atomic_write_text_safe(self.meta_file, json.dumps(data, ensure_ascii=False, indent=2))

    def save_success(self, account: Dict[str, Any], site_name: str = ""):
        api_key = str(account.get("api_key", "") or "").strip()
        password = str(account.get("password", "") or "")
        extra = f"----{api_key}" if api_key and api_key != password else ""
        email = str(account.get("email", "") or "")
        line = f"{email}----{password}----{account.get('username', '')}{extra}\n"
        with interprocess_file_lock(self.success_file):
            with open(self.success_file, "a", encoding="utf-8") as f:
                f.write(line)
        eff_site = str(site_name or account.get("site") or "").strip().lower()
        if eff_site:
            self._record_account_meta(email, api_key if extra else "", eff_site)
        self.log("success", f"Credential saved to {self.success_file}")

    def save_failed(
        self,
        reason: str,
        account: Dict[str, Any],
        snapshot_path: Optional[Path] = None,
        site_name: str = "",
    ):
        snap_info = f" | Snapshot: {snapshot_path.name}" if snapshot_path else ""
        eff_site = str(site_name or account.get("site") or "").strip().lower()
        site_info = f" | Site: {eff_site}" if eff_site else ""
        line = f"{datetime.now().isoformat()} | {reason} | {account}{snap_info}{site_info}\n"
        with interprocess_file_lock(self.failed_file):
            with open(self.failed_file, "a", encoding="utf-8") as f:
                f.write(line)

    def _launch_browser(self, p):
        """Launch browser with calibrated geometry (if available), stealth and genuine Chrome channel."""
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check"
        ]
        self._calibrated_zoom = None
        if not self.headless:
            try:
                from core.calibrator import DEFAULT_GEOMETRY_PATH, get_window_layout
                if DEFAULT_GEOMETRY_PATH.exists():
                    lay = get_window_layout(0, 1)
                    launch_args.extend([
                        f"--window-position={lay['x']},{lay['y']}",
                        f"--window-size={lay['width']},{lay['height']}",
                    ])
                    self._calibrated_zoom = float(lay.get("zoom", 0.85))
                else:
                    launch_args.append("--start-maximized")
            except Exception:
                launch_args.append("--start-maximized")
        else:
            launch_args.append("--start-maximized")
        
        launch_kwargs = {
            "headless": self.headless,
            "slow_mo": self.slow_mo,
            "args": launch_args,
            "ignore_default_args": ["--enable-automation"]
        }

        pw_proxy, proxy_label = self._resolve_launch_proxy()
        if pw_proxy:
            self.log("info", f"Using proxy server: {proxy_label}")
            launch_kwargs["proxy"] = pw_proxy

        # Try launching official Google Chrome first if available
        if self.use_chrome_channel:
            try:
                self.log("info", "Attempting to launch official Google Chrome (channel='chrome')...")
                return p.chromium.launch(channel="chrome", **launch_kwargs)
            except Exception as e:
                self.log("warn", f"Official Google Chrome not available ({e}), falling back to bundled Chromium.")

        return p.chromium.launch(**launch_kwargs)

    def _resolve_launch_proxy(self) -> Tuple[Optional[Dict[str, str]], str]:
        """Resolve Playwright proxy dict from built-in proxy pool or config (credentials masked in logs)."""
        from core.secrets import mask_proxy_url, proxy_url_to_playwright_dict

        if self.proxy_mode == "builtin":
            db_path = self.output_dir / "builtin_proxies.db"
            if db_path.is_file():
                try:
                    from core.proxy_builtin import get_builtin_proxy_store

                    store = get_builtin_proxy_store(db_path)
                    entries = store.list_runtime_entries(browser_only=True, auto_replenish=True)
                    if entries:
                        picked = entries[0]
                        if picked.get("kind") == "residential_gateway":
                            fresh = store.materialize_gateway_by_id(int(picked["id"]))
                            if fresh:
                                picked = fresh
                        pw_dict = picked.get("playwright_proxy")
                        if isinstance(pw_dict, dict) and pw_dict.get("server"):
                            label = f"builtin:{picked.get('label') or picked.get('id')} ({picked.get('masked_url') or pw_dict['server']})"
                            return pw_dict, label
                except Exception as exc:
                    self.log("warn", f"Built-in proxy pool resolution failed ({exc}); falling back to direct/config.")
            self.log("warn", "Built-in proxy pool has no enabled browser-compatible proxy; falling back.")

        if self.proxy_server and self.proxy_mode != "direct":
            try:
                return proxy_url_to_playwright_dict(str(self.proxy_server)), mask_proxy_url(str(self.proxy_server))
            except Exception:
                return {"server": str(self.proxy_server)}, mask_proxy_url(str(self.proxy_server))

        if self.proxy_server and self.proxy_mode == "direct":
            # Respect explicit HTTP_PROXY / HTTPS_PROXY env or legacy config when proxy_server is set
            try:
                return proxy_url_to_playwright_dict(str(self.proxy_server)), mask_proxy_url(str(self.proxy_server))
            except Exception:
                return {"server": str(self.proxy_server)}, mask_proxy_url(str(self.proxy_server))
        return None, ""

    def execute_adapter(self, adapter_cls: Type[BaseSiteAdapter], count: int = 1) -> int:
        success_count = 0
        site_name = _resolve_adapter_site_name(adapter_cls)
        mode_desc = f"external CDP ({self.cdp_endpoint})" if self.cdp_endpoint else f"desktop browser (headless={self.headless})"
        engine_name, pw_factory = resolve_browser_engine(
            stealth_backend=self.stealth_backend,
            use_chrome_channel=self.use_chrome_channel,
            cdp_endpoint=self.cdp_endpoint,
        )
        self.log("info", f"Starting automation session (target count: {count}, mode: {mode_desc}, engine: {engine_name}, stealth: {self.enable_stealth})...")

        with pw_factory() as p:
            if self.cdp_endpoint:
                self.log("info", f"Connecting to external fingerprint browser at {self.cdp_endpoint}...")
                browser = connect_fingerprint_browser(p, self.cdp_endpoint)
                context = browser.contexts[0] if browser.contexts else browser.new_context(viewport={"width": 1280, "height": 800})
            else:
                browser = self._launch_browser(p)
                context = browser.new_context(
                    no_viewport=True
                )

            if self.enable_stealth:
                apply_stealth_to_context(context)
                self.log("info", "Anti-fingerprint masking scripts injected into context.")

            if getattr(self, "_calibrated_zoom", None) and not self.headless:
                try:
                    from core.calibrator import build_zoom_init_script
                    context.add_init_script(build_zoom_init_script(self._calibrated_zoom))
                except Exception:
                    pass
            
            for idx in range(1, count + 1):
                self.log("info", f"=== Processing task [{idx}/{count}] ===")
                page = context.new_page()
                page.set_default_timeout(self.timeout_ms)
                adapter = adapter_cls(page, context, self.config)
                task_start = time.time()
                
                try:
                    ok = adapter.run()
                    elapsed = round(time.time() - task_start, 2)
                    if ok:
                        self.log("success", f"Task [{idx}/{count}] finished successfully in {elapsed}s!")
                        self.save_success(adapter.account_data, site_name=site_name)
                        success_count += 1
                        if not self.headless:
                            time.sleep(2)
                    else:
                        snap = self.capture_snapshot(page, f"task_{idx}_failed")
                        self.log("warn", f"Task [{idx}/{count}] failed after {elapsed}s.")
                        self.save_failed("Form submission returned False", adapter.account_data, snap, site_name=site_name)
                        if not self.headless:
                            self.log("info", "Keeping window visible for 5 seconds for review...")
                            time.sleep(5)
                except Exception as e:
                    elapsed = round(time.time() - task_start, 2)
                    snap = self.capture_snapshot(page, f"task_{idx}_exception")
                    self.log("error", f"Task [{idx}/{count}] encountered exception after {elapsed}s: {e}")
                    self.save_failed(str(e), adapter.account_data, snap, site_name=site_name)
                    if not self.headless:
                        self.log("info", "Keeping window visible for 6 seconds before closing...")
                        time.sleep(6)
                finally:
                    mc = getattr(adapter, "mail_client", None)
                    if mc is not None and callable(getattr(mc, "release", None)):
                        try:
                            mc.release()
                        except Exception:
                            pass
                    page.close()

            if not self.cdp_endpoint:
                context.close()
            browser.close()

        self.log("info", f"Session completed. Success: {success_count}/{count}")
        return success_count
