# ⚡ AutoRegister

<p align="center">
  <a href="https://github.com/waw666waw666/register-community/blob/master/LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg" alt="Python Version"></a>
  <a href="https://playwright.dev/"><img src="https://img.shields.io/badge/Playwright-Automated-orange.svg" alt="Playwright"></a>
  <a href="https://opencv.org/"><img src="https://img.shields.io/badge/OpenCV-CV%20Slider-red.svg" alt="OpenCV"></a>
  <a href="https://bitwarden.com/"><img src="https://img.shields.io/badge/Export-Bitwarden%2FCSV-teal.svg" alt="Bitwarden Export"></a>
</p>

<p align="center">
  <b>现代化无头账号生命周期测试与受控表单自动化框架</b><br>
  <i>The Modern Headless Account Lifecycle & Form Automation Framework for Humans & AI Agents</i>
</p>

---

## 🌟 为什么选择 AutoRegister？ (Why AutoRegister?)

在现代 Web 自动化中，传统脚本常常面临三大痛点：**Cloudflare Turnstile 验证码死循环**、**抢夺桌面焦点弹窗打扰用户**、以及**复杂的受控表单校验不触发**。

AutoRegister 从底层重构了自动化工程范式：
- 🛡️ **官方真机环境自洽**：通过原生 Chrome + 自动化标志擦除，保持 JavaScript `[native code]` 原型链纯净，让 Turnstile 与 reCAPTCHA **自然通过，绝不循环刷新**。
- 🧩 **离线 OpenCV 智能滑块**：自研边缘特征匹配算子 + 贝塞尔曲线拟人手抖拖拽，无需对接付费第三方打码平台。
- 🌐 **专业指纹浏览器 (CDP) 降维接入**：一键无缝连接 AdsPower、比特浏览器 (BitBrowser) 或日常 Chrome，直接继承干净住宅 IP 与真实会话。
- 🤖 **AI 编程副驾驶就绪**：内置工业级 `AGENTS.md`，深度适配 Cursor / Claude Code / Windsurf / Gemini，让 AI 自动嗅探表单、编写适配器并闭环实跑！

---

## 🎮 内置示范适配器矩阵 (Showcase Adapters)

项目开箱内置了 **6 大涵盖不同技术栈与防护形态的经典实战适配器**：

| 适配器标识 | 目标站点 | 核心技术方案 | 场景与价值 |
| :--- | :--- | :--- | :--- |
| **`demo`** | 内置离线 Base64 靶场 | 0 配置离线执行 | 30 秒极速体验流水线，验证环境完备度 |
| **`quanticdata`** | Quantic Data 注册页 | OpenCV 缺口匹配 + 贝塞尔拖拽 | 拼图滑块挑战、现代响应式受控表单 |
| **`huggingface`** | Hugging Face 社区 | SvelteKit 受控组件 + 软频控熔断 | 开发者平台注册、动态表单状态流转 |
| **`automation_exercise`** | 国际自动化基准靶场 | 两步向导向交互 + 真实断言 | 标准两步式向导、多选框与下拉级联 |
| **`deno`** | Deno Deploy 控制台 | Turnstile 监听 + 状态探测 | 现代云开发平台认证入口与状态探测 |
| **`iteracompute`** | IteraCompute 算力平台 | 动态 SPA + Cookie 弹窗自适应 | 弹窗阻断清理、显式条件等待 (Wait-for) |

---

## ⚡ 30 秒极速开箱体验 (Quick Start)

### 1. 克隆与安装依赖
```bash
git clone https://github.com/waw666waw666/register-community.git
cd register-community

# 安装 Python 依赖（支持 OpenCV 智能识图与 Playwright）
pip install -r requirements.txt

# 初始化系统浏览器内核
playwright install chromium
```

### 2. 环境健康自检
```bash
python main.py doctor
```

### 3. 一键实跑内置离线靶场（无需联网，0 成本验证）
```bash
python main.py --run demo
```
> 运行完毕后，生成的凭据将自动以标准结构持久化保存至 `results/accounts.txt`。

---

## 🛠️ 核心功能与用法 (Features & CLI)

### 1. 查看所有已装载的适配器
```bash
python main.py --list
```

