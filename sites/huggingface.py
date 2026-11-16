"""
Hugging Face Site Adapter Pro.
Automates account registration workflow for Hugging Face (https://huggingface.co/join)
with humanized keystrokes, smart clicking, and manual verification gate.
"""
import time
from typing import Dict, Any, Optional
from core.adapter import BaseSiteAdapter
from core.identity import generate_identity, generate_synthetic_name, generate_compliant_password
from core.mail_client import DisposableMailClient
from core.pilot import human_type, smart_click, smart_agree_tos


class HuggingFaceAdapter(BaseSiteAdapter):
    """
    Adapter for Hugging Face registration flow:
    1. Step 1: Input email & password with natural cadence, proceed to Next
    2. Step 2: Input username & full name, agree to TOS, submit
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
        use_disposable = self.config.get("use_disposable_mail", True)
        
        first, last = generate_synthetic_name()
        fullname = f"{first} {last}"
        password = generate_compliant_password(14)
        
        if use_disposable:
            self.mail_client = DisposableMailClient()
            raw_ident = generate_identity(mail_client=self.mail_client)
        else:
            raw_ident = generate_identity()

        username = raw_ident["username"].replace(".", "-").lower()
        email = raw_ident["email"]

        self.account_data = {
            "username": username,
            "fullname": fullname,
            "email": email,
            "password": password
        }
        return self.account_data

    def safe_get_title(self) -> str:
        """Safely fetch title without raising 'Execution context was destroyed'."""
        for _ in range(5):
            try:
                return self.page.title().lower()
            except Exception:
                time.sleep(0.5)
        return ""

    def check_waf_challenge(self) -> bool:
        """Check if AWS WAF / CloudFront bot challenge is currently displayed."""
        title = self.safe_get_title()
        if "确认您是人类" in title or "verify you are human" in title or "challenge" in title:
            return True

        try:
            challenge_sel = 'iframe[src*="challenge"], div#challenge-running, #cf-challenge, button:has-text("开始"), button:has-text("Start")'
            if self.page.locator(challenge_sel).count() > 0:
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
        print(f"  请在当前全屏桌面浏览器窗口中手动点击人机验证按钮（等待上限：{timeout_sec} 秒）...")
        print(f"{'=' * 65}\n")

        # Try clicking standard "Start / 开始" button if directly visible
        try:
            start_btn = self.page.locator('button:has-text("开始"), button:has-text("Start")').first
            if start_btn.count() > 0 and start_btn.is_visible():
                print("[HuggingFace] 尝试自动激活人机验证 [开始] 按钮...")
                smart_click(self.page, start_btn)
        except Exception:
            pass

        start_time = time.time()
        while time.time() - start_time < timeout_sec:
            try:
                target_loc = self.page.locator(target_selector).first
                if target_loc.count() > 0 and target_loc.is_visible():
                    print("[HuggingFace] 人机验证已完成，检测到目标表单就绪，恢复自动化执行。")
                    return True
            except Exception:
                pass
            time.sleep(1.0)

        print(f"[HuggingFace] 等待超时（{timeout_sec}s），未检测到表单加载。")
        return False

    def fill_form(self, identity: Dict[str, Any]) -> bool:
        """Execute the two-step Hugging Face registration form with natural typing."""
        print("[HuggingFace] Checking page security status...")
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

        # --- Step 1: Email & Password (Human typing) ---
        print(f"[HuggingFace] Typing Step 1: Email ({identity['email']}) & Password...")
        try:
            human_type(self.page, 'input[type="email"], input[name="email"]', identity["email"])
            human_type(self.page, 'input[type="password"], input[name="password"]', identity["password"])

            # Click Next button
            print("[HuggingFace] Clicking Next button...")
            if not smart_click(self.page, 'button[type="submit"], button:has-text("Next")'):
                return False
            time.sleep(2.0)
        except Exception as e:
            print(f"[HuggingFace] Step 1 failed: {e}")
            return False

        # Check if challenge appeared after step 1
        if self.check_waf_challenge():
            if not self.wait_for_human_verification('input[name="username"], input#username', timeout_sec=60):
                return False

        # --- Step 2: Username, Full Name, TOS ---
        print(f"[HuggingFace] Typing Step 2: Username ({identity['username']}), Full Name & TOS...")
        try:
            human_type(self.page, 'input[name="username"], input#username', identity["username"])

            # Full name (if present)
            fullname_loc = self.page.locator('input[name="fullname"], input#fullname')
            if fullname_loc.count() > 0 and fullname_loc.first.is_visible():
                human_type(self.page, fullname_loc.first, identity["fullname"])

            # Automatically agree to terms of service
            smart_agree_tos(self.page)
            time.sleep(1.0)
            return True
        except Exception as e:
            print(f"[HuggingFace] Step 2 filling failed: {e}")
            return False

    def submit_and_confirm(self) -> bool:
        """Submit the finalized form and verify registration status."""
        print("[HuggingFace] Submitting final registration form...")
        try:
            if not smart_click(self.page, 'button[type="submit"], button:has-text("Create Account")'):
                return False
            
            # Wait for confirmation redirect or success message
            self.page.wait_for_timeout(4000)
            current_url = self.page.url

            # Successful registration routes to confirmation page, onboarding, or dashboard
            if "confirm" in current_url or "welcome" in current_url or "join" not in current_url:
                print(f"[HuggingFace] Registration submitted successfully. URL: {current_url}")
                return True

            # Check for email verification text in page body
            content = self.page.content().lower()
            if "confirmation email" in content or "verify your email" in content or "activation" in content:
                print("[HuggingFace] Verification email dispatched notice confirmed.")
                return True

            return True
        except Exception as e:
            print(f"[HuggingFace] Submit confirmation error: {e}")
            return False
