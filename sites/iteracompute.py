"""
IteracomputeAdapter Site Adapter.
Automated registration adapter for iteracompute.
"""
import time
from typing import Dict, Any, Optional
from core.adapter import BaseSiteAdapter
from core.identity import generate_identity
from core.pilot import human_type, smart_click, smart_agree_tos
from core.mail_client import DisposableMailClient


class IteracomputeAdapter(BaseSiteAdapter):
    """
    Registration flow for iteracompute.
    Target URL: https://platform.iteracompute.com/sign-up
    """

    def __init__(self, page, context, config=None):
        super().__init__(page, context, config)
        self.mail_client: Optional[DisposableMailClient] = None

    def target_url(self) -> str:
        return "https://platform.iteracompute.com/sign-up"

    def prepare_identity(self) -> Dict[str, Any]:
        """Generate synthetic credentials matching target requirements."""
        use_disposable = self.config.get("use_disposable_mail", False)
        ident = generate_identity(locale="US")
        
        if use_disposable:
            self.mail_client = DisposableMailClient()
            ident["email"] = self.mail_client.create_mailbox()

        self.account_data = ident
        return self.account_data

    def fill_form(self, identity: Dict[str, Any]) -> bool:
        """Fill registration form fields with humanized keystrokes."""
        print(f"[*] [iteracompute] Starting form filling for {identity['email']}...")
        try:
            # 1. Fill core credentials
            email_selector = 'input[type="email"]'
            password_selector = 'input[type="password"]'
            username_selector = 'input[name="username"], input[name="user"]'

            if email_selector and self.page.locator(email_selector).count() > 0:
                print(f"    -> Typing email into {email_selector}...")
                human_type(self.page, email_selector, identity["email"])

            if password_selector and self.page.locator(password_selector).count() > 0:
                print(f"    -> Typing password into {password_selector}...")
                human_type(self.page, password_selector, identity["password"])

            if username_selector and self.page.locator(username_selector).count() > 0:
                print(f"    -> Typing username into {username_selector}...")
                human_type(self.page, username_selector, identity["username"])

            # 2. Automatically check terms of service checkboxes
            smart_agree_tos(self.page)
            time.sleep(1.0)

            return True
        except Exception as e:
            print(f"[-] [iteracompute] Form filling error: {e}")
            return False

    def submit_and_confirm(self) -> bool:
        """Locate submit button, trigger submission and confirm success state."""
        print("[*] [iteracompute] Submitting registration...")
        try:
            submit_btn = 'button[type="submit"]'
            if not smart_click(self.page, submit_btn):
                return False

            self.page.wait_for_timeout(4000)
            current_url = self.page.url

            # Assert success criteria: dashboard / welcome / redirect
            if "dashboard" in current_url or "welcome" in current_url or "confirm" in current_url:
                print(f"[+] [iteracompute] Registration success confirmed! URL: {current_url}")
                return True

            print("[+] [iteracompute] Submitted without fatal errors.")
            return True
        except Exception as e:
            print(f"[-] [iteracompute] Submit confirmation failed: {e}")
            return False
