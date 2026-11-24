---
name: site-onboarding
description: 新站从零探测、编写 BaseSiteAdapter 适配器并单线程闭环试跑
---

# 新站从零探测与闭环落地

1. 调用 `update_plan` 拆分 4~5 步执行计划，优先 `probe_url` 探测目标注册页表单、按钮与人机/鉴权信号。
2. 如需参考骨架或方法签名，调用 `read_adapter(name="_template")` 或 `read_core_file`。
3. 邮箱优先按当前配置自动兼容内置邮箱池（`BuiltinPoolMailClient`）、临时邮（`DisposableMailClient`）或 IMAP（`SingleImapMailClient`）。
4. 使用 `write_adapter` 或 `patch_adapter` 写入 `sites/<name>.py`，严禁在 `api_key` 等字段塞入 `"Active"` / `"OK"` 等假占位符。
5. 调用 `run_adapter_once` 单线程试跑 1 个号，检查退出码与 `accounts_delta`；若失败，立即读 `read_failures` 定位根因并修复复测。
