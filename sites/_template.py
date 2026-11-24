"""
Site Adapter Development Template.
Copy or scaffold this file to create a new target site automation adapter.
"""
import time
from typing import Dict, Any, Optional
from core.adapter import BaseSiteAdapter
from core.identity import generate_identity
from core.pilot import human_type, smart_click, smart_agree_tos
from core.mail_client import DisposableMailClient
from core.imap_mail import SingleImapMailClient


class SiteTemplateAdapter(BaseSiteAdapter):
    """
    Standard Production-Grade Registration Flow Template.
    Covers multi-step inputs, humanized typing, terms agreement, and email verification.
    """

    def __init__(self, page, context, config=None):
        super().__init__(page, context, config)
        self.mail_client: Optional[Any] = None

    def target_url(self) -> str:
        """
        [TODO]: Replace with target registration / signup page URL.
        """
        return "https://example.com/signup"

    def prepare_identity(self) -> Dict[str, Any]:
        """
        [TODO]: Customize synthetic identity generation.
        Supports locale switching: generate_identity(locale='US'|'UK'|'DE')
        Automatically switches between DisposableMailClient and SingleImapMailClient via config.json.
        """
        defaults = self.config.get("defaults") or {}
        mail_cfg = self.config.get("mail") or {}
        use_disposable = defaults.get("use_disposable_mail", self.config.get("use_disposable_mail", True))
        locale = defaults.get("country", "US")
        pwd_len = int(defaults.get("password_length", 14) or 14)

        # 1. Hook up public disposable mailbox or single-account IMAP based on config.json
        if use_disposable:
            self.mail_client = DisposableMailClient()
            ident = generate_identity(locale=locale, mail_client=self.mail_client, password_length=pwd_len)
        else:
            ident = generate_identity(locale=locale, password_length=pwd_len)
            imap_server = str(mail_cfg.get("imap_server") or "").strip()
            imap_user = str(mail_cfg.get("imap_user") or "").strip()
            imap_password = str(mail_cfg.get("imap_password") or "")
            if imap_server and imap_user and imap_password:
                self.mail_client = SingleImapMailClient(
                    server=imap_server,
                    port=int(mail_cfg.get("imap_port") or 993),
                    user=imap_user,
                    password=imap_password,
                )
                ident["email"] = imap_user
            elif self.config.get("email"):
                ident["email"] = str(self.config["email"]).strip()

        self.account_data = ident
        return self.account_data

    def fill_form(self, identity: Dict[str, Any]) -> bool:
        """
        [TODO]: Customize form field selectors and filling steps.
        Use human_type() for realistic typing cadence and smart_click() for robust clicks.
        """
        print(f"[*] Starting form filling for {identity['email']}...")

        try:
            # --- Step 1: Core Credentials ---
            email_sel = 'input[type="email"], input[name="email"]'
            password_sel = 'input[type="password"], input[name="password"]'
            username_sel = 'input[name="username"], input[name="user"]'

            if self.page.locator(email_sel).count() > 0:
                print("    -> Entering email...")
                human_type(self.page, email_sel, identity["email"])

            if self.page.locator(password_sel).count() > 0:
                print("    -> Entering password...")
                human_type(self.page, password_sel, identity["password"])

            if self.page.locator(username_sel).count() > 0:
                print("    -> Entering username...")
                human_type(self.page, username_sel, identity["username"])

            # --- Step 2: Terms of Service Checkbox ---
            smart_agree_tos(self.page)
            time.sleep(1.0)

            # --- Step 3: Optional Next Step / Wizard Button ---
            next_btn = self.page.locator('button:has-text("Next"), button:has-text("下一步")')
            if next_btn.count() > 0 and next_btn.first.is_visible():
                print("    -> Clicking wizard Next button...")
                smart_click(self.page, next_btn.first)
                time.sleep(2.0)

            return True

        except Exception as e:
            print(f"[-] Form filling encountered error: {e}")
            return False

    def handle_email_verification(self) -> bool:
        """
        [OPTIONAL]: Poll verification code if target requires email OTP.
        """
        if not self.mail_client:
            return True

        print("[*] Polling disposable email for verification code (60s timeout)...")
        code = self.mail_client.poll_for_verification_code(timeout_secs=60)
        if not code:
            print("[-] Verification code timeout.")
            return False

        print(f"[+] Received OTP code: {code}")
        otp_input = self.page.locator('input[name*="code" i], input[placeholder*="code" i]').first
        if otp_input.count() > 0:
            human_type(self.page, otp_input, code)
            return True
        return False

    def submit_and_confirm(self) -> bool:
        """
        [TODO]: Locate submit button and assert success state.
        """
        print("[*] Submitting registration form...")
        try:
            submit_btn = 'button[type="submit"], button:has-text("Sign up"), button:has-text("Register"), button:has-text("注册")'
            if not smart_click(self.page, submit_btn):
                return False

            # Wait for response redirect or confirmation message
            time.sleep(4.0)
            current_url = self.page.url

            # [TODO]: Define explicit success criteria (never treat ambiguous pages as success)
            if any(kw in current_url.lower() for kw in ("dashboard", "welcome", "confirm", "onboarding")):
                print(f"[+] Registration success confirmed! Current URL: {current_url}")
                return True

            error_loc = self.page.locator('.error, [role="alert"], .alert-danger').first
            if error_loc.count() > 0 and error_loc.is_visible():
                print(f"[-] Rejected by site error banner: {(error_loc.text_content() or '')[:100]}")
                return False

            print("[!] Ambiguous result state after submission; snapshot captured for review.")
            return False

        except Exception as e:
            print(f"[-] Submission confirmation failed: {e}")
            return False
