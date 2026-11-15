"""
Demo Site Adapter.
Demonstrates standard form filling, identity generation, and submission.
"""
from typing import Dict, Any
from core.adapter import BaseSiteAdapter
from core.identity import generate_identity


class DemoSiteAdapter(BaseSiteAdapter):
    """
    Standard demonstration adapter.
    Uses an embedded self-contained registration page to guarantee
    100% successful offline or local execution.
    """

    def target_url(self) -> str:
        # A lightweight data URI containing a complete, standard registration form
        html_content = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>Demo Registration Portal</title>
            <style>
                body { font-family: sans-serif; background: #f0f2f5; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
                .card { background: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 360px; }
                h2 { margin-top: 0; color: #1a1a1a; text-align: center; }
                .field { margin-bottom: 15px; }
                label { display: block; margin-bottom: 5px; font-weight: bold; font-size: 13px; color: #555; }
                input { width: 100%; box-sizing: border-box; padding: 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 14px; }
                button { width: 100%; padding: 12px; background: #0066cc; color: white; border: none; border-radius: 4px; font-size: 15px; cursor: pointer; font-weight: bold; margin-top: 10px; }
                button:hover { background: #0052a3; }
                .success-msg { display: none; color: #2e7d32; text-align: center; font-weight: bold; margin-top: 15px; }
            </style>
        </head>
        <body>
            <div class="card">
                <h2>Create Account</h2>
                <form id="regForm" onsubmit="event.preventDefault(); document.getElementById('regForm').style.display='none'; document.getElementById('success').style.display='block';">
                    <div class="field">
                        <label>Username</label>
                        <input type="text" id="username" name="username" placeholder="Enter username" required>
                    </div>
                    <div class="field">
                        <label>Email Address</label>
                        <input type="email" id="email" name="email" placeholder="name@example.com" required>
                    </div>
                    <div class="field">
                        <label>Password</label>
                        <input type="password" id="password" name="password" placeholder="At least 8 chars" required>
                    </div>
                    <button type="submit" id="submitBtn">Sign up</button>
                </form>
                <div id="success" class="success-msg">
                    <h3>Welcome aboard!</h3>
                    <p>Account registered successfully.</p>
                </div>
            </div>
        </body>
        </html>
        """
        import base64
        encoded = base64.b64encode(html_content.encode("utf-8")).decode("utf-8")
        return f"data:text/html;base64,{encoded}"

    def prepare_identity(self) -> Dict[str, Any]:
        self.account_data = generate_identity(email_domain="demo-community.org")
        return self.account_data

    def fill_form(self, identity: Dict[str, Any]) -> bool:
        self.page.wait_for_selector("#username", timeout=10000)
        self.page.fill("#username", identity["username"])
        self.page.fill("#email", identity["email"])
        self.page.fill("#password", identity["password"])
        return True

    def submit_and_confirm(self) -> bool:
        self.page.click("#submitBtn")
        self.page.wait_for_selector("#success", state="visible", timeout=10000)
        return True
