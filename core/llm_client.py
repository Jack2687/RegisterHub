"""Zero-dependency OpenAI-compatible LLM Client (Python stdlib only).

Persists a single LLM profile atomically at `results/llm_config.json`, masks
the API key on public reads, preserves existing keys when saving blank input,
discovers `/models`, and calls OpenAI-compatible `/chat/completions` endpoints
(with Function Calling tool definitions and optional HTTP/HTTPS proxy) using
`urllib.request`.
"""

from __future__ import annotations

import collections
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlsplit

from core.net_guard import (
    PrivateAddressBlockedError,
    validate_public_http_url,
    _SENSITIVE_CROSS_ORIGIN_HEADERS,
)
from core.secrets import mask_proxy_url, sanitize_error_text

ROOT_DIR = Path(__file__).resolve().parent.parent
LLM_CONFIG_FILE = ROOT_DIR / "results" / "llm_config.json"

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_FORMAT = "openai"
VALID_FORMATS = ("openai", "anthropic", "openai-responses")
DEFAULT_MAX_TOKENS = 4096
DEFAULT_MAX_TOKENS_FIELD = "max_tokens"
DEFAULT_TEMPERATURE = 0.2
VALID_MAX_TOKENS_FIELDS = ("max_tokens", "max_completion_tokens")
_REASONING_MODEL_PREFIXES = ("o1", "o3", "o4", "gpt-5")
_PROXY_ERROR_MSG = "标准库仅支持 http:// 或 https:// 代理，请填写 HTTP 代理端口如 http://127.0.0.1:7897"

_CONFIG_LOCK = threading.Lock()
_CALLS_LOCK = threading.Lock()
_RECENT_CALLS: "collections.deque[Dict[str, Any]]" = collections.deque(maxlen=200)
_RECENT_CALLS_KEY: str = ""


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


def _normalize_format(fmt: Any) -> str:
    val = str(fmt or DEFAULT_FORMAT).strip().lower()
    return val if val in VALID_FORMATS else DEFAULT_FORMAT


