---
name: ledger-key-audit
description: 盘点 results/accounts.txt 凭据台账并可选执行 OpenAI 兼容在线验活
---

# 本地账号台账与 Key 验活

1. 调用 `verify_accounts(limit=20)` 统计 `results/accounts.txt` 总账号数、有效 Key 占比与近期脱敏样本。
2. 若用户提供了公开的 OpenAI 兼容 `base_url`，传入 `verify_accounts(base_url=..., limit=5)` 在线抽检 `/v1/models` 存活与延迟。
3. 严格脱敏展示 Key（仅展示前后缀掩码），绝不在输出中打印完整私钥。
