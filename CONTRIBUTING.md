# Contributing to RegisterHub

感谢关注并参与 **RegisterHub** 开源社区建设！提交 Issue 或 Pull Request 前请确认以下工程规范：

## 1. 社区版架构底线

- **零额外依赖原则**：`web/` 控制面、`core/ai_agent.py`、`core/llm_client.py`、`core/mail_builtin.py`、`core/proxy_builtin.py`、`core/secrets.py` 必须仅使用 **Python 标准库**（由 `tests/test_ai_agent.py::test_stdlib_only_imports_in_llm_and_agent` 自动门禁校验）。
- **纯净无硬编码**：请勿在主仓库 `sites/` 中提交包含商业站点硬编码或私有密钥/个人域名的适配器；主仓库仅保留通用框架能力与公开测试靶场示例。
- **单线程安全模式**：社区版默认单并发串行执行，避免无节制多开干扰宿主机桌面与进程树。
- **假成功零容忍**：所有断言必须基于真实状态回读，严禁在凭据字段塞入 `"Active"` / `"OK"` / `"placeholder"` 等占位值。

## 2. 本地开发与测试

```bash
# 安装依赖
pip install -r requirements.txt

# 运行全量自动化测试套件
python -m pytest tests/ -v

# 启动本地 Web 工作台（默认 http://127.0.0.1:18886）
python main.py --web
```

## 3. 提交 PR 清单

- [ ] 新增或修改的功能已附带对应的 `pytest` 单元测试，且全量测试通过。
- [ ] 不引入硬编码路径、Token、Cookie、个人邮箱或 `.db` 运行产物。
- [ ] Windows `.bat` 脚本保持 `CRLF` 行尾与 `chcp 65001 >nul`。
- [ ] 清晰描述改动动机、影响范围与本地验证方式。
