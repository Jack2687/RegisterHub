"""
Hugging Face Site Adapter.
Automates account registration workflow for Hugging Face (https://huggingface.co/join).
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
        """Check if AWS WAF / CloudFront bot challenge is triggered."""
        title = self.page.title().lower()
        if "确认您是人类" in title or "verify you are human" in title or "challenge" in title:
            return True
        if self.page.locator('iframe[src*="challenge"], div#challenge-running, #cf-challenge').count() > 0:
            return True
        return False

    def fill_form(self, identity: Dict[str, Any]) -> bool:
        """Execute the two-step Hugging Face registration form."""
        print("[HuggingFace] Checking page security status...")
        self.page.wait_for_load_state("domcontentloaded")
        time.sleep(1)

        if self.check_waf_challenge():
            print("[HuggingFace] [BLOCK] WAF/人机验证已触发。根据安全规范，请在可视化窗口中手动完成人机验证。")
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

        # Check again if challenge appeared after step 1
        if self.check_waf_challenge():
            print("[HuggingFace] [BLOCK] 人机验证在步骤 1 提交后被触发，需人工交互。")
            return False

        # --- Step 2: Username, Full Name, TOS ---
        print("[HuggingFace] Filling Step 2: Username, Full Name & TOS...")
        try:
            username_input = self.page.locator('input[name="username"], input#username').first
            username_input.wait_for(state="visible", timeout=10000)
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
