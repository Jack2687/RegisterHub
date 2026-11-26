"""交互式视窗几何标定与自适应防切割微调引擎 (Window Calibrator & Anti-Clipping Engine)。

定位与核心价值：
1. 解决多窗口并发（双并发左右分屏、四分屏等）因视口宽度窄化导致“按钮被裁切、同意协议勾选框丢失”的痛点；
2. 彻底放弃无意义物理截断，全面采用 CSS 等比缩放 (zoom) + 弹性盒自适应容器，确保 100% 同屏完整展现；
3. 极速秒级启动：禁用无用扩展与背景网络，无 URL 传入时使用纯本地极速 Canvas，彻底杜绝外网超时卡顿；
4. 支持单窗口微调、双并发（左右两屏全高 960x1040）、四分屏（960x520）联动标定，实时动态调节缩放比并持久化。
"""

from __future__ import annotations

import json
import sys
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

try:
    from .ua import desktop_chrome_ua
except ImportError:
    def desktop_chrome_ua() -> str:
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/134.0.0.0 Safari/537.36"
        )

from .locks import atomic_write_text_safe, interprocess_file_lock, load_json_dict_strict


@contextmanager
def _managed_playwright(factory, browsers, unregister):
    """在 Playwright 停止前关闭已登记浏览器，异常时也不丢进程白名单。"""
    with factory() as playwright:
        try:
            yield playwright
        finally:
            for browser in browsers:
                try:
                    browser.close()
                except Exception:
                    pass
                else:
                    unregister(browser)


def build_zoom_init_script(zoom: float = 0.85) -> str:
    """生成根级动态 zoom 脚本；Playwright 仅在实际启动标定时延迟导入。"""
    z = round(max(0.4, min(1.5, float(zoom))), 2)
    return f"""
    (() => {{
        const DEFAULT_ZOOM = {z};
        function getTargetZoom() {{
            if (typeof window.__AGENT_TARGET_ZOOM === 'number' && window.__AGENT_TARGET_ZOOM >= 0.4 && window.__AGENT_TARGET_ZOOM <= 1.5) return window.__AGENT_TARGET_ZOOM;
            try {{
                const saved = parseFloat(sessionStorage.getItem('__agent_zoom') || '');
                if (!isNaN(saved) && saved >= 0.4 && saved <= 1.5) {{ window.__AGENT_TARGET_ZOOM = saved; return saved; }}
            }} catch (e) {{}}
            window.__AGENT_TARGET_ZOOM = DEFAULT_ZOOM;
            return DEFAULT_ZOOM;
        }}
        function enforce() {{
            const cur = String(getTargetZoom());
            if (document.documentElement && document.documentElement.style.zoom !== cur) document.documentElement.style.setProperty('zoom', cur, 'important');
        }}
        enforce();
        document.addEventListener('DOMContentLoaded', enforce);
        window.addEventListener('load', enforce);
    }})();
    """

DEFAULT_GEOMETRY_PATH = Path("config/window_geometry.json")
_OS_DPI_CACHE: float | None = None


def _geometry_locked(save_arg_index: int):
    """把窗口几何的完整读改写包进跨进程锁。"""
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            save_path = kwargs.get("save_path")
            if save_path is None and len(args) > save_arg_index:
                save_path = args[save_arg_index]
            target = Path(save_path) if save_path else DEFAULT_GEOMETRY_PATH
            with interprocess_file_lock(target, timeout=10.0):
                return fn(*args, **kwargs)
        return wrapped
    return decorator


def _get_os_dpi_scale() -> float:
    """获取当前操作系统基础显示缩放比（如 Windows 125% 返回 1.25），幂等缓存避免二次调用失真。"""
    global _OS_DPI_CACHE
    if _OS_DPI_CACHE is not None:
        return _OS_DPI_CACHE
    if sys.platform != "win32":
        _OS_DPI_CACHE = 1.0
        return 1.0
    try:
        import ctypes
        user32 = ctypes.windll.user32
        w_before = user32.GetSystemMetrics(0)
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
        # Per-Monitor DPI Aware 生效后，GetDpiForSystem() 直接给出真实主屏 DPI (96/120/144/192...)
        try:
            dpi_val = int(user32.GetDpiForSystem())
            if 96 <= dpi_val <= 288:
                _OS_DPI_CACHE = round(dpi_val / 96.0, 2)
                return _OS_DPI_CACHE
        except Exception:
            pass
        w_after = user32.GetSystemMetrics(0)
        if w_before > 0 and w_after >= w_before:
            scale = round(w_after / w_before, 2)
            if 1.0 <= scale <= 3.0:
                _OS_DPI_CACHE = scale
                return _OS_DPI_CACHE
    except Exception:
        pass
    _OS_DPI_CACHE = 1.0
    return 1.0


def get_logical_work_area() -> dict[str, Any]:
    """读取本机屏幕逻辑工作区 (Chrome --window-position / --window-size 所用的 DIP 坐标)。

    Windows 下通过 SPI_GETWORKAREA 自动扣除任务栏，并根据进程当前 DPI 感知状态精确换算为逻辑坐标；
    非 Windows 环境安全回退到 1920x1040 @ 1.0。
    """
    dpi = _get_os_dpi_scale()
    if sys.platform != "win32":
        return {
            "left": 0, "top": 0, "width": 1920, "height": 1040,
            "dpi": dpi, "phys_width": 1920, "phys_height": 1080,
        }
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        SPI_GETWORKAREA = 0x0030
        ok = user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
        sm_w = int(user32.GetSystemMetrics(0))
        sm_h = int(user32.GetSystemMetrics(1))
        if not ok or rect.right <= rect.left or rect.bottom <= rect.top:
            rect.left, rect.top, rect.right, rect.bottom = 0, 0, sm_w or 1920, sm_h or 1080

        # _get_os_dpi_scale 已切入 DPI Aware，此时 SPI_GETWORKAREA 与 GetSystemMetrics 为物理像素；
        # 换算为逻辑 DIP 坐标（若 DPI=1.0 则等价不变）。
        raw_left, raw_top, raw_right, raw_bottom = rect.left, rect.top, rect.right, rect.bottom
        phys_w = sm_w or (raw_right - raw_left)
        phys_h = sm_h or (raw_bottom - raw_top)
        log_left = int(round(raw_left / dpi))
        log_top = int(round(raw_top / dpi))
        log_w = max(640, int(round((raw_right - raw_left) / dpi)))
        log_h = max(480, int(round((raw_bottom - raw_top) / dpi)))
        return {
            "left": log_left,
            "top": log_top,
            "width": log_w,
            "height": log_h,
            "dpi": dpi,
            "phys_width": phys_w,
            "phys_height": phys_h,
        }
    except Exception:
        return {
            "left": 0, "top": 0, "width": 1920, "height": 1040,
            "dpi": dpi, "phys_width": 1920, "phys_height": 1080,
        }


def _calc_auto_zoom(win_w: int, win_h: int, total_slots: int) -> float:
    """按窗口逻辑宽高与并发档位计算防切割等比缩放比 (受界夹紧，避免按钮裁切或过小)。"""
    if total_slots <= 1:
        z = min(win_w / 1800.0, win_h / 1020.0)
        return round(max(0.75, min(1.0, z)), 2)
    if total_slots == 2:
        z = min(win_w / 920.0, win_h / 1020.0)
        return round(max(0.70, min(1.0, z)), 2)
    if total_slots <= 4:
        z = min(win_w / 1120.0, win_h / 630.0)
        return round(max(0.55, min(0.95, z)), 2)
    z = min(win_w / 890.0, win_h / 730.0)
    return round(max(0.50, min(0.90, z)), 2)


