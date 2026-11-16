<h1 align="center">RegisterHub</h1>

<p align="center">
  <b>Modern Headless Account Lifecycle & Form Automation Framework</b><br>
  <i>A deterministic, anti-fingerprinting automation engine built for robust form orchestration and agentic workflows.</i>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-22272e.svg?style=flat-square" alt="License"></a>
  <a href="DISCLAIMER.md"><img src="https://img.shields.io/badge/Disclaimer-Legal%20Terms-red.svg?style=flat-square" alt="Disclaimer"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10%2B-3776ab.svg?style=flat-square&logo=python&logoColor=white" alt="Python Version"></a>
  <a href="https://playwright.dev/"><img src="https://img.shields.io/badge/Playwright-Engine-2EAD33.svg?style=flat-square&logo=playwright&logoColor=white" alt="Playwright"></a>
  <a href="https://opencv.org/"><img src="https://img.shields.io/badge/OpenCV-CV%20Engine-5C3EE8.svg?style=flat-square&logo=opencv&logoColor=white" alt="OpenCV"></a>
  <a href="tests/"><img src="https://img.shields.io/badge/Tests-Passing-brightgreen.svg?style=flat-square" alt="Tests"></a>
  <a href="https://bitwarden.com/"><img src="https://img.shields.io/badge/Export-Bitwarden%2FCSV-175DDC.svg?style=flat-square&logo=bitwarden&logoColor=white" alt="Bitwarden Export"></a>
</p>

<p align="center">
  <a href="./README_EN.md">English</a> | <a href="./README.md">简体中文</a>
</p>

---

## Overview

RegisterHub is an engineering-first automation framework designed to eliminate the fragility of traditional browser automation. Built on top of Playwright, OpenCV-based computer vision solvers, and Chrome DevTools Protocol (CDP), RegisterHub provides deterministic execution across modern single-page applications (SPAs), reactive state managers, and controlled DOM interfaces.

### Engineering Core Pillars

- **Runtime Environment Parity**: Eliminates synthetic automation signatures via native Chrome runtime integration, strict prototype preservation (`[native code]`), and clean JavaScript execution context.
- **Algorithmic Vision Solver**: Built-in computer vision pipeline featuring dual-pass Canny edge detection and human-kinetic Bezier curve trajectory generation, executing offline with zero cloud API dependencies.
- **CDP Bridge Architecture**: Native integration with enterprise fingerprint browsers (AdsPower, BitBrowser) and local Chrome instances, allowing seamless reuse of clean residential IPs and persistent user sessions.
- **Agentic Automation Protocol**: Natively integrated with `AGENTS.md`, enabling autonomous AI agents (Cursor, Claude Code, Windsurf, Gemini CLI) to probe DOM targets, generate adapters, and execute end-to-end verification.

---

## Architecture

```text
RegisterHub/
├── core/
│   ├── adapter.py         # Abstract base adapter (BaseSiteAdapter) & lifecycle hooks
│   ├── pilot.py           # Human-like keystroke dynamics & resilient interaction helpers
│   ├── stealth.py         # Native Chrome consistency & CDP bridge engine
│   ├── inspector.py       # Heuristic DOM form inspector (FormInspector)
│   ├── solver.py          # OpenCV puzzle gap detector & Bezier trajectory planner
│   ├── identity.py        # High-entropy synthetic persona & credential generator
│   ├── mail_client.py     # Adaptive polling mailbox synchronization client
│   ├── runner.py          # Deterministic task execution, snapshot logger & ledger writer
│   ├── scaffold.py        # Automated adapter scaffolding & code generation engine
│   └── exporter.py        # Bitwarden, 1Password, CSV & JSON credential exporter
├── sites/                 # Production & reference adapters
│   ├── _template.py       # Standard adapter implementation template
│   ├── demo_site.py       # Self-contained offline Base64 reference target
│   ├── quanticdata.py     # Slider puzzle & Bezier drag verification adapter
│   ├── deepgram.py        # Deepgram $200 USD allowance & key harvesting adapter
│   ├── amd.py             # AMD AI Developer Program onboarding adapter
│   ├── coarena.py         # Post-Google OAuth pure protocol registration & API key minting
│   ├── milionseek.py      # 100milionseek 100M tokens/day key harvester & Turnstile solver
│   └── automation_exercise.py # Multi-step wizard workflow adapter
├── tests/                 # Unit & regression test suite
├── config.json            # Global runtime configuration
├── pytest.ini             # Test runner isolation rules
├── main.py                # Unified CLI entry point
├── run.bat                # Windows interactive execution bootstrap (CRLF)
└── requirements.txt       # Core dependency manifest
```

