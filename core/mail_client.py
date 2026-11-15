"""
Public Disposable Email Client.
Provides a free, zero-config temporary email receiver for demonstration.
"""
import time
import re
import requests
from typing import Optional, List, Dict, Any


class DisposableMailClient:
    """Client for free public disposable email service."""

    def __init__(self, timeout: int = 15):
        self.session = requests.Session()
        self.timeout = timeout
        self.api_base = "https://www.1secmail.com/api/v1/"
        self.active_address: Optional[str] = None
        self.login: Optional[str] = None
        self.domain: Optional[str] = None

    def create_mailbox(self) -> str:
        """Acquire a fresh temporary mailbox address."""
        url = f"{self.api_base}?action=genRandomMailbox&count=1"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                emails = resp.json()
                if emails and isinstance(emails, list):
                    self.active_address = emails[0]
                    self.login, self.domain = self.active_address.split("@", 1)
                    return self.active_address
        except Exception:
            pass
        
        # Fallback offline mockup address if network unavailable
        self.active_address = "demo_test@1secmail.com"
        self.login, self.domain = "demo_test", "1secmail.com"
        return self.active_address

    def check_messages(self) -> List[Dict[str, Any]]:
        """Fetch incoming message summaries."""
        if not self.login or not self.domain:
            return []
        url = f"{self.api_base}?action=getMessages&login={self.login}&domain={self.domain}"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return []

    def read_message(self, message_id: int) -> Dict[str, Any]:
        """Fetch full details of a specific message."""
        url = f"{self.api_base}?action=readMessage&login={self.login}&domain={self.domain}&id={message_id}"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {}

    def poll_for_verification_code(self, timeout_secs: int = 60, pattern: str = r"\b\d{4,8}\b") -> Optional[str]:
        """Poll mailbox until a numeric verification code is detected."""
        start_time = time.time()
        while time.time() - start_time < timeout_secs:
            msgs = self.check_messages()
            if msgs:
                latest_id = msgs[0].get("id")
                if latest_id:
                    detail = self.read_message(latest_id)
                    body = detail.get("body", "") or detail.get("textBody", "")
                    match = re.search(pattern, body)
                    if match:
                        return match.group(0)
            time.sleep(3)
        return None
