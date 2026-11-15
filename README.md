# AutoRegister

> **The Modern Headless Account & Form Automation Framework**  
> A lightweight, modular, and developer-friendly framework for automating web form submissions, synthetic identity generation, and headless account onboarding.

---

## ✨ Features

- **🚀 Heuristic Form Inspection**: Pass any target registration URL with `--url`, and AutoRegister automatically identifies username, email, password, and submission controls.
- **👤 Synthetic Identity Engine**: Generates coherent, believable multi-locale usernames, compliant high-entropy passwords, and realistic personas.
- **📬 Disposable Email Integration**: Built-in support for free disposable mailboxes to receive verification codes without paid third-party APIs.
- **🧩 Local Slider & Track Simulator**: CPU-based puzzle calculation and non-linear bezier track generator mimicking human hand inertia.
- **📦 Clean Architecture**: Extensible `BaseSiteAdapter` lifecycle design (`probe` -> `fill_form` -> `submit_and_confirm`).
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

---

## 🛠️ Project Structure

```text
autoregister/
├── core/
│   ├── adapter.py         # BaseSiteAdapter abstract class
│   ├── runner.py          # Execution scheduler & ledger recorder
│   ├── inspector.py       # Heuristic DOM form inspector
│   ├── identity.py        # Multi-locale synthetic identity generator
│   ├── mail_client.py     # Public disposable email polling client
│   └── solver.py          # Bezier curve track generator
├── sites/
│   ├── __init__.py
│   ├── demo_site.py       # Complete runnable demonstration adapter
│   └── huggingface.py     # Hugging Face registration adapter
├── config.json            # Global runtime configuration
├── main.py                # Command-line interface entrypoint
├── run.bat                # Windows quick launcher
└── requirements.txt       # Project dependencies
```

---

## 📖 How to Add a New Site Adapter

Create a new file in `sites/my_site.py` and inherit from `BaseSiteAdapter`:

```python
from core.adapter import BaseSiteAdapter
from core.identity import generate_identity

class MySiteAdapter(BaseSiteAdapter):
    def target_url(self) -> str:
        return "https://target-service.com/register"

    def prepare_identity(self):
        self.account_data = generate_identity()
        return self.account_data

    def fill_form(self, identity):
        self.page.fill('input[name="email"]', identity["email"])
        self.page.fill('input[name="password"]', identity["password"])
        return True

    def submit_and_confirm(self):
        self.page.click('button[type="submit"]')
        self.page.wait_for_url("**/dashboard", timeout=15000)
        return True
```

Register your adapter in `main.py` or run it via the runner.

---

## 📄 License

This project is licensed under the Apache-2.0 License.

## ⚠️ Disclaimer

This tool is designed for software developers, QA testers, and researchers conducting authorized browser automation, UI testing, and synthetic user onboarding. Users are solely responsible for complying with the terms of service of target websites.
