"""
Hugging Face Site Adapter.
Automates account registration workflow for Hugging Face (https://huggingface.co/join).
Supports human-in-the-loop manual verification when AWS WAF challenges are triggered.
"""
import time
from typing import Dict, Any, Optional
from core.adapter import BaseSiteAdapter
from core.identity import generate_identity, generate_synthetic_name, generate_compliant_password
from core.mail_client import DisposableMailClient


class HuggingFaceAdapter(BaseSiteAdapter):
    """
    Adapter for Hugging Face registration flow:
    1. Step 1: Input email & password, proceed to Next
    2. Step 2: Input username & full name, agree to Terms of Service, submit
    3. Step 3: Handle post-submission verification notice
    * Includes Human-in-the-loop pause for AWS WAF / CloudFront bot challenges.
    """

    def __init__(self, page, context, config=None):
        super().__init__(page, context, config)
        self.mail_client: Optional[DisposableMailClient] = None

    def target_url(self) -> str:
        return "https://huggingface.co/join"

    def prepare_identity(self) -> Dict[str, Any]:
        """Generate credentials for Hugging Face."""
        use_disposable = self.config.get("use_disposable_mail", False)
        
        first, last = generate_synthetic_name()
        fullname = f"{first} {last}"
        password = generate_compliant_password(14)
        
        # Hugging Face username constraints: alphanumeric and hyphens/underscores
        raw_ident = generate_identity()
        username = raw_ident["username"].replace(".", "-").lower()

        if use_disposable:
            self.mail_client = DisposableMailClient()
            email = self.mail_client.create_mailbox()
        else:
            email = raw_ident["email"]

        self.account_data = {
            "username": username,
            "fullname": fullname,
            "email": email,
            "password": password
        }
        return self.account_data

    def check_waf_challenge(self) -> bool:
        """Check if AWS WAF / CloudFront bot challenge is currently displayed."""
        for _ in range(3):
            try:
                title = self.page.title().lower()
                if "确认您是人类" in title or "verify you are human" in title or "challenge" in title:
                    return True
                break
            except Exception:
                time.sleep(0.5)

        try:
            if self.page.locator('iframe[src*="challenge"], div#challenge-running, #cf-challenge').count() > 0:
                return True
        except Exception:
            pass
        return False

    def wait_for_human_verification(self, target_selector: str, timeout_sec: int = 90) -> bool:
        """
        When bot challenge is detected, pause and guide the user to solve it in the visible UI.
        Once the target selector becomes visible, resumes automation.
        """
        print(f"\n{'=' * 65}")
        print("  [HuggingFace] 【需人工交互】检测到 AWS WAF / 边缘人机验证挑战！")
        print(f"  请在当前弹出的桌面浏览器窗口中手动点击完成人机验证（等待上限：{timeout_sec} 秒）...")
        print(f"{'=' * 65}\n")

        try:
            target_loc = self.page.locator(target_selector).first
            target_loc.wait_for(state="visible", timeout=timeout_sec * 1000)
            print("[HuggingFace] 人机验证已完成，检测到目标表单就绪，恢复自动化执行。")
            return True
        except Exception as e:
            print(f"[HuggingFace] 等待超时（{timeout_sec}s），未检测到表单加载：{e}")
            return False

    def fill_form(self, identity: Dict[str, Any]) -> bool:
        """Execute the two-step Hugging Face registration form with manual gate support."""
        print("[HuggingFace] Checking page security status...")
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        time.sleep(1.5)

        # Check if email input is already visible without needing manual interaction
        email_ready = False
        try:
            email_check = self.page.locator('input[type="email"], input[name="email"]').first
            if email_check.count() > 0 and email_check.is_visible():
                email_ready = True
        except Exception:
            pass

        # If not ready or WAF challenge is present, wait for human verification
        if not email_ready or self.check_waf_challenge():
            if not self.wait_for_human_verification('input[type="email"], input[name="email"]', timeout_sec=90):
                return False

        # --- Step 1: Email & Password ---
        print("[HuggingFace] Filling Step 1: Email and Password...")
        try:
            email_input = self.page.locator('input[type="email"], input[name="email"]').first
            email_input.wait_for(state="visible", timeout=15000)
            email_input.fill(identity["email"])

            pwd_input = self.page.locator('input[type="password"], input[name="password"]').first
            pwd_input.wait_for(state="visible", timeout=5000)
            pwd_input.fill(identity["password"])

            # Click Next / Submit button
            next_btn = self.page.locator('button[type="submit"], button:has-text("Next")').first
            next_btn.click()
            time.sleep(1.5)
        except Exception as e:
            print(f"[HuggingFace] Step 1 failed: {e}")
            return False

        # Check if challenge appeared after step 1
        if self.check_waf_challenge():
            if not self.wait_for_human_verification('input[name="username"], input#username', timeout_sec=60):
                return False

        # --- Step 2: Username, Full Name, TOS ---
        print("[HuggingFace] Filling Step 2: Username, Full Name & TOS...")
        try:
            username_input = self.page.locator('input[name="username"], input#username').first
            username_input.wait_for(state="visible", timeout=15000)
            username_input.fill(identity["username"])

            # Full name (if present)
            fullname_input = self.page.locator('input[name="fullname"], input#fullname')
            if fullname_input.count() > 0 and fullname_input.first.is_visible():
                fullname_input.first.fill(identity["fullname"])

            # Terms of service checkbox
            tos_checkbox = self.page.locator('input[type="checkbox"]').first
            if tos_checkbox.count() > 0 and not tos_checkbox.is_checked():
                tos_checkbox.check()

            return True
        except Exception as e:
            print(f"[HuggingFace] Step 2 filling failed: {e}")
            return False

    def submit_and_confirm(self) -> bool:
        """Submit the finalized form and verify registration status."""
        print("[HuggingFace] Submitting final registration form...")
        try:
            submit_btn = self.page.locator('button[type="submit"], button:has-text("Create Account")').first
            submit_btn.click()
            
            # Wait for confirmation redirect or success message
            self.page.wait_for_timeout(3000)
            current_url = self.page.url

            # Successful registration either routes to confirmation page, onboarding, or dashboard
            if "confirm" in current_url or "welcome" in current_url or "join" not in current_url:
                print(f"[HuggingFace] Registration submitted successfully. URL: {current_url}")
                return True

            # Check for email verification text in page body
            content = self.page.content().lower()
            if "confirmation email" in content or "verify your email" in content or "activation" in content:
                print("[HuggingFace] Verification email dispatched notice confirmed.")
                return True

            return False
        except Exception as e:
            print(f"[HuggingFace] Submit confirmation error: {e}")
            return False
