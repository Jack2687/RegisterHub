"""
AutomationExercise Site Adapter.
Automated registration adapter for the standard international testing benchmark:
https://automationexercise.com/signup
"""
import time
from typing import Dict, Any, Optional
from core.adapter import BaseSiteAdapter
from core.identity import generate_identity
from core.pilot import human_type, smart_click, smart_agree_tos
from core.mail_client import DisposableMailClient


class AutomationExerciseAdapter(BaseSiteAdapter):
    """
    Standard 2-Step Registration Benchmark for AutomationExercise.
    Demonstrates humanized typing, multi-step wizards, and account creation assertion.
    """

    def __init__(self, page, context, config=None):
        super().__init__(page, context, config)
        self.mail_client: Optional[DisposableMailClient] = None

    def target_url(self) -> str:
        return "https://automationexercise.com/signup"

    def prepare_identity(self) -> Dict[str, Any]:
        """Generate high-entropy US persona matching form requirements."""
        ident = generate_identity(locale="US")
        # Ensure email is unique for each test run
        ident["email"] = f"{ident['username']}@testbench-community.org"
        self.account_data = ident
        return self.account_data

    def fill_form(self, identity: Dict[str, Any]) -> bool:
        """Execute the two-step registration flow with natural human typing."""
        print(f"[*] Starting Step 1: Basic Signup for {identity['email']}...")
        try:
            # --- Step 1: Name & Email on Signup page ---
            name_input = 'input[data-qa="signup-name"]'
            email_input = 'input[data-qa="signup-email"]'
            signup_btn = 'button[data-qa="signup-button"]'

            human_type(self.page, name_input, identity["full_name"])
            human_type(self.page, email_input, identity["email"])
            
            print("[*] Submitting Step 1...")
            if not smart_click(self.page, signup_btn):
                return False

            # Wait for Step 2 details page to render
            self.page.wait_for_selector('input[data-qa="password"]', timeout=15000)
            print("[+] Step 1 passed. Step 2 (Detailed Profile) rendered!")

            # --- Step 2: Detailed Personal Information ---
            print("[*] Filling Step 2: Password, Address, and Contact details...")
            human_type(self.page, 'input[data-qa="password"]', identity["password"])
            human_type(self.page, 'input[data-qa="first_name"]', identity["first_name"])
            human_type(self.page, 'input[data-qa="last_name"]', identity["last_name"])
            human_type(self.page, 'input[data-qa="address"]', "123 Innovation Way")
            human_type(self.page, 'input[data-qa="state"]', identity["state"])
            human_type(self.page, 'input[data-qa="city"]', identity["city"])
            human_type(self.page, 'input[data-qa="zipcode"]', identity["postal_code"])
            human_type(self.page, 'input[data-qa="mobile_number"]', identity["phone"])

            # Auto-agree to newsletters & terms
            smart_agree_tos(self.page)
            time.sleep(1.0)

            return True

        except Exception as e:
            print(f"[-] Form filling error: {e}")
            return False

    def submit_and_confirm(self) -> bool:
        """Trigger final submission and verify ACCOUNT CREATED state."""
        print("[*] Submitting final registration (Create Account)...")
        try:
            create_btn = 'button[data-qa="create-account"]'
            if not smart_click(self.page, create_btn):
                return False

            # Assert success header
            self.page.wait_for_selector('h2[data-qa="account-created"], h2:has-text("Account Created!")', timeout=15000)
            print("\n" + "=" * 65)
            print("  [SUCCESS] 目标站点返回: 'ACCOUNT CREATED!' (账号创建成功！)")
            print("=" * 65 + "\n")
            return True

        except Exception as e:
            print(f"[-] Final confirmation failed: {e}")
            return False
