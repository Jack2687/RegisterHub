"""Zero-dependency OpenAI-compatible LLM Client (Python stdlib only).

Persists a single LLM profile atomically at `results/llm_config.json`, masks
the API key on public reads, preserves existing keys when saving blank input,
discovers `/models`, and calls OpenAI-compatible `/chat/completions` endpoints
(with Function Calling tool definitions and optional HTTP/HTTPS proxy) using
`urllib.request`.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

ROOT_DIR = Path(__file__).resolve().parent.parent
LLM_CONFIG_FILE = ROOT_DIR / "results" / "llm_config.json"

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_MAX_TOKENS_FIELD = "max_tokens"
DEFAULT_TEMPERATURE = 0.2
VALID_MAX_TOKENS_FIELDS = ("max_tokens", "max_completion_tokens")
_REASONING_MODEL_PREFIXES = ("o1", "o3", "o4", "gpt-5")
_PROXY_ERROR_MSG = "标准库仅支持 http:// 或 https:// 代理，请填写 HTTP 代理端口如 http://127.0.0.1:7897"

_CONFIG_LOCK = threading.Lock()


class LLMClientError(RuntimeError):
    """Structured error raised when LLM configuration or HTTP requests fail."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _default_config() -> Dict[str, Any]:
    return {
        "base_url": DEFAULT_BASE_URL,
        "model": DEFAULT_MODEL,
        "api_key": "",
        "proxy_url": "",
        "max_tokens": DEFAULT_MAX_TOKENS,
        "max_tokens_field": DEFAULT_MAX_TOKENS_FIELD,
        "temperature": DEFAULT_TEMPERATURE,
    }


def mask_api_key(api_key: str) -> str:
    """Return a non-reversible hint for UI display (never exposes raw secret)."""
    key = str(api_key or "").strip()
    if not key:
        return ""
    if len(key) <= 4:
        return "***"
    if len(key) <= 10:
        return f"{key[:2]}***{key[-2:]}"
    return f"{key[:5]}...{key[-4:]}"


