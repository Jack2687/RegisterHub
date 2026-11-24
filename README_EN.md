<h1 align="center">RegisterHub</h1>

<p align="center">
  <b>Industrial-Grade Universal Account Registration & Lifecycle Testing Engine</b><br>
  <i>A deterministic, stealth automation framework for universal account registration and agentic workflows.</i>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-22272e.svg?style=flat-square" alt="License"></a>
  <a href="DISCLAIMER.md"><img src="https://img.shields.io/badge/Disclaimer-Legal%20Terms-red.svg?style=flat-square" alt="Disclaimer"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10%2B-3776ab.svg?style=flat-square&logo=python&logoColor=white" alt="Python Version"></a>
  <a href="https://playwright.dev/"><img src="https://img.shields.io/badge/Playwright-Engine-2EAD33.svg?style=flat-square&logo=playwright&logoColor=white" alt="Playwright"></a>
  <a href="https://opencv.org/"><img src="https://img.shields.io/badge/OpenCV-CV%20Engine-5C3EE8.svg?style=flat-square&logo=opencv&logoColor=white" alt="OpenCV"></a>
  <a href="tests/"><img src="https://img.shields.io/badge/Tests-Passing-brightgreen.svg?style=flat-square" alt="Tests"></a>
  <a href="https://bitwarden.com/"><img src="https://img.shields.io/badge/Export-Bitwarden%2FCSV-175DDC.svg?style=flat-square&logo=bitwarden&logoColor=white" alt="Bitwarden Export"></a>
  <a href="https://waw666waw666.github.io/registerhub-demo/"><img src="https://img.shields.io/badge/Live%20Demo-Web%20Workbench-10b981.svg?style=flat-square" alt="Live Demo"></a>
</p>

<p align="center">
  <a href="./README.md">简体中文</a> | <a href="./README_EN.md">English</a> | <a href="https://waw666waw666.github.io/registerhub-demo/">🌐 Live Interactive Demo</a>
</p>

---

## Overview

RegisterHub is an engineering-first automation framework designed to eliminate the fragility of traditional browser automation. Built on top of Playwright, OpenCV-based computer vision solvers, and Chrome DevTools Protocol (CDP), RegisterHub provides deterministic execution across modern single-page applications (SPAs), reactive state managers, and controlled DOM interfaces.

### Engineering Core Pillars

- **Runtime Environment Parity**: Eliminates synthetic automation signatures via native Chrome runtime integration, strict prototype preservation (`[native code]`), and optional zero-`Runtime.enable` **`patchright`** auto-acceleration (gracefully falling back to Playwright when not installed).
- **Zero-Dependency Web Workbench & Autonomous Streaming AI Agent (`RegisterHub Lite`)**: Pure Python stdlib HTTP/SSE server (no Node.js or FastAPI required). Launch via `python main.py --web`, `run_web.bat` (Windows), or `./run_web.sh` (macOS/Linux):
  - **Autonomous AI Agent**: Real 3-protocol SSE streaming (OpenAI Chat / Anthropic Messages / OpenAI Responses), live Prompt Cache / TTFT / tok/s telemetry, manual `/compact` & automatic 85% context compaction, 16 built-in tools, 5 editable Markdown engineering skills (`skills/*.md`), step-level adapter checkpoints & restore, persistent guard rules with approvals, follow-up queue, `/goal` loop, and streaming `/btw` side questions.
  - **Built-in Mail Pool & Proxy Pool (Zero Docker / Zero Extra Deps)**: Local SQLite WAL + Windows DPAPI-encrypted pools supporting auto-detected bulk import for Outlook OAuth2 / iCloud links / IMAP mailboxes, static HTTP/HTTPS/SOCKS5 proxies, residential Sticky-Session gateways, and short-lived API extract sources with live health probing & IP purity scoring.
  - **Complete Engineering Toolkit**: Single-thread runner, online AST-validated Adapter Code Editor, HAR-to-Protocol extractor, credential vault export (Bitwarden / CSV / JSON), cross-resolution window layout calibrator, and synthetic identity / Luhn card / TOTP 2FA / Tax-Phone-ID-Hardware utility toolbox.
- **Algorithmic Vision Solver**: Built-in computer vision pipeline featuring dual-pass Canny edge detection and human-kinetic Bezier curve trajectory generation, executing offline with zero cloud API dependencies.
- **CDP Bridge Architecture**: Native integration with enterprise fingerprint browsers (AdsPower, BitBrowser) and local Chrome instances, allowing seamless reuse of clean residential IPs and persistent user sessions.
- **Agentic Automation Protocol**: Natively integrated with `AGENTS.md` and the built-in AI Agent workbench, supporting any OpenAI-compatible LLM as well as Cursor, Claude Code, Windsurf, and Gemini CLI.

---

## Architecture

