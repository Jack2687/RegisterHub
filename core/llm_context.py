"""三仓共享 LLM 上下文窗口元数据提取与模型家族推断引擎（纯标准库，零外部依赖）。

单一真相源（Mother / PRO / Community 三仓 SHA-256 严格一致）：
  1. 从 `/v1/models` 响应提取各模型真实上下文窗口（兼容 10 种主流字段名与嵌套结构）；
  2. 统一将原始 Token 数归一化为 K（16..1000，其中 1000 = 1M）；
  3. 上游未返回上下文元数据时，按统一模型家族规则推断真实窗口（如 DeepSeek-V4 / Gemini / GPT-4.1+ / Claude 4.6+ = 1M）；
  4. 为 Agent 自动压缩（85% 阈值）与控制台 UI 提供完全一致的上下文窗口计算口径。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Optional

MIN_CONTEXT_WINDOW_K = 16
MAX_CONTEXT_WINDOW_K = 1000
DEFAULT_CONTEXT_WINDOW_K = 128

_AT_K_RE = re.compile(r"@(\d+)k\b")
_ONE_M_SUFFIX_RE = re.compile(r"[@\-_]1m\b")

_1M_MODEL_PREFIXES: tuple[str, ...] = (
    "gemini-1.5",
    "gemini-2.",
    "gemini-3",
    "gemini-4",
    "gpt-4.1",
    "gpt-5",
    "gpt-6",
    "deepseek-v4",
    "mimo-v2",
    "kimi-k3",
    "minimax-m3",
    "qwen4",
    "qwen3.8",
    "claude-opus-4-6",
    "claude-opus-4.6",
    "claude-opus-4-7",
    "claude-opus-4.7",
    "claude-opus-4-8",
    "claude-opus-4.8",
    "claude-opus-5",
    "claude-sonnet-4-6",
    "claude-sonnet-4.6",
    "claude-sonnet-5",
    "claude-haiku-5",
    "claude-fable-5",
    "glm-5.2",
    "glm-5.3-flash",
    "glm-5.3-fast",
    "mistral-large-4",
)

_256K_MODEL_PREFIXES: tuple[str, ...] = (
    "kimi-k2",
    "qwen3",
    "grok-4",
)

_200K_MODEL_PREFIXES: tuple[str, ...] = (
    "claude-3",
    "claude-4",
    "claude-sonnet-4",
    "claude-opus-4",
    "claude-haiku-4",
    "o1",
    "o3",
    "o4",
    "glm-5",
)

_CONTEXT_META_KEYS: tuple[str, ...] = (
    "context_window",
    "context_length",
    "max_input_tokens",
    "input_token_limit",
    "inputTokenLimit",
    "max_model_len",
    "max_context_length",
    "max_context_tokens",
)


def _clamp_ctx_k(val: int, default: int = DEFAULT_CONTEXT_WINDOW_K) -> int:
    try:
        iv = int(val)
    except (TypeError, ValueError):
        return default
    return max(MIN_CONTEXT_WINDOW_K, min(MAX_CONTEXT_WINDOW_K, iv))


def tokens_to_context_k(raw_tokens: Any) -> Optional[int]:
    """将上游 /models 返回的原始上下文 token 数（如 1048576 / 1000000 / 200000）归一化为 K（16..1000，1000=1M）。"""
    if raw_tokens is None or isinstance(raw_tokens, bool):
        return None
    try:
        val = int(float(raw_tokens))
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    if val <= 2048:
        k = val
    elif val >= 1_000_000:
        k = 1000
    elif val % 1024 == 0:
        k = val // 1024
    else:
        k = round(val / 1000)
    return _clamp_ctx_k(k)


def extract_model_context_windows(
    body: Any,
    *,
    skip_model_fn: Optional[Callable[[str], bool]] = None,
) -> Dict[str, int]:
    """从 /models JSON 响应提取每个模型的真实上下文窗口（单位 K，如 1048576 -> 1000 即 1M）。"""
    items: Any = None
    if isinstance(body, dict):
        if isinstance(body.get("data"), list):
            items = body["data"]
        elif isinstance(body.get("models"), list):
            items = body["models"]
    elif isinstance(body, list):
        items = body
    if not isinstance(items, list):
        return {}

    out: Dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
        if mid.startswith("models/"):
            mid = mid[len("models/"):]
        if not mid or (skip_model_fn is not None and skip_model_fn(mid)):
            continue

        raw_ctx: Any = None
        for key in _CONTEXT_META_KEYS:
            if item.get(key):
                raw_ctx = item.get(key)
                break
        if not raw_ctx and isinstance(item.get("top_provider"), dict):
            raw_ctx = item["top_provider"].get("context_length")
        if not raw_ctx and isinstance(item.get("limits"), dict):
            raw_ctx = item["limits"].get("context") or item["limits"].get("input")

        k = tokens_to_context_k(raw_ctx)
        if k is not None:
            out[mid] = k
    return out


def sanitize_model_context_windows(
    raw: Any,
    limit: int = 1000,
    *,
    skip_model_fn: Optional[Callable[[str], bool]] = None,
) -> Dict[str, int]:
    """清洗持久化的 model_context_windows 映射表，过滤非法键与越界值。"""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, int] = {}
    for k, v in raw.items():
        mid = str(k or "").strip()
        if not mid or (skip_model_fn is not None and skip_model_fn(mid)):
            continue
        ctx_k = tokens_to_context_k(v)
        if ctx_k is not None:
            out[mid] = ctx_k
            if len(out) >= limit:
                break
    return out


def lookup_model_context_window_k(
    model_id: str,
    model_contexts: Optional[Dict[str, int]] = None,
) -> Optional[int]:
    """仅从上游 /models 元数据映射中查找模型上下文窗口（精确 -> 去前缀短名 -> 忽略大小写）。"""
    mid = (model_id or "").strip()
    if not mid or not isinstance(model_contexts, dict) or not model_contexts:
        return None
    if mid in model_contexts:
        return tokens_to_context_k(model_contexts[mid])
    short = mid.rsplit("/", 1)[-1]
    if short in model_contexts:
        return tokens_to_context_k(model_contexts[short])
    low_mid = mid.lower()
    low_short = short.lower()
    for k, v in model_contexts.items():
        kl = str(k or "").strip().lower()
        if kl == low_mid or kl == low_short or kl.endswith("/" + low_short):
            return tokens_to_context_k(v)
    return None


def infer_model_context_window_k(
    model_id: str,
    model_contexts: Optional[Dict[str, int]] = None,
) -> Optional[int]:
    """优先命中上游 /models 真实元数据，未返回时按统一模型家族规则推断真实上下文窗口（单位 K）。"""
    mid = (model_id or "").strip()
    if not mid:
        return None

    from_meta = lookup_model_context_window_k(mid, model_contexts)
    if from_meta is not None:
        return from_meta

    low = mid.lower()
    m_at = _AT_K_RE.search(low)
    if m_at:
        return _clamp_ctx_k(int(m_at.group(1)))
    if _ONE_M_SUFFIX_RE.search(low):
        return MAX_CONTEXT_WINDOW_K

    short_l = low.rsplit("/", 1)[-1]
    if any(short_l.startswith(pfx) for pfx in _1M_MODEL_PREFIXES):
        return MAX_CONTEXT_WINDOW_K
    if any(short_l.startswith(pfx) for pfx in _256K_MODEL_PREFIXES):
        return 256
    if any(short_l.startswith(pfx) for pfx in _200K_MODEL_PREFIXES):
        return 200
    return None


def resolve_context_window_k(
    profile: Optional[Dict[str, Any]] = None,
    model_name: str = "",
) -> int:
    """统一计算给定供应商档案与模型名的有效上下文窗口（单位 K，16..1000）。

    优先级：
      1. 档案 `model_context_windows` 中的上游真实元数据；
      2. 若会话临时切换了与档案默认不同的模型：优先按模型家族推断；
      3. 若档案保存了非默认值（!= 128）的 `context_window_k`：尊重档案值；
      4. 按模型家族推断（修复旧档案遗留的默认 128K）；
      5. 兜底返回档案值或 128K。
    """
    prof = profile if isinstance(profile, dict) else {}
    prof_model = str(prof.get("model") or "").strip()
    eff_model = (model_name or "").strip() or prof_model
    mcw = prof.get("model_context_windows") if isinstance(prof.get("model_context_windows"), dict) else None

    if eff_model and mcw:
        hit = lookup_model_context_window_k(eff_model, mcw)
        if hit is not None:
            return hit

    same_model = (not model_name.strip()) or (eff_model.lower() == prof_model.lower())
    stored_k = tokens_to_context_k(prof.get("context_window_k")) if prof else None

    if not same_model and eff_model:
        inferred_override = infer_model_context_window_k(eff_model, None)
        if inferred_override is not None:
            return inferred_override

    if stored_k is not None and stored_k != DEFAULT_CONTEXT_WINDOW_K:
        return stored_k

    if eff_model:
        inferred = infer_model_context_window_k(eff_model, None)
        if inferred is not None:
            return inferred

    return stored_k if stored_k is not None else DEFAULT_CONTEXT_WINDOW_K


def resolve_context_window_tokens(
    profile: Optional[Dict[str, Any]] = None,
    model_name: str = "",
) -> int:
    """返回有效上下文窗口 Token 总数（K * 1000）。"""
    return resolve_context_window_k(profile, model_name) * 1000
