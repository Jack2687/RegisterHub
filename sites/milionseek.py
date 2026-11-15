"""
MilionseekAdapter Site Adapter Pro.
Automates account registration workflow for 100milionseek (https://100milionseek.top/signup)
with genuine Chrome consistency, natural email input, modal dismissal, Cloudflare Turnstile clearance,
and instantaneous API key extraction.
"""
import time
import random
import string
from pathlib import Path
from typing import Dict, Any, Optional
from core.adapter import BaseSiteAdapter
from core.identity import generate_identity
from core.pilot import human_type


class MilionseekAdapter(BaseSiteAdapter):
    """
    Registration flow for 100milionseek:
    1. Navigate to signup page with referral code (?ref=3a4iwe7j)
    2. Dismiss any floating outage / announcement modals
    3. Input valid email with humanized typing rhythms
    4. Trigger & resolve Cloudflare Turnstile
    5. Submit registration form ('Create my key')
    6. Intercept and extract generated OpenAI-compatible API key from #done container
    """

    def __init__(self, page, context, config=None):
        super().__init__(page, context, config)
        self.ref_code = self.config.get("ref", "3a4iwe7j")

    def target_url(self) -> str:
        base = "https://100milionseek.top/signup"
        return f"{base}?ref={self.ref_code}" if self.ref_code else base

    def prepare_identity(self) -> Dict[str, Any]:
        """
        Generate credentials matching 100milionseek requirements.
        Allows custom email override via config or generates an address on an accepted domain.
        """
        custom_email = self.config.get("email")
        if custom_email:
            ident = generate_identity()
            ident["email"] = custom_email
        else:
            # 100milionseek performs MX domain validation and blocks typical disposable domains.
            accepted_domains = ["outlook.com", "gmail.com", "proton.me", "icloud.com", "yahoo.com", "hotmail.com"]
            chosen_domain = random.choice(accepted_domains)
            ident = generate_identity(email_domain=chosen_domain, locale="US")
            
            # Append 4 random digits to ensure zero collision across batch runs
            rnd_suffix = "".join(random.choices(string.digits, k=4))
            user_part = ident["email"].split("@")[0]
            ident["email"] = f"{user_part}_{rnd_suffix}@{chosen_domain}"

        self.account_data = ident
        return self.account_data

    def dismiss_modals(self):
        """Dismiss floating outage, announcement, or consent dialogs that intercept clicks."""
        try:
            # 1. Click explicit close buttons if visible
            for sel in ['[data-outage-close]', '[data-announce-close]', 'button:has-text("Got it")']:
                close_btn = self.page.locator(sel).first
                if close_btn.count() > 0 and close_btn.is_visible():
                    close_btn.click()
                    time.sleep(0.3)
        except Exception:
            pass

        # 2. Hard removal of overlay and restoration of body scroll
        try:
            self.page.evaluate("""() => {
                document.querySelectorAll('[data-outage], #outage, .outage, [data-announce]').forEach(el => el.remove());
                document.body.classList.remove('noscroll');
                try {
                    localStorage.setItem('hm_outage_20261115', '1');
                } catch(e) {}
            }""")
        except Exception:
            pass

    def wait_for_turnstile(self, timeout_sec: int = 60) -> bool:
        """
        Wait for Cloudflare Turnstile token clearance.
        In genuine stealth Chrome, Turnstile typically passes automatically in 5~10s.
        If flagged, falls back gracefully to human-in-the-loop prompt.
        """
        print("[*] [100milionseek] Waiting for Cloudflare Turnstile token clearance...")
        start_time = time.time()
        prompted_user = False

        while time.time() - start_time < timeout_sec:
            try:
                self.dismiss_modals()
                tok = self.page.evaluate(
                    "() => window.tsToken || document.querySelector('[name=cf-turnstile-response]')?.value || ''"
                )
                btn_disabled = self.page.evaluate(
                    "() => document.querySelector('#signup button[type=submit]')?.getAttribute('aria-disabled')"
                )
                if tok and btn_disabled != "true":
                    print(f"[+] [100milionseek] Turnstile token acquired (length: {len(tok)}).")
                    return True

                if not prompted_user and time.time() - start_time > 12:
                    prompted_user = True
                    print(f"\n{'=' * 65}")
                    print("  [100milionseek] 【需人机协同】Turnstile 正在验证中...")
                    print("  若桌面浏览器窗口弹出人机复选框，请在页面中轻点一下完成验证。")
                    print(f"{'=' * 65}\n")

            except Exception:
                pass

            time.sleep(1.0)

        print(f"[-] [100milionseek] Turnstile clearance timed out after {timeout_sec}s.")
        return False

    def fill_form(self, identity: Dict[str, Any]) -> bool:
        """Fill registration form fields and wait for client-side checks."""
        email = identity.get("email")
        print(f"[*] [100milionseek] Starting registration flow with email: {email}...")

        try:
            # 1. Dismiss any blocking modals
            self.dismiss_modals()
            time.sleep(0.5)

            # 2. Fill email into input[name="email"]
            email_selector = '#signup input[name="email"]'
            self.page.wait_for_selector(email_selector, state="visible", timeout=15000)
            human_type(self.page, email_selector, email)

            # Trigger blur event to ensure client-side email domain validation fires
            self.page.locator(email_selector).evaluate("e => e.blur()")
            time.sleep(1.0)

            # 3. Check for immediate frontend domain rejection (e.g. disposable_email)
            err_msg_loc = self.page.locator('#signup [data-emailmsg]')
            if err_msg_loc.count() > 0 and err_msg_loc.is_visible():
                err_text = err_msg_loc.inner_text().strip()
                if err_text:
                    print(f"[-] [100milionseek] Email domain rejected: {err_text}")
                    return False

            # 4. Resolve Cloudflare Turnstile challenge
            if not self.wait_for_turnstile(timeout_sec=45):
                return False

            return True

        except Exception as e:
            print(f"[-] [100milionseek] Form filling error: {e}")
            return False

    def submit_and_confirm(self) -> bool:
        """Submit the signup form and extract the generated API key."""
        print("[*] [100milionseek] Submitting registration form...")
        try:
            self.dismiss_modals()
            submit_btn = '#signup button[type="submit"]'
            
            btn_loc = self.page.locator(submit_btn)
            if not btn_loc.is_visible():
                print("[-] [100milionseek] Submit button not found or not visible.")
                return False

            btn_loc.click()
            print("[*] [100milionseek] 'Create my key' clicked. Waiting for API response & key generation...")

            # Wait for response & key generation
            for _ in range(30):
                # Check for server error alerts
                err_loc = self.page.locator('#signup [data-err]')
                if err_loc.count() > 0 and err_loc.is_visible():
                    err_txt = err_loc.inner_text().strip()
                    if err_txt:
                        print(f"[-] [100milionseek] Registration rejected by server: {err_txt}")
                        return False

                # Check if success block #done is displayed
                done_loc = self.page.locator('#done')
                if done_loc.count() > 0 and done_loc.is_visible():
                    key_loc = self.page.locator('#done [data-p="key"]')
                    if key_loc.count() > 0:
                        api_key = key_loc.inner_text().strip()
                        if api_key:
                            print(f"\n{'=' * 65}")
                            print(f"  [+] [100milionseek] 账号创建成功！API Key 提取成功！")
                            print(f"  Email   : {self.account_data.get('email')}")
                            print(f"  API Key : {api_key}")
                            print(f"  Base URL: https://100milionseek.top/v1")
                            print(f"  Model   : deepseek-v4.1-flash-free (100M tokens/day)")
                            print(f"{'=' * 65}\n")

                            self.account_data["api_key"] = api_key
                            # Standardize password field as api_key for accounts.txt export
                            self.account_data["password"] = api_key

                            # Also save clean API key list to keys.txt for convenient copying
                            try:
                                out_dir = Path(self.config.get("output_dir", "./results"))
                                out_dir.mkdir(parents=True, exist_ok=True)
                                keys_file = out_dir / "keys.txt"
                                with open(keys_file, "a", encoding="utf-8") as kf:
                                    kf.write(f"{api_key}\n")
                            except Exception:
                                pass

                            return True

                time.sleep(0.5)

            print("[-] [100milionseek] Timed out waiting for #done key card to appear.")
            return False

        except Exception as e:
            print(f"[-] [100milionseek] Submit confirmation exception: {e}")
            return False
