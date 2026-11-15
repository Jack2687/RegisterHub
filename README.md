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
  <b>现代化无头账号生命周期测试与受控表单自动化框架</b><br>
  <i>A deterministic, anti-fingerprinting automation engine built for robust form orchestration and agentic workflows.</i>
</p>

<p align="center">
  <a href="./README.md">简体中文</a> | <a href="./README_EN.md">English</a>
</p>

---

## 项目概述

AutoRegister 是一个面向工程研发的现代化 Web 自动化框架，旨在彻底解决传统无头脚本在复杂前端交互中的脆弱性问题。框架基于 Playwright 运行时、OpenCV 离线计算机视觉算子及 Chrome DevTools Protocol (CDP) 架构构建，为现代单页应用 (SPA)、响应式受控表单以及自动化测试提供确定性的交互与状态编排能力。

### 核心工程支柱

- **运行环境自洽性 (Runtime Environment Parity)**：通过原生 Chrome 二进制接入与底层自动化特征消除，保持 JavaScript 原型链纯净（`[native code]`），消除自动化标记对现代 Web 应用的异常干扰。
- **离线视觉算法求解器 (Algorithmic Vision Solver)**：内置双通 Canny 边缘轮廓计算与三次贝塞尔拟人手部动力学模型，纯本地离线执行，零第三方付费 API 依赖。
- **CDP 会话桥接架构 (CDP Bridge Architecture)**：深度支持外部指纹浏览器（AdsPower、比特浏览器等）与本地已登录 Chrome 会话无缝接入，复用既有干净住宅网络与登录状态。
- **AI 代理原生集成规范 (Agentic Co-Pilot Protocol)**：仓库根目录内置工业级 `AGENTS.md` 交互契约，深度适配 Cursor、Claude Code、Windsurf 与 Gemini CLI，支持 AI 自主嗅探表单、装配适配器并完成闭环验证。

---

## 架构拓扑

```text
register-community/
├── core/
│   ├── adapter.py         # 抽象适配器基类 (BaseSiteAdapter) 与生命周期契约
│   ├── pilot.py           # 拟人按键动力学 (human_type) 与智能交互守门员
│   ├── stealth.py         # 原生环境自洽引擎与 CDP 会话桥接器
│   ├── inspector.py       # 启发式 DOM 表单探测器 (FormInspector)
│   ├── solver.py          # OpenCV 缺口匹配计算与贝塞尔轨迹规划器
│   ├── identity.py        # 高熵多国身份与合规密码发生器
│   ├── mail_client.py     # 毫秒级自适应退避邮箱轮询客户端
│   ├── runner.py          # 调度引擎、黑匣子快照抓取与原子台账持久化
│   ├── scaffold.py        # 适配器自动派生与预装配代码生成引擎
│   └── exporter.py        # Bitwarden、1Password、CSV 及 JSON 资产导出器
├── sites/                 # 业务与示范适配器池
│   ├── _template.py       # 工业级标准适配器实现模板
│   ├── demo_site.py       # 自包含离线 Base64 靶场适配器
│   ├── quanticdata.py     # OpenCV 拼图滑块与贝塞尔拖拽验证适配器
│   ├── huggingface.py     # SvelteKit 响应式受控表单适配器
│   ├── automation_exercise.py # 两步式向导交互基准适配器
│   ├── deno.py            # 现代云平台控制台交互适配器
│   ├── iteracompute.py    # 动态 SPA 与模态弹窗拦截治理适配器
│   └── milionseek.py      # 100milionseek 裂变母号接力与 Turnstile 过盾适配器
├── tests/                 # 单元与回归自动化测试套件
├── config.json            # 全局浏览器运行配置
├── pytest.ini             # Pytest 测试隔离规范
├── main.py                # 统一命令行调度入口
├── run.bat                # Windows 交互引导脚本 (CRLF 规范)
└── requirements.txt       # 核心生产依赖清单
```

---

## 内置示范适配器矩阵

AutoRegister 开箱内置 7 个涵盖不同前端框架与交互范式的经典实战适配器：

| 适配器标识 | 目标应用 | 技术实现特性 | 核心评测与验证重点 |
| :--- | :--- | :--- | :--- |
| `demo` | 内置离线 Base64 靶场 | 零配置全离线管道 | 快速环境自洽性与测试生命周期验证 |
| `quanticdata` | Quantic Data 服务 | OpenCV 缺口识别 + 贝塞尔动力学 | 拼图滑块挑战与现代响应式输入验证 |
| `huggingface` | Hugging Face 社区 | SvelteKit 受控组件状态流转 | 动态校验规则与软频控熔断防护 |
| `automation_exercise` | 标准测试基准靶场 | 多阶段向导向注册流 | 复杂下拉联动与多选框状态断言 |
| `deno` | Deno Console 平台 | 现代平台交互与状态探测 | 云开发者平台接入与动态状态验证 |
| `iteracompute` | IteraCompute 算力引擎 | 异步 SPA 与阻断弹窗自适应清理 | 显式条件等待 (Wait-for) 与视窗守门员 |
| `milionseek` | 100milionseek 平台 | 真实 Chrome 环境 + Turnstile 识别 + 母号接力池 | 邀请裂变自主轮换与 API Key 毫秒提取 |

---

## 快速上手

### 1. 环境准备与依赖安装

```bash
# 克隆仓库
git clone https://github.com/waw666waw666/register-community.git
cd register-community

# 安装 Python 生产依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器内核
playwright install chromium
```

### 2. 环境健康自检

检测宿主机运行环境、代理可用性以及依赖完整性：

```bash
python main.py doctor
```

### 3. 基线自测验证

执行内置离线靶场（无需外部网络或代理配置）：

```bash
python main.py --run demo
```

生成的凭据记录将自动以原子写入模式持久化至 `results/accounts.txt`。

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

AutoRegister 针对自主编程智能体进行了全流程规范定义。根目录的 `AGENTS.md` 约束以下核心纪律：

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

## 开源协议与合规声明

1. **开源协议**：本项目基于 [Apache License 2.0](LICENSE) 许可协议开源。
2. **合规安全声明**：本项目定位为**授权环境下的自动化流程验证、测试账号生命周期管理与 Web 技术工程研究工具**。使用者须严格遵守相关法律法规及目标站点的服务条款 (Terms of Service)。严禁将本项目用于未经授权的渗透探测、欺诈或破坏性活动。滥用后果由使用者自行承担，与项目作者无关。