```text
RegisterHub/
├── core/
│   ├── adapter.py         # Abstract base adapter (BaseSiteAdapter) & lifecycle hooks
│   ├── calibrator.py      # Cross-resolution symmetric window layouts & anti-clipping zoom
│   ├── doctor.py          # Executable environment diagnostics (OpenCV, Chrome, network, FS)
│   ├── pilot.py           # Human-like keystroke dynamics & resilient interaction helpers
│   ├── stealth.py         # Native Chrome consistency & CDP bridge engine
│   ├── inspector.py       # Heuristic DOM form inspector (FormInspector)
│   ├── solver.py          # OpenCV puzzle gap detector & Bezier trajectory planner
│   ├── identity.py        # High-entropy synthetic persona & credential generator
│   ├── mail_client.py     # Adaptive polling disposable mailbox client
│   ├── imap_mail.py       # Stdlib single-account IMAP & BuiltinPoolMailClient
│   ├── mail_builtin.py    # Zero-Docker built-in mail pool (SQLite + DPAPI)
│   ├── proxy_builtin.py   # Zero-dep built-in proxy pool (Static / Residential / API)
│   ├── secrets.py         # Windows DPAPI credential protection & log redaction
│   ├── ai_agent.py        # Pure-stdlib autonomous single-thread AI Agent (16 tools)
│   ├── llm_client.py      # 3-protocol SSE streaming LLM client + cache/TTFT telemetry
│   ├── ip_scorer.py       # Exit IP residential attribution & risk purity scorer
│   ├── runner.py          # Deterministic task execution, builtin proxy resolution & ledger
│   ├── scaffold.py        # Automated adapter scaffolding (builtin pool / tempmail / IMAP)
│   └── exporter.py        # Bitwarden, 1Password, CSV & JSON credential exporter
├── skills/                # 5 built-in editable AI Agent skill recipes (*.md)
├── config/
│   └── window_geometry.json # Calibrated multi-window layout & zoom presets
├── docs/
│   └── WINDOW_CALIBRATION.md # Cross-resolution window calibration & preview guide
├── sites/                 # Clean reference adapters & scaffold template
│   ├── _template.py       # Standard adapter template (auto-switches tempmail / IMAP)
│   ├── demo_site.py       # Self-contained offline Base64 reference target
│   └── automation_exercise.py # Public multi-step automation benchmark adapter
├── web/                   # Zero-dependency RegisterHub Web Workbench & built-in AI Agent
│   ├── server.py          # Stdlib HTTP/SSE backend
│   ├── studio_bridge.py   # Studio control-plane API bridge
│   └── dist/              # Studio-aligned SPA frontend bundle
├── tests/                 # Unit & regression test suite
├── config.example.json    # Runtime configuration template
├── pytest.ini             # Test runner isolation rules
├── main.py                # Unified CLI & Web entry point
├── run.bat                # Windows interactive execution bootstrap (CRLF)
├── run_web.bat            # Windows 1-click Web Workbench launcher (:18886)
├── calibrate.bat          # Windows interactive window layout calibrator & preview launcher
└── requirements.txt       # Core dependency manifest
```

---

## Reference Adapters

RegisterHub ships exclusively with clean local & public benchmark adapters—zero hardcoded commercial targets—ready out of the box to verify the full lifecycle or serve as blueprints for new adapters:

| Adapter Key | Target Environment | Technical Characteristics | Primary Evaluation Target |
| :--- | :--- | :--- | :--- |
| `demo` | Embedded Base64 Target | Zero-configuration offline pipeline | Rapid 3s verification of browser launch, SSE logs, and ledger persistence |
| `automation_exercise` | Public Automation Benchmark | Multi-stage registration wizard | Complex form fields, dropdown cascading & checkbox assertions |
| `_template` | Standard Adapter Blueprint | Auto-wires tempmail / IMAP mail clients | Scaffold new adapters via `--scaffold` or Web **AI Chat** |

---

## Official Standalone Derived Projects

To keep the core `RegisterHub` repository clean and free of hardcoded commercial targets, production protocol reference implementations are maintained as standalone open-source repositories:

| Standalone Project | Architecture & Highlights | Repository |
| :--- | :--- | :--- |
| **[TokenHarbor Register](https://github.com/waw666waw666/tokenharbor-register)** | **100% Pure Protocol (Zero Browser)** · Next.js Server Action self-healing IDs + Tempmail / IMAP activation + automatic `:free` route consent unlock & live OpenAI-compatible `thk_live_` Key verification | [GitHub](https://github.com/waw666waw666/tokenharbor-register) |

---

## Quickstart

### 1. Prerequisites and Installation

```bash
# Clone the repository
git clone https://github.com/waw666waw666/RegisterHub.git
cd RegisterHub

# Install core dependencies
pip install -r requirements.txt

# Install Playwright browser binaries (optional: `pip install patchright` for zero-Runtime.enable stealth)
playwright install chromium
```

### 2. Runtime Environment Diagnostics

Verify host configuration, proxy availability, and dependency health:

```bash
python main.py doctor
# Also accepts flag form: python main.py --doctor
```

### 3. Launch Web Workbench (Recommended) or Baseline Demo

**Option A: Zero-Dependency Web Workbench & Built-in AI Agent (Recommended)**
```bash
python main.py --web
# Or double-click run_web.bat on Windows, then open http://127.0.0.1:18886
```

**Option B: Self-Contained 3s Offline Baseline Verification**
```bash
python main.py --run demo
```

Resulting credentials and execution artifacts are written atomically to `results/accounts.txt`.

### 4. AI Agent Collaborative Development (Cursor / Claude Code / Windsurf / Gemini)

RegisterHub is engineered from the ground up for seamless co-development with autonomous AI programming agents. The workspace root includes strictly audited [AGENTS.md](AGENTS.md) and [SETUP_FOR_AI.md](SETUP_FOR_AI.md) agent contracts.

**Prerequisite**: Open the **`RegisterHub`** project folder as the root active workspace in your AI IDE (Cursor, Windsurf, VS Code with Claude Code, or Gemini CLI).

Once loaded into the workspace, you can instruct your AI using either of the two approaches:

#### Method 1: Direct Prompting (Fast & Lightweight)
Drop the target URL directly into your AI chat:
```text
Follow the workspace conventions and AGENTS.md to generate an automated registration adapter for: https://example.com/signup
```
The AI reads the root rules, probes DOM elements, scaffolds the adapter, and runs baseline tests automatically.

#### Method 2: Provide Human-Walkthrough Steps (Strongly Recommended & Highest Success Rate)
For modern multi-step SPAs, onboarding wizards, or sites with interactive challenges, **the most reliable approach is to manually register one account yourself first in your regular browser**.  
Provide your recorded walkthrough—including URLs, clicked buttons, form inputs, OTP steps, and UI screenshots—directly to the AI:
```text
Please follow RegisterHub standards and AGENTS.md to build an automated adapter matching my verified manual workflow:
Target URL: https://example.com/signup

My manual walkthrough (screenshots attached):
1. Navigate to signup page, fill in email and password, and click "Create Account";
2. A Cloudflare Turnstile box appears; click the checkbox to clear;
3. Check inbox for the 6-digit OTP code and input it into the verification field;
4. Lands on the dashboard, navigate to "API Keys" in the top-right menu, and click "Create New Key";
5. Copy the displayed sk-xxxx key.

Please assemble sites/example_site.py according to this exact walkthrough and run a local verification test!
```
The AI will precisely map and automate your verified human interaction chain, eliminating guesswork and ensuring production-grade reliability on the first attempt!

---

## Proxy & Email Verification Configuration

Manage pools and egress strategies directly inside the top-level **Mail Pool**, **Proxy Pool**, or **Settings** views (persisted atomically to `config.json` and `results/*.db`):

1. **Built-in Proxy Pool (`builtin`, Recommended) & Custom Proxy**:
   - Bulk-import static HTTP/HTTPS/SOCKS5 proxies, configure dynamic residential gateways (Smartproxy / IPRoyal / Oxylabs / Bright Data / custom Sticky Session templates), or attach short-lived API extract URLs—all encrypted locally via DPAPI with automatic credential masking.
   - Also supports **Direct / System Proxy (`direct`)**, **Single Custom Proxy (`proxy`)**, and **External Anti-Detect Browser CDP Bridge**, plus real-time IP purity scoring.
2. **Built-in Mail Pool (`builtin`, Recommended, Zero Docker)**:
   - Paste mixed lines of **Outlook OAuth2 (3/4-column)**, **iCloud verification links**, or **Gmail/QQ/163/custom IMAP** accounts with masked dry-run preview, batch verification, inbox viewer with OTP extraction, and transactional lease/release.
3. **Public Disposable Mail (`tempmail`) & Single-Account IMAP (`imap`)**:
   - Out-of-the-box `DisposableMailClient` works with zero setup; single-account non-destructive IMAP (`BODY.PEEK[]`) is available directly in Settings.

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
# Bitwarden / 1Password compatible CSV
python main.py export --export bitwarden

# Standard CSV format
python main.py export --export csv

# Raw JSON format
python main.py export --export json
```

### Cross-Resolution Window Layouts & Anti-Clipping Preview
Automatically read the host display work area (`SPI_GETWORKAREA` + DPI scale) to generate symmetric, zero-overflow window presets (`1 / 2 / 4x2 / 6x2`) with adaptive anti-clipping CSS zoom (see [docs/WINDOW_CALIBRATION.md](docs/WINDOW_CALIBRATION.md)):
```bash
# Auto-generate symmetric window presets for the current display
python main.py --auto-layout

# Switch default 4-window or 6-window preset mode
python main.py --auto-layout --quad-mode centered_compact --hex-mode wing_cascade

# Preview all 6 presets sequentially (10s per preset by default)
python main.py --demo-layouts --demo-sec 10

# Launch interactive drag/resize & zoom calibrator (or double-click calibrate.bat)
python main.py --calibrate -c 2
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

---

## Community & Support

Join our developer community for technical discussions, custom adapter requests, and browser automation best practices:

- **Discord Community**: [Join Discord](https://discord.gg/USnVJ9YQX)
- **QQ Group**: 1130324020
