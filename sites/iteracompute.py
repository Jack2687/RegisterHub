"""
IteraCompute Site Adapter Pro.
Automates account registration workflow for IteraCompute (https://platform.iteracompute.com/sign-up)
with humanized keystrokes, cookie banner dismissal, terms agreement, and Cloudflare Turnstile human-in-the-loop gate.
"""
import time
from typing import Dict, Any, Optional
from core.adapter import BaseSiteAdapter
from core.identity import generate_identity, generate_compliant_password
from core.pilot import human_type, smart_click, smart_agree_tos
from core.mail_client import DisposableMailClient


class IteracomputeAdapter(BaseSiteAdapter):
    """
    Registration flow for IteraCompute:
    1. Dismiss privacy / source preferences banner
    2. Input email and compliant password with natural typing jitter
    3. Check Service Agreement & Privacy Policy checkbox
    4. Cloudflare Turnstile human-in-the-loop verification gate
    5. Submit registration and confirm registration receipt
    """

    def __init__(self, page, context, config=None):
        super().__init__(page, context, config)
        self.mail_client: Optional[DisposableMailClient] = None

    def target_url(self) -> str:
        return "https://platform.iteracompute.com/sign-up"

    def prepare_identity(self) -> Dict[str, Any]:
        """Generate high-entropy synthetic credentials matching IteraCompute password policy."""
        use_disposable = self.config.get("use_disposable_mail", False)
        ident = generate_identity(locale="US")
        ident["password"] = generate_compliant_password(14)

        if use_disposable:
            self.mail_client = DisposableMailClient()
            ident["email"] = self.mail_client.create_mailbox()

        self.account_data = ident
        return self.account_data

    def dismiss_cookie_banner(self):
        """Dismiss floating privacy preferences overlay if present."""
        try:
            # First attempt: click Decline button if visible
            decline_btn = self.page.locator('button:has-text("Decline")').first
            if decline_btn.count() > 0 and decline_btn.is_visible():
                smart_click(self.page, decline_btn)
                time.sleep(0.5)
        except Exception:
            pass

        # Ensure DOM overlay cleanup
        try:
            self.page.evaluate(
                "() => document.querySelectorAll('aside, #itera-source-consent, .modal-backdrop').forEach(e => e.remove())"
            )
        except Exception:
            pass

    def check_turnstile_challenge(self) -> bool:
        """Detect whether Cloudflare Turnstile iframe is present on the page."""
        try:
            for _ in range(6):
                for frame in self.page.frames:
                    if "challenges.cloudflare.com" in frame.url or "turnstile" in frame.url:
                        return True
                if self.page.locator("iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile'], [class*='cf-turnstile']").count() > 0:
                    return True
                time.sleep(0.5)
        except Exception:
            pass
        return False

    def wait_for_turnstile_verification(self, timeout_sec: int = 90) -> bool:
        """
        When Cloudflare Turnstile is detected, pause and guide the user to solve it in the visible UI.
        Once the submit button becomes enabled, automation automatically resumes.
        """
        print(f"\n{'=' * 65}")
        print("  [IteraCompute] 【需人工交互】检测到 Cloudflare Turnstile 人机验证！")
        print("  请在当前全屏桌面浏览器窗口中点击【Verify you are human / 请验证您是真人】复选框...")
        print(f"  等待状态就绪上限：{timeout_sec} 秒")
        print(f"{'=' * 65}\n")

        start_time = time.time()
        while time.time() - start_time < timeout_sec:
            try:
                self.dismiss_cookie_banner()
                submit_btn = self.page.locator("button[type='submit']").first
                if submit_btn.count() > 0 and submit_btn.is_enabled():
                    print("[IteraCompute] Turnstile 人机验证已通过（提交按钮已激活），恢复自动化执行。")
                    return True
            except Exception:
                pass
            time.sleep(1.0)

        print(f"[IteraCompute] 等待超时（{timeout_sec}s），提交按钮仍处于 disabled 状态。")
        return False

    def fill_form(self, identity: Dict[str, Any]) -> bool:
        """Fill registration form fields with humanized keystrokes and handle terms/Turnstile."""
        print(f"[*] [iteracompute] Starting form filling for {identity['email']}...")
        try:
            # 1. Dismiss source/privacy banner
            self.dismiss_cookie_banner()
            time.sleep(0.5)

            # 2. Fill email
            email_selector = "input#email, input[type='email']"
            if self.page.locator(email_selector).count() > 0:
                print(f"    -> Typing email into {email_selector}...")
                human_type(self.page, email_selector, identity["email"])

            # 3. Fill compliant password
            password_selector = "input#password, input[type='password']"
            if self.page.locator(password_selector).count() > 0:
                print(f"    -> Typing password into {password_selector}...")
                human_type(self.page, password_selector, identity["password"])

            # 4. Check terms checkbox
            terms_selector = "input#accepted_terms, input[type='checkbox']"
            terms_cb = self.page.locator(terms_selector).first
            if terms_cb.count() > 0:
                if not terms_cb.is_checked():
                    print("    -> Checking Service Agreement checkbox...")
                    terms_cb.check()
            else:
                smart_agree_tos(self.page)

            time.sleep(0.5)

            # 5. Handle Cloudflare Turnstile if present
            if self.check_turnstile_challenge():
                submit_btn = self.page.locator("button[type='submit']").first
                if submit_btn.count() > 0 and not submit_btn.is_enabled():
                    # If submit button is not yet enabled, wait for human verification in visible UI
                    if not self.wait_for_turnstile_verification(timeout_sec=60):
                        return False

            return True
        except Exception as e:
            print(f"[-] [iteracompute] Form filling error: {e}")
            return False

    def submit_and_confirm(self) -> bool:
        """Locate submit button, trigger submission and confirm success/acceptance state."""
        print("[*] [iteracompute] Submitting registration form...")
        try:
            submit_btn = self.page.locator("button[type='submit']").first
            if not submit_btn.is_enabled():
                print("[!] Submit button is currently disabled. Waiting for Turnstile clearance...")
                if not self.wait_for_turnstile_verification(timeout_sec=30):
                    return False

            if not smart_click(self.page, submit_btn):
                return False

            print("[*] Form submitted. Waiting for server confirmation...")
            self.page.wait_for_timeout(3000)

            # Check for error messages
            err_loc = self.page.locator(
                "div[class*='error'], p[class*='error'], :text('already exists'), :text('is not available')"
            )
            if err_loc.count() > 0 and err_loc.first.is_visible():
                err_text = err_loc.first.inner_text().strip()
                print(f"[-] [iteracompute] Registration rejected by server: {err_text}")
                return False

            current_url = self.page.url

            # Assert success criteria: Registration received / Verification requested / Dashboard
            success_indicators = self.page.locator(
                ":text('Registration received'), :text('Verification requested'), :text('Check your email')"
            )
            if success_indicators.count() > 0:
                print(f"[+] [iteracompute] Registration accepted by server: '{success_indicators.first.inner_text().strip()}'")
                return True

            if "dashboard" in current_url or "welcome" in current_url:
                print(f"[+] [iteracompute] Direct dashboard access confirmed! URL: {current_url}")
                return True

            print(f"[+] [iteracompute] Submitted without fatal errors. Current URL: {current_url}")
            return True
        except Exception as e:
            print(f"[-] [iteracompute] Submit confirmation failed: {e}")
            return False
