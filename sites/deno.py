"""
DenoAdapter Site Adapter.
Automated registration adapter for deno.
"""
import time
from typing import Dict, Any, Optional
from core.adapter import BaseSiteAdapter
from core.identity import generate_identity
from core.pilot import smart_click
from core.mail_client import DisposableMailClient


class DenoAdapter(BaseSiteAdapter):
    """
    Registration flow for deno.
    Target URL: https://dash.deno.com/signin
    """

    def __init__(self, page, context, config=None):
        super().__init__(page, context, config)
        self.mail_client: Optional[DisposableMailClient] = None

    def target_url(self) -> str:
        return "https://dash.deno.com/signin"

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
        """
        Deno Deploy has no native username/password form.
        It uses third-party OAuth (GitHub / Google) preceded by Cloudflare Turnstile.
        """
        print(f"[*] [deno] Inspecting signin options for {identity['email']}...")
        try:
            github_btn = 'button[name="provider"][value="github"]'
            google_btn = 'button[name="provider"][value="google"]'
            
            self.page.wait_for_timeout(3000)
            has_github = self.page.locator(github_btn).count() > 0
            has_google = self.page.locator(google_btn).count() > 0
            if has_github or has_google:
                print("[*] [deno] Detected third-party OAuth providers (GitHub / Google). No native password form available.")
                return True
            print("[-] [deno] Could not locate OAuth provider buttons (GitHub or Google).")
            return False
        except Exception as e:
            print(f"[-] [deno] Inspection error: {e}")
            return False

    def submit_and_confirm(self) -> bool:
        """
        Trigger OAuth flow or verify success state.
        """
        print("[*] [deno] Deno Deploy requires OAuth delegation.")
        try:
            github_btn = 'button[name="provider"][value="github"]'
            if self.page.locator(github_btn).count() > 0:
                btn = self.page.locator(github_btn)
                if btn.is_disabled():
                    print("[!] [deno] GitHub signin button is disabled pending Cloudflare Turnstile completion.")
                    return False
                print("[*] [deno] Clicking Sign in with GitHub...")
                return smart_click(self.page, github_btn)
            return False
        except Exception as e:
            print(f"[-] [deno] Submit confirmation failed: {e}")
            return False
