"""
Heuristic Form Inspector.
Scans standard HTML pages to detect form inputs and submit buttons.
"""
from typing import Dict, Any, Optional
from playwright.sync_api import Page, ElementHandle


class FormInspector:
    """Basic heuristic scanner for standard web registration forms."""

    def __init__(self, page: Page):
        self.page = page

    def detect_inputs(self) -> Dict[str, Optional[str]]:
        """
        Analyze page DOM and return discovered selectors for standard fields:
        email, password, username, submit.
        """
        results: Dict[str, Optional[str]] = {
            "email": None,
            "password": None,
            "username": None,
            "submit": None
        }

        # 1. Detect Email input
        email_candidates = [
            'input[type="email"]',
            'input[name*="email" i]',
            'input[id*="email" i]',
            'input[placeholder*="email" i]',
            'input[placeholder*="邮箱" i]'
        ]
        for sel in email_candidates:
            loc = self.page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                results["email"] = sel
                break

        # 2. Detect Password input
        pwd_candidates = [
            'input[type="password"]',
            'input[name*="password" i]',
            'input[id*="password" i]',
            'input[placeholder*="密码" i]'
        ]
        for sel in pwd_candidates:
            loc = self.page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                results["password"] = sel
                break

        # 3. Detect Username input
        user_candidates = [
            'input[name*="username" i]',
            'input[id*="username" i]',
            'input[name*="user" i]',
            'input[placeholder*="用户名" i]',
            'input[placeholder*="username" i]'
        ]
        for sel in user_candidates:
            loc = self.page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                results["username"] = sel
                break

        # 4. Detect Submit button
        submit_candidates = [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Sign up")',
            'button:has-text("Register")',
            'button:has-text("注册")',
            'button:has-text("Create account")',
            'button:has-text("Submit")'
        ]
        for sel in submit_candidates:
            loc = self.page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                results["submit"] = sel
                break

        return results