def _normalize_base_url(base_url: str) -> str:
    """Strip trailing slashes and accidental endpoint suffixes like /chat/completions, /responses, /messages."""
    clean = str(base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    while True:
        low = clean.lower()
        stripped = False
        for suffix in ("/chat/completions", "/responses", "/messages"):
            if low.endswith(suffix):
                clean = clean[: -len(suffix)].rstrip("/")
                stripped = True
                break
        if not stripped:
            break
    return clean


def _validate_base_url(raw_url: str, *, resolve_dns: bool = False) -> str:
    """Validate and normalize an explicit http(s) Base URL, blocking non-loopback private/metadata SSRF."""
    val = str(raw_url or "").strip().rstrip("/")
    if not val:
        return ""
    if not val.startswith(("http://", "https://")):
        raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
    parsed = urlparse(val)
    if not parsed.netloc or not parsed.hostname:
        raise ValueError("Base URL 格式非法，缺少主机名（例如 https://api.openai.com/v1）")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Base URL 不允许内嵌用户名或密码凭据")
    # Allow explicit local LLM servers (Ollama / vLLM / LM Studio), enforce net_guard on all other hosts
    if not _is_local_endpoint(val):
        validate_public_http_url(val, resolve_dns=resolve_dns)
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
    raw_mt = raw.get("max_tokens")
    cfg["max_tokens"] = DEFAULT_MAX_TOKENS if raw_mt in (None, "", 0, "0") else _clamp_int(raw_mt, DEFAULT_MAX_TOKENS, 64, 65536)
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
    """Strip raw api_key and mask proxy credentials for UI-safe public reads."""
    key = str(cfg.get("api_key") or "").strip()
    raw_px = str(cfg.get("proxy_url") or "").strip()
    return {
        "base_url": str(cfg.get("base_url") or DEFAULT_BASE_URL),
        "model": str(cfg.get("model") or DEFAULT_MODEL),
        "api_key_hint": mask_api_key(key),
        "has_key": bool(key),
        "proxy_url": mask_proxy_url(raw_px) if raw_px else "",
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
            inc_px = str(updates.get("proxy_url") or "").strip()
            if "***@" in inc_px and existing.get("proxy_url"):
                pass
            else:
                cfg["proxy_url"] = _validate_proxy_url(inc_px)

        if "max_tokens" in updates:
            raw_mt = updates.get("max_tokens")
            cfg["max_tokens"] = DEFAULT_MAX_TOKENS if raw_mt in (None, "", 0, "0") else _clamp_int(raw_mt, cfg["max_tokens"], 64, 65536)

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


def _origin_tuple(url: str) -> Tuple[str, str]:
    p = urlsplit(str(url or "").strip())
    return (p.scheme.lower(), (p.netloc or "").lower())


def _merge_runtime_config(
    overrides: Optional[Dict[str, Any]] = None,
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    cfg = load_raw_llm_config(config_path=config_path)
    orig_origin = _origin_tuple(cfg.get("base_url") or DEFAULT_BASE_URL)
    merged = dict(cfg)
    merged.setdefault("format", DEFAULT_FORMAT)
    merged.setdefault("profile_id", "")
    merged.setdefault("profile_name", "")
    merged.setdefault("thinking_mode", "none")
    merged.setdefault("reasoning_effort", "none")
    merged.setdefault("streaming", True)
    explicit_key_supplied = False
    base_url_overridden = False
    if isinstance(overrides, dict):
        if overrides.get("format"):
            merged["format"] = _normalize_format(overrides.get("format"))
        if overrides.get("profile_id"):
            merged["profile_id"] = str(overrides.get("profile_id") or "").strip()
        if overrides.get("profile_name"):
            merged["profile_name"] = str(overrides.get("profile_name") or "").strip()
        if overrides.get("thinking_mode") in ("none", "enabled", "disabled"):
            merged["thinking_mode"] = str(overrides.get("thinking_mode"))
        if overrides.get("reasoning_effort") in ("none", "low", "medium", "high", "xhigh", "max"):
            merged["reasoning_effort"] = str(overrides.get("reasoning_effort"))
        if isinstance(overrides.get("streaming"), bool):
            merged["streaming"] = overrides["streaming"]
        if "base_url" in overrides and str(overrides.get("base_url") or "").strip():
            try:
                merged["base_url"] = _validate_base_url(str(overrides.get("base_url") or ""))
                base_url_overridden = True
            except ValueError as exc:
                raise LLMClientError(str(exc)) from exc
        if overrides.get("model"):
            merged["model"] = str(overrides["model"]).strip()
        if "proxy_url" in overrides:
            inc_px = str(overrides.get("proxy_url") or "").strip()
            if "***@" in inc_px and merged.get("proxy_url"):
                pass
            else:
                merged["proxy_url"] = inc_px
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
                explicit_key_supplied = True

    try:
        merged["base_url"] = _validate_base_url(merged.get("base_url") or DEFAULT_BASE_URL) or DEFAULT_BASE_URL
        merged["proxy_url"] = _validate_proxy_url(merged.get("proxy_url") or "")
    except ValueError as exc:
        raise LLMClientError(str(exc)) from exc

    # Security (W-01): never attach a previously stored API key when an ad-hoc override points to a different host
    if base_url_overridden and not explicit_key_supplied and _origin_tuple(merged["base_url"]) != orig_origin:
        merged["api_key"] = ""
    return merged


def _build_chat_url(base_url: str) -> str:
    clean = _normalize_base_url(base_url)
    path = (urlparse(clean).path or "").rstrip("/")
    if re.search(r"/v\d+", path, re.IGNORECASE) or "/openai" in path.lower():
        return f"{clean}/chat/completions"
    return f"{clean}/v1/chat/completions"


def _build_anthropic_messages_url(base_url: str) -> str:
    clean = _normalize_base_url(base_url)
    path = (urlparse(clean).path or "").rstrip("/")
    if re.search(r"/v\d+", path, re.IGNORECASE):
        return f"{clean}/messages"
    return f"{clean}/v1/messages"


def _build_responses_url(base_url: str) -> str:
    clean = _normalize_base_url(base_url)
    path = (urlparse(clean).path or "").rstrip("/")
    if re.search(r"/v\d+", path, re.IGNORECASE) or "/openai" in path.lower():
        return f"{clean}/responses"
    return f"{clean}/v1/responses"


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


def _extract_model_ids_and_ctx(data: Any) -> Tuple[List[str], Dict[str, int]]:
    from core.llm_context import extract_model_context_windows

    items: Any = None
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            items = data["data"]
        elif isinstance(data.get("models"), list):
            items = data["models"]
    elif isinstance(data, list):
        items = data
    if not isinstance(items, list):
        return [], {}
    found = set()
    for item in items:
        if isinstance(item, str) and item.strip():
            mid = item.strip()
            if mid.startswith("models/"):
                mid = mid[len("models/"):]
            if mid:
                found.add(mid)
        elif isinstance(item, dict):
            mid = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
            if mid.startswith("models/"):
                mid = mid[len("models/"):]
            if mid:
                found.add(mid)
    ctx_map = extract_model_context_windows(data)
    return sorted(found), ctx_map


def _extract_model_ids(data: Any) -> List[str]:
    ids, _ = _extract_model_ids_and_ctx(data)
    return ids


def _calls_file() -> Path:
    return Path(LLM_CONFIG_FILE).with_name("llm_calls.jsonl")


def _record_llm_call(entry: Dict[str, Any]) -> None:
    global _RECENT_CALLS_KEY
    fp = _calls_file()
    fp_key = str(fp)
    with _CALLS_LOCK:
        if _RECENT_CALLS_KEY != fp_key:
            _RECENT_CALLS.clear()
            _RECENT_CALLS_KEY = fp_key
        _RECENT_CALLS.appendleft(entry)
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        with fp.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def list_llm_calls(limit: int = 100) -> List[Dict[str, Any]]:
    global _RECENT_CALLS_KEY
    lim = max(1, min(500, int(limit or 100)))
    fp = _calls_file()
    fp_key = str(fp)
    with _CALLS_LOCK:
        if _RECENT_CALLS_KEY == fp_key and _RECENT_CALLS:
            return list(_RECENT_CALLS)[:lim]
    if not fp.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        for ln in fp.read_text(encoding="utf-8", errors="ignore").splitlines()[-lim:]:
            s = ln.strip()
            if not s:
                continue
            obj = json.loads(s)
            if isinstance(obj, dict):
                rows.append(obj)
    except Exception:
        return []
    rows.reverse()
    with _CALLS_LOCK:
        _RECENT_CALLS.clear()
        _RECENT_CALLS.extend(rows[:200])
        _RECENT_CALLS_KEY = fp_key
    return rows[:lim]


def count_llm_calls() -> int:
    """Return true total count of recorded LLM calls (untruncated by list limit)."""
    fp = _calls_file()
    if fp.is_file():
        try:
            return sum(1 for ln in fp.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip())
        except Exception:
            pass
    with _CALLS_LOCK:
        return len(_RECENT_CALLS)


def get_llm_call(call_id: str) -> Optional[Dict[str, Any]]:
    cid = str(call_id or "").strip()
    if not cid:
        return None
    for row in list_llm_calls(limit=500):
        if row.get("id") == cid:
            return row
    return None


def _openai_messages_to_anthropic(messages: List[Dict[str, Any]], system_prompt: str) -> Tuple[str, List[Dict[str, Any]]]:
    sys_parts: List[str] = [system_prompt] if system_prompt else []
    out: List[Dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "user")
        if role == "system":
            txt = _extract_content_text(m.get("content"))
            if txt:
                sys_parts.append(txt)
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": str(m.get("tool_call_id") or "call_1"),
                "content": str(m.get("content") or ""),
            }
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        if role == "assistant":
            blocks: List[Dict[str, Any]] = []
            txt = _extract_content_text(m.get("content"))
            if txt:
                blocks.append({"type": "text", "text": txt})
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                raw_args = fn.get("arguments") or "{}"
                try:
                    parsed = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except Exception:
                    parsed = {}
                blocks.append({
                    "type": "tool_use",
                    "id": str(tc.get("id") or f"toolu_{secrets.token_hex(4)}"),
                    "name": str(fn.get("name") or "tool"),
                    "input": parsed if isinstance(parsed, dict) else {},
                })
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": " "}]})
            continue
        txt = _extract_content_text(m.get("content"))
        if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
            out[-1]["content"].append({"type": "text", "text": txt or " "})
        else:
            out.append({"role": "user", "content": txt or " "})
    return "\n\n".join(p for p in sys_parts if p), out


def _openai_tools_to_anthropic(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "description": str(fn.get("description") or ""),
            "input_schema": fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object", "properties": {}},
        })
    return out


def _openai_messages_to_responses_input(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "user")
        if role == "system":
            continue
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(m.get("tool_call_id") or "call_1"),
                "output": str(m.get("content") or ""),
            })
            continue
        if role == "assistant":
            txt = _extract_content_text(m.get("content"))
            if txt:
                items.append({"role": "assistant", "content": txt})
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(args or {}, ensure_ascii=False)
                items.append({
                    "type": "function_call",
                    "call_id": str(tc.get("id") or f"call_{secrets.token_hex(4)}"),
                    "name": str(fn.get("name") or "tool"),
                    "arguments": args,
                })
            continue
        items.append({"role": "user", "content": _extract_content_text(m.get("content"))})
    return items


def _openai_tools_to_responses(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "type": "function",
            "name": name,
            "description": str(fn.get("description") or ""),
            "parameters": fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object", "properties": {}},
        })
    return out


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


_ORIG_BUILD_OPENER = urllib.request.build_opener


class _SafeLLMRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Block 30x redirects to private/loopback/metadata hosts and strip auth headers across origins (W-01 / N-2)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        resolved_target = urljoin(req.full_url, newurl)
        orig_origin = _origin_tuple(req.full_url)
        new_origin = _origin_tuple(resolved_target)
        # Never allow a public LLM endpoint to 30x-redirect into loopback/LAN/metadata;
        # even for local endpoints, cross-origin redirects must pass public URL validation.
        if not _is_local_endpoint(req.full_url) or orig_origin != new_origin:
            validate_public_http_url(resolved_target, resolve_dns=True)
        redirected = super().redirect_request(req, fp, code, msg, headers, resolved_target)
        if redirected is not None and orig_origin != new_origin:
            for hdr_map in (redirected.headers, redirected.unredirected_hdrs):
                for k in list(hdr_map.keys()):
                    if str(k).lower() in _SENSITIVE_CROSS_ORIGIN_HEADERS:
                        hdr_map.pop(k, None)
        return redirected


