# 视窗几何与自适应防切割规范 (Window Calibration & Anti-Clipping Spec)

## 1. 核心原则：等比缩放 vs 物理切割

在多窗口并发（双并发、四并发等）场景下，窗口宽度由全屏 1920px 缩窄为 960px（或更窄）。

| 处理方式 | 现象与后果 | 判定 |
| :--- | :--- | :--- |
| **物理切割 (Clipping)** | 容器保持绝对像素宽度（如 1200px），超出视口部分被直接截断。**右侧/底部的“同意服务条款勾选框”、“确认提交按钮”被裁切消失**（如历史 Echo 注册站点故障）。 | ❌ 严禁使用 |
| **等比缩放 (Anti-Clipping Zoom)** | 注入 CSS `zoom`（双并发 85%，四并发 62%）配合 `max-width: 100%`。**全站元素等比缩小，弹窗、协议勾选框、提交键 100% 同屏完整展现**。 | ✅ 标准规范 |

---

## 2. 并发布局与几何决策矩阵

针对 1920×1080 屏幕标准：

> **⚠️ 并发前提**：默认单并发（用单窗口）。下表的双/四并发布局**仅在用户明确要求并发时**才使用（见 `AGENTS.md` 第 0.2 条）。

| 模式 | 窗口规格 (宽×高) | 屏幕布局与坐标 (X, Y) | 默认缩放 (Zoom) | 适用场景 |
| :--- | :--- | :--- | :--- | :--- |
| **双并发** (`-c 2`) | 960 × 1040 | 左：`(0, 0)`<br>右：`(960, 0)` | **85%** (`0.85`) | 左右并排全屏铺满，纵向保留 40px 任务栏。**需用户明确要求并发时才用**。 |
| **四并发** (`-c 4`) | 960 × 520 | 左上：`(0, 0)`，右上：`(960, 0)`<br>左下：`(0, 520)`，右下：`(960, 520)` | **62%** (`0.62`) | 黄金四分屏。4 通道流水线并行，全屏 0 缝隙 0 溢出。**需用户明确要求并发时才用**。 |
| **单窗口** (`-c 1` / 不带 `-c`) | 960 × 560 | 居中偏左：`(100, 80)` | **85%** (`0.85`) | **默认模式**。单账号逻辑调试与元素探测。 |

---

## 3. 防切割技术标准 (Anti-Clipping Rules)

所有浏览器适配器在初始化时，通过 `install_compact_context_layout`（或由 `Runner` 自动透传标定 `zoom`）注入防切割规则。**严禁写死静态 CSS `html { zoom: ... !important; }`**（会导致运行期浮窗 `-`/`+` 按钮点不动），统一通过行内动态属性设置：

```javascript
// 1. 动态写入根元素缩放（与顶部浮窗 window.__AGENT_TARGET_ZOOM 实时联动）
document.documentElement.style.setProperty('zoom', String(z), 'important');
```

```css
/* 2. 禁止横向溢出裁切，穿透弹窗、协议框与表单容器 */
body { overflow-x: auto !important; max-width: 100vw !important; }
.auth-pane, .auth-card, .login-shell, .cl-card, .cl-rootBox,
[role="dialog"], .modal, .modal-content, .agreement-modal,
form, .form-container, .terms-box {
    max-width: 100% !important;
    box-sizing: border-box !important;
}

/* 3. 关键交互组件强制自适应不被裁剪 */
button[type="submit"], .submit-btn, .agree-btn, .terms-checkbox,
input[type="checkbox"], label {
    max-width: 100% !important;
    white-space: normal !important;
}
```

---

## 4. 调试与标定操作命令

### 4.1 CLI 命令
```bash
# 一键自动适配本机屏幕（读取 SPI_GETWORKAREA，生成 1/2/4双态/6双态 6 套对称无溢出预设）
python main.py --auto-layout
python main.py --auto-layout --quad-mode edge_2x2 --hex-mode wing_cascade  # 顺手切换 4/6 并发默认形态

# 全景轮播巡演验收全部 6 套布局
python main.py --demo-layouts --demo-sec 8

# 交互式微调标定（仅当对自动布局有个性化偏好时使用）
python main.py --calibrate                                                 # 单窗口 (默认)
python main.py --calibrate -c 2                                            # 双并发左右并排
python main.py --calibrate -c 4                                            # 四分屏联动
python main.py --calibrate -c 6                                            # 六并发联动
python main.py --calibrate -c 2 --calibrate-url https://echo.tracerml.ai/login
```

### 4.2 独立批处理
双击运行根目录 [`calibrate.bat`](../calibrate.bat)，直接通过交互菜单一键自动适配屏幕、巡演 6 套布局、切换 4/6 并发形态或进入交互微调。

### 4.3 极速冷启优化
* **0 延迟本地靶场**：未指定 URL 时，标定器直接加载本地内置演练画布（包含邮箱、密码、同意条款与提交按钮），0.05 秒秒启；
* **极速内核参数**：禁用无关扩展、组件更新与后台网络探测（`--disable-extensions`, `--disable-background-networking`），启动耗时降低 75%。

---

## 5. 配置文件契约 (`config/window_geometry.json`)

标定器点击【💾 锁定并保存此配置】后，自动将几何参数与缩放比持久化：

