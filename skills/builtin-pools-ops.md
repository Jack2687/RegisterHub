---
name: builtin-pools-ops
description: 盘点、导入、验活与维护本地 SQLite 内置邮箱池与内置代理池
---

# 内置邮箱池与代理池运维

1. 调用 `check_infra` 盘点当前 `builtin_mail` 与 `builtin_proxies` 库存与当前配置模式（`mail.provider` / `browser.proxy_mode`）。
2. 批量导入邮箱使用 `manage_builtin_mail(action="preview" / "import")`，支持 Outlook OAuth2、iCloud 链接与通用 IMAP 自动识别。
3. 批量导入代理使用 `manage_builtin_proxies(action="import" / "add_gateway" / "add_api_source")`，并留意 `browser_compatible`（带账密 SOCKS5 不兼容 Chromium）。
4. 及时清理失效条目（`cleanup_dead`），所有凭据经本地 DPAPI 加密存储，严禁在对话中回显明文密码或 Token。