def _guard_live_endpoint_dns(endpoint: str) -> None:
    """Enforce full DNS SSRF check on non-loopback live calls (skipped only when urllib build_opener is unit-test mocked)."""
    if _is_local_endpoint(endpoint):
        return
    if urllib.request.build_opener is _ORIG_BUILD_OPENER:
        validate_public_http_url(endpoint, resolve_dns=True)
    else:
        validate_public_http_url(endpoint, resolve_dns=False)


def _build_opener(proxy: str) -> urllib.request.OpenerDirector:
    clean_proxy = proxy.strip()
    handlers: List[urllib.request.BaseHandler] = []
    if clean_proxy:
        handlers.append(urllib.request.ProxyHandler({"http": clean_proxy, "https": clean_proxy}))
    handlers.append(_SafeLLMRedirectHandler())
    return urllib.request.build_opener(*handlers)


_SECRET_VAL_RE = re.compile(r"\b(sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._-]{12,})")


def _safe_nonneg_int(val: Any) -> int:
    """Coerce int, float, or numeric string (including decimals like '12.5') into a non-negative int."""
    if val is None or isinstance(val, bool):
        return 0
    if isinstance(val, int):
        return max(0, val)
    if isinstance(val, float):
        return max(0, int(val)) if val == val else 0
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        try:
            f = float(str(val).strip())
            return max(0, int(f)) if f == f else 0
        except (TypeError, ValueError):
            return 0


def _extract_usage_metrics(usage_raw: Any, fmt: str) -> Tuple[int, int, int, int]:
    """Return (prompt_tokens, completion_tokens, cache_read_tokens, cache_write_tokens)."""
    if not isinstance(usage_raw, dict):
        return 0, 0, 0, 0
    if fmt == "anthropic":
        raw_in = _safe_nonneg_int(usage_raw.get("input_tokens"))
        cr = _safe_nonneg_int(usage_raw.get("cache_read_input_tokens"))
        cw = _safe_nonneg_int(usage_raw.get("cache_creation_input_tokens"))
        comp_t = _safe_nonneg_int(usage_raw.get("output_tokens"))
        return raw_in + cr + cw, comp_t, cr, cw

    if fmt == "openai-responses":
        prompt_t = _safe_nonneg_int(usage_raw.get("input_tokens") if usage_raw.get("input_tokens") is not None else usage_raw.get("prompt_tokens"))
        comp_t = _safe_nonneg_int(usage_raw.get("output_tokens") if usage_raw.get("output_tokens") is not None else usage_raw.get("completion_tokens"))
        det = usage_raw.get("input_tokens_details") or usage_raw.get("prompt_tokens_details") or {}
        if not isinstance(det, dict):
            det = {}
        cr = _safe_nonneg_int(det.get("cached_tokens") or usage_raw.get("cached_tokens") or usage_raw.get("cache_read_input_tokens"))
        cw = _safe_nonneg_int(det.get("cache_creation_tokens") or usage_raw.get("cache_creation_input_tokens"))
        if prompt_t > 0 and cr > prompt_t:
            cr = prompt_t
        return prompt_t, comp_t, cr, cw

    prompt_t = _safe_nonneg_int(usage_raw.get("prompt_tokens") if usage_raw.get("prompt_tokens") is not None else usage_raw.get("input_tokens"))
    comp_t = _safe_nonneg_int(usage_raw.get("completion_tokens") if usage_raw.get("completion_tokens") is not None else usage_raw.get("output_tokens"))
    det = usage_raw.get("prompt_tokens_details") or usage_raw.get("input_tokens_details") or {}
    if not isinstance(det, dict):
        det = {}
    cr = _safe_nonneg_int(
        det.get("cached_tokens")
        or usage_raw.get("prompt_cache_hit_tokens")
        or usage_raw.get("cached_tokens")
        or usage_raw.get("cache_read_input_tokens")
    )
    cw = _safe_nonneg_int(
        det.get("cache_creation_tokens")
        or usage_raw.get("cache_creation_input_tokens")
    )
    if prompt_t > 0 and cr > prompt_t:
        cr = prompt_t
    return prompt_t, comp_t, cr, cw


def _normalize_stop_reason(raw_reason: Any) -> str:
    s = str(raw_reason or "").strip().lower()
    if s in ("length", "max_tokens", "incomplete"):
        return "max_tokens"
    if s in ("tool_calls", "tool_use", "function_call"):
        return "tool_calls"
    if s in ("stop", "end_turn", "completed"):
        return "stop"
    return s


def _check_truncated_tool_calls(tool_calls: List[Dict[str, Any]], stop_reason: str) -> bool:
    if not tool_calls:
        return False
    if stop_reason == "max_tokens":
        return True
    for tc in tool_calls:
        fn = tc.get("function") if isinstance(tc, dict) else None
        if not isinstance(fn, dict):
            continue
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args)
                if not isinstance(parsed, dict):
                    return True
            except Exception:
                return True
    return False


def _sanitize_body_for_log(obj: Any, *, max_str: int = 2000, max_depth: int = 6) -> Any:
    if max_depth <= 0:
        return "... [depth limit]"
    if isinstance(obj, str):
        cleaned = _SECRET_VAL_RE.sub("[REDACTED]", obj)
        return cleaned if len(cleaned) <= max_str else (cleaned[:max_str] + "... [truncated]")
    if isinstance(obj, list):
        return [_sanitize_body_for_log(x, max_str=max_str, max_depth=max_depth - 1) for x in obj[:40]]
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for idx, (k, v) in enumerate(obj.items()):
            if idx >= 50:
                out["_truncated_keys"] = True
                break
            kl = str(k).lower()
            if kl in ("api_key", "authorization", "x-api-key", "password", "secret"):
                out[str(k)] = "[REDACTED]"
            else:
                out[str(k)] = _sanitize_body_for_log(v, max_str=max_str, max_depth=max_depth - 1)
        return out
    return obj