def build_auto_layout(
    work_w: int,
    work_h: int,
    work_x: int = 0,
    work_y: int = 0,
    dpi: float = 1.0,
    phys_w: int = 0,
    phys_h: int = 0,
    quad_mode: str = "centered_compact",
    hex_mode: str = "wing_cascade",
    frame_pad: int = 6,
    bottom_pad: int = 7,
) -> dict[str, Any]:
    """基于逻辑工作区 (work_x, work_y, work_w, work_h) 纯数学生成 6 套对称无溢出布局预设。

    包含：
      - 1 并发：全屏满屏 (single_concurrency)
      - 2 并发：左右全高贴边 (dual_concurrency)
      - 4 并发双形态：贴满四边 (edge_2x2) + 对称留白居中紧凑 (centered_compact)
      - 6 并发双形态：贴满六宫格 (edge_3x2) + 左右镜像八字展翼 (wing_cascade)
    """
    W = max(640, int(work_w))
    H = max(480, int(work_h))
    X0 = int(work_x)
    Y0 = int(work_y)
    pad = max(0, int(frame_pad))
    bpad = max(0, int(bottom_pad))
    dpi_val = round(max(1.0, min(3.0, float(dpi))), 2)
    pw = int(phys_w) if phys_w > 0 else int(round(W * dpi_val))
    ph = int(phys_h) if phys_h > 0 else int(round(H * dpi_val))

    # 1 并发：全屏满屏
    s1_w = W + 2 * pad
    s1_h = H + bpad
    s1_z = _calc_auto_zoom(s1_w, s1_h, 1)
    single_slots = [
        {"slot": 0, "name": "单窗口 (全屏)", "x": X0 - pad, "y": Y0, "width": s1_w, "height": s1_h, "dpr": dpi_val, "zoom": s1_z}
    ]

    # 2 并发：左右贴边对称
    half_w = W // 2
    s2_w = half_w + 2 * pad + 2
    s2_h = H + bpad
    s2_z = _calc_auto_zoom(s2_w, s2_h, 2)
    dual_slots = [
        {"slot": 0, "name": "左窗口 (#1)", "x": X0 - pad, "y": Y0, "width": s2_w, "height": s2_h, "dpr": dpi_val, "zoom": s2_z},
        {"slot": 1, "name": "右窗口 (#2)", "x": X0 + half_w - pad, "y": Y0, "width": s2_w, "height": s2_h, "dpr": dpi_val, "zoom": s2_z},
    ]

    # 4 并发形式一：edge_2x2 严丝合缝贴满四边
    half_h = H // 2
    q1_w = half_w + 2 * pad + 2
    q1_h = half_h + bpad
    q1_z = _calc_auto_zoom(q1_w, q1_h, 4)
    quad_edge_slots = [
        {"slot": 0, "name": "左上 (#1)", "x": X0 - pad, "y": Y0, "width": q1_w, "height": q1_h, "dpr": dpi_val, "zoom": q1_z},
        {"slot": 1, "name": "右上 (#2)", "x": X0 + half_w - pad, "y": Y0, "width": q1_w, "height": q1_h, "dpr": dpi_val, "zoom": q1_z},
        {"slot": 2, "name": "左下 (#3)", "x": X0 - pad, "y": Y0 + half_h, "width": q1_w, "height": q1_h, "dpr": dpi_val, "zoom": q1_z},
        {"slot": 3, "name": "右下 (#4)", "x": X0 + half_w - pad, "y": Y0 + half_h, "width": q1_w, "height": q1_h, "dpr": dpi_val, "zoom": q1_z},
    ]

    # 4 并发形式二：centered_compact 四周对称留白居中紧凑微叠
    q2_w = max(320, min(W - 40, int(round(W * 0.508))))
    q2_h = max(260, min(H - 40, int(round(H * 0.509))))
    q2_mx = max(0, min((W - q2_w) // 2, int(round(W * 0.0508))))
    q2_my = max(0, min((H - q2_h) // 2, int(round(H * 0.0741))))
    q2_x_left = X0 + q2_mx
    q2_x_right = X0 + W - q2_mx - q2_w
    q2_y_top = Y0 + q2_my
    q2_y_bot = Y0 + H - q2_my - q2_h
    q2_z = _calc_auto_zoom(q2_w, q2_h, 4)
    quad_compact_slots = [
        {"slot": 0, "name": "左上 (#1)", "x": q2_x_left,  "y": q2_y_top, "width": q2_w, "height": q2_h, "dpr": dpi_val, "zoom": q2_z},
        {"slot": 1, "name": "右上 (#2)", "x": q2_x_right, "y": q2_y_top, "width": q2_w, "height": q2_h, "dpr": dpi_val, "zoom": q2_z},
        {"slot": 2, "name": "左下 (#3)", "x": q2_x_left,  "y": q2_y_bot, "width": q2_w, "height": q2_h, "dpr": dpi_val, "zoom": q2_z},
        {"slot": 3, "name": "右下 (#4)", "x": q2_x_right, "y": q2_y_bot, "width": q2_w, "height": q2_h, "dpr": dpi_val, "zoom": q2_z},
    ]

    # 6 并发形式一：edge_3x2 严丝合缝贴满六宫格
    col_w = W // 3
    h1_w = col_w + 2 * pad + 2
    h1_h = half_h + bpad + 1
    h1_z = _calc_auto_zoom(h1_w, h1_h, 6)
    hex_edge_slots = [
        {"slot": 0, "name": "上左 (#1)", "x": X0 - pad,             "y": Y0,          "width": h1_w, "height": h1_h, "dpr": dpi_val, "zoom": h1_z},
        {"slot": 1, "name": "上中 (#2)", "x": X0 + col_w - pad,     "y": Y0,          "width": h1_w, "height": h1_h, "dpr": dpi_val, "zoom": h1_z},
        {"slot": 2, "name": "上右 (#3)", "x": X0 + 2 * col_w - pad, "y": Y0,          "width": h1_w, "height": h1_h, "dpr": dpi_val, "zoom": h1_z},
        {"slot": 3, "name": "下左 (#4)", "x": X0 - pad,             "y": Y0 + half_h, "width": h1_w, "height": h1_h, "dpr": dpi_val, "zoom": h1_z},
        {"slot": 4, "name": "下中 (#5)", "x": X0 + col_w - pad,     "y": Y0 + half_h, "width": h1_w, "height": h1_h, "dpr": dpi_val, "zoom": h1_z},
        {"slot": 5, "name": "下右 (#6)", "x": X0 + 2 * col_w - pad, "y": Y0 + half_h, "width": h1_w, "height": h1_h, "dpr": dpi_val, "zoom": h1_z},
    ]

    # 6 并发形式二：wing_cascade 左右镜像对称八字阶梯展翼
    h2_w = max(320, min(W // 2, int(round(W * 0.430))))
    h2_h = max(260, min(H - 20, int(round(H * 0.5556))))
    y_top = Y0 + max(0, int(round(H * 0.028)))
    y_bot = Y0 + max(0, H - h2_h)
    if y_top > y_bot:
        y_top = y_bot
    y_mid = (y_top + y_bot) // 2
    x_l2 = X0 - pad
    max_step = max(0, (W // 2 - h2_w // 2 - (x_l2 - X0)) // 2)
    step = min(max_step, max(0, int(round(W * 0.0391))))
    x_l1 = x_l2 + step
    x_l0 = x_l2 + 2 * step
    # 右翼严格以工作区中心镜像对称：xr = 2*X0 + W - (xl + w)
    x_r0 = 2 * X0 + W - (x_l0 + h2_w)
    x_r1 = 2 * X0 + W - (x_l1 + h2_w)
    x_r2 = 2 * X0 + W - (x_l2 + h2_w)
    h2_z = _calc_auto_zoom(h2_w, h2_h, 6)
    hex_wing_slots = [
        {"slot": 0, "name": "左翼上 (#1)", "x": x_l0, "y": y_top, "width": h2_w, "height": h2_h, "dpr": dpi_val, "zoom": h2_z},
        {"slot": 1, "name": "右翼上 (#2)", "x": x_r0, "y": y_top, "width": h2_w, "height": h2_h, "dpr": dpi_val, "zoom": h2_z},
        {"slot": 2, "name": "左翼中 (#3)", "x": x_l1, "y": y_mid, "width": h2_w, "height": h2_h, "dpr": dpi_val, "zoom": h2_z},
        {"slot": 3, "name": "右翼中 (#4)", "x": x_r1, "y": y_mid, "width": h2_w, "height": h2_h, "dpr": dpi_val, "zoom": h2_z},
        {"slot": 4, "name": "左翼下 (#5)", "x": x_l2, "y": y_bot, "width": h2_w, "height": h2_h, "dpr": dpi_val, "zoom": h2_z},
        {"slot": 5, "name": "右翼下 (#6)", "x": x_r2, "y": y_bot, "width": h2_w, "height": h2_h, "dpr": dpi_val, "zoom": h2_z},
    ]

    q_mode = quad_mode if quad_mode in ("edge_2x2", "centered_compact") else "centered_compact"
    h_mode = hex_mode if hex_mode in ("edge_3x2", "wing_cascade") else "wing_cascade"
    active_quad = quad_edge_slots if q_mode == "edge_2x2" else quad_compact_slots

    dpi_pct = int(round(dpi_val * 100))
    return {
        "calibrated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "description": f"自动适配 {pw}x{ph} @ {dpi_pct}% DPI (逻辑工作区 {W}x{H}) 全家桶对称预设：1全屏 / 2左右 / 4双形态 / 6双形态",
        "screen_resolution": f"{pw}x{ph} ({dpi_pct}% -> {W}x{H})",
        "work_area": {"left": X0, "top": Y0, "width": W, "height": H},
        "total_slots": 4,
        "quad_mode": q_mode,
        "hex_mode": h_mode,
        "single_concurrency": {
            "total_slots": 1,
            "description": "1并发：全屏满屏",
            "slots": single_slots,
        },
        "dual_concurrency": {
            "total_slots": 2,
            "description": "2并发：左右两侧全高贴边 (对称无溢出)",
            "slots": dual_slots,
        },
        "quad_presets": {
            "edge_2x2": {
                "total_slots": 4,
                "description": "4并发形式一：严丝合缝贴满四边 (0缝隙0重叠)",
                "slots": quad_edge_slots,
            },
            "centered_compact": {
                "total_slots": 4,
                "description": "4并发形式二：四周对称留白居中紧凑微叠 (鼠标行程短)",
                "slots": quad_compact_slots,
            },
        },
        "hex_presets": {
            "edge_3x2": {
                "total_slots": 6,
                "description": "6并发形式一：严丝合缝贴满六宫格 (0缝隙0重叠)",
                "slots": hex_edge_slots,
            },
            "wing_cascade": {
                "total_slots": 6,
                "description": "6并发形式二：左右镜像对称八字阶梯展翼 (大视野、0切边、状态栏全裸露)",
                "slots": hex_wing_slots,
            },
        },
        "slots": active_quad,
        "x": single_slots[0]["x"],
        "y": single_slots[0]["y"],
        "width": single_slots[0]["width"],
        "height": single_slots[0]["height"],
        "zoom": single_slots[0]["zoom"],
        "dpr": dpi_val,
    }


@_geometry_locked(0)
def auto_generate_layout(
    save_path: Path | str | None = None,
    quad_mode: str | None = None,
    hex_mode: str | None = None,
) -> dict[str, Any]:
    """读取本机 SPI_GETWORKAREA 逻辑工作区，一键生成并原子持久化 6 套对称无溢出布局预设。"""
    target_path = Path(save_path) if save_path else DEFAULT_GEOMETRY_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # 若已有配置，保留用户的 quad_mode / hex_mode 形态偏好
    existing_q_mode = "centered_compact"
    existing_h_mode = "wing_cascade"
    if target_path.exists():
        old = load_json_dict_strict(target_path)
        if old.get("quad_mode") in ("edge_2x2", "centered_compact"):
            existing_q_mode = old["quad_mode"]
        if old.get("hex_mode") in ("edge_3x2", "wing_cascade"):
            existing_h_mode = old["hex_mode"]

    wa = get_logical_work_area()
    payload = build_auto_layout(
        work_w=wa["width"],
        work_h=wa["height"],
        work_x=wa["left"],
        work_y=wa["top"],
        dpi=wa["dpi"],
        phys_w=wa["phys_width"],
        phys_h=wa["phys_height"],
        quad_mode=quad_mode or existing_q_mode,
        hex_mode=hex_mode or existing_h_mode,
        frame_pad=6 if sys.platform == "win32" else 0,
        bottom_pad=7 if sys.platform == "win32" else 0,
    )

    atomic_write_text_safe(target_path, json.dumps(payload, indent=2, ensure_ascii=False))

    print("\n" + "=" * 68)
    print(f"🖥️  [跨分辨率自动布局生成器] 已适配本机屏幕: {payload['screen_resolution']}")
    print("=" * 68)
    print(f"  逻辑工作区 : origin=({wa['left']}, {wa['top']}) | {wa['width']} × {wa['height']} (DPI {int(round(wa['dpi'] * 100))}%)")
    print(f"  配置文件   : {target_path}")
    print(f"  1 并发全屏 : {payload['single_concurrency']['slots'][0]['width']}×{payload['single_concurrency']['slots'][0]['height']} | zoom={int(payload['single_concurrency']['slots'][0]['zoom']*100)}%")
    print(f"  2 并发左右 : {payload['dual_concurrency']['slots'][0]['width']}×{payload['dual_concurrency']['slots'][0]['height']} ×2 | zoom={int(payload['dual_concurrency']['slots'][0]['zoom']*100)}%")
    q_e = payload["quad_presets"]["edge_2x2"]["slots"][0]
    q_c = payload["quad_presets"]["centered_compact"]["slots"][0]
    print(f"  4 并发双态 : edge_2x2 ({q_e['width']}×{q_e['height']}, {int(q_e['zoom']*100)}%) / centered_compact ({q_c['width']}×{q_c['height']}, {int(q_c['zoom']*100)}%) [当前: {payload['quad_mode']}]")
    h_e = payload["hex_presets"]["edge_3x2"]["slots"][0]
    h_w = payload["hex_presets"]["wing_cascade"]["slots"][0]
    print(f"  6 并发双态 : edge_3x2 ({h_e['width']}×{h_e['height']}, {int(h_e['zoom']*100)}%) / wing_cascade ({h_w['width']}×{h_w['height']}, {int(h_w['zoom']*100)}%) [当前: {payload['hex_mode']}]")
    print("-" * 68)
    print("  下一步验收 : 运行 python main.py --demo-layouts 巡演全套布局；如需微调再跑 --calibrate")
    print("=" * 68 + "\n", flush=True)
    return payload

# 极速秒启 Chrome 参数：屏蔽无关网络嗅探、扩展与后台任务
FAST_CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=Translate,OptimizationHints,MediaRouter",
    "--disable-extensions",
    "--disable-component-update",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-sync",
    "--disable-default-apps",
    "--no-first-run",
    "--no-default-browser-check",
    "--lang=en-US",
    "--no-proxy-server",
]

# 本地纯极速标定演示画布 (0 网络依赖，0.05 秒瞬启)
LOCAL_PLAYGROUND_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>视窗标定与防切割自适应演练</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #f8fafc;
            color: #0f172a;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 70px 16px 20px;
        }
        .container {
            width: 100%;
            max-width: 440px;
            background: #ffffff;
            border-radius: 12px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.06);
            padding: 24px;
            border: 1px solid #e2e8f0;
        }
        h2 { font-size: 18px; margin-bottom: 8px; color: #1e293b; }
        p.desc { font-size: 13px; color: #64748b; margin-bottom: 16px; line-height: 1.5; }
        .field { margin-bottom: 12px; }
        label { display: block; font-size: 12px; font-weight: 600; margin-bottom: 4px; color: #475569; }
        input[type="text"], input[type="password"] {
            width: 100%;
            padding: 8px 12px;
            border: 1px solid #cbd5e1;
            border-radius: 6px;
            font-size: 13px;
            outline: none;
        }
        .terms-row {
            display: flex;
            align-items: flex-start;
            gap: 8px;
            margin: 16px 0;
            padding: 8px 10px;
            background: #f1f5f9;
            border-radius: 6px;
            font-size: 12px;
            color: #334155;
        }
        .terms-row input { margin-top: 2px; }
        .submit-btn {
            width: 100%;
            background: #2563eb;
            color: #ffffff;
            border: none;
            padding: 10px;
            border-radius: 6px;
            font-weight: 600;
            font-size: 14px;
            cursor: pointer;
        }
        .notice-tag {
            display: inline-block;
            background: #dbeafe;
            color: #1e40af;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            margin-bottom: 8px;
        }
    </style>
</head>
<body>
    <div class="container">
        <span class="notice-tag">💡 防切割验证靶场</span>
        <h2>自动化注册视窗演示</h2>
        <p class="desc">若窗口宽度不足且未等比缩放，右侧或底部按钮会被物理切割。本标定器通过 CSS 缩放消除任何截断。</p>
        <div class="field">
            <label>注册邮箱 (Email)</label>
            <input type="text" value="user_demo@outlook.com" readonly>
        </div>
        <div class="field">
            <label>账号密码 (Password)</label>
            <input type="password" value="Password@123" readonly>
        </div>
        <div class="terms-row">
            <input type="checkbox" id="terms" checked>
            <label for="terms">我已阅读并同意《服务条款》与《隐私保护协议》（不可裁切关键组件）</label>
        </div>
        <button class="submit-btn" id="submit-demo">同意条款并立即注册 (Submit)</button>
    </div>
</body>
</html>
"""

# 并发多窗口预设模板 (针对主流 1920x1080 屏幕)
CONCURRENCY_PRESETS = {
    # 双并发：左右两屏各占一半 (960px 宽)，全高 1040px (留出 40px 任务栏)，默认缩放 85%
    2: [
        {"slot": 0, "name": "左窗口 (#1)", "color_start": "#059669", "color_end": "#10b981", "init_x": 0,   "init_y": 0, "init_w": 960, "init_h": 1040, "zoom": 0.85},
        {"slot": 1, "name": "右窗口 (#2)", "color_start": "#0284c7", "color_end": "#38bdf8", "init_x": 960, "init_y": 0, "init_w": 960, "init_h": 1040, "zoom": 0.85},
    ],
    # 四并发：适配 125% DPI 四分屏初值 (2列 × 2行，可自由拖动再锁定)
    4: [
        {"slot": 0, "name": "左上 (#1)", "color_start": "#059669", "color_end": "#10b981", "init_x": -6,  "init_y": 0,   "init_w": 782, "init_h": 439, "zoom": 0.75},
        {"slot": 1, "name": "右上 (#2)", "color_start": "#0284c7", "color_end": "#38bdf8", "init_x": 762, "init_y": 0,   "init_w": 782, "init_h": 439, "zoom": 0.75},
        {"slot": 2, "name": "左下 (#3)", "color_start": "#7c3aed", "color_end": "#a855f7", "init_x": -6,  "init_y": 432, "init_w": 782, "init_h": 439, "zoom": 0.75},
        {"slot": 3, "name": "右下 (#4)", "color_start": "#d97706", "color_end": "#f59e0b", "init_x": 762, "init_y": 432, "init_w": 782, "init_h": 439, "zoom": 0.75},
    ],
    # 六并发：适配 125% DPI 六宫格初值 (3列 × 2行，可自由拖动再锁定)
    6: [
        {"slot": 0, "name": "上左 (#1)", "color_start": "#059669", "color_end": "#10b981", "init_x": -6,   "init_y": 0,   "init_w": 524, "init_h": 439, "zoom": 0.65},
        {"slot": 1, "name": "上中 (#2)", "color_start": "#0284c7", "color_end": "#38bdf8", "init_x": 506,  "init_y": 0,   "init_w": 524, "init_h": 439, "zoom": 0.65},
        {"slot": 2, "name": "上右 (#3)", "color_start": "#2563eb", "color_end": "#60a5fa", "init_x": 1018, "init_y": 0,   "init_w": 524, "init_h": 439, "zoom": 0.65},
        {"slot": 3, "name": "下左 (#4)", "color_start": "#7c3aed", "color_end": "#a855f7", "init_x": -6,   "init_y": 432, "init_w": 524, "init_h": 439, "zoom": 0.65},
        {"slot": 4, "name": "下中 (#5)", "color_start": "#d97706", "color_end": "#f59e0b", "init_x": 506,  "init_y": 432, "init_w": 524, "init_h": 439, "zoom": 0.65},
        {"slot": 5, "name": "下右 (#6)", "color_start": "#db2777", "color_end": "#f472b6", "init_x": 1018, "init_y": 432, "init_w": 524, "init_h": 439, "zoom": 0.65},
    ],
}


#: 几何字段的合理区间（★ 清单第 36 条：不可信标量必须校验上下界，Rule 28）。
#: 值来自 config/window_geometry.json —— 可能被手改、被旧版本写坏、被外部工具覆盖。
_GEOM_BOUNDS = {
    "x": (-200, 8000),
    "y": (-200, 8000),
    "width": (200, 3840),
    "height": (200, 3840),
    "zoom": (0.4, 1.5),
    "dpr": (1.0, 3.0),
}


def _safe_int(value: Any, default: int, field: str) -> int:
    """把不可信值转成受界整数（★ 清单第 36 条）。

    旧行为是裸 `int(s.get("x", 0))`：一个字段写成 `"960px"` 就抛 ValueError，
    被外层 `except` 捕获后**整份配置作废**（其他 7 个好字段一起被丢弃）。
    现在逐字段兜底：转换失败或越界 → 只换这一项为默认值。
    """
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return default
    lo, hi = _GEOM_BOUNDS.get(field, (-10**9, 10**9))
    if v < lo or v > hi:
        return default
    return v


def _safe_float(value: Any, default: float, field: str) -> float:
    """把不可信值转成受界浮点（★ 清单第 36 条）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    lo, hi = _GEOM_BOUNDS.get(field, (-1e9, 1e9))
    if v < lo or v > hi:
        return default
    return v


def get_window_layout(
    slot_id: int = 0,
    total_slots: int = 1,
    config_path: Path | str | None = None,
) -> dict[str, Any]:
    """获取指定通道槽位的完整几何与等比缩放规范。
    
    返回字段：{"x": int, "y": int, "width": int, "height": int, "zoom": float, "dpr": float}
    """
    cfg_file = Path(config_path) if config_path else DEFAULT_GEOMETRY_PATH
    if cfg_file.exists():
        try:
            data = load_json_dict_strict(cfg_file)

            if total_slots <= 1 and "single_concurrency" in data:
                single_slots = data["single_concurrency"].get("slots", [])
                if single_slots:
                    s = single_slots[0]
                    return {
                        "x": _safe_int(s.get("x"), 0, "x"),
                        "y": _safe_int(s.get("y"), 0, "y"),
                        "width": _safe_int(s.get("width"), 1548, "width"),
                        "height": _safe_int(s.get("height"), 871, "height"),
                        "zoom": _safe_float(s.get("zoom"), 0.85, "zoom"),
                        "dpr": _safe_float(s.get("dpr"), 1.0, "dpr"),
                    }

            if total_slots == 2 and "dual_concurrency" in data:
                dual_slots = data["dual_concurrency"].get("slots", [])
                if len(dual_slots) > slot_id:
                    s = dual_slots[slot_id]
                    return {
                        "x": _safe_int(s.get("x"), 0, "x"),
                        "y": _safe_int(s.get("y"), 0, "y"),
                        "width": _safe_int(s.get("width"), 960, "width"),
                        "height": _safe_int(s.get("height"), 1040, "height"),
                        "zoom": _safe_float(s.get("zoom"), 0.85, "zoom"),
                        "dpr": _safe_float(s.get("dpr"), 1.0, "dpr"),
                    }

            if total_slots == 6 and "hex_concurrency" in data:
                hex_slots = data["hex_concurrency"].get("slots", [])
                if len(hex_slots) > slot_id:
                    s = hex_slots[slot_id]
                    return {
                        "x": _safe_int(s.get("x"), 0, "x"),
                        "y": _safe_int(s.get("y"), 0, "y"),
                        "width": _safe_int(s.get("width"), 524, "width"),
                        "height": _safe_int(s.get("height"), 439, "height"),
                        "zoom": _safe_float(s.get("zoom"), 0.65, "zoom"),
                        "dpr": _safe_float(s.get("dpr"), 1.25, "dpr"),
                    }

            if total_slots == 6 and "hex_presets" in data:
                h_mode = data.get("hex_mode", "wing_cascade")
                h_slots = (data["hex_presets"].get(h_mode) or {}).get("slots", [])
                if len(h_slots) > slot_id:
                    s = h_slots[slot_id]
                    return {
                        "x": _safe_int(s.get("x"), 0, "x"),
                        "y": _safe_int(s.get("y"), 0, "y"),
                        "width": _safe_int(s.get("width"), 660, "width"),
                        "height": _safe_int(s.get("height"), 480, "height"),
                        "zoom": _safe_float(s.get("zoom"), 0.65, "zoom"),
                        "dpr": _safe_float(s.get("dpr"), 1.25, "dpr"),
                    }

            if total_slots == 4 and "quad_concurrency" in data:
                quad_slots = data["quad_concurrency"].get("slots", [])
                if len(quad_slots) > slot_id:
                    s = quad_slots[slot_id]
                    return {
                        "x": _safe_int(s.get("x"), 0, "x"),
                        "y": _safe_int(s.get("y"), 0, "y"),
                        "width": _safe_int(s.get("width"), 782, "width"),
                        "height": _safe_int(s.get("height"), 439, "height"),
                        "zoom": _safe_float(s.get("zoom"), 0.70, "zoom"),
                        "dpr": _safe_float(s.get("dpr"), 1.25, "dpr"),
                    }

            if total_slots == 4 and "quad_presets" in data:
                q_mode = data.get("quad_mode", "edge_2x2")
                q_slots = (data["quad_presets"].get(q_mode) or {}).get("slots", [])
                if len(q_slots) > slot_id:
                    s = q_slots[slot_id]
                    return {
                        "x": _safe_int(s.get("x"), 0, "x"),
                        "y": _safe_int(s.get("y"), 0, "y"),
                        "width": _safe_int(s.get("width"), 782, "width"),
                        "height": _safe_int(s.get("height"), 439, "height"),
                        "zoom": _safe_float(s.get("zoom"), 0.70, "zoom"),
                        "dpr": _safe_float(s.get("dpr"), 1.25, "dpr"),
                    }

            slots = data.get("slots")
            if slots and isinstance(slots, list) and len(slots) >= total_slots and len(slots) > slot_id:
                s = slots[slot_id]
                return {
                    "x": _safe_int(s.get("x"), 0, "x"),
                    "y": _safe_int(s.get("y"), 0, "y"),
                    "width": _safe_int(s.get("width"), 960, "width"),
                    "height": _safe_int(s.get("height"), 520, "height"),
                    "zoom": _safe_float(s.get("zoom"), 0.62 if total_slots >= 4 else 0.85, "zoom"),
                    "dpr": _safe_float(s.get("dpr"), 1.0, "dpr"),
                }

            w = _safe_int(data.get("width"), 960, "width")
            h = _safe_int(data.get("height"), 520, "height")
            base_x = _safe_int(data.get("x"), 0, "x")
            base_y = _safe_int(data.get("y"), 0, "y")
            zoom = _safe_float(data.get("zoom"), 0.85 if total_slots <= 2 else 0.62, "zoom")
            dpr = _safe_float(data.get("dpr"), 1.0, "dpr")

            if total_slots <= 1:
                return {"x": base_x, "y": base_y, "width": w, "height": h, "zoom": zoom, "dpr": dpr}
            elif total_slots == 2:
                return {"x": slot_id * w, "y": base_y, "width": w, "height": h, "zoom": zoom, "dpr": dpr}
            else:
                return {"x": base_x + slot_id * w, "y": base_y, "width": w, "height": h, "zoom": zoom, "dpr": dpr}
        except Exception as e:
            print(f"[WARN] 视窗几何配置 {cfg_file} 解析失败，使用默认布局: {e}")

    # 默认未标定时的物理标准 (1920x1080 满屏对齐)
    if total_slots <= 1:
        return {"x": 100, "y": 80, "width": 960, "height": 560, "zoom": 0.85, "dpr": 1.0}
    elif total_slots == 2:
        return {"x": slot_id * 960, "y": 0, "width": 960, "height": 1040, "zoom": 0.85, "dpr": 1.0}
    elif total_slots == 3:
        return {"x": slot_id * 640, "y": 0, "width": 640, "height": 520, "zoom": 0.70, "dpr": 1.0}
    else:
        # 四并发：黄金四分屏
        col = slot_id % 2
        row = slot_id // 2
        return {"x": col * 960, "y": row * 520, "width": 960, "height": 520, "zoom": 0.62, "dpr": 1.0}


def get_window_geometry(
    slot_id: int = 0,
    total_slots: int = 1,
    config_path: Path | str | None = None,
    default_w: int = 960,
    default_h: int = 520,
) -> tuple[int, int, int, int]:
    """计算指定通道槽位的窗口坐标 (x, y, w, h)。保持原有向后兼容调用契约。"""
    cfg_file = Path(config_path) if config_path else DEFAULT_GEOMETRY_PATH
    if cfg_file.exists():
        try:
            data = load_json_dict_strict(cfg_file)

            if total_slots <= 1 and "single_concurrency" in data:
                single_slots = data["single_concurrency"].get("slots", [])
                if single_slots:
                    s = single_slots[0]
                    return (
                        _safe_int(s.get("x"), 0, "x"),
                        _safe_int(s.get("y"), 0, "y"),
                        _safe_int(s.get("width"), default_w, "width"),
                        _safe_int(s.get("height"), default_h, "height"),
                    )

            if total_slots == 2 and "dual_concurrency" in data:
                dual_slots = data["dual_concurrency"].get("slots", [])
                if len(dual_slots) > slot_id:
                    s = dual_slots[slot_id]
                    return (
                        _safe_int(s.get("x"), 0, "x"),
                        _safe_int(s.get("y"), 0, "y"),
                        _safe_int(s.get("width"), default_w, "width"),
                        _safe_int(s.get("height"), default_h, "height"),
                    )

            if total_slots == 6 and "hex_concurrency" in data:
                hex_slots = data["hex_concurrency"].get("slots", [])
                if len(hex_slots) > slot_id:
                    s = hex_slots[slot_id]
                    return (
                        _safe_int(s.get("x"), 0, "x"),
                        _safe_int(s.get("y"), 0, "y"),
                        _safe_int(s.get("width"), default_w, "width"),
                        _safe_int(s.get("height"), default_h, "height"),
                    )

            if total_slots == 6 and "hex_presets" in data:
                h_mode = data.get("hex_mode", "wing_cascade")
                h_slots = (data["hex_presets"].get(h_mode) or {}).get("slots", [])
                if len(h_slots) > slot_id:
                    s = h_slots[slot_id]
                    return (
                        _safe_int(s.get("x"), 0, "x"),
                        _safe_int(s.get("y"), 0, "y"),
                        _safe_int(s.get("width"), default_w, "width"),
                        _safe_int(s.get("height"), default_h, "height"),
                    )

            if total_slots == 4 and "quad_concurrency" in data:
                quad_slots = data["quad_concurrency"].get("slots", [])
                if len(quad_slots) > slot_id:
                    s = quad_slots[slot_id]
                    return (
                        _safe_int(s.get("x"), 0, "x"),
                        _safe_int(s.get("y"), 0, "y"),
                        _safe_int(s.get("width"), default_w, "width"),
                        _safe_int(s.get("height"), default_h, "height"),
                    )

            if total_slots == 4 and "quad_presets" in data:
                q_mode = data.get("quad_mode", "edge_2x2")
                q_slots = (data["quad_presets"].get(q_mode) or {}).get("slots", [])
                if len(q_slots) > slot_id:
                    s = q_slots[slot_id]
                    return (
                        _safe_int(s.get("x"), 0, "x"),
                        _safe_int(s.get("y"), 0, "y"),
                        _safe_int(s.get("width"), default_w, "width"),
                        _safe_int(s.get("height"), default_h, "height"),
                    )

            slots = data.get("slots")
            if slots and isinstance(slots, list) and len(slots) >= total_slots and len(slots) > slot_id:
                s = slots[slot_id]
                return (
                    _safe_int(s.get("x"), 0, "x"),
                    _safe_int(s.get("y"), 0, "y"),
                    _safe_int(s.get("width"), default_w, "width"),
                    _safe_int(s.get("height"), default_h, "height"),
                )

            w = _safe_int(data.get("width"), default_w, "width")
            h = _safe_int(data.get("height"), default_h, "height")
            base_x = _safe_int(data.get("x"), 0, "x")
            base_y = _safe_int(data.get("y"), 0, "y")

            if total_slots <= 1:
                return base_x, base_y, w, h
            elif total_slots <= 3:
                x = base_x + slot_id * w
                y = base_y
                return x, y, w, h
        except Exception as e:
            print(f"[WARN] 视窗几何配置 {cfg_file} 解析失败，使用默认布局: {e}")

    # 兜底计算 (测试与无配置文件 / 并发数超出已标定槽位数)
    if total_slots <= 1:
        return 100, 80, 960, 560
    elif total_slots == 2:
        return slot_id * 960, 0, 960, 520
    elif total_slots == 3:
        return slot_id * 640, 0, 640, 520
    elif total_slots == 4:
        col = slot_id % 2
        row = slot_id // 2
        return col * 960, row * 520, 960, 520
    else:
        col = slot_id % 3
        row = slot_id // 3
        return col * 640, row * 520, 640, 520


def calibrate_window(
    url: str = "",
    save_path: Path | str | None = None,
    total_windows: int = 1,
) -> dict[str, Any]:
    """极速启动交互式视窗标定工具（秒级冷启，支持 1 / 2 / 4 / 6 并发模式与防切割实时微调）。"""
    if total_windows not in (1, 2, 4, 6):
        raise ValueError(f"不支持的并发标定档位: {total_windows}（仅支持 1 / 2 / 4 / 6）")
    target_path = Path(save_path) if save_path else DEFAULT_GEOMETRY_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_url = url.strip()

    # 自动选定槽位配置
    if total_windows == 2:
        slots_cfg = CONCURRENCY_PRESETS[2]
        mode_desc = "双并发左右并排模式 (2 窗口并排全屏)"
    elif total_windows == 6:
        slots_cfg = CONCURRENCY_PRESETS[6]
        mode_desc = "六并发六宫格联动模式 (6 窗口标定)"
    elif total_windows == 4:
        slots_cfg = CONCURRENCY_PRESETS[4]
        mode_desc = "四并发黄金四分屏模式 (4 窗口联动)"
    else:
        slots_cfg = [
            {"slot": 0, "name": "单窗口", "color_start": "#059669", "color_end": "#10b981", "init_x": 100, "init_y": 80, "init_w": 960, "init_h": 560, "zoom": 0.85}
        ]
        mode_desc = "单窗口高精度微调模式"

    print("\n" + "=" * 68)
    print(f"🎯 [视窗几何与自适应防切割标定器] 正在极速启动: {mode_desc}")
    print("=" * 68)
    print(f"目标页面 : {target_url or '本地瞬启测试靶场 (0 延迟)'}")
    print(f"配置文件 : {target_path}")
    print("核心防线 : 页面内部采用 CSS 等比缩放 (zoom) 替代物理裁切，")
    print("           保证同意协议、勾选框与提交按钮 100% 同屏可见。")
    print("操作提示 : 拖动窗口边缘调整大小，点击顶部 HUD 的 [-]/[+] 调整缩放，")
    print("           满意后点击【💾 锁定并保存】写入永久配置。")
    print("=" * 68 + "\n", flush=True)

    from playwright.sync_api import sync_playwright
    try:
        from .browser import _register_browser_proc, _unregister_browser_proc
    except ImportError:
        def _register_browser_proc(_b: Any) -> None:
            pass

        def _unregister_browser_proc(_b: Any) -> None:
            pass
    browsers = []
    with _managed_playwright(sync_playwright, browsers, _unregister_browser_proc) as p:
        pages = []

        for item in slots_cfg:
            launch_args = list(FAST_CHROME_ARGS) + [
                f"--window-position={item['init_x']},{item['init_y']}",
                f"--window-size={item['init_w']},{item['init_h']}",
            ]
            b = p.chromium.launch(headless=False, args=launch_args)
            _register_browser_proc(b)
            browsers.append(b)
            ctx = b.new_context(
                no_viewport=True,
                locale="en-US",
                user_agent=desktop_chrome_ua(),
            )
            pg = ctx.new_page()
            pages.append(pg)

        # 页面加载：优先本地瞬启，若有 URL 则带短超时快速探测
        for pg in pages:
            if target_url:
                try:
                    pg.goto(target_url, timeout=7000, wait_until="domcontentloaded")
                except Exception:
                    # 网络异常或被阻断时，快速降级加载本地演练靶场，绝不卡死
                    pg.set_content(LOCAL_PLAYGROUND_HTML)
            else:
                pg.set_content(LOCAL_PLAYGROUND_HTML)

        # 注入防切割样式与交互式标定 HUD
        os_dpi = _get_os_dpi_scale()
        for idx, (pg, cfg) in enumerate(zip(pages, slots_cfg)):
            try:
                cfg_with_dpi = {**cfg, "os_dpi": os_dpi}
                pg.evaluate("""(cfg) => {
                    // 1. 注入防切割与防截断全局 CSS（不写 html zoom !important 死锁，让动态按钮实时生效）
                    const style = document.createElement('style');
                    style.id = 'anti-clipping-calibrator-style';
                    style.innerHTML = `
                        body { overflow-x: auto !important; max-width: 100vw !important; }
                        
                        /* 常见弹窗与表单容器自适应，防止右侧同意按钮被裁切 */
                        .auth-pane, .auth-card, .login-shell, .cl-card, .cl-rootBox,
                        [role="dialog"], .modal, .modal-content, .agreement-modal,
                        form, .form-container, .terms-box {
                            max-width: 100% !important;
                            box-sizing: border-box !important;
                        }
                        
                        /* 按钮和勾选行自适应 */
                        button[type="submit"], .submit-btn, .agree-btn, .terms-checkbox,
                        input[type="checkbox"], label {
                            max-width: 100% !important;
                            white-space: normal !important;
                        }
                    `;
                    document.head.appendChild(style);

                    // 2. 注入交互式标定控制器（同时联动监听浏览器原生 Ctrl+/- 缩放与页面 CSS zoom）
                    const osDpi = cfg.os_dpi || 1.0;
                    let currentZoom = cfg.zoom || 0.85;
                    const calcEffectiveZoom = () => {
                        const browserZoom = Math.max(0.25, Math.min(3.0, window.devicePixelRatio / osDpi));
                        const effectiveZoom = Math.max(0.4, Math.min(1.5, Math.round(browserZoom * currentZoom * 100) / 100));
                        window.__BROWSER_ZOOM = Math.round(browserZoom * 100) / 100;
                        window.__EFFECTIVE_ZOOM = effectiveZoom;
                        return { browserZoom, effectiveZoom };
                    };
                    const applyCalZoom = (z) => {
                        currentZoom = Math.max(0.5, Math.min(1.25, Math.round(z * 100) / 100));
                        window.__CURRENT_ZOOM = currentZoom;
                        window.__AGENT_TARGET_ZOOM = currentZoom;
                        document.documentElement.style.setProperty('zoom', String(currentZoom), 'important');
                        const hud = document.getElementById('window-calibrator-hud');
                        if (hud) hud.style.zoom = String(Math.round((1 / currentZoom) * 1000) / 1000);
                        const { effectiveZoom } = calcEffectiveZoom();
                        const zt = document.getElementById('zoom-val-text');
                        if (zt) zt.innerText = Math.round(effectiveZoom * 100) + '%';
                    };
                    window.__CURRENT_ZOOM = currentZoom;

                    const bar = document.createElement('div');
                    bar.id = 'window-calibrator-hud';
                    bar.innerHTML = `
                        <div style="position:fixed; top:0; left:0; right:0; z-index:999999999; background:linear-gradient(135deg, ${cfg.color_start} 0%, ${cfg.color_end} 100%); color:#ffffff; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif; padding:8px 14px; box-shadow:0 4px 16px rgba(0,0,0,0.3); display:flex; align-items:center; justify-content:space-between; user-select:none;">
                            <div style="display:flex; align-items:center; gap:12px;">
                                <div style="font-size:13px; font-weight:700;">🎯 ${cfg.name} 标定</div>
                                <div id="live-coords" style="font-size:12px; font-family:Consolas,monospace; opacity:0.95;">
                                    X: 0, Y: 0 | 0 × 0
                                </div>
                            </div>
                            
                            <!-- 核心：缩放调节器 (避免物理切割导致按钮丢失) -->
                            <div style="display:flex; align-items:center; gap:8px;">
                                <div style="display:flex; align-items:center; gap:4px; background:rgba(0,0,0,0.25); padding:2px 8px; border-radius:5px;">
                                    <span style="font-size:11px; font-weight:600;">等比缩放:</span>
                                    <button id="zoom-dec-btn" style="background:#ffffff; color:#111; border:none; width:20px; height:20px; border-radius:3px; font-weight:bold; cursor:pointer; line-height:1;">-</button>
                                    <span id="zoom-val-text" style="min-width:36px; text-align:center; font-family:monospace; font-weight:700;">${Math.round(currentZoom * 100)}%</span>
                                    <button id="zoom-inc-btn" style="background:#ffffff; color:#111; border:none; width:20px; height:20px; border-radius:3px; font-weight:bold; cursor:pointer; line-height:1;">+</button>
                                </div>
                                
                                <button id="lock-save-btn" style="background:#ffffff; color:#065f46; border:none; padding:5px 14px; border-radius:5px; font-size:12px; font-weight:700; cursor:pointer; box-shadow:0 2px 6px rgba(0,0,0,0.2); transition:all 0.2s;">
                                    💾 锁定并保存此配置
                                </button>
                            </div>
                        </div>
                    `;
                    document.body.appendChild(bar);

                    document.getElementById('zoom-dec-btn').addEventListener('click', (e) => {
                        e.preventDefault();
                        applyCalZoom(currentZoom - 0.05);
                    });

                    document.getElementById('zoom-inc-btn').addEventListener('click', (e) => {
                        e.preventDefault();
                        applyCalZoom(currentZoom + 0.05);
                    });
                    applyCalZoom(currentZoom);

                    const saveBtn = document.getElementById('lock-save-btn');
                    saveBtn.addEventListener('click', () => {
                        saveBtn.disabled = true;
                        saveBtn.innerText = '✅ 已锁定并保存！';
                        saveBtn.style.background = '#d1fae5';
                        saveBtn.style.color = '#065f46';
                        window.__TRIGGER_SAVE_ALL = true;
                    });

                    function updateHUD() {
                        const { browserZoom, effectiveZoom } = calcEffectiveZoom();
                        const zt = document.getElementById('zoom-val-text');
                        if (zt) {
                            zt.innerText = Math.round(effectiveZoom * 100) + '%';
                        }
                        const el = document.getElementById('live-coords');
                        if (el) {
                            const bPct = Math.round(browserZoom * 100);
                            const cPct = Math.round(currentZoom * 100);
                            el.innerText = `X: ${window.screenX}, Y: ${window.screenY} | ${window.outerWidth} × ${window.outerHeight} (览${bPct}%×页${cPct}%)`;
                        }
                    }
                    window.addEventListener('resize', updateHUD);
                    setInterval(updateHUD, 250);
                    updateHUD();
                }""", cfg_with_dpi)
            except Exception:
                pass

        # 监听用户锁定保存或关闭窗口事件
        locked = False
        while not locked:
            time.sleep(0.3)
            alive_pages = []
            for pg in pages:
                try:
                    if pg.is_closed():
                        continue
                    alive_pages.append(pg)
                    if pg.evaluate("() => Boolean(window.__TRIGGER_SAVE_ALL)"):
                        locked = True
                        break
                except Exception:
                    pass
            if not alive_pages:
                break

        # 收集最终精确参数并持久化
        slots_data = []
        for idx, (pg, cfg) in enumerate(zip(pages, slots_cfg)):
            try:
                if not pg.is_closed():
                    geom = pg.evaluate("""() => {
                        return {
                            x: window.screenX,
                            y: window.screenY,
                            width: window.outerWidth,
                            height: window.outerHeight,
                            dpr: window.devicePixelRatio,
                            zoom: window.__EFFECTIVE_ZOOM || window.__CURRENT_ZOOM || 0.85,
                            css_zoom: window.__CURRENT_ZOOM || 0.85,
                            browser_zoom: window.__BROWSER_ZOOM || 1.0
                        };
                    }""")
                    slots_data.append({
                        "slot": idx,
                        "name": cfg["name"],
                        "x": geom["x"],
                        "y": geom["y"],
                        "width": geom["width"],
                        "height": geom["height"],
                        "dpr": geom.get("dpr", 1.0),
                        "zoom": round(float(geom.get("zoom", cfg.get("zoom", 0.85))), 2),
                        "css_zoom": round(float(geom.get("css_zoom", cfg.get("zoom", 0.85))), 2),
                        "browser_zoom": round(float(geom.get("browser_zoom", 1.0)), 2),
                    })
                else:
                    slots_data.append({
                        "slot": idx,
                        "name": cfg["name"],
                        "x": cfg["init_x"],
                        "y": cfg["init_y"],
                        "width": cfg["init_w"],
                        "height": cfg["init_h"],
                        "dpr": 1.0,
                        "zoom": cfg.get("zoom", 0.85),
                    })
            except Exception as e:
                print(f"[WARN] 通道 #{idx} 几何采集失败，未写入配置: {e}")

        # ★ 保护：若所有通道都采集失败（slots_data 为空），绝不能写出一个
        #   slots:[] + 硬编码默认 x/y/w/h 的"空配置"并打印"成功持久化"——
        #   那会静默覆盖掉此前可用的标定，且下游会拿这套假几何去摆窗口。
        if not slots_data:
            print("\n❌ 标定失败：所有通道的窗口几何均未采集到，未写入任何配置（保留原文件）。")
            return None

        # 读取-合并-写入必须是同一事务，避免并发标定互相覆盖档位。
        with interprocess_file_lock(target_path):
            # 读取旧配置并合并保留其它并发档位，避免标定 4 并发时覆盖掉 1/2 并发
            existing: dict[str, Any] = load_json_dict_strict(target_path) if target_path.exists() else {}

            # 组装持久化结构，同时维护单窗口、双并发与四分屏
            final_payload: dict[str, Any] = dict(existing)
            final_payload.update({
                "calibrated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "description": "多窗口自适应几何与防切割等比缩放规范",
                "screen_resolution": existing.get("screen_resolution", "1920x1080"),
                "total_slots": len(slots_data),
                "slots": slots_data,
                "x": slots_data[0]["x"] if slots_data else 0,
                "y": slots_data[0]["y"] if slots_data else 0,
                "width": slots_data[0]["width"] if slots_data else 960,
                "height": slots_data[0]["height"] if slots_data else 520,
                "zoom": slots_data[0]["zoom"] if slots_data else 0.85,
                "dpr": slots_data[0]["dpr"] if slots_data else 1.0,
            })

            if total_windows <= 1:
                final_payload["single_concurrency"] = {
                    "total_slots": 1,
                    "description": "单并发全屏规范",
                    "slots": slots_data,
                }
            elif total_windows == 2:
                final_payload["dual_concurrency"] = {
                    "total_slots": 2,
                    "description": "双并发左右并排全屏防切割规范",
                    "slots": slots_data,
                }
            elif total_windows >= 6:
                final_payload["hex_concurrency"] = {
                    "total_slots": len(slots_data),
                    "description": "六并发标定规范",
                    "slots": slots_data,
                }
                # 保留原有 4 并发 slots 不被 6 并发覆盖
                if "slots" in existing:
                    final_payload["slots"] = existing["slots"]
                    final_payload["total_slots"] = existing.get("total_slots", len(existing["slots"]))
            elif total_windows >= 4:
                final_payload["quad_concurrency"] = {
                    "total_slots": len(slots_data),
                    "description": "四并发标定规范",
                    "slots": slots_data,
                }

            # 原子写入：先写 .tmp 再 replace，避免写到一半崩溃把旧标定截断损坏。
            atomic_write_text_safe(target_path, json.dumps(final_payload, indent=2, ensure_ascii=False))

        print(f"\n✅ 视窗几何与防切割配置已成功持久化至: {target_path}")
        for s in slots_data:
            print(f"   - 通道 #{s['slot']} [{s['name']}]: X={s['x']}, Y={s['y']}, 尺寸={s['width']}×{s['height']}, 缩放比={int(s['zoom']*100)}%")

        return final_payload


def demo_layouts(
    config_path: Path | str | None = None,
    hold_sec: int = 10,
) -> list[str]:
    """按配置文件依次在桌面轮播演示全部并发视窗预设（1 / 2 / 4 双形态 / 6 双形态），每套停留 hold_sec 秒。"""
    cfg_file = Path(config_path) if config_path else DEFAULT_GEOMETRY_PATH
    if not cfg_file.exists():
        raise FileNotFoundError(f"未找到视窗配置文件: {cfg_file}")

    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    stages: list[tuple[str, list[dict[str, Any]]]] = []
    if data.get("single_concurrency", {}).get("slots"):
        stages.append(("1并发 · 全屏满屏", data["single_concurrency"]["slots"]))
    if data.get("dual_concurrency", {}).get("slots"):
        stages.append(("2并发 · 左右两侧贴边", data["dual_concurrency"]["slots"]))
    for key, label in (("edge_2x2", "4并发① · 贴满四边 (edge_2x2)"), ("centered_compact", "4并发② · 对称居中紧凑 (centered_compact)")):
        slots = (data.get("quad_presets") or {}).get(key, {}).get("slots")
        if slots:
            stages.append((label, slots))
    for key, label in (("edge_3x2", "6并发① · 贴满六宫格 (edge_3x2)"), ("wing_cascade", "6并发② · 左右对称八字阶梯 (wing_cascade)")):
        slots = (data.get("hex_presets") or {}).get(key, {}).get("slots")
        if slots:
            stages.append((label, slots))
    if not stages and data.get("slots"):
        stages.append((f"{len(data['slots'])}并发 · 默认预设", data["slots"]))

    colors = [
        ("#059669", "#10b981"),
        ("#0284c7", "#38bdf8"),
        ("#2563eb", "#60a5fa"),
        ("#7c3aed", "#a855f7"),
        ("#d97706", "#f59e0b"),
        ("#db2777", "#f472b6"),
    ]
    shown: list[str] = []
    hold_sec = max(1, int(hold_sec))

    from playwright.sync_api import sync_playwright
    try:
        from .browser import _register_browser_proc, _unregister_browser_proc
    except ImportError:
        def _register_browser_proc(_b: Any) -> None:
            pass

        def _unregister_browser_proc(_b: Any) -> None:
            pass
    with sync_playwright() as p:
        for stage_idx, (title, slots) in enumerate(stages, 1):
            print(f"\n=== [{stage_idx}/{len(stages)}] {title} ({len(slots)} 窗, 停留 {hold_sec}s) ===", flush=True)
            browsers = []
            pages = []
            try:
                for i, s in enumerate(slots):
                    args = list(FAST_CHROME_ARGS) + [
                        f"--window-position={s['x']},{s['y']}",
                        f"--window-size={s['width']},{s['height']}",
                    ]
                    b = p.chromium.launch(headless=False, args=args)
                    _register_browser_proc(b)
                    browsers.append(b)
                    ctx = b.new_context(no_viewport=True, locale="zh-CN")
                    pg = ctx.new_page()
                    pg.set_content(LOCAL_PLAYGROUND_HTML)
                    c1, c2 = colors[i % len(colors)]
                    z = float(s.get("zoom", 0.85))
                    pg.evaluate(
                        """({stageTitle, slotName, x, y, w, h, z, c1, c2, holdSec}) => {
                            document.documentElement.style.setProperty('zoom', String(z), 'important');
                            const bar = document.createElement('div');
                            bar.id = 'demo-hud';
                            bar.style.zoom = String(Math.round((1 / z) * 1000) / 1000);
                            bar.innerHTML = `
                                <div style="position:fixed; top:0; left:0; right:0; z-index:999999999; background:linear-gradient(135deg, ${c1} 0%, ${c2} 100%); color:#fff; font-family:system-ui,Segoe UI,sans-serif; padding:7px 12px; display:flex; align-items:center; justify-content:space-between; box-shadow:0 3px 12px rgba(0,0,0,0.28); font-size:12px; font-weight:700;">
                                    <span>${stageTitle} · ${slotName}</span>
                                    <span style="font-family:Consolas,monospace; opacity:0.95;">(${x},${y}) ${w}×${h} · ${Math.round(z*100)}%</span>
                                    <span id="demo-cd" style="background:rgba(0,0,0,0.28); padding:2px 8px; border-radius:4px; font-family:Consolas,monospace;">${holdSec}s</span>
                                </div>
                            `;
                            document.body.appendChild(bar);
                        }""",
                        {
                            "stageTitle": f"[{stage_idx}/{len(stages)}] {title}",
                            "slotName": s.get("name", f"#{i+1}"),
                            "x": s["x"], "y": s["y"], "w": s["width"], "h": s["height"],
                            "z": z, "c1": c1, "c2": c2, "holdSec": hold_sec,
                        },
                    )
                    pages.append(pg)

                for left in range(hold_sec, 0, -1):
                    for pg in pages:
                        try:
                            if not pg.is_closed():
                                pg.evaluate(f"() => {{ const el = document.getElementById('demo-cd'); if (el) el.innerText = '{left}s'; }}")
                        except Exception:
                            pass
                    time.sleep(1.0)
                shown.append(title)
            finally:
                for b in browsers:
                    try:
                        b.close()
                    except Exception:
                        pass
                    else:
                        _unregister_browser_proc(b)
                time.sleep(0.4)

    print(f"\n✅ 全部 {len(shown)} 套视窗布局演示完毕！", flush=True)
    return shown


if __name__ == "__main__":
    url_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    calibrate_window(url=url_arg)