### 2. 自动化脚手架：输入网址，1 秒装配生产级代码！
无需手动去浏览器 F12 抄写选择器，AutoRegister 能够深度探测页面输入框并自动注入脚手架：
```bash
# 智能探测目标网页并生成适配器骨架
python main.py --scaffold mysite --url https://target-service.com/signup

# 立即实跑测试
python main.py --run mysite
```

### 3. 连接外部指纹浏览器 (AdsPower / BitBrowser / CDP)
无需处理复杂的硬件指纹对抗，直接连入已配置好住宅 IP 与登录态的外部浏览器：
```bash
python main.py --run demo --cdp http://127.0.0.1:9222
```

### 4. 密码库资产一键导出 (Bitwarden / CSV / JSON)
批量测试产生的账号资产，可一键转换为密码管理器专用导入包：
```bash
# 导出为 Bitwarden / 1Password 官方兼容 JSON
python main.py export --export bitwarden

# 导出为通用 CSV
python main.py export --export csv
```

---

## 📁 优雅架构目录树 (Architecture)

```text
register-community/
├── core/                  # 核心工程底座
│   ├── adapter.py         # BaseSiteAdapter 抽象基类与生命周期规范
│   ├── pilot.py           # 拟人按键抖动 (human_type) 与智能勾选 (smart_agree_tos)
│   ├── stealth.py         # 真机环境自洽引擎与 CDP 桥接
│   ├── inspector.py       # 启发式表单字段探测器 (FormInspector Pro)
│   ├── solver.py          # OpenCV 拼图边缘计算与三次贝塞尔拟人轨迹生成器
│   ├── identity.py        # 多国合规高熵身份与强密码发生器
│   ├── mail_client.py     # 毫秒级自适应邮箱轮询中枢
│   ├── runner.py          # 调度引擎、黑匣子快照抓取与原子台账
│   ├── scaffold.py        # 1 秒自动化脚手架代码生成引擎
│   └── exporter.py        # Bitwarden / CSV / JSON 密码库格式化导出器
├── sites/                 # 业务适配器池
│   ├── _template.py       # 标准工业级适配器开发模板
│   ├── demo_site.py       # 离线 Base64 嵌入式自包含靶场
│   ├── quanticdata.py     # OpenCV 拼图滑块与贝塞尔拖拽实战
│   ├── huggingface.py     # Hugging Face 受控表单适配器
│   ├── automation_exercise.py # 两步法向导基准测试
│   ├── deno.py            # Deno Deploy 状态探测适配器
│   └── iteracompute.py    # 动态 SPA 与弹窗治理适配器
├── tests/                 # 自动化单元测试套件 (Pytest 100% 全绿)
├── config.json            # 全局浏览器运行配置
├── pytest.ini             # 测试收集隔离配置
├── main.py                # 统一命令行调度入口
├── run.bat                # Windows 极简交互式启动脚本 (CRLF)
└── requirements.txt       # 项目依赖清单
```

---

## 🤖 AI 编程助手专属交互契约 (`AGENTS.md`)

本项目深度拥抱 AI 原生开发，仓库根目录自带专属 [AGENTS.md](AGENTS.md)：
- **零提示词盲测流水线**：在 Cursor / Claude Code 中直接给 AI 一个网址，AI 会自动探测、编写适配器并本地实跑验证！
- **五段式高信号汇报**：拒绝长篇大论的废话，以资深工程师的视角直接交付【实现过程】【这是个啥站】【当前进度】【卡点归因】【需要你做什么】。
- **高压防假成功断言**：禁止无脑 sleep，必须硬性断言 URL 跳转或真实 Token，杜绝“代码没报错就假装成功”的恶习。

---

## 📜 开源协议与合规免责声明 (License & Disclaimer)

1. **开源协议**：本项目基于 [Apache License 2.0](LICENSE) 许可协议开源，保留署名，商业友好。
2. **合规声明**：本项目定位为**授权环境下的 Web 表单自动化测试工具、测试账号生命周期管理与工程逆向学术研究**。请使用者严格遵守目标网站的服务条款（Terms of Service）及相关法律法规。严禁将本项目用于未经授权的网络渗透、黑灰产套利或任何非法破坏活动。因滥用本项目产生的一切后果由使用者自行承担，与本项目作者无关。