def _build_request_spec(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    system_prompt: str,
    cfg: Dict[str, Any],
    max_tok: int,
    *,
    stream: bool = False,
    include_stream_options: bool = True,
) -> Tuple[str, str, Dict[str, str], Dict[str, Any]]:
    fmt = _normalize_format(cfg.get("format"))
    api_key = cfg["api_key"]
    if fmt == "anthropic":
        endpoint = _build_anthropic_messages_url(cfg["base_url"])
    elif fmt == "openai-responses":
        endpoint = _build_responses_url(cfg["base_url"])
    else:
        endpoint = _build_chat_url(cfg["base_url"])

    if not api_key and not _is_local_endpoint(endpoint):
        raise LLMClientError("尚未配置 API Key，请先在「AI 引擎 ➔ 模型接口」填写并保存")

    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": "RegisterHub-Lite/2.2",
    }

    thinking_mode = str(cfg.get("thinking_mode") or "none").strip()
    reasoning_effort = str(cfg.get("reasoning_effort") or "none").strip()

    if fmt == "anthropic":
        sys_txt, anth_msgs = _openai_messages_to_anthropic(messages, system_prompt)
        anth_msgs = anth_msgs or [{"role": "user", "content": "Hello"}]
        # Prompt cache breakpoint 3 (rolling history tail): mark the last cacheable block
        if len(anth_msgs) >= 2:
            last_msg = dict(anth_msgs[-1])
            last_content = last_msg.get("content")
            if isinstance(last_content, str):
                if last_content.strip():
                    last_msg["content"] = [
                        {
                            "type": "text",
                            "text": last_content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                    anth_msgs[-1] = last_msg
            elif isinstance(last_content, list) and last_content:
                copied_blocks = list(last_content)
                for b_idx in range(len(copied_blocks) - 1, -1, -1):
                    blk = copied_blocks[b_idx]
                    if not isinstance(blk, dict):
                        continue
                    btype = blk.get("type")
                    if (btype == "text" and str(blk.get("text") or "").strip()) or btype == "tool_result":
                        copied_blocks[b_idx] = {**blk, "cache_control": {"type": "ephemeral"}}
                        last_msg["content"] = copied_blocks
                        anth_msgs[-1] = last_msg
                        break
        payload: Dict[str, Any] = {
            "model": cfg["model"],
            "max_tokens": int(max_tok),
            "messages": anth_msgs,
        }
        if sys_txt:
            # Prompt cache breakpoint 1 (stable system prompt)
            payload["system"] = [
                {
                    "type": "text",
                    "text": sys_txt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        if thinking_mode == "enabled" and int(max_tok) > 1280:
            budget = min(max(1024, int(max_tok) // 2), int(max_tok) - 256)
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        else:
            payload["temperature"] = cfg["temperature"]
        anth_tools = _openai_tools_to_anthropic(tools)
        if anth_tools:
            # Prompt cache breakpoint 2 (stable tool definitions)
            anth_tools[-1] = {**anth_tools[-1], "cache_control": {"type": "ephemeral"}}
            payload["tools"] = anth_tools
        if stream:
            payload["stream"] = True
        headers["anthropic-version"] = "2023-06-01"
        if api_key:
            headers["x-api-key"] = api_key
    elif fmt == "openai-responses":
        payload = {
            "model": cfg["model"],
            "input": _openai_messages_to_responses_input(messages),
            "max_output_tokens": int(max_tok),
        }
        if system_prompt:
            payload["instructions"] = system_prompt
        if not _is_reasoning_model(cfg["model"], cfg["max_tokens_field"]):
            payload["temperature"] = cfg["temperature"]
        if reasoning_effort and reasoning_effort != "none":
            payload["reasoning"] = {"effort": reasoning_effort, "summary": "auto"}
        resp_tools = _openai_tools_to_responses(tools)
        if resp_tools:
            payload["tools"] = resp_tools
        if stream:
            payload["stream"] = True
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    else:
        full_messages: List[Dict[str, Any]] = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)
        payload = {
            "model": cfg["model"],
            "messages": full_messages,
            cfg["max_tokens_field"]: int(max_tok),
        }
        if not _is_reasoning_model(cfg["model"], cfg["max_tokens_field"]):
            payload["temperature"] = cfg["temperature"]
        if reasoning_effort and reasoning_effort != "none":
            payload["reasoning_effort"] = reasoning_effort
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if stream:
            payload["stream"] = True
            if include_stream_options:
                payload["stream_options"] = {"include_usage": True}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    return fmt, endpoint, headers, payload


def _parse_non_stream_json_response(
    data: Dict[str, Any],
    fmt: str,
    cfg: Dict[str, Any],
    max_tok: int,
    t_start: float,
) -> Dict[str, Any]:
    lat_ms = round((time.perf_counter() - t_start) * 1000, 1)

    if fmt == "anthropic":
        if data.get("type") == "error" or (data.get("error") and not data.get("content")):
            err_obj = data.get("error") or {}
            err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
            raise LLMClientError(f"接口报错: {err_msg}")
        text_parts: List[str] = []
        think_parts: List[str] = []
        norm_tool_calls: List[Dict[str, Any]] = []
        for idx, blk in enumerate(data.get("content") or []):
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "text":
                text_parts.append(str(blk.get("text") or ""))
            elif btype == "thinking":
                think_parts.append(str(blk.get("thinking") or ""))
            elif btype == "tool_use":
                norm_tool_calls.append({
                    "id": str(blk.get("id") or f"call_{idx + 1}"),
                    "type": "function",
                    "function": {
                        "name": str(blk.get("name") or ""),
                        "arguments": json.dumps(blk.get("input") or {}, ensure_ascii=False),
                    },
                })
        prompt_t, comp_t, cache_r, cache_w = _extract_usage_metrics(data.get("usage") or {}, fmt)
        model_used = str(data.get("model") or cfg["model"])
        raw_fr = str(data.get("stop_reason") or "")
        stop_reason = _normalize_stop_reason(raw_fr)
        content = "\n".join(p for p in text_parts if p)
        thinking = "\n".join(p for p in think_parts if p)
    elif fmt == "openai-responses":
        if data.get("error") and not data.get("output"):
            err_obj = data["error"]
            err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
            raise LLMClientError(f"接口报错: {err_msg}")
        text_parts = []
        think_parts = []
        norm_tool_calls = []
        if isinstance(data.get("output_text"), str) and data["output_text"]:
            text_parts.append(data["output_text"])
        for idx, item in enumerate(data.get("output") or []):
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "message" and not text_parts:
                for sub in item.get("content") or []:
                    if isinstance(sub, dict) and sub.get("type") in ("output_text", "text"):
                        text_parts.append(str(sub.get("text") or ""))
            elif itype == "reasoning":
                for sub in item.get("summary") or []:
                    if isinstance(sub, dict) and sub.get("text"):
                        think_parts.append(str(sub["text"]))
            elif itype == "function_call":
                fn_args = item.get("arguments")
                if not isinstance(fn_args, str):
                    fn_args = json.dumps(fn_args or {}, ensure_ascii=False)
                norm_tool_calls.append({
                    "id": str(item.get("call_id") or item.get("id") or f"call_{idx + 1}"),
                    "type": "function",
                    "function": {"name": str(item.get("name") or ""), "arguments": fn_args},
                })
        prompt_t, comp_t, cache_r, cache_w = _extract_usage_metrics(data.get("usage") or {}, fmt)
        model_used = str(data.get("model") or cfg["model"])
        raw_fr = str(data.get("status") or "")
        stop_reason = _normalize_stop_reason(raw_fr)
        content = "\n".join(p for p in text_parts if p)
        thinking = "\n".join(p for p in think_parts if p)
    else:
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
        norm_tool_calls = []
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
        prompt_t, comp_t, cache_r, cache_w = _extract_usage_metrics(data.get("usage") or {}, fmt)
        model_used = str(data.get("model") or cfg["model"])
        raw_fr = str(choice0.get("finish_reason") or "")
        stop_reason = _normalize_stop_reason(raw_fr)

    total_t = prompt_t + comp_t
    hit_pct = min(100, max(0, round(cache_r / prompt_t * 100))) if prompt_t > 0 else 0
    tok_s = round(comp_t / max(0.05, lat_ms / 1000.0), 1) if comp_t > 0 else 0.0
    truncated_tc = _check_truncated_tool_calls(norm_tool_calls, stop_reason)

    return {
        "ok": True,
        "content": content,
        "thinking": thinking,
        "tool_calls": norm_tool_calls,
        "usage": {
            "prompt_tokens": prompt_t,
            "completion_tokens": comp_t,
            "total_tokens": total_t,
            "cache_read_tokens": cache_r,
            "cache_write_tokens": cache_w,
        },
        "input_tokens": prompt_t,
        "output_tokens": comp_t,
        "cache_read_tokens": cache_r,
        "cache_write_tokens": cache_w,
        "cache_hit_pct": hit_pct,
        "latency_ms": lat_ms,
        "ttft_ms": lat_ms,
        "tok_s": tok_s,
        "model": model_used,
        "finish_reason": raw_fr,
        "stop_reason": stop_reason,
        "effective_max_tokens": int(max_tok),
        "truncated_tool_calls": truncated_tc,
    }


def _emit_call_log(
    cfg: Dict[str, Any],
    fmt: str,
    t_start: float,
    *,
    ok: bool,
    session_id: str = "",
    model_used: str = "",
    in_t: int = 0,
    out_t: int = 0,
    cache_r: int = 0,
    cache_w: int = 0,
    ttft_ms: float = 0.0,
    tok_s: float = 0.0,
    err_msg: str = "",
    req_body: Any = None,
    resp_body: Any = None,
) -> None:
    lat_ms = round((time.perf_counter() - t_start) * 1000, 1)
    hit_pct = min(100, max(0, round(cache_r / in_t * 100))) if in_t > 0 else 0
    entry: Dict[str, Any] = {
        "id": f"call_{int(time.time() * 1000)}_{secrets.token_hex(3)}",
        "ts": time.strftime("%H:%M:%S"),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": session_id,
        "profile_id": cfg.get("profile_id") or "",
        "profile_name": cfg.get("profile_name") or cfg["model"],
        "format": fmt,
        "model": model_used or cfg["model"],
        "ok": ok,
        "status": "ok" if ok else "error",
        "latency_ms": lat_ms,
        "ttft_ms": round(ttft_ms or lat_ms, 1),
        "tok_s": round(tok_s, 1),
        "input_tokens": in_t,
        "output_tokens": out_t,
        "prompt_tokens": in_t,
        "completion_tokens": out_t,
        "total_tokens": in_t + out_t,
        "cache_read_tokens": cache_r,
        "cache_write_tokens": cache_w,
        "cache_hit_pct": hit_pct,
        "context_tokens": in_t,
        "error": sanitize_error_text(err_msg, max_len=240) if err_msg else "",
    }
    if req_body is not None or resp_body is not None:
        entry["has_bodies"] = True
        if req_body is not None:
            entry["request_body"] = _sanitize_body_for_log(req_body)
        if resp_body is not None:
            entry["response_body"] = _sanitize_body_for_log(resp_body)
    _record_llm_call(entry)


def chat_completion(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    system_prompt: str = "",
    *,
    config_override: Optional[Dict[str, Any]] = None,
    config_path: Optional[Path] = None,
    timeout: float = 90.0,
    max_tokens_override: Optional[int] = None,
    session_id: str = "",
) -> Dict[str, Any]:
    """Invoke OpenAI Chat, OpenAI Responses, or Anthropic Messages with pure stdlib `urllib.request`."""
    t_start = time.perf_counter()
    cfg = _merge_runtime_config(config_override, config_path=config_path)
    max_tok = max_tokens_override if max_tokens_override is not None else cfg["max_tokens"]
    fmt, endpoint, headers, payload = _build_request_spec(
        messages, tools, system_prompt, cfg, int(max_tok), stream=False
    )

    try:
        _guard_live_endpoint_dns(endpoint)
    except Exception as exc:
        err_s = sanitize_error_text(f"目标端点被安全策略拦截: {exc}")
        _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg=err_s, req_body=payload)
        raise LLMClientError(err_s) from exc

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
        err_s = sanitize_error_text(f"HTTP {he.code}: {_parse_http_error(he)}")
        _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg=err_s, req_body=payload)
        raise LLMClientError(err_s, status_code=he.code) from he
    except urllib.error.URLError as ue:
        err_s = sanitize_error_text(f"网络请求失败: {ue.reason}")
        _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg=err_s, req_body=payload)
        raise LLMClientError(err_s) from ue
    except TimeoutError as te:
        err_s = f"LLM 请求超时 ({int(timeout)}s)"
        _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg=err_s, req_body=payload)
        raise LLMClientError(err_s) from te
    except Exception as exc:
        err_s = sanitize_error_text(f"LLM 调用异常: {exc}")
        _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg=err_s, req_body=payload)
        raise LLMClientError(err_s) from exc

    try:
        data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    except Exception as exc:
        err_s = sanitize_error_text(f"LLM 返回内容不是合法 JSON: {exc}")
        _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg=err_s, req_body=payload)
        raise LLMClientError(err_s) from exc

    if not isinstance(data, dict):
        _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg="LLM 响应根节点非 JSON 对象", req_body=payload)
        raise LLMClientError("LLM 响应根节点非 JSON 对象")

    try:
        res = _parse_non_stream_json_response(data, fmt, cfg, int(max_tok), t_start)
    except LLMClientError as exc:
        _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg=str(exc), req_body=payload, resp_body=data)
        raise
    except Exception as exc:
        err_s = sanitize_error_text(f"LLM 响应解析失败: {exc}")
        _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg=err_s, req_body=payload, resp_body=data)
        raise LLMClientError(err_s) from exc

    _emit_call_log(
        cfg,
        fmt,
        t_start,
        ok=True,
        session_id=session_id,
        model_used=res["model"],
        in_t=res["input_tokens"],
        out_t=res["output_tokens"],
        cache_r=res["cache_read_tokens"],
        cache_w=res["cache_write_tokens"],
        ttft_ms=res["ttft_ms"],
        tok_s=res["tok_s"],
        req_body=payload,
        resp_body={"content": res["content"][:2000], "thinking": res["thinking"][:1000], "tool_calls": res["tool_calls"], "usage": res["usage"]},
    )
    return res


