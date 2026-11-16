"""
QuanticdataAdapter Site Adapter.
Automated registration adapter for QuanticData (https://app.quanticdata.io/register).
Includes automated computer-vision solving for in-house slider puzzle captcha.
"""
import time
import base64
import random
from typing import Dict, Any, Optional

import cv2
import numpy as np
from core.adapter import BaseSiteAdapter
from core.identity import generate_identity
from core.pilot import human_type, smart_click
from core.mail_client import DisposableMailClient


def solve_slider_gap(bg_b64: str, piece_b64: str, piece_y: int, offset: int = 10) -> int:
    """
    Computes horizontal offset x of the slider gap via Canny edge detection
    and normalized template matching on masked puzzle piece.
    """
    bg_bytes = base64.b64decode(bg_b64.split(",")[-1])
    piece_bytes = base64.b64decode(piece_b64.split(",")[-1])

    bg = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
    piece = cv2.imdecode(np.frombuffer(piece_bytes, np.uint8), cv2.IMREAD_UNCHANGED)

    alpha = piece[:, :, 3]
    mask = (alpha > 0).astype(np.uint8) * 255
    piece_bgr = piece[:, :, :3]

    bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
    piece_gray = cv2.cvtColor(piece_bgr, cv2.COLOR_BGR2GRAY)

    bg_edge = cv2.Canny(bg_gray, 100, 200)
    piece_edge = cv2.Canny(piece_gray, 100, 200)
    piece_edge = cv2.bitwise_and(piece_edge, piece_edge, mask=mask)

    y_target = piece_y - offset
    y_min = max(0, y_target - 6)
    y_max = min(bg.shape[0], y_target + piece.shape[0] + 6)
    bg_band = bg_edge[y_min:y_max, :]

    res = cv2.matchTemplate(bg_band, piece_edge, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)

    predicted_x = max_loc[0] + offset
    print(f"[*] [quanticdata] Vision solver predicted slider target x={predicted_x} (score={max_val:.3f})")
    return predicted_x


