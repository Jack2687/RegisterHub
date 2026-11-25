---
name: site-onboarding
description: 新站从零探测、编写 BaseSiteAdapter 适配器并单线程闭环试跑
---

# 新站从零探测与闭环落地

1. 调用 `update_plan` 拆分 4~5 步执行计划，优先 `probe_url`（或 `run_scratch_python` 轻量请求）探测目标注册页表单、真实后端 API 与人机/鉴权信号。
2. 遵循「协议优先 ➔ 无头优先 ➔ 混合取票」铁律：能用纯 HTTP 协议完成的链路不走笨重 DOM 填表；如需参考骨架或方法签名，调用 `read_adapter(name="_template")` 或 `read_core_file`。
3. 邮箱优先按当前配置自动兼容内置邮箱池（`BuiltinPoolMailClient`）、临时邮（`DisposableMailClient`）或 IMAP（`SingleImapMailClient`）；人设姓名与 Handle 使用高离散真人化组合，避免固定乱码聚类。
4. 使用 `write_adapter` 或 `patch_adapter` 写入 `sites/<name>.py`，严禁在 `api_key` 等字段塞入 `"Active"` / `"OK"` 等假占位符；必须校验真实进入工作区而非 Waitlist。
5. 调用 `run_adapter_once` 单线程试跑 1 个号，检查退出码、`accounts_delta` 并用 `verify_accounts` 复核落盘凭据；若失败，立即读 `read_failures` 按四象限精准定位并修复复测。
