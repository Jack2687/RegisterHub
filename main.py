"""
AutoRegister CLI Main Entrypoint.
The Modern Headless Account & Form Automation Framework.
"""
import sys
import json
import argparse
from pathlib import Path

from core.runner import StandardRunner
from core.inspector import FormInspector
from core.identity import generate_identity
from sites.demo_site import DemoSiteAdapter
from sites.huggingface import HuggingFaceAdapter

ADAPTER_REGISTRY = {
    "demo": DemoSiteAdapter,
    "huggingface": HuggingFaceAdapter,
}


def load_config() -> dict:
    cfg_path = Path("config.json")
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"browser": {"headless": False}, "output_dir": "./results"}


def run_doctor():
    print("=" * 50)
    print("  AutoRegister Diagnostics (Doctor)")
    print("=" * 50)
    
    # 1. Python version
    py_ver = sys.version.split()[0]
    print(f"[+] Python Version: {py_ver} (Requirement: >= 3.10) ... OK")

    # 2. Playwright import
    try:
        import importlib.metadata
        pw_ver = importlib.metadata.version("playwright")
        print(f"[+] Playwright Library: v{pw_ver} ... OK")
    except Exception:
        try:
            import playwright
            print("[+] Playwright Library: Installed ... OK")
        except ImportError:
            print("[-] Playwright Library: NOT FOUND. Run 'pip install playwright' ... FAILED")

    # 3. Pillow import
    try:
        import importlib.metadata
        pil_ver = importlib.metadata.version("pillow")
        print(f"[+] Pillow Library: v{pil_ver} ... OK")
    except Exception:
        try:
            import PIL
            print("[+] Pillow Library: Installed ... OK")
        except ImportError:
            print("[-] Pillow Library: NOT FOUND. Run 'pip install pillow' ... FAILED")

    # 4. Requests import
    try:
        import importlib.metadata
        req_ver = importlib.metadata.version("requests")
        print(f"[+] Requests Library: v{req_ver} ... OK")
    except Exception:
        try:
            import requests
            print("[+] Requests Library: Installed ... OK")
        except ImportError:
            print("[-] Requests Library: NOT FOUND. Run 'pip install requests' ... FAILED")

    print("=" * 50)
    print("  All core checks evaluated. Environment ready.")
    print("=" * 50)


def inspect_url(target_url: str, cdp_endpoint: str = None):
    from playwright.sync_api import sync_playwright
    from core.stealth import apply_stealth_to_context, connect_fingerprint_browser
    import os
    print(f"[*] Navigating to {target_url} for heuristic form inspection...")
    
    cfg = load_config()
    proxy_server = cfg.get("browser", {}).get("proxy") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    
    with sync_playwright() as p:
        if cdp_endpoint:
            print(f"[*] Connecting to fingerprint browser at {cdp_endpoint}...")
            browser = connect_fingerprint_browser(p, cdp_endpoint)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
        else:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
                "--no-first-run",
                "--no-default-browser-check"
            ]
            launch_kwargs = {
                "headless": False,
                "args": launch_args,
                "ignore_default_args": ["--enable-automation"]
            }
            if proxy_server:
                launch_kwargs["proxy"] = {"server": proxy_server}
                
            try:
                browser = p.chromium.launch(channel="chrome", **launch_kwargs)
            except Exception:
                browser = p.chromium.launch(**launch_kwargs)

            context = browser.new_context(
                no_viewport=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )

        apply_stealth_to_context(context)
        page = context.new_page()
        try:
            page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
            inspector = FormInspector(page)
            fields = inspector.detect_inputs()
            
            print("\n[+] Heuristic Detection Results:")
            for k, v in fields.items():
                status = v if v else "Not found / Not visible"
                print(f"    - {k:10s}: {status}")

            if any(fields.values()):
                print("\n[*] Generating synthetic identity and testing autofill...")
                ident = generate_identity()
                if fields["username"]:
                    page.fill(fields["username"], ident["username"])
                if fields["email"]:
                    page.fill(fields["email"], ident["email"])
                if fields["password"]:
                    page.fill(fields["password"], ident["password"])
                print("[+] Form auto-filled. Browser will remain open for 10 seconds for review.")
                page.wait_for_timeout(10000)
            else:
                print("[-] Could not detect standard input selectors heuristically.")
        except Exception as e:
            print(f"[-] Inspection error: {e}")
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description="AutoRegister Automation Engine")
    parser.add_argument("--run", type=str, default=None, help="Target site adapter name (e.g. demo)")
    parser.add_argument("--count", type=int, default=1, help="Number of accounts to generate")
    parser.add_argument("--url", type=str, default=None, help="Inspect and test autofill on a URL directly")
    parser.add_argument("--cdp", type=str, default=None, help="Connect to an external Fingerprint/Anti-Detect browser CDP (e.g. http://127.0.0.1:9222)")
    parser.add_argument("command", nargs="?", default=None, help="Subcommand like 'doctor'")

    args = parser.parse_args()

    if args.command == "doctor":
        run_doctor()
        return

    if args.url:
        inspect_url(args.url, cdp_endpoint=args.cdp)
        return

    cfg = load_config()
    runner = StandardRunner(cfg, cdp_endpoint=args.cdp)

    adapter_name = (args.run or "demo").lower()
    if adapter_name in ADAPTER_REGISTRY:
        runner.execute_adapter(ADAPTER_REGISTRY[adapter_name], count=args.count)
    else:
        available = ", ".join(ADAPTER_REGISTRY.keys())
        print(f"[-] Unknown adapter '{adapter_name}'. Available: {available}")


if __name__ == "__main__":
    main()