class QuanticdataAdapter(BaseSiteAdapter):
    """
    Registration adapter for QuanticData.
    Target URL: https://app.quanticdata.io/register
    """

    def __init__(self, page, context, config=None):
        super().__init__(page, context, config)
        self.mail_client: Optional[DisposableMailClient] = None
        self.captcha_data: Dict[str, Any] = {}
        self.signup_success: bool = False

        # Attach network listener to capture captcha payloads and signup response
        self.page.on("response", self._on_response)

    def _on_response(self, res):
        url = res.url
        try:
            if "/api/v1/auth/captcha" in url and "check" not in url and "policy" not in url:
                data = res.json()
                if data.get("payload") and "background" in data["payload"]:
                    self.captcha_data.update(data["payload"])
            elif "/api/v1/auth/signup" in url:
                data = res.json()
                if res.status == 200 and data.get("type") == "response":
                    self.signup_success = True
        except Exception:
            pass

    def target_url(self) -> str:
        return "https://app.quanticdata.io/register"

    def prepare_identity(self) -> Dict[str, Any]:
        """Generate synthetic credentials matching target requirements."""
        use_disposable = self.config.get("use_disposable_mail", True)
        if use_disposable:
            self.mail_client = DisposableMailClient()
            ident = generate_identity(locale="US", mail_client=self.mail_client)
        else:
            ident = generate_identity(locale="US")

        self.account_data = ident
        return self.account_data

    def _solve_and_slide(self) -> bool:
        """Wait for captcha data, calculate gap coordinates, and slide puzzle piece."""
        # Wait up to 5s for captcha data to be received
        for _ in range(50):
            if "background" in self.captcha_data and "pieceImage" in self.captcha_data:
                break
            time.sleep(0.1)

        if not ("background" in self.captcha_data and "pieceImage" in self.captcha_data):
            print("[-] [quanticdata] Captcha puzzle images not received from API.")
            return False

        target_x = solve_slider_gap(
            self.captcha_data["background"],
            self.captcha_data["pieceImage"],
            self.captcha_data["pieceY"],
            self.captcha_data.get("offset", 10)
        )

        slider = self.page.locator('[role="slider"]')
        slider.wait_for(state="visible", timeout=8000)
        slider.focus()

        # Keyboard accessibility driver
        num_shift = target_x // 20
        num_single = (target_x % 20) // 5

        for _ in range(num_shift):
            self.page.keyboard.press("Shift+ArrowRight")
            time.sleep(random.uniform(0.02, 0.04))

        for _ in range(num_single):
            self.page.keyboard.press("ArrowRight")
            time.sleep(random.uniform(0.02, 0.03))

        # Commit slider position
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(1800)

        # Confirm slider passed
        slider_text = slider.text_content() or ""
        box_text = self.page.locator("#f-captcha").text_content() or ""
        if "\u2713" in slider_text or "place" in box_text.lower() or "done" in box_text.lower():
            print("[+] [quanticdata] Slider puzzle solved successfully!")
            return True

        print("[-] [quanticdata] Slider check did not confirm pass. Box status:", box_text.strip())
        return False

    def fill_form(self, identity: Dict[str, Any]) -> bool:
        """Fill registration form fields and solve captcha if requested."""
        print(f"[*] [quanticdata] Starting form filling for {identity['email']}...")
        try:
            # 1. Fill email and password (avoiding honeypots)
            email_selector = '#f-email, input[type="email"]'
            password_selector = '#f-pass, input[type="password"]'

            self.page.wait_for_selector(email_selector, state="visible", timeout=15000)
            human_type(self.page, email_selector, identity["email"])
            time.sleep(0.3)

            self.page.wait_for_selector(password_selector, state="visible", timeout=10000)
            human_type(self.page, password_selector, identity["password"])
            time.sleep(0.5)

            # 2. Check if captcha puzzle is active
            captcha_container = self.page.locator("#f-captcha")
            if captcha_container.count() > 0 and captcha_container.is_visible():
                print("[*] [quanticdata] Slider captcha detected on page, solving...")
                if not self._solve_and_slide():
                    # Retry once if first attempt failed
                    time.sleep(1.0)
                    if not self._solve_and_slide():
                        return False

            return True
        except Exception as e:
            print(f"[-] [quanticdata] Form filling error: {e}")
            return False

    def submit_and_confirm(self) -> bool:
        """Locate submit button, trigger submission and confirm success state."""
        print("[*] [quanticdata] Submitting registration...")
        try:
            submit_btn = "form button.btn-cta, button.btn-cta"
            if not smart_click(self.page, submit_btn):
                print("[-] [quanticdata] Failed to click CTA submit button.")
                return False

            # Wait for backend processing / redirection
            self.page.wait_for_timeout(5000)
            current_url = self.page.url

            # 1. Check network confirmation flag
            if self.signup_success:
                print(f"[+] [quanticdata] Signup API confirmed success 200! URL: {current_url}")
                return True

            # 2. Assert page text for success keywords (e.g. "Check your email", "verification link")
            body_text = self.page.locator("body").inner_text()
            success_phrases = ["check your email", "verification link", "confirm your email", "welcome", "dashboard"]
            if any(phrase in body_text.lower() for phrase in success_phrases):
                print(f"[+] [quanticdata] Registration success confirmed via on-page confirmation message!")
                return True

            # 3. Check for visible form validation errors
            error_loc = self.page.locator('.error, [role="alert"], .alert-danger, :text-matches("already exists|invalid|error", "i")').first
            if error_loc.count() > 0 and error_loc.is_visible():
                print(f"[-] [quanticdata] Rejected by site error banner: {error_loc.text_content()[:120]}")
                return False

            # 4. URL redirect check
            if any(kw in current_url.lower() for kw in ["dashboard", "welcome", "confirm", "home", "account", "verify"]):
                print(f"[+] [quanticdata] Registration success confirmed via URL redirection: {current_url}")
                return True

            print("[!] [quanticdata] Ambiguous state after submission; snapshot captured.")
            return False
        except Exception as e:
            print(f"[-] [quanticdata] Submit confirmation failed: {e}")
            return False
