<h1 align="center">RegisterHub</h1>

<p align="center">
  <b>工业级万能账号自动化注册框架与生命周期测试引擎</b><br>
  <i>A deterministic, stealth automation framework for universal account registration and agentic workflows.</i>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-22272e.svg?style=flat-square" alt="License"></a>
  <a href="DISCLAIMER.zh.md"><img src="https://img.shields.io/badge/Disclaimer-Legal%20Terms-red.svg?style=flat-square" alt="Disclaimer"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10%2B-3776ab.svg?style=flat-square&logo=python&logoColor=white" alt="Python Version"></a>
  <a href="https://playwright.dev/"><img src="https://img.shields.io/badge/Playwright-Engine-2EAD33.svg?style=flat-square&logo=playwright&logoColor=white" alt="Playwright"></a>
  <a href="https://opencv.org/"><img src="https://img.shields.io/badge/OpenCV-CV%20Engine-5C3EE8.svg?style=flat-square&logo=opencv&logoColor=white" alt="OpenCV"></a>
  <a href="tests/"><img src="https://img.shields.io/badge/Tests-Passing-brightgreen.svg?style=flat-square" alt="Tests"></a>
  <a href="https://bitwarden.com/"><img src="https://img.shields.io/badge/Export-Bitwarden%2FCSV-175DDC.svg?style=flat-square&logo=bitwarden&logoColor=white" alt="Bitwarden Export"></a>
  <a href="https://waw666waw666.github.io/registerhub-demo/"><img src="https://img.shields.io/badge/Live%20Demo-Web%20Workbench-10b981.svg?style=flat-square" alt="Live Demo"></a>
</p>

<p align="center">
  <a href="./README.md">简体中文</a> | <a href="./README_EN.md">English</a> | <a href="https://waw666waw666.github.io/registerhub-demo/">🌐 在线交互演示 (Live Demo)</a>
</p>

---

## 项目概述

RegisterHub 是一个面向工程研发的现代化 Web 自动化框架，旨在彻底解决传统无头脚本在复杂前端交互中的脆弱性问题。框架基于 Playwright 运行时、OpenCV 离线计算机视觉算子及 Chrome DevTools Protocol (CDP) 架构构建，为现代单页应用 (SPA)、响应式受控表单以及自动化测试提供确定性的交互与状态编排能力。

### 核心工程支柱

- **运行环境自洽性 (Runtime Environment Parity)**：通过原生 Chrome 二进制接入与底层自动化特征消除，支持可选安装 **`patchright`** 实现零 `Runtime.enable` CDP 隐匿（自动检测并优先启用，未安装平滑回退 Playwright），保持 JavaScript 原型链纯净（`[native code]`）。
- **零依赖 Web 可视化工作台 + 全自主流式 AI Agent (`RegisterHub Lite`)**：纯 Python 标准库 HTTP/SSE 驱动（免装 Node/FastAPI），双击 `run_web.bat`（macOS/Linux 运行 `./run_web.sh`）或执行 `python main.py --web` 即可打开全套 Studio 工作台：
  - **全自主 AI 对话**：三协议真 SSE 流式输出（OpenAI Chat / Anthropic Messages / OpenAI Responses）、Prompt Cache 命中率 / TTFT / 吞吐速度遥测、手动 `/compact` 与 85% 自动上下文压缩、16 项内置工具、5 套可编辑工程技能（`skills/*.md`）、步级代码检查点一键回滚、持久化安全护栏审批、追问队列、`/goal` 长跑闭环与流式 `/btw` 旁白提问；
  - **内置邮箱池 & 内置代理池（零 Docker / 零外部依赖）**：本地 SQLite + Windows DPAPI 加密存储，支持混合批量粘贴导入 Outlook OAuth2 / iCloud 接码链接 / 通用 IMAP 邮箱与静态 HTTP/SOCKS5 代理 / 动态住宅网关（Sticky Session）/ 短效 API 提取源，支持在线验活、收件箱读信提码与 IP 纯净度打分；
  - **全栈开发工具集**：单线程任务台、站点工坊（在线 AST 语法/安全校验编辑器 + HAR 抓包提炼）、账号与 Key 验活/导出（Bitwarden / CSV / JSON）、跨分辨率视窗标定、多国身份/虚拟卡/TOTP 2FA/税号手机证件硬件指纹四合一工具箱。
