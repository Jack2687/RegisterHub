---
name: failure-diagnosis
description: 按四象限快速排查失败日志、黑匣子截图与代理/邮箱根因
---

# 失败日志四象限诊断

1. 并行调用 `read_failures(limit=20)` 与 `check_infra(check_ip_score=false)` 获取近期失败原因、截图与当前代理/邮箱池余量。
2. 按四象限归因：
   - **收不到验证码**：检查邮箱池状态与出口 IP 是否触发限流，优先用 `manage_builtin_mail` / `manage_builtin_proxies` 排查。
   - **账号已存在 / 域被拒**：切换内置邮箱池（Outlook/IMAP）替代公共临时域。
   - **人机盾卡死**：检查 `channel="chrome"` 与出口 IP 纯净度，或建议切换外部指纹浏览器 CDP。
   - **页面超时 / 选择器变更**：结合 `probe_url` 与 `patch_adapter` 精准修正选择器并复测。
3. 修复后必须用 `run_adapter_once` 实跑复核，绝不盲猜交差。
