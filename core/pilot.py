"""
Humanized Pilot Engine.
Provides natural keyboard typing rhythms, robust clicking, and viewport scrolling.
"""
import time
import random
from typing import Optional, Union
from playwright.sync_api import Page, Locator


def human_type(page: Page, target: Union[str, Locator], text: str, min_delay: float = 0.03, max_delay: float = 0.09):
    """
    Type text character-by-character with natural human cadence,
    including slight keystroke jitter, to defeat basic keystroke dynamics bot detection.
    """
    loc = page.locator(target) if isinstance(target, str) else target
    loc.wait_for(state="visible", timeout=10000)
    loc.scroll_into_view_if_needed()
    loc.click()
    time.sleep(random.uniform(0.1, 0.2))

    # Clear existing content cleanly
    loc.fill("")
    time.sleep(random.uniform(0.05, 0.1))

    for char in text:
        loc.press_sequentially(char, delay=int(random.uniform(min_delay, max_delay) * 1000))
        # Occasional micro-pause simulating reading / thinking
        if random.random() < 0.08:
            time.sleep(random.uniform(0.1, 0.25))

    time.sleep(random.uniform(0.1, 0.2))


def smart_click(page: Page, target: Union[str, Locator], timeout: int = 15000) -> bool:
    """
    Click an element after ensuring it is visible, in viewport, and enabled (not disabled).
    """
    loc = page.locator(target) if isinstance(target, str) else target
    try:
        loc.wait_for(state="visible", timeout=timeout)
        loc.scroll_into_view_if_needed()
        
        # Wait until button is enabled if it's currently disabled
        start_time = time.time()
        while time.time() - start_time < (timeout / 1000.0):
            if loc.is_enabled():
                break
            time.sleep(0.3)

        # Micro-hover before clicking
        box = loc.bounding_box()
        if box:
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            time.sleep(random.uniform(0.05, 0.15))

        loc.click(timeout=timeout)
        return True
    except Exception as e:
        print(f"[Pilot] Smart click failed on {target}: {e}")
        return False


def smart_agree_tos(page: Page) -> bool:
    """
    Detect and check terms-of-service / privacy policy checkboxes automatically.
    """
    tos_selectors = [
        'input[type="checkbox"][name*="terms" i]',
        'input[type="checkbox"][name*="tos" i]',
        'input[type="checkbox"][name*="agree" i]',
        'input[type="checkbox"][id*="terms" i]',
        'input[type="checkbox"][id*="tos" i]',
        'input[type="checkbox"][id*="agree" i]',
        'label:has-text("Terms") input[type="checkbox"]',
        'label:has-text("Agree") input[type="checkbox"]',
        'label:has-text("同意") input[type="checkbox"]',
        'input[type="checkbox"]'
    ]

    for sel in tos_selectors:
        try:
            locs = page.locator(sel)
            count = locs.count()
            if count > 0:
                for i in range(count):
                    box = locs.nth(i)
                    if box.is_visible() and not box.is_checked():
                        box.check(force=True)
                        time.sleep(random.uniform(0.1, 0.2))
                return True
        except Exception:
            continue
    return False