- **离线视觉算法求解器 (Algorithmic Vision Solver)**：内置双通 Canny 边缘轮廓计算与三次贝塞尔拟人手部动力学模型，纯本地离线执行，零第三方付费 API 依赖。
- **CDP 会话桥接架构 (CDP Bridge Architecture)**：深度支持外部指纹浏览器（AdsPower、比特浏览器等）与本地已登录 Chrome 会话无缝接入，复用既有干净住宅网络与登录状态。
- **AI 代理原生集成规范 (Agentic Co-Pilot Protocol)**：仓库根目录内置工业级 `AGENTS.md` 交互契约，既可在内置 Web「AI 直跑」接入任意 OpenAI 兼容大模型自主写适配器并实测，也深度适配 Cursor、Claude Code、Windsurf 与 Gemini CLI。

---

## 架构拓扑

```text
RegisterHub/
├── core/
│   ├── adapter.py         # 抽象适配器基类 (BaseSiteAdapter) 与生命周期契约
│   ├── pilot.py           # 拟人按键动力学 (human_type) 与智能交互守门员
│   ├── stealth.py         # 原生环境自洽引擎与 CDP 会话桥接器
│   ├── inspector.py       # 启发式 DOM 表单探测器 (FormInspector)
│   ├── solver.py          # OpenCV 缺口匹配计算与贝塞尔轨迹规划器
│   ├── identity.py        # 高熵多国身份与合规密码发生器
│   ├── mail_client.py     # 毫秒级自适应退避临时邮箱轮询客户端
│   ├── imap_mail.py       # 零依赖标准库单账号 IMAP 与内置邮箱池接码客户端
│   ├── mail_builtin.py    # 零 Docker 内置邮箱池（SQLite WAL + DPAPI + Outlook/iCloud/IMAP）
│   ├── proxy_builtin.py   # 零依赖内置代理池（静态代理 / 动态住宅网关 / API 提取源）
│   ├── secrets.py         # Windows DPAPI 本地凭据加密与全链路日志脱敏
│   ├── ai_agent.py        # 纯标准库单线程自主 AI Agent（16 工具 / 压缩 / 检查点 / 护栏）
│   ├── llm_client.py      # 三协议真 SSE 流式模型客户端与 Cache/TTFT/tok_s 遥测
│   ├── ip_scorer.py       # 出口 IP 住宅属性与风控纯净度评分器
│   ├── runner.py          # 调度引擎、内置代理自动解析、黑匣子快照与原子台账持久化
│   ├── scaffold.py        # 适配器自动派生与预装配代码生成引擎（自动联动内置池 / 临时邮 / IMAP）
│   └── exporter.py        # Bitwarden、1Password、CSV 及 JSON 资产导出器
├── skills/                # 5 套内置可编辑 AI Agent 工程技能规范 (*.md)
├── sites/                 # 业务与示范适配器池
│   ├── _template.py       # 工业级标准适配器实现模板（支持 tempmail / IMAP 自动切换）
│   ├── demo_site.py       # 自包含离线 Base64 靶场适配器
│   └── automation_exercise.py # 国际两步式向导交互基准靶场适配器
├── web/                   # 零依赖 RegisterHub Web 可视化工作台与内置 AI 直跑
│   ├── server.py          # 纯标准库 HTTP/SSE 后端（免额外依赖）
│   ├── studio_bridge.py   # Studio 控制平面协议桥接层
│   └── dist/              # Studio 风格单页工作台构建产物
├── tests/                 # 单元与回归自动化测试套件
├── config.example.json    # 运行配置模板（首次运行可按需复制为 config.json）
├── pytest.ini             # Pytest 测试隔离规范
├── main.py                # 统一命令行与 Web 调度入口
├── run.bat                # Windows 交互引导脚本 (CRLF 规范)
├── run_web.bat            # Windows 一键启动 Web 可视化工作台 (:18886)
└── requirements.txt       # 核心生产依赖清单
```

