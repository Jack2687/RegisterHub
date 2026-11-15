# AutoRegister

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-22272e.svg?style=flat-square" alt="License"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10%2B-3776ab.svg?style=flat-square&logo=python&logoColor=white" alt="Python Version"></a>
  <a href="https://playwright.dev/"><img src="https://img.shields.io/badge/Playwright-Engine-2EAD33.svg?style=flat-square&logo=playwright&logoColor=white" alt="Playwright"></a>
  <a href="https://opencv.org/"><img src="https://img.shields.io/badge/OpenCV-CV%20Engine-5C3EE8.svg?style=flat-square&logo=opencv&logoColor=white" alt="OpenCV"></a>
  <a href="tests/"><img src="https://img.shields.io/badge/Tests-Passing-brightgreen.svg?style=flat-square" alt="Tests"></a>
  <a href="https://bitwarden.com/"><img src="https://img.shields.io/badge/Export-Bitwarden%2FCSV-175DDC.svg?style=flat-square&logo=bitwarden&logoColor=white" alt="Bitwarden Export"></a>
</p>

<p align="center">
  <b>Modern Headless Account Lifecycle & Form Automation Framework</b><br>
  <i>A deterministic, anti-fingerprinting automation engine built for robust form orchestration and agentic workflows.</i>
</p>

---

## Overview

AutoRegister is an engineering-first automation framework designed to eliminate the fragility of traditional browser automation. Built on top of Playwright, OpenCV-based computer vision solvers, and Chrome DevTools Protocol (CDP), AutoRegister provides deterministic execution across modern single-page applications (SPAs), reactive state managers, and controlled DOM interfaces.

### Engineering Core Pillars

- **Runtime Environment Parity**: Eliminates synthetic automation signatures via native Chrome runtime integration, strict prototype preservation (`[native code]`), and clean JavaScript execution context.
- **Algorithmic Vision Solver**: Built-in computer vision pipeline featuring dual-pass Canny edge detection and human-kinetic Bezier curve trajectory generation, executing offline with zero cloud API dependencies.
- **CDP Bridge Architecture**: Native integration with enterprise fingerprint browsers (AdsPower, BitBrowser) and local Chrome instances, allowing seamless reuse of clean residential IPs and persistent user sessions.
- **Agentic Automation Protocol**: Natively integrated with `AGENTS.md`, enabling autonomous AI agents (Cursor, Claude Code, Windsurf, Gemini CLI) to probe DOM targets, generate adapters, and execute end-to-end verification.

---

## Architecture

```text
register-community/
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
│   ├── huggingface.py     # SvelteKit controlled component adapter
│   ├── automation_exercise.py # Multi-step wizard workflow adapter
│   ├── deno.py            # Modern cloud platform interaction adapter
│   └── iteracompute.py    # Reactive SPA & modal handling adapter
├── tests/                 # Unit & regression test suite
├── config.json            # Global runtime configuration
├── pytest.ini             # Test runner isolation rules
├── main.py                # Unified CLI entry point
├── run.bat                # Windows interactive execution bootstrap
└── requirements.txt       # Core dependency manifest
```

---

## Showcase Adapters

AutoRegister includes six reference implementations covering diverse architectural paradigms:

| Adapter Key | Target Environment | Technical Characteristics | Primary Evaluation Target |
| :--- | :--- | :--- | :--- |
| `demo` | Embedded Base64 Target | Zero-configuration offline pipeline | Rapid runtime environment verification |
| `quanticdata` | Quantic Data Service | OpenCV edge detection + Bezier kinematics | Slider puzzle resolution & reactive inputs |
| `huggingface` | Hugging Face Community | SvelteKit controlled state management | Dynamic form validation & rate limit mitigation |
| `automation_exercise` | Standard Benchmark Portal | Multi-stage registration wizard | Complex dropdown cascading & checkbox grids |
| `deno` | Deno Console | Turnstile challenge & state probing | Modern cloud developer portal onboarding |
| `iteracompute` | IteraCompute Engine | Asynchronous SPA & modal intervention | Blocking element clearance & explicit gate guards |

---

## Quickstart

### 1. Prerequisites and Installation

```bash
# Clone the repository
git clone https://github.com/waw666waw666/register-community.git
cd register-community

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

AutoRegister is architected for seamless collaboration with autonomous programming agents. The root `AGENTS.md` enforces strict engineering disciplines:

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

## Compliance & Security Notice

1. **License**: This project is licensed under the [Apache License 2.0](LICENSE).
2. **Authorized Use Only**: AutoRegister is strictly intended for authorized security audits, integration testing, quality assurance automation, and academic research on web standards. Users are required to adhere to applicable laws and the Terms of Service of target endpoints. The authors assume no liability for unauthorized usage or policy violations.