def stream_chat_completion(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    system_prompt: str = "",
    *,
    config_override: Optional[Dict[str, Any]] = None,
    config_path: Optional[Path] = None,
    timeout: float = 120.0,
    max_tokens_override: Optional[int] = None,
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    stop_event: Optional[threading.Event] = None,
    session_id: str = "",
) -> Dict[str, Any]:
    """True SSE streaming completion (OpenAI Chat / Anthropic Messages / OpenAI Responses) using stdlib only."""
    t_start = time.perf_counter()
    cfg = _merge_runtime_config(config_override, config_path=config_path)
    max_tok = max_tokens_override if max_tokens_override is not None else cfg["max_tokens"]

    def _fire(ev: Dict[str, Any]) -> None:
        if callable(on_event):
            try:
                on_event(ev)
            except Exception:
                pass

    if cfg.get("streaming") is False:
        res = chat_completion(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            config_override=config_override,
            config_path=config_path,
            timeout=timeout,
            max_tokens_override=max_tokens_override,
            session_id=session_id,
        )
        if res.get("thinking"):
            _fire({"type": "thinking_delta", "delta": str(res["thinking"])})
        if res.get("content"):
            _fire({"type": "text_delta", "delta": str(res["content"])})
        return res

    include_stream_opts = True
    fmt, endpoint, headers, payload = _build_request_spec(
        messages, tools, system_prompt, cfg, int(max_tok), stream=True, include_stream_options=include_stream_opts
    )
    try:
        _guard_live_endpoint_dns(endpoint)
    except Exception as exc:
        err_s = sanitize_error_text(f"目标端点被安全策略拦截: {exc}")
        _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg=err_s, req_body=payload)
        raise LLMClientError(err_s) from exc
    opener = _build_opener(cfg.get("proxy_url", ""))

    def _open_stream(req_payload: Dict[str, Any]) -> Any:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(req_payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        return opener.open(req, timeout=timeout)

    try:
        resp = _open_stream(payload)
    except urllib.error.HTTPError as he:
        err_detail = _parse_http_error(he)
        if fmt == "openai" and include_stream_opts and he.code == 400 and "stream_options" in err_detail.lower():
            fmt, endpoint, headers, payload = _build_request_spec(
                messages, tools, system_prompt, cfg, int(max_tok), stream=True, include_stream_options=False
            )
            try:
                resp = _open_stream(payload)
            except urllib.error.HTTPError as he2:
                err_s = f"HTTP {he2.code}: {_parse_http_error(he2)}"
                _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg=err_s, req_body=payload)
                raise LLMClientError(err_s, status_code=he2.code) from he2
            except Exception as exc2:
                err_s = f"LLM 流式调用异常: {exc2}"
                _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg=err_s, req_body=payload)
                raise LLMClientError(err_s) from exc2
        else:
            err_s = f"HTTP {he.code}: {err_detail}"
            _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg=err_s, req_body=payload)
            raise LLMClientError(err_s, status_code=he.code) from he
    except urllib.error.URLError as ue:
        err_s = f"网络请求失败: {ue.reason}"
        _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg=err_s, req_body=payload)
        raise LLMClientError(err_s) from ue
    except TimeoutError as te:
        err_s = f"LLM 流式请求超时 ({int(timeout)}s)"
        _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg=err_s, req_body=payload)
        raise LLMClientError(err_s) from te
    except Exception as exc:
        err_s = f"LLM 流式调用异常: {exc}"
        _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg=err_s, req_body=payload)
        raise LLMClientError(err_s) from exc

    content_parts: List[str] = []
    thinking_parts: List[str] = []
    # OpenAI tool_calls by index / Anthropic blocks by index / Responses by call_id or item_id
    tc_map: Dict[Any, Dict[str, Any]] = {}
    tc_order: List[Any] = []
    anth_block_types: Dict[int, str] = {}

    model_used = cfg["model"]
    raw_fr = ""
    prompt_t = 0
    comp_t = 0
    cache_r = 0
    cache_w = 0
    first_token_at: Optional[float] = None
    aborted = False
    sse_error = ""

    def _mark_first_token() -> None:
        nonlocal first_token_at
        if first_token_at is None:
            first_token_at = time.perf_counter()

    try:
        with resp:
            ctype = str(resp.headers.get("Content-Type") or "").lower()
            # Graceful fallback if upstream proxy ignored stream=true and returned JSON
            if "application/json" in ctype and "text/event-stream" not in ctype:
                raw_bytes = resp.read()
                data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
                if not isinstance(data, dict):
                    raise LLMClientError("LLM 响应根节点非 JSON 对象")
                res = _parse_non_stream_json_response(data, fmt, cfg, int(max_tok), t_start)
                if res.get("thinking"):
                    _fire({"type": "thinking_delta", "delta": res["thinking"]})
                if res.get("content"):
                    _fire({"type": "text_delta", "delta": res["content"]})
                _emit_call_log(
                    cfg, fmt, t_start, ok=True, session_id=session_id,
                    model_used=res["model"], in_t=res["input_tokens"], out_t=res["output_tokens"],
                    cache_r=res["cache_read_tokens"], cache_w=res["cache_write_tokens"],
                    ttft_ms=res["ttft_ms"], tok_s=res["tok_s"], req_body=payload,
                    resp_body={"content": res["content"][:2000], "tool_calls": res["tool_calls"], "usage": res["usage"]},
                )
                return res

            cur_event_name = ""
            while True:
                if stop_event is not None and stop_event.is_set():
                    aborted = True
                    break
                raw_line = resp.readline()
                if not raw_line:
                    break
                if stop_event is not None and stop_event.is_set():
                    aborted = True
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    cur_event_name = ""
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    cur_event_name = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    # Handle proxies that send a raw JSON error line without SSE prefix
                    if line.startswith("{"):
                        try:
                            err_j = json.loads(line)
                            if isinstance(err_j, dict) and err_j.get("error"):
                                err_o = err_j["error"]
                                sse_error = err_o.get("message") if isinstance(err_o, dict) else str(err_o)
                                break
                        except Exception:
                            pass
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                if data_str == "[DONE]":
                    break
                try:
                    ev = json.loads(data_str)
                except Exception:
                    continue
                if not isinstance(ev, dict):
                    continue

                if fmt == "anthropic":
                    ev_type = str(ev.get("type") or cur_event_name or "")
                    if ev_type == "error":
                        eo = ev.get("error") or {}
                        sse_error = eo.get("message") if isinstance(eo, dict) else str(eo)
                        break
                    if ev_type == "message_start":
                        msg_obj = ev.get("message") or {}
                        if isinstance(msg_obj, dict):
                            if msg_obj.get("model"):
                                model_used = str(msg_obj["model"])
                            u_m = msg_obj.get("usage")
                            if isinstance(u_m, dict):
                                p0, c0, cr0, cw0 = _extract_usage_metrics(u_m, fmt)
                                prompt_t = max(prompt_t, p0)
                                comp_t = max(comp_t, c0)
                                cache_r = max(cache_r, cr0)
                                cache_w = max(cache_w, cw0)
                    elif ev_type == "content_block_start":
                        idx = int(ev.get("index") or 0)
                        cb = ev.get("content_block") or {}
                        if isinstance(cb, dict):
                            btype = str(cb.get("type") or "")
                            anth_block_types[idx] = btype
                            if btype == "tool_use":
                                tc_map[idx] = {
                                    "id": str(cb.get("id") or f"toolu_{idx}"),
                                    "name": str(cb.get("name") or ""),
                                    "args_parts": [],
                                }
                                tc_order.append(idx)
                                _mark_first_token()
                                _fire({"type": "tool_call_delta", "name": tc_map[idx]["name"], "delta": ""})
                            elif btype == "text" and cb.get("text"):
                                txt = str(cb["text"])
                                content_parts.append(txt)
                                _mark_first_token()
                                _fire({"type": "text_delta", "delta": txt})
                            elif btype == "thinking" and cb.get("thinking"):
                                th = str(cb["thinking"])
                                thinking_parts.append(th)
                                _mark_first_token()
                                _fire({"type": "thinking_delta", "delta": th})
                    elif ev_type == "content_block_delta":
                        idx = int(ev.get("index") or 0)
                        delta = ev.get("delta") or {}
                        if not isinstance(delta, dict):
                            continue
                        dtype = str(delta.get("type") or "")
                        if dtype == "text_delta":
                            txt = str(delta.get("text") or "")
                            if txt:
                                content_parts.append(txt)
                                _mark_first_token()
                                _fire({"type": "text_delta", "delta": txt})
                        elif dtype == "thinking_delta":
                            th = str(delta.get("thinking") or "")
                            if th:
                                thinking_parts.append(th)
                                _mark_first_token()
                                _fire({"type": "thinking_delta", "delta": th})
                        elif dtype == "input_json_delta":
                            part = str(delta.get("partial_json") or "")
                            if part and idx in tc_map:
                                tc_map[idx]["args_parts"].append(part)
                                _mark_first_token()
                                _fire({"type": "tool_call_delta", "name": tc_map[idx]["name"], "delta": part})
                    elif ev_type == "message_delta":
                        delta = ev.get("delta") or {}
                        if isinstance(delta, dict) and delta.get("stop_reason"):
                            raw_fr = str(delta["stop_reason"])
                        u_d = ev.get("usage")
                        if isinstance(u_d, dict):
                            if u_d.get("output_tokens") is not None:
                                comp_t = max(comp_t, _safe_nonneg_int(u_d.get("output_tokens")))
                            if u_d.get("input_tokens") is not None:
                                p1, c1, cr1, cw1 = _extract_usage_metrics(u_d, fmt)
                                prompt_t = max(prompt_t, p1)
                                comp_t = max(comp_t, c1)
                                cache_r = max(cache_r, cr1)
                                cache_w = max(cache_w, cw1)
                    elif ev_type == "message_stop":
                        break

                elif fmt == "openai-responses":
                    ev_type = str(ev.get("type") or cur_event_name or "")
                    if ev_type == "error":
                        eo = ev.get("error") or {}
                        sse_error = eo.get("message") if isinstance(eo, dict) else str(eo)
                        break
                    if ev_type == "response.output_text.delta":
                        txt = str(ev.get("delta") or "")
                        if txt:
                            content_parts.append(txt)
                            _mark_first_token()
                            _fire({"type": "text_delta", "delta": txt})
                    elif ev_type in ("response.reasoning_summary_text.delta", "response.reasoning.delta"):
                        th = str(ev.get("delta") or "")
                        if th:
                            thinking_parts.append(th)
                            _mark_first_token()
                            _fire({"type": "thinking_delta", "delta": th})
                    elif ev_type == "response.output_item.added":
                        item = ev.get("item") or {}
                        if isinstance(item, dict) and item.get("type") == "function_call":
                            key = str(item.get("id") or item.get("call_id") or f"fc_{len(tc_order)}")
                            tc_map[key] = {
                                "id": str(item.get("call_id") or item.get("id") or key),
                                "name": str(item.get("name") or ""),
                                "args_parts": [],
                            }
                            if key not in tc_order:
                                tc_order.append(key)
                            _mark_first_token()
                            _fire({"type": "tool_call_delta", "name": tc_map[key]["name"], "delta": ""})
                    elif ev_type == "response.function_call_arguments.delta":
                        key = str(ev.get("item_id") or ev.get("call_id") or (tc_order[-1] if tc_order else "fc_0"))
                        part = str(ev.get("delta") or "")
                        if key not in tc_map:
                            tc_map[key] = {"id": key, "name": str(ev.get("name") or ""), "args_parts": []}
                            tc_order.append(key)
                        if part:
                            tc_map[key]["args_parts"].append(part)
                            _mark_first_token()
                            _fire({"type": "tool_call_delta", "name": tc_map[key]["name"], "delta": part})
                    elif ev_type in ("response.completed", "response.incomplete"):
                        r_obj = ev.get("response") or {}
                        if isinstance(r_obj, dict):
                            if r_obj.get("model"):
                                model_used = str(r_obj["model"])
                            raw_fr = str(r_obj.get("status") or ("incomplete" if ev_type == "response.incomplete" else "completed"))
                            u_r = r_obj.get("usage")
                            if isinstance(u_r, dict):
                                p1, c1, cr1, cw1 = _extract_usage_metrics(u_r, fmt)
                                prompt_t, comp_t, cache_r, cache_w = p1, c1, cr1, cw1

                else:
                    # OpenAI Chat Completions SSE
                    if ev.get("error") and not ev.get("choices"):
                        eo = ev["error"]
                        sse_error = eo.get("message") if isinstance(eo, dict) else str(eo)
                        break
                    if ev.get("model"):
                        model_used = str(ev["model"])
                    if isinstance(ev.get("usage"), dict):
                        p1, c1, cr1, cw1 = _extract_usage_metrics(ev["usage"], fmt)
                        if p1 or c1:
                            prompt_t, comp_t, cache_r, cache_w = p1, c1, cr1, cw1
                    choices = ev.get("choices") or []
                    if not choices or not isinstance(choices[0], dict):
                        continue
                    c0 = choices[0]
                    if c0.get("finish_reason"):
                        raw_fr = str(c0["finish_reason"])
                    delta = c0.get("delta") or {}
                    if not isinstance(delta, dict):
                        continue
                    d_think = delta.get("reasoning_content")
                    if d_think is None:
                        d_think = delta.get("reasoning")
                    if isinstance(d_think, str) and d_think:
                        thinking_parts.append(d_think)
                        _mark_first_token()
                        _fire({"type": "thinking_delta", "delta": d_think})
                    d_content = delta.get("content")
                    if isinstance(d_content, str) and d_content:
                        content_parts.append(d_content)
                        _mark_first_token()
                        _fire({"type": "text_delta", "delta": d_content})
                    for tc in delta.get("tool_calls") or []:
                        if not isinstance(tc, dict):
                            continue
                        idx = tc.get("index")
                        if idx is None:
                            idx = len(tc_order)
                        if idx not in tc_map:
                            tc_map[idx] = {
                                "id": str(tc.get("id") or f"call_{len(tc_order) + 1}"),
                                "name": "",
                                "args_parts": [],
                            }
                            tc_order.append(idx)
                        elif tc.get("id"):
                            tc_map[idx]["id"] = str(tc["id"])
                        fn = tc.get("function") or {}
                        if isinstance(fn, dict):
                            if fn.get("name"):
                                tc_map[idx]["name"] += str(fn["name"])
                            arg_d = fn.get("arguments")
                            if isinstance(arg_d, str) and arg_d:
                                tc_map[idx]["args_parts"].append(arg_d)
                                _mark_first_token()
                                _fire({"type": "tool_call_delta", "name": tc_map[idx]["name"], "delta": arg_d})
                            elif fn.get("name"):
                                _mark_first_token()
                                _fire({"type": "tool_call_delta", "name": tc_map[idx]["name"], "delta": ""})
    except Exception as exc:
        if stop_event is not None and stop_event.is_set():
            aborted = True
        else:
            err_s = f"LLM 流式读取中断: {exc}"
            _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg=err_s, req_body=payload)
            raise LLMClientError(err_s) from exc

    content = "".join(content_parts)
    thinking = "".join(thinking_parts)
    if aborted:
        lat_ms = round((time.perf_counter() - t_start) * 1000, 1)
        ttft_ms = round((first_token_at - t_start) * 1000, 1) if first_token_at is not None else lat_ms
        return {
            "ok": False,
            "aborted": True,
            "content": content,
            "thinking": thinking,
            "partial_content": content,
            "partial_thinking": thinking,
            "tool_calls": [],
            "usage": {"prompt_tokens": prompt_t, "completion_tokens": comp_t, "total_tokens": prompt_t + comp_t},
            "input_tokens": prompt_t,
            "output_tokens": comp_t,
            "cache_read_tokens": cache_r,
            "cache_write_tokens": cache_w,
            "cache_hit_pct": min(100, max(0, round(cache_r / prompt_t * 100))) if prompt_t > 0 else 0,
            "latency_ms": lat_ms,
            "ttft_ms": ttft_ms,
            "tok_s": 0.0,
            "model": model_used,
            "finish_reason": "aborted",
            "stop_reason": "aborted",
            "effective_max_tokens": int(max_tok),
            "truncated_tool_calls": False,
        }

    if sse_error:
        err_s = f"接口报错: {sse_error}"
        _emit_call_log(cfg, fmt, t_start, ok=False, session_id=session_id, err_msg=err_s, req_body=payload)
        raise LLMClientError(err_s)

    norm_tool_calls: List[Dict[str, Any]] = []
    for k in tc_order:
        ent = tc_map[k]
        fn_name = str(ent.get("name") or "").strip()
        if not fn_name:
            continue
        raw_args = "".join(ent.get("args_parts") or [])
        if not raw_args.strip():
            raw_args = "{}"
        norm_tool_calls.append({
            "id": str(ent.get("id") or f"call_{len(norm_tool_calls) + 1}"),
            "type": "function",
            "function": {"name": fn_name, "arguments": raw_args},
        })

    t_end = time.perf_counter()
    lat_ms = round((t_end - t_start) * 1000, 1)
    ttft_ms = round((first_token_at - t_start) * 1000, 1) if first_token_at is not None else lat_ms
    # Estimate output tokens if upstream gateway omitted usage in stream mode
    if comp_t == 0 and (content or thinking or norm_tool_calls):
        approx_chars = len(content) + len(thinking) + sum(len(tc["function"]["arguments"]) for tc in norm_tool_calls)
        comp_t = max(1, approx_chars // 3)
    gen_secs = max(0.05, (t_end - first_token_at) if first_token_at is not None else (t_end - t_start))
    tok_s = round(comp_t / gen_secs, 1) if comp_t > 0 else 0.0
    total_t = prompt_t + comp_t
    hit_pct = min(100, max(0, round(cache_r / prompt_t * 100))) if prompt_t > 0 else 0
    stop_reason = _normalize_stop_reason(raw_fr)
    truncated_tc = _check_truncated_tool_calls(norm_tool_calls, stop_reason)

    _emit_call_log(
        cfg,
        fmt,
        t_start,
        ok=True,
        session_id=session_id,
        model_used=model_used,
        in_t=prompt_t,
        out_t=comp_t,
        cache_r=cache_r,
        cache_w=cache_w,
        ttft_ms=ttft_ms,
        tok_s=tok_s,
        req_body=payload,
        resp_body={"content": content[:2000], "thinking": thinking[:1000], "tool_calls": norm_tool_calls, "usage": {"prompt_tokens": prompt_t, "completion_tokens": comp_t, "cache_read_tokens": cache_r, "cache_write_tokens": cache_w}},
    )

    return {
        "ok": True,
        "content": content,
        "thinking": thinking,
        "tool_calls": norm_tool_calls,
        "usage": {
            "prompt_tokens": prompt_t,
            "completion_tokens": comp_t,
            "total_tokens": total_t,
            "cache_read_tokens": cache_r,
            "cache_write_tokens": cache_w,
        },
        "input_tokens": prompt_t,
        "output_tokens": comp_t,
        "cache_read_tokens": cache_r,
        "cache_write_tokens": cache_w,
        "cache_hit_pct": hit_pct,
        "latency_ms": lat_ms,
        "ttft_ms": ttft_ms,
        "tok_s": tok_s,
        "model": model_used,
        "finish_reason": raw_fr,
        "stop_reason": stop_reason,
        "effective_max_tokens": int(max_tok),
        "truncated_tool_calls": truncated_tc,
    }


_ORIG_CHAT_COMPLETION = chat_completion
_ORIG_STREAM_CHAT_COMPLETION = stream_chat_completion


def discover_models(
    overrides: Optional[Dict[str, Any]] = None,
    config_path: Optional[Path] = None,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """Probe `<base>/models` and `<root>/v1/models` with stdlib `urllib.request` (OpenAI & Anthropic)."""
    try:
        cfg = _merge_runtime_config(overrides, config_path=config_path)
    except Exception as exc:
        return {
            "ok": False,
            "models": [],
            "model_context_windows": {},
            "source_url": "",
            "count": 0,
            "error": str(exc),
        }

    fmt = _normalize_format(cfg.get("format"))
    headers: Dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": "RegisterHub-Lite/2.1",
    }
    if fmt == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if cfg.get("api_key"):
            headers["x-api-key"] = cfg["api_key"]
    elif cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    opener = _build_opener(cfg.get("proxy_url", ""))
    candidates = _build_models_urls(cfg["base_url"])
    last_error = ""
    last_url = candidates[0] if candidates else ""

    for url in candidates:
        last_url = url
        try:
            _guard_live_endpoint_dns(url)
        except Exception as exc:
            last_error = sanitize_error_text(f"目标端点被安全策略拦截: {exc}")
            break
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw_bytes = resp.read(2_000_000)
        except urllib.error.HTTPError as he:
            last_error = sanitize_error_text(f"HTTP {he.code}: {_parse_http_error(he)}")
            if he.code in (401, 403):
                break
            continue
        except urllib.error.URLError as ue:
            last_error = sanitize_error_text(f"网络请求失败: {ue.reason}")
            continue
        except TimeoutError:
            last_error = f"拉取模型列表超时 ({int(timeout)}s)"
            continue
        except Exception as exc:
            last_error = sanitize_error_text(f"拉取模型列表异常: {exc}")
            continue

        try:
            parsed = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        except Exception as exc:
            last_error = f"模型接口返回非合法 JSON: {exc}"
            continue

        models, ctx_map = _extract_model_ids_and_ctx(parsed)
        if models:
            from core.llm_context import infer_model_context_window_k

            active_model = str((overrides or {}).get("model") or cfg.get("model") or "").strip()
            detected_ctx = infer_model_context_window_k(active_model, ctx_map) if active_model else None
            return {
                "ok": True,
                "models": models,
                "model_context_windows": ctx_map,
                "detected_context_window_k": detected_ctx,
                "source_url": url,
                "count": len(models),
                "error": "",
            }
        last_error = "接口返回空模型列表"

    return {
        "ok": False,
        "models": [],
        "model_context_windows": {},
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
            "error": sanitize_error_text(str(exc)),
        }