---

## 内置示范适配器矩阵

RegisterHub 开箱内置纯本地与公开测试靶场示范适配器，零第三方商业站点硬编码，开箱即可验证完整生命周期或作为新站派生基准：

| 适配器标识 | 目标环境 | 技术实现特性 | 核心评测与验证重点 |
| :--- | :--- | :--- | :--- |
| `demo` | 内置离线 Base64 靶场 | 零配置全离线管道 | 3 秒快速验证环境自洽、浏览器启动、SSE 日志与账号落盘 |
| `automation_exercise` | 国际公开自动化测试靶场 | 多阶段向导式注册流 | 复杂表单字段、下拉联动与多选框状态硬断言 |
| `_template` | 工业级标准适配器模板 | 自动联动临时邮 / IMAP 接码 | 通过 `--scaffold` 或 Web「AI 对话」一键派生自定义站点适配器 |

---

## 官方独立实战派生工程 (Official Standalone Derived Projects)

为保持主框架仓库纯粹（零商业站点硬编码），高频平台的完整实战案例已解耦为 100% 自包含的独立 GitHub 开源工程，开箱即用并与主框架保持双向联动：

| 独立开源项目 | 核心架构与技术特性 | 仓库直达 |
| :--- | :--- | :--- |
| **[TokenHarbor Register](https://github.com/waw666waw666/tokenharbor-register)** | **100% 纯协议（零浏览器依赖）** · Next.js Server Action 动态哈希自愈 + 三通道临时邮 / IMAP 接码 + 自动解锁 `:free` 免费模型路由并在线验活 OpenAI 兼容 `thk_live_` Key | [GitHub 仓库](https://github.com/waw666waw666/tokenharbor-register) |
| **[Deepgram Register](https://github.com/waw666waw666/deepgram-register)** | 官方 **$200 免费额度** · 谷歌 reCAPTCHA v2 主动穿透 + 纯 REST 协议秒级签发 40 位 API Key | [GitHub 仓库](https://github.com/waw666waw666/deepgram-register) |
| **[100MilionSeek Register](https://github.com/waw666waw666/100milionseek-register)** | **每日 1 亿 Tokens 免费额度** · Turnstile 穿透 + 免收信页面卡片秒出 Key + 母号裂变接力 | [GitHub 仓库](https://github.com/waw666waw666/100milionseek-register) |
| **[AMD AI Dev Register](https://github.com/waw666waw666/amd-register)** | AMD 官方 AI 开发者计划注册 · Akamai 遥测自洽 + OneTrust 模态弹窗穿透与凭据获取 | [GitHub 仓库](https://github.com/waw666waw666/amd-register) |
| **[Coarena Register](https://github.com/waw666waw666/coarena-register)** | **谷歌登录后 100% 协议提 Key** · OAuth 会话捕获 + 纯 REST 条款签署与每日额度秒验 | [GitHub 仓库](https://github.com/waw666waw666/coarena-register) |

---

## 快速上手

### 1. 环境准备与依赖安装

```bash
# 克隆仓库
git clone https://github.com/waw666waw666/RegisterHub.git
cd RegisterHub

# 安装 Python 生产依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器内核（可选再装 `pip install patchright` 自动开启零 Runtime.enable 隐匿内核）
playwright install chromium
```

### 2. 环境健康自检

检测宿主机运行环境、代理可用性以及依赖完整性：

```bash
python main.py doctor
# 也支持带横杠写法：python main.py --doctor
```

### 3. 启动可视化 Web 工作台（推荐）或基线自测

**方式 A：一键打开零依赖 Web 工作台 + 内置 AI 直跑（推荐）**
```bash
python main.py --web
# 或在 Windows 直接双击 run_web.bat，浏览器访问 http://127.0.0.1:18886
```

**方式 B：命令行 3 秒跑通内置离线靶场（无需外部网络或代理）**
```bash
python main.py --run demo
```

生成的凭据记录将自动以原子写入模式持久化至 `results/accounts.txt`。

### 4. AI 助手协同使用指南 (Cursor / Claude Code / Windsurf / Gemini)

本项目为 AI 编程助手深度优化，仓库根目录自带专属 [AGENTS.md](AGENTS.md) 行为约束与 [SETUP_FOR_AI.md](SETUP_FOR_AI.md) 环境协议。

**前置操作**：使用你的 AI 编程客户端（如 Cursor、Windsurf、VS Code 搭配 Claude Code / Gemini CLI），将 **`RegisterHub` 项目文件夹作为工作区根目录打开**。

工作区载入后，支持通过以下两种方式让 AI 协助开发新站点的注册适配器：

#### 方法一：极简直接丢（快捷交互模式）
直接在 AI 对话框中把目标网站注册地址发给 AI：
```text
请参考本项目规范与 AGENTS.md，为我编写该网站的自动化注册适配器：https://example.com/signup
```
AI 会自动读取根目录规则与模板，自主嗅探表单字段、生成代码骨架并尝试本地实测。

#### 方法二：人工自跑流程给步骤（强烈推荐，成号最稳健）
面对复杂的现代 Web 应用、多步向导或带交互验证的站点，**最稳健的做法是：你自己先在浏览器中手动注册成功一次**。  
把你在真实操作中经历的**核心网址、点击按钮、表单字段、验证码类型及关键截图**直接发给 AI，例如：
```text
请参考本项目规范与 AGENTS.md，帮我把刚才手动跑通的注册流程写成自动化适配器：
目标网址：https://example.com/signup

我的手动操作步骤如下（附关键界面截图）：
1. 打开注册页，填入邮箱和密码，点击“Create Account”；
2. 页面弹出了 Cloudflare 人机验证框，手动点一下绿勾；
3. 去邮箱查收 6 位数字验证码，回填到页面的 OTP 输入框中；
4. 进入控制台仪表盘，点击右上角“API Keys”菜单，点击“Create New Key”；
5. 复制弹出的 sk-xxxx 密钥，完成流程。

请根据我的真实操作路径编写 sites/example_site.py，并进行本地试跑验收！
```
AI 将直接对照你跑通的真实人类行为路径进行精确代码装配与接口逆向，避免无头盲测走弯路，一击必中！

---

## 关于代理与邮箱接码配置 (Proxy & Email Strategies)

在 Web 工作台左侧独立导航 **「邮箱池」**、**「代理池」** 或 **「系统设置」**（同步持久化至 `config.json` 与 `results/*.db`）中，开源版支持零搭建开箱即用的双池与单通道模式：

1. **内置代理池（`builtin`，推荐，零外部依赖）与单代理接入**：
   - 在 **「代理池」** 批量粘贴静态 HTTP/HTTPS/SOCKS5 代理、配置动态住宅代理网关（Smartproxy / IPRoyal / Oxylabs / Bright Data / 自定义 Sticky Session 模板）或粘贴短效 API 提取链接，所有账密均经本地 DPAPI 加密落盘并自动脱敏展示；
   - 亦支持 **直连 / 系统代理（`direct`）**、**单条自定义代理（`proxy`）** 及 **外部指纹浏览器 CDP 桥接**，支持一键全量测活与住宅纯净度评分。
2. **内置邮箱池（`builtin`，推荐，零 Docker）**：
   - 在 **「邮箱池」** 智能混合粘贴导入 **Outlook OAuth2（三/四段正反序）**、**iCloud 接码链接** 或 **QQ/163/Gmail/自定义 IMAP**，支持导入前脱敏预览、一键全量验活、收件箱原文查看/验证码提取与事务租约（任务结束自动释放）；
   - 一键切换为 `mail.provider = "builtin"` 后，脚手架 `--scaffold` 生成的适配器与 AI Agent 会自动通过 `BuiltinPoolMailClient` 租借接码。
3. **默认免费临时邮箱（`tempmail`）与单账号 IMAP（`imap`）**：
   - 首次零配置即可用 `DisposableMailClient` 三通道公网临时邮快速验证；也可在「系统设置」填入单个自有 IMAP 账号一键测试连通并无损收码（`BODY.PEEK[]`）。

---

## 命令行接口 (CLI)

### 查看已装载适配器
列出所有已注册的适配器及其技术规格：
```bash
python main.py --list
```

### 自动化脚手架生成
输入目标网页 URL，自动嗅探 DOM 输入字段并生成类型完整的适配器骨架代码：
```bash
# 智能探测目标网页并生成适配器代码
python main.py --scaffold target_service --url https://target-service.com/signup

# 执行生成的新适配器
python main.py --run target_service
```

### 外部指纹浏览器 CDP 桥接
直接附加至已在运行的外部指纹浏览器（AdsPower、比特浏览器）或开启调试端口的 Chrome 实例：
```bash
python main.py --run demo --cdp http://127.0.0.1:9222
```

### 跨分辨率视窗适配与巡演验收
针对不同分辨率/DPI 屏幕一键生成 `1全屏 / 2左右 / 4双形态 / 6双形态` 对称无溢出预设，并在有头运行期自动应用防切割等比缩放（详见 `docs/WINDOW_CALIBRATION.md`）：
```bash
# 读取本机屏幕工作区，一键生成 6 套对称无溢出预设（也可直接双击 calibrate.bat）
python main.py --auto-layout
python main.py --auto-layout --quad-mode edge_2x2 --hex-mode wing_cascade

# 桌面轮播巡演验收全部 6 套布局
python main.py --demo-layouts --demo-sec 8

# 交互式手工微调标定
python main.py --calibrate -c 2
```

### 密码库资产标准导出
将测试产出的账号资产一键转换为标准格式：
```bash
# 导出为 Bitwarden / 1Password 兼容 JSON
python main.py export --export bitwarden

# 导出为通用 CSV
python main.py export --export csv

# 导出为原始 JSON
python main.py export --export json
```

---

## AI 代理协作规范 (`AGENTS.md`)

RegisterHub 针对自主编程智能体进行了全流程规范定义。根目录的 `AGENTS.md` 约束以下核心纪律：

- **默认单并发纪律**：默认按单线程串行执行，避免无节制多开撕裂用户桌面；仅在单路稳定且等信占比超 50% 时主动提醒提速。
- **阶段守门员断言**：严禁无目的的时间等待 (`time.sleep`)，必须通过 URL 状态、DOM 属性或凭据落盘事实进行强校验。
- **宿主进程零干扰**：退出钩子严格基于明确的子进程树清理，严禁全局通杀系统进程，严禁全局抢占用户桌面焦点。

---

## 测试套件执行

运行完整单元测试集以验证核心启发式探针、OpenCV 计算引擎与代码生成逻辑：

```bash
pytest tests/
```

---

## 免责声明与开源协议

1. **开源许可证**：本项目基于标准的 [Apache License 2.0](LICENSE) 许可协议发布。
2. **免责与合规说明**：本项目定位为**授权环境下的自动化流程走查与 Web 工程研究工具**。所有代码仅供技术交流与学术验证参考，完整法律声明与技术中立性说明请参阅：
   - [DISCLAIMER.zh.md](DISCLAIMER.zh.md) (中文版)
   - [DISCLAIMER.md](DISCLAIMER.md) (English)

---

## 开发者社区与交流

如果你在账号工程自动化、协议逆向、云端风控自洽或 API Key 提取落地中有任何技术疑问、适配需求或交流想法，欢迎加入我们的开发者社群：

- **QQ 交流群**：1130324020
- **Discord 社区**：[点击加入 Discord](https://discord.gg/USnVJ9YQX)
