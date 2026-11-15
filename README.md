# AutoRegister

> **The Modern Headless Account & Form Automation Framework**  
> A lightweight, modular, and developer-friendly framework for automating web form submissions, synthetic identity generation, and headless account onboarding.

---

## ✨ Features

- **🕵️ Native Genuine Chrome & Stealth Engine**: Launches system official Google Chrome (`channel="chrome"`) with stripped automation flags (`--disable-blink-features=AutomationControlled`, `navigator.webdriver=false`). Preserves JavaScript prototype chains (`[native code]`) without fragile monkey-patching, ensuring Cloudflare Turnstile and reCAPTCHA challenges pass naturally.
- **🌐 Fingerprint Browser Bridge (CDP)**: Natively connects to external anti-detect/fingerprint browsers (AdsPower, BitBrowser, Hubstudio, MoreLogin, or standard Chrome with remote debugging) via a single `--cdp` flag.
- **🤖 Pilot Humanized Cadence**: Natural keystroke typing jitter (`human_type`), viewport scrolling, smart clicking, and automated Terms of Service (`smart_agree_tos`) agreement.
- **📸 Blackbox Visual Snapshots**: Automatically captures full visual PNG snapshots on failure/exception under `results/snapshots/` for instant post-mortem debugging.
- **🚀 Heuristic Form Inspection Pro**: Scans any live URL with `--url`, detecting username, email, password, confirm password, fullname, verification codes, and wizard buttons.
- **👤 Multi-Locale Identity Engine**: Generates believable personas (US, UK, DE), phone numbers, street addresses, and strictly compliant high-entropy passwords.
- **📬 Disposable Email Integration**: Built-in support for free disposable mailboxes to receive verification codes without paid third-party APIs.
- **🧩 Local Slider & Track Simulator**: CPU-based puzzle calculation and non-linear bezier track generator mimicking human hand inertia.
- **📦 Clean Architecture**: Extensible `BaseSiteAdapter` lifecycle design (`probe` -> `fill_form` -> `submit_and_confirm`).
- **📊 Universal Vault Exporter**: Export generated credentials into CSV, structured JSON, or official **Bitwarden / 1Password** importable vault format (`python main.py export --export bitwarden`).
- **🛡️ Network Jitter Resilience**: Automatic retry decorators (`@retry_on_failure`) with exponential backoff on transient network delays or DOM mutations.
- **🧪 Production Test Suite**: Comes with comprehensive unit tests (`pytest tests/`) validating identity entropy, dynamic discovery, and exporter accuracy.
- **🩺 Built-in Doctor Diagnostics**: One-command environment check (`python main.py doctor`) to ensure Playwright and dependencies are operational.

---

## ⚡ Quick Start

### 1. Installation

```bash
git clone https://github.com/your-username/autoregister.git
cd autoregister

# Install dependencies
pip install -r requirements.txt

# Install Playwright browser binaries
playwright install chromium
```

### 2. Verify Environment

```bash
python main.py doctor
```

### 3. Run Out-of-the-Box Demo

AutoRegister comes with a zero-config, embedded offline demo form to verify that your browser automation pipeline works 100%:

```bash
# Run headless or headful demo
python main.py --run demo
```

Credentials will be saved automatically to `results/accounts.txt`.

### 4. Inspect Any Live Website

You can test heuristic field detection on any live registration page:

```bash
python main.py --url https://example.com/signup
```

### 5. Use External Fingerprint Browser (AdsPower / BitBrowser / Hubstudio)

Connect directly to your active anti-detect browser profile via CDP:

```bash
python main.py --run demo --cdp http://127.0.0.1:9222
```

### 6. Cloudflare Turnstile & Anti-Bot Resilience

AutoRegister enforces **Fingerprint Tri-Consistency** (`User-Agent` ↔ `Sec-CH-UA` ↔ `TLS/JA4`):
- Uses native Chrome User-Agent dynamically fetched from your system binary, avoiding hardcoded UA mismatches.
- Never injects destructive monkey-patches into `plugins` or `WebGLRenderingContext`, preserving `[native code]` prototype integrity.
- Under high-risk challenges (like Cloudflare Turnstile), interactive clicks pass successfully without triggering infinite loop reloads.

---

## 🛠️ Project Structure

```text
autoregister/
├── core/
│   ├── adapter.py         # BaseSiteAdapter abstract class
│   ├── runner.py          # Execution scheduler, snapshot capture & ledger recorder
│   ├── pilot.py           # Humanized keystroke typing, smart click & TOS agree
│   ├── inspector.py       # Heuristic DOM form inspector pro
│   ├── identity.py        # Multi-locale synthetic identity generator
│   ├── stealth.py         # Client-side anti-fingerprint masking & CDP bridge
│   ├── mail_client.py     # Public disposable email polling client
│   └── solver.py          # Bezier curve track generator
├── sites/
│   ├── __init__.py
│   ├── _template.py       # Production standard adapter development template
│   ├── demo_site.py       # Complete runnable offline demonstration adapter
│   ├── automation_exercise.py # Standard international benchmark (2-step wizard)
│   ├── huggingface.py     # Hugging Face registration adapter with WAF gate
│   ├── deno.py            # Deno Deploy auth & Turnstile state detector
│   ├── iteracompute.py    # Dynamic SPA form with cookie banner cleaner
│   └── quanticdata.py     # OpenCV gap edge detection & accessible slider solver
├── config.json            # Global runtime configuration
├── main.py                # Command-line interface entrypoint
├── run.bat                # Windows interactive quick launcher
└── requirements.txt       # Project dependencies
```

---

## 📖 How to Scaffold a New Site Adapter

You don't need to write boilerplate code from scratch. AutoRegister provides an automated scaffolding tool that can inspect your target registration page and generate a production-ready adapter instantly:

```bash
# 1. Scaffold adapter with automatic form field pre-assembly
python main.py --scaffold mysite --url https://target-service.com/signup

# Or scaffold a clean standard template directly:
python main.py --scaffold mysite
```

This generates `sites/mysite.py` pre-equipped with humanized typing (`human_type`), smart clicking (`smart_click`), and automated TOS agreements (`smart_agree_tos`).

### Run Your New Adapter Instantly

Thanks to automatic dynamic adapter discovery, you can execute your newly created adapter immediately without manually modifying any registry:

```bash
python main.py --run mysite
```

---

## 🤖 AI-Native Ready (AGENTS.md)

AutoRegister is architected to be **AI-Native**. When paired with coding agents (Cursor, Windsurf, Claude Code, GitHub Copilot, Gemini), the project enforces a standardized protocol in [`AGENTS.md`](./AGENTS.md):
- **Three-Tier Architecture Rule**: Protocol (API) First ➔ Headless Browser ➔ Headful Chrome fallback.
- **High-Signal Reporting Contract**: Deterministic five-stage status reports without speculative commentary.
- **Human-in-the-Loop Coordination**: Visible desktop gatekeeper handling for reCAPTCHA v2 / Turnstile challenges.

---

## 📄 License

This project is licensed under the Apache-2.0 License.

## ⚠️ Disclaimer

This tool is designed for software developers, QA testers, and researchers conducting authorized browser automation, UI testing, and synthetic user onboarding. Users are solely responsible for complying with the terms of service of target websites.
