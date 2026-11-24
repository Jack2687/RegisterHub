---
name: har-to-protocol
description: 从用户上传的 .har 抓包提炼真实 API 链路并生成轻量适配器
---

# HAR 抓包提炼协议链路

1. 先调用 `read_session_file()` 列出当前会话上传的 `.har` 文件，再调用 `analyze_har(filename=...)` 提取核心注册/发码/登录/提 Key 接口与风控信号。
2. 若全链路均为标准 HTTP JSON 请求且无强交互盾，优先在适配器中直接调用 `requests` 协议提交，减少浏览器开销。
3. 若仅单个提交步骤带 Turnstile/reCAPTCHA，将浏览器定位为“取票器”，拿到 Token 后切回纯协议收信与提 Key。
4. 写入 `sites/<name>.py` 后调用 `run_adapter_once` 验证真实凭据落盘。