```json
{
  "calibrated_at": "2026-11-02 22:38:00",
  "screen_resolution": "1920x1080",
  "total_slots": 2,
  "slots": [
    {
      "slot": 0,
      "name": "左窗口 (#1)",
      "x": 0,
      "y": 0,
      "width": 960,
      "height": 1040,
      "zoom": 0.85,
      "dpr": 1.0
    },
    {
      "slot": 1,
      "name": "右窗口 (#2)",
      "x": 960,
      "y": 0,
      "width": 960,
      "height": 1040,
      "zoom": 0.85,
      "dpr": 1.0
    }
  ],
  "dual_concurrency": {
    "total_slots": 2,
    "slots": [ ... ]
  }
}
```

### 5.0 读入上下界

`get_window_layout` / `get_window_geometry` 读这份 JSON 时，坏字段只换那一项。宽高 200–3840，坐标 −200–8000，缩放 0.4–1.5，dpr 1.0–3.0。整份 JSON 损坏时用默认布局，并且不写回。上面的 960×1040 / 520、缩放 0.85 / 0.62 都在界内。

### 5.1 业务代码调用
```python
from core.calibrator import get_window_geometry, get_window_layout
from core.browser import install_compact_context_layout

# 获取第 slot_id 个通道的几何与缩放
layout = get_window_layout(slot_id=0, total_slots=2)
# layout 包含: {'x': 0, 'y': 0, 'width': 960, 'height': 1040, 'zoom': 0.85, 'dpr': 1.0}

# 注入 BrowserContext 底层，路由跳转时始终保持该缩放比
install_compact_context_layout(context, zoom=layout["zoom"])
```

---

## 6. Windows DPI 缩放与动态真缩放实测规范

1. **Windows DPI 逻辑坐标适配**：Chrome `--window-position` / `--window-size` 吃的是系统逻辑坐标（DIP）。例如物理 `1920×1080` 在 **125% 系统缩放**下实际工作区为 `1536×864`，且 Win11 窗口左右含约 `6px` 隐形边框：
   - **1 并发（全屏）**：`single_concurrency` `(-6, 0, 1548, 871)`，默认等比缩放 **`85%`**；
   - **2 并发（左右两侧全高）**：`dual_concurrency` 左 `(-6, 0, 782, 871)` / 右 `(762, 0, 782, 871)`，默认等比缩放 **`85%`**；
   - **4 并发（双形态可一键切换 `quad_mode`）**：
     - **`edge_2x2`（形式一：满屏贴边无重叠）**：`(-6, 0)` / `(762, 0)` / `(-6, 432)` / `(762, 432)`，每窗 `782×439`，综合缩放 **`68%`**；
     - **`centered_compact`（形式二：对称留白居中紧凑微叠，鼠标行程更短）**：`(78, 64)` / `(678, 64)` / `(78, 360)` / `(678, 360)`，每窗 `780×440`，综合缩放 **`70%`**。
   - **6 并发（双形态可一键切换 `hex_mode`）**：
     - **`edge_3x2`（形式一：满屏六宫格无重叠）**：上排 `(-6, 0)` / `(506, 0)` / `(1018, 0)`，下排 `(-6, 432)` / `(506, 432)` / `(1018, 432)`，每窗 `526×440`，综合缩放 **`59%`**；
     - **`wing_cascade`（形式二：左右对称八字展翼阶梯）**：上 `(114, 24)` / `(762, 24)`、中 `(54, 204)` / `(822, 204)`、下 `(-6, 384)` / `(882, 384)`，每窗 **`660×480`** 大视野、`65%` 缩放，按上→中→下顺序启动，6 条标题/状态栏全裸露、0 切边 0 溢出。
2. **禁止 CSS `html { zoom: ... !important; }` 死锁**：
   - 底层 `install_compact_context_layout` 与顶部浮窗 `_render_floating_banner` 统一读写 `window.__AGENT_TARGET_ZOOM`（并持久化到 `sessionStorage.__agent_zoom`），通过 `document.documentElement.style.setProperty('zoom', z, 'important')` 真实生效；
   - 浮窗自身反向缩放（`1 / z`）保持按钮清晰，点击 `-` / `+` 按 `5%` 步进（`50% ~ 125%`，精准覆盖 `80% / 85% / 90%`）实时生效，同时监听 `window.devicePixelRatio / os_dpi` 联动浏览器原生缩放，杜绝“假标签”。
3. **四步开箱适配与巡演验收法（适用于交付与任意分辨率机型复用）**：
   - **第零步：跨分辨率一键开箱适配（推荐首选）**：运行 `python main.py --auto-layout`，自动读取本机 Windows `SPI_GETWORKAREA` 逻辑工作区（自动扣除任务栏并换算 DPI），按比例一次性生成 `1全屏 / 2左右 / 4双形态 / 6双形态` 对称无溢出预设与自适应 `zoom`，写入 `config/window_geometry.json`；
   - **第一步：一键全景巡演验收**：运行 `python main.py --demo-layouts`（可选 `--demo-sec 10`），自动在桌面依次轮播 `1 / 2 / 4(双形态) / 6(双形态)` 全部预设并带倒计时，所见即所得确认；
   - **第二步：交互式微调标定（仅当有个性化偏好时）**：运行 `python main.py --calibrate -c 2` / `-c 4` / `-c 6`，用 Windows Snap 或手动拖出理想位置并调好综合缩放，点击【💾 锁定并保存】合并写入 `config/window_geometry.json`；
   - **第三步：运行期自动透传生效**：`Runner` 启动有头浏览器（含单并发、多并发与预热浏览器池）时，自动按 `get_window_layout(slot_id, total_slots)` 同时下发 `(x, y, width, height)` 与 `zoom` 给 `install_compact_context_layout` 及 `inject_floating_banner`，子项目适配器无需再硬编码 `zoom=0.85`。