def _clamp_int(val: Any, default: int, lo: int, hi: int) -> int:
    try:
        num = int(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


def _clamp_float(val: Any, default: float, lo: float, hi: float) -> float:
    try:
        num = float(val)
    except (TypeError, ValueError):
        return default
    if num != num:  # NaN guard
        return default
    return round(max(lo, min(hi, num)), 3)


def _normalize_base_url(base_url: str) -> str:
    """Strip trailing slashes and accidental endpoint suffixes like /chat/completions or /responses."""
    clean = str(base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    while True:
        low = clean.lower()
        stripped = False
        for suffix in ("/chat/completions", "/responses"):
            if low.endswith(suffix):
                clean = clean[: -len(suffix)].rstrip("/")
                stripped = True
                break
        if not stripped:
            break
    return clean


def _validate_base_url(raw_url: str) -> str:
    """Validate and normalize an explicit http(s) Base URL without silent fallback."""
    val = str(raw_url or "").strip().rstrip("/")
    if not val:
        return ""
    if not val.startswith(("http://", "https://")):
        raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
    parsed = urlparse(val)
    if not parsed.netloc:
        raise ValueError("Base URL 格式非法，缺少主机名（例如 https://api.openai.com/v1）")
    return _normalize_base_url(val)


def _validate_proxy_url(proxy_url: Any) -> str:
    """Reject socks5/socks5h and non-HTTP proxies since stdlib urllib only supports HTTP(S)."""
    val = str(proxy_url or "").strip()
    if not val:
        return ""
    low = val.lower()
    if not low.startswith(("http://", "https://")):
        raise ValueError(_PROXY_ERROR_MSG)
    parsed = urlparse(val)
    if not parsed.netloc:
        raise ValueError(_PROXY_ERROR_MSG)
    return val


def _is_reasoning_model(model: str, max_tokens_field: str = "") -> bool:
    """Return True for OpenAI reasoning / GPT-5 models or max_completion_tokens mode."""
    if max_tokens_field == "max_completion_tokens":
        return True
    low = str(model or "").strip().lower()
    leaf = low.rsplit("/", 1)[-1]
    return leaf.startswith(_REASONING_MODEL_PREFIXES)


def _normalize_config(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = _default_config()
    if not isinstance(raw, dict):
        return cfg
    base_url = _normalize_base_url(str(raw.get("base_url") or "").strip())
    if base_url:
        cfg["base_url"] = base_url
    model = str(raw.get("model") or "").strip()
    if model:
        cfg["model"] = model
    cfg["api_key"] = str(raw.get("api_key") or "").strip()
    cfg["proxy_url"] = str(raw.get("proxy_url") or "").strip()
    cfg["max_tokens"] = _clamp_int(raw.get("max_tokens"), DEFAULT_MAX_TOKENS, 64, 65536)
    mtf = str(raw.get("max_tokens_field") or DEFAULT_MAX_TOKENS_FIELD).strip()
    cfg["max_tokens_field"] = mtf if mtf in VALID_MAX_TOKENS_FIELDS else DEFAULT_MAX_TOKENS_FIELD
    cfg["temperature"] = _clamp_float(raw.get("temperature"), DEFAULT_TEMPERATURE, 0.0, 2.0)
    return cfg


def load_raw_llm_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load internal LLM configuration including raw api_key (server-side only)."""
    target = Path(config_path) if config_path is not None else LLM_CONFIG_FILE
    with _CONFIG_LOCK:
        if not target.exists():
            return _default_config()
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            raise LLMClientError(f"LLM 配置文件解析失败 ({target.name}): {exc}") from exc
    return _normalize_config(data)


def to_public_llm_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Strip raw api_key and return UI-safe profile with api_key_hint + has_key."""
    key = str(cfg.get("api_key") or "").strip()
    return {
        "base_url": str(cfg.get("base_url") or DEFAULT_BASE_URL),
        "model": str(cfg.get("model") or DEFAULT_MODEL),
        "api_key_hint": mask_api_key(key),
        "has_key": bool(key),
        "proxy_url": str(cfg.get("proxy_url") or ""),
        "max_tokens": int(cfg.get("max_tokens") or DEFAULT_MAX_TOKENS),
        "max_tokens_field": str(cfg.get("max_tokens_field") or DEFAULT_MAX_TOKENS_FIELD),
        "temperature": float(cfg.get("temperature") if cfg.get("temperature") is not None else DEFAULT_TEMPERATURE),
    }


def get_public_llm_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Return masked LLM config safe for GET /api/llm/config."""
    try:
        raw = load_raw_llm_config(config_path=config_path)
        return to_public_llm_config(raw)
    except LLMClientError:
        pub = to_public_llm_config(_default_config())
        pub["corrupt"] = True
        return pub


def save_llm_config(updates: Dict[str, Any], config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Atomically update and persist LLM config; blank api_key preserves existing key."""
    if not isinstance(updates, dict):
        raise ValueError("配置提交体必须为 JSON 对象")
    target = Path(config_path) if config_path is not None else LLM_CONFIG_FILE

    with _CONFIG_LOCK:
        existing = _default_config()
        if target.exists():
            try:
                disk = json.loads(target.read_text(encoding="utf-8"))
                existing = _normalize_config(disk)
            except Exception as exc:
                raise LLMClientError(f"拒绝覆盖损坏的配置文件 ({target.name}): {exc}") from exc

        cfg = dict(existing)

        if "base_url" in updates:
            val = _validate_base_url(str(updates.get("base_url") or ""))
            cfg["base_url"] = val or DEFAULT_BASE_URL

        if "model" in updates:
            val = str(updates.get("model") or "").strip()
            cfg["model"] = val or DEFAULT_MODEL

        if "proxy_url" in updates:
            cfg["proxy_url"] = _validate_proxy_url(updates.get("proxy_url"))

        if "max_tokens" in updates:
            cfg["max_tokens"] = _clamp_int(updates.get("max_tokens"), cfg["max_tokens"], 64, 65536)

        if "max_tokens_field" in updates:
            mtf = str(updates.get("max_tokens_field") or "").strip()
            if mtf not in VALID_MAX_TOKENS_FIELDS:
                raise ValueError(f"max_tokens_field 仅支持 {', '.join(VALID_MAX_TOKENS_FIELDS)}")
            cfg["max_tokens_field"] = mtf

        if "temperature" in updates:
            cfg["temperature"] = _clamp_float(updates.get("temperature"), cfg["temperature"], 0.0, 2.0)

        if bool(updates.get("clear_api_key")):
            cfg["api_key"] = ""
        elif "api_key" in updates:
            incoming_key = str(updates.get("api_key") or "").strip()
            existing_hint = mask_api_key(existing.get("api_key", ""))
            # Blank or masked placeholder preserves the persisted secret untouched
            if incoming_key and incoming_key != "***" and incoming_key != existing_hint:
                cfg["api_key"] = incoming_key

        from core.locks import atomic_write_text_safe, interprocess_file_lock

        with interprocess_file_lock(target):
            atomic_write_text_safe(target, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")

    return to_public_llm_config(cfg)


def _merge_runtime_config(
    overrides: Optional[Dict[str, Any]] = None,
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    cfg = load_raw_llm_config(config_path=config_path)
    merged = dict(cfg)
    if isinstance(overrides, dict):
        if "base_url" in overrides and str(overrides.get("base_url") or "").strip():
            try:
                merged["base_url"] = _validate_base_url(str(overrides.get("base_url") or ""))
            except ValueError as exc:
                raise LLMClientError(str(exc)) from exc
        if overrides.get("model"):
            merged["model"] = str(overrides["model"]).strip()
        if "proxy_url" in overrides:
            merged["proxy_url"] = str(overrides.get("proxy_url") or "").strip()
        if "max_tokens" in overrides and overrides.get("max_tokens") not in (None, ""):
            merged["max_tokens"] = _clamp_int(overrides.get("max_tokens"), merged["max_tokens"], 64, 65536)
        if overrides.get("max_tokens_field") in VALID_MAX_TOKENS_FIELDS:
            merged["max_tokens_field"] = str(overrides["max_tokens_field"])
        if "temperature" in overrides and overrides.get("temperature") not in (None, ""):
            merged["temperature"] = _clamp_float(overrides.get("temperature"), merged["temperature"], 0.0, 2.0)
        if "api_key" in overrides:
            k = str(overrides.get("api_key") or "").strip()
            if k and k != "***" and k != mask_api_key(cfg.get("api_key", "")):
                merged["api_key"] = k

    try:
        merged["base_url"] = _validate_base_url(merged.get("base_url") or DEFAULT_BASE_URL) or DEFAULT_BASE_URL
        merged["proxy_url"] = _validate_proxy_url(merged.get("proxy_url") or "")
    except ValueError as exc:
        raise LLMClientError(str(exc)) from exc
    return merged


def _build_chat_url(base_url: str) -> str:
    clean = _normalize_base_url(base_url)
    path = (urlparse(clean).path or "").rstrip("/")
    if re.search(r"/v\d+", path, re.IGNORECASE) or "/openai" in path.lower():
        return f"{clean}/chat/completions"
    return f"{clean}/v1/chat/completions"


def _build_models_urls(base_url: str) -> List[str]:
    clean = _normalize_base_url(base_url)
    parsed = urlparse(clean)
    root = f"{parsed.scheme}://{parsed.netloc}"
    path = (parsed.path or "").rstrip("/")

    candidates: List[str] = [f"{clean}/models"]
    if not re.search(r"/v\d+", path, re.IGNORECASE) and "/openai" not in path.lower():
        candidates.append(f"{clean}/v1/models")
    candidates.append(f"{root}/v1/models")

    seen = set()
    ordered: List[str] = []
    for url in candidates:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def _is_local_endpoint(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def _extract_content_text(raw_content: Any) -> str:
    if isinstance(raw_content, str):
        return raw_content
    if isinstance(raw_content, list):
        parts: List[str] = []
        for item in raw_content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in ("text", "output_text", "thinking", "reasoning"):
                parts.append(str(item.get("text") or item.get("thinking") or ""))
        return "\n".join(p for p in parts if p)
    return ""


def _extract_model_ids(data: Any) -> List[str]:
    items: Any = None
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            items = data["data"]
        elif isinstance(data.get("models"), list):
            items = data["models"]
    elif isinstance(data, list):
        items = data
    if not isinstance(items, list):
        return []
    found = set()
    for item in items:
        if isinstance(item, str) and item.strip():
            found.add(item.strip())
        elif isinstance(item, dict):
            mid = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
            if mid:
                found.add(mid)
    return sorted(found)


def _parse_http_error(err: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        raw = err.read(4096).decode("utf-8", errors="ignore").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    inner = parsed.get("error")
                    if isinstance(inner, dict):
                        detail = str(inner.get("message") or inner.get("code") or raw[:240])
                    elif inner:
                        detail = str(inner)
                    elif parsed.get("message"):
                        detail = str(parsed["message"])
                    else:
                        detail = raw[:240]
                else:
                    detail = raw[:240]
            except Exception:
                detail = raw[:240]
    except Exception:
        detail = str(err.reason or "")
    return detail or str(err.reason or "Unknown HTTP Error")


def _build_opener(proxy: str) -> urllib.request.OpenerDirector:
    clean_proxy = proxy.strip()
    handlers = [urllib.request.ProxyHandler({"http": clean_proxy, "https": clean_proxy})] if clean_proxy else []
    return urllib.request.build_opener(*handlers)


def chat_completion(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    system_prompt: str = "",
    *,
    config_override: Optional[Dict[str, Any]] = None,
    config_path: Optional[Path] = None,
    timeout: float = 90.0,
    max_tokens_override: Optional[int] = None,
) -> Dict[str, Any]:
    """Invoke OpenAI-compatible `/chat/completions` with pure stdlib `urllib.request`."""
    cfg = _merge_runtime_config(config_override, config_path=config_path)
    endpoint = _build_chat_url(cfg["base_url"])
    api_key = cfg["api_key"]

    if not api_key and not _is_local_endpoint(endpoint):
        raise LLMClientError("尚未配置 API Key，请先在「LLM 配置」填写并保存")

    full_messages: List[Dict[str, Any]] = []
    if system_prompt:
        full_messages.append({"role": "system", "content": system_prompt})
    full_messages.extend(messages)

    max_tok = max_tokens_override if max_tokens_override is not None else cfg["max_tokens"]
    payload: Dict[str, Any] = {
        "model": cfg["model"],
        "messages": full_messages,
        cfg["max_tokens_field"]: int(max_tok),
    }
    if not _is_reasoning_model(cfg["model"], cfg["max_tokens_field"]):
        payload["temperature"] = cfg["temperature"]
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "RegisterHub-Lite/2.0",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    opener = _build_opener(cfg.get("proxy_url", ""))

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with opener.open(req, timeout=timeout) as resp:
            raw_bytes = resp.read()
    except urllib.error.HTTPError as he:
        raise LLMClientError(f"HTTP {he.code}: {_parse_http_error(he)}", status_code=he.code) from he
    except urllib.error.URLError as ue:
        raise LLMClientError(f"网络请求失败: {ue.reason}") from ue
    except TimeoutError as te:
        raise LLMClientError(f"LLM 请求超时 ({int(timeout)}s)") from te
    except Exception as exc:
        raise LLMClientError(f"LLM 调用异常: {exc}") from exc

    try:
        data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise LLMClientError(f"LLM 返回内容不是合法 JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise LLMClientError("LLM 响应根节点非 JSON 对象")
    if data.get("error") and not data.get("choices"):
        err_obj = data["error"]
        err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
        raise LLMClientError(f"接口报错: {err_msg}")

    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise LLMClientError("LLM 响应缺少 choices 字段")

    choice0 = choices[0]
    msg = choice0.get("message") or {}
    content = _extract_content_text(msg.get("content"))
    raw_thinking = msg.get("reasoning_content")
    if raw_thinking is None:
        raw_thinking = msg.get("reasoning")
    thinking = _extract_content_text(raw_thinking)

    norm_tool_calls: List[Dict[str, Any]] = []
    for idx, tc in enumerate(msg.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        tc_id = str(tc.get("id") or f"call_{idx + 1}")
        fn_name = str(fn.get("name") or "").strip()
        fn_args = fn.get("arguments")
        if not isinstance(fn_args, str):
            fn_args = json.dumps(fn_args if fn_args is not None else {}, ensure_ascii=False)
        if fn_name:
            norm_tool_calls.append({
                "id": tc_id,
                "type": "function",
                "function": {"name": fn_name, "arguments": fn_args},
            })

    usage_raw = data.get("usage") or {}
    prompt_t = int(usage_raw.get("prompt_tokens") or 0)
    comp_t = int(usage_raw.get("completion_tokens") or 0)
    total_t = int(usage_raw.get("total_tokens") or (prompt_t + comp_t))

    return {
        "content": content,
        "thinking": thinking,
        "tool_calls": norm_tool_calls,
        "usage": {
            "prompt_tokens": prompt_t,
            "completion_tokens": comp_t,
            "total_tokens": total_t,
        },
        "model": str(data.get("model") or cfg["model"]),
        "finish_reason": str(choice0.get("finish_reason") or ""),
    }


def discover_models(
    overrides: Optional[Dict[str, Any]] = None,
    config_path: Optional[Path] = None,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """Probe `<base>/models` and `<root>/v1/models` with stdlib `urllib.request`."""
    try:
        cfg = _merge_runtime_config(overrides, config_path=config_path)
    except Exception as exc:
        return {
            "ok": False,
            "models": [],
            "source_url": "",
            "count": 0,
            "error": str(exc),
        }

    headers: Dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": "RegisterHub-Lite/2.0",
    }
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    opener = _build_opener(cfg.get("proxy_url", ""))
    candidates = _build_models_urls(cfg["base_url"])
    last_error = ""
    last_url = candidates[0] if candidates else ""

    for url in candidates:
        last_url = url
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw_bytes = resp.read(2_000_000)
        except urllib.error.HTTPError as he:
            last_error = f"HTTP {he.code}: {_parse_http_error(he)}"
            if he.code in (401, 403):
                break
            continue
        except urllib.error.URLError as ue:
            last_error = f"网络请求失败: {ue.reason}"
            continue
        except TimeoutError:
            last_error = f"拉取模型列表超时 ({int(timeout)}s)"
            continue
        except Exception as exc:
            last_error = f"拉取模型列表异常: {exc}"
            continue

        try:
            parsed = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        except Exception as exc:
            last_error = f"模型接口返回非合法 JSON: {exc}"
            continue

        models = _extract_model_ids(parsed)
        if models:
            return {
                "ok": True,
                "models": models,
                "source_url": url,
                "count": len(models),
                "error": "",
            }
        last_error = "接口返回空模型列表"

    return {
        "ok": False,
        "models": [],
        "source_url": last_url,
        "count": 0,
        "error": last_error or "未获取到可用模型",
    }


def test_connection(
    overrides: Optional[Dict[str, Any]] = None,
    config_path: Optional[Path] = None,
    timeout: float = 25.0,
) -> Dict[str, Any]:
    """Send a minimal 1-turn probe (`Reply with OK`) and return latency + reply."""
    t0 = time.perf_counter()
    cfg_preview = _default_config()
    try:
        cfg_preview = _merge_runtime_config(overrides, config_path=config_path)
        is_reasoning_mode = (
            cfg_preview.get("max_tokens_field") == "max_completion_tokens"
            or _is_reasoning_model(cfg_preview.get("model", ""), cfg_preview.get("max_tokens_field", ""))
        )
        probe_max_tokens = 1024 if is_reasoning_mode else 512
        res = chat_completion(
            messages=[{"role": "user", "content": "Reply with OK"}],
            tools=None,
            system_prompt="",
            config_override=overrides,
            config_path=config_path,
            timeout=timeout,
            max_tokens_override=probe_max_tokens,
        )
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        reply = (res.get("content") or "").strip()[:240]
        if not reply:
            return {
                "ok": False,
                "latency_ms": latency_ms,
                "reply": "",
                "model": res.get("model") or cfg_preview["model"],
                "error": "模型返回空白回复",
            }
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "reply": reply,
            "model": res.get("model") or cfg_preview["model"],
            "error": "",
        }
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "ok": False,
            "latency_ms": latency_ms,
            "reply": "",
            "model": cfg_preview.get("model", DEFAULT_MODEL),
            "error": str(exc),
        }