---

## Showcase Adapters

RegisterHub includes reference implementations covering diverse architectural paradigms:

| Adapter Key | Target Environment | Technical Characteristics | Primary Evaluation Target |
| :--- | :--- | :--- | :--- |
| `demo` | Embedded Base64 Target | Zero-configuration offline pipeline | Rapid 3s runtime environment verification |
| `quanticdata` | Quantic Data Service | OpenCV edge detection + Bezier kinematics | Local slider puzzle resolution & reactive inputs |
| `deepgram` | Deepgram Voice AI | Google reCAPTCHA v2 active penetration | $200 USD free credit allowance & API key extraction |
| `amd` | AMD AI Developer Program | Akamai telemetry parity + OneTrust clearance | Big Tech AI developer registration & token acquisition |
| `coarena` | Coarena AI Platform | **Post-Google OAuth Protocol Registration & Key Minting** | Demonstrates OAuth session capture, pure REST terms acceptance & instant API key minting |
| `milionseek` | 100milionseek Platform | **100M Tokens/day Free Compute** + Turnstile Clearance | Instant on-page key generation (no mail verification needed) + referral cascade pool |
| `automation_exercise` | Standard Benchmark Portal | Multi-stage registration wizard | Complex dropdown cascading & checkbox grids |

---

## Standalone Showcase Ecosystem

> **Standalone Decoupling & Reciprocal Funnel Notice**:  
> For developers interested in automating a **single specific service**, we have decoupled our primary production workflows into **100% self-contained standalone GitHub repositories**.  
> Each independent repository includes its own Windows `run.bat` launcher, dedicated `main.py` entry point, and embedded runtime engine without requiring the full framework. You can also run them within this unified engine via `python main.py --run <site>`:

| Standalone Project | Key Features & Free Benefits | Repository Direct Link |
| :--- | :--- | :--- |
| **[Deepgram Register](https://github.com/waw666waw666/deepgram-register)** | Official **$200 USD Free Credit Allowance** + reCAPTCHA v2 Penetration + Pure REST Protocol Key Extraction | [GitHub Repo](https://github.com/waw666waw666/deepgram-register) |
| **[100MilionSeek Register](https://github.com/waw666waw666/100milionseek-register)** | **100M Tokens/day Free Allowance** + Turnstile Clearance + Instant Card Key Harvester + Referral Pool | [GitHub Repo](https://github.com/waw666waw666/100milionseek-register) |
| **[AMD AI Dev Register](https://github.com/waw666waw666/amd-register)** | Official AMD AI Developer Program + Akamai Telemetry Parity + OneTrust Modal Penetration | [GitHub Repo](https://github.com/waw666waw666/amd-register) |
| **[Coarena Register](https://github.com/waw666waw666/coarena-register)** | **Post-OAuth Pure REST Protocol Key Minting** + Session Capture + Terms Acceptance & Quota Validation | [GitHub Repo](https://github.com/waw666waw666/coarena-register) |

---

## Quickstart

### 1. Prerequisites and Installation

```bash
# Clone the repository
git clone https://github.com/waw666waw666/RegisterHub.git
cd RegisterHub

# Install core dependencies
pip install -r requirements.txt

# Install Playwright browser binaries
playwright install chromium
```

### 2. Runtime Environment Diagnostics

Verify host configuration, proxy availability, and dependency health:

```bash
python main.py doctor
```

### 3. Baseline Verification

Execute the self-contained offline verification target (requires no network or proxy):

```bash
python main.py --run demo
```

Resulting credentials and execution artifacts are written atomically to `results/accounts.txt`.

---

## Email Verification & Mailbox Strategies

Target platforms maintain diverse risk-control rules for signup mailboxes. RegisterHub provides three flexible tiers:

1. **Zero-Email-Verification Targets (Instant Minting)**:
   - Showcase Target: `100milionseek`
   - Requires **no verification emails or confirmation links whatsoever**. Enter any standard email format, resolve Turnstile, and the API key is minted directly on the page within 10 seconds.

2. **Session Capture Targets (OAuth Handoff)**:
   - Showcase Target: `coarena`
   - Leverages genuine Chrome solely for Google OAuth token acquisition; subsequent terms consent, key minting, and quota validation are executed 100% via background REST protocol.

3. **High-Value / Strict Risk Targets (e.g. Deepgram $200 USD Allowance, AMD)**:
   - **Default Mode (Public Disposable Email, Automated)**:
     With `use_disposable_mail: true`, the engine automatically polls tri-channel public APIs (mail.tm, etc.) for 6-digit codes.
   - **Custom Mailbox Mode (Recommended, 100% Success Guarantee)**:
     Platforms like Deepgram and AMD dynamically blacklist public temporary email domains. **We strongly recommend supplying your own Outlook, Hotmail, Gmail, or custom domain address** in `config.json` (`"email": "your_email@outlook.com"`). The script automatically completes form submission and reCAPTCHA penetration, prompts for the 6-digit code in the terminal, and guarantees minting of your **$200 USD API key**!
   - **Enterprise MailHub Hub (Advanced)**:
     Easily integrates with self-hosted MailHub Docker containers, Cloudflare Catch-all Email Routing, or IMAP protocols for multi-worker headless automation.

---

## Command Line Interface (CLI)

### Adapter Discovery
List all registered site adapters and their technical descriptors:
```bash
python main.py --list
```

### Automated Adapter Scaffolding
Inspect a target registration page and generate a typed adapter skeleton pre-populated with discovered input selectors:
```bash
# Analyze target DOM and scaffold adapter skeleton
python main.py --scaffold target_service --url https://target-service.com/signup

# Execute scaffolded adapter
python main.py --run target_service
```

### External CDP Session Attach
Attach to an already running external browser instance (AdsPower, BitBrowser, or Chrome with `--remote-debugging-port`):
```bash
python main.py --run demo --cdp http://127.0.0.1:9222
```

### Credential Vault Export
Export verified account records into standardized credential management formats:
```bash
# Bitwarden / 1Password compatible JSON
python main.py export --export bitwarden

# Standard CSV format
python main.py export --export csv

# Raw JSON format
python main.py export --export json
```

---

## AI Agent Integration (`AGENTS.md`)

RegisterHub is architected for seamless collaboration with autonomous programming agents. The root `AGENTS.md` enforces strict engineering disciplines:

- **Single Concurrency Default**: Enforces deterministic single-threaded execution by default to prevent desktop disruption and IP exhaustion; prompts for concurrency scaling only after sustained success.
- **Stage-Gate Assertions**: Prohibits arbitrary timeout delays; mandates strict validation of URL mutations, DOM response states, and token persistence.
- **Zero-Interference Process Lifecycle**: Manages child process trees explicitly via POSIX/Windows hooks, ensuring external user processes and personal browser sessions remain completely untouched.

---

## Verification & Testing

Execute the test suite to validate core heuristics, OpenCV solvers, and code scaffolding:

```bash
pytest tests/
```

---

## Disclaimer & Legal Notice

1. **License**: This project is licensed under the [Apache License 2.0](LICENSE).
2. **Legal Disclaimer**: RegisterHub is strictly intended for **authorized security audits, regression test automation, and web standards research**.
   - **Accessing, downloading, or running this code constitutes unconditional agreement to be bound by the full Legal Disclaimer**;
   - Mass registration abuse, credential stuffing, scraping protected assets, or circumventing commercial defenses is strictly prohibited;
   - Users assume all legal risks and responsibilities. The authors accept zero liability for any damages or violations arising from misuse.

> **Full Legal Disclaimer**: [DISCLAIMER.md](DISCLAIMER.md) · [简体中文免责声明](DISCLAIMER.zh.md)
