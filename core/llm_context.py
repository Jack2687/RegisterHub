"""三仓共享 LLM 上下文窗口元数据提取与模型家族推断引擎（纯标准库，零外部依赖）。

单一真相源（Mother / PRO / Community 三仓 SHA-256 严格一致）：
  1. 从 `/v1/models` 响应提取各模型真实上下文窗口（兼容 10 种主流字段名与嵌套结构）；
  2. 统一将原始 Token 数归一化为 K（16..1000，其中 1000 = 1M）；
  3. 上游未返回上下文元数据时，按统一模型家族规则推断真实窗口（如 DeepSeek-V4 / Gemini / GPT-4.1+ / Claude 4.6+ = 1M）；
  4. 为 Agent 自动压缩（85% 阈值）与控制台 UI 提供完全一致的上下文窗口计算口径。
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Dict, Optional, Tuple

MIN_CONTEXT_WINDOW_K = 16
MAX_CONTEXT_WINDOW_K = 1000
DEFAULT_CONTEXT_WINDOW_K = 128

_AT_K_RE = re.compile(r"@(\d+)k\b")
_ONE_M_SUFFIX_RE = re.compile(r"[@\-_:\[]1m(?:\]|$|\b)")

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
    "claude-4-6",
    "claude-4.6",
    "claude-4-7",
    "claude-4.7",
    "claude-4-8",
    "claude-4.8",
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

_128K_EXPLICIT_PREFIXES: tuple[str, ...] = (
    "o1-mini",
    "o1-preview",
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


def tokens_to_context_k(raw_tokens: Any, *, raw_tokens_only: bool = False) -> Optional[int]:
    """将上游 /models 返回的原始上下文 token 数（如 1048576 / 1000000 / 200000）归一化为 K（16..1000，1000=1M）。"""
    if raw_tokens is None or isinstance(raw_tokens, bool):
        return None
    try:
        fval = float(raw_tokens)
        if not math.isfinite(fval):
            return None
        val = int(fval)
    except (TypeError, ValueError, OverflowError):
        return None
    if val <= 0:
        return None
    if not raw_tokens_only and val <= MAX_CONTEXT_WINDOW_K:
        k = val
    elif val >= 1_000_000:
        k = MAX_CONTEXT_WINDOW_K
    elif val % 1024 == 0 and val % 1000 != 0:
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

        k: Optional[int] = None
        for key in _CONTEXT_META_KEYS:
            if item.get(key) is not None:
                cand_k = tokens_to_context_k(item.get(key), raw_tokens_only=True)
                if cand_k is not None:
                    k = cand_k
                    break
        if k is None and isinstance(item.get("top_provider"), dict):
            k = tokens_to_context_k(item["top_provider"].get("context_length"), raw_tokens_only=True)
        if k is None and isinstance(item.get("limits"), dict):
            lim = item["limits"]
            k = tokens_to_context_k(lim.get("context"), raw_tokens_only=True)
            if k is None:
                k = tokens_to_context_k(lim.get("input"), raw_tokens_only=True)

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
    """仅从上游 /models 元数据映射中查找模型上下文窗口（精确 -> 去前缀短名 -> 忽略大小写精确 -> 短名回退）。"""
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
    # Pass 1: 优先忽略大小写的完整模型 ID 精确匹配，避免不同渠道前缀互相遮蔽
    for k, v in model_contexts.items():
        kl = str(k or "").strip().lower()
        if kl == low_mid:
            hit = tokens_to_context_k(v)
            if hit is not None:
                return hit
    # Pass 2: 退回到忽略大小写的短名/尾段匹配
    for k, v in model_contexts.items():
        kl = str(k or "").strip().lower()
        if kl == low_short or kl.endswith("/" + low_short):
            hit = tokens_to_context_k(v)
            if hit is not None:
                return hit
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
        at_val = int(m_at.group(1))
        if at_val > 0:
            return _clamp_ctx_k(at_val)
    if _ONE_M_SUFFIX_RE.search(low):
        return MAX_CONTEXT_WINDOW_K

    short_l = low.rsplit("/", 1)[-1]
    if any(short_l.startswith(pfx) for pfx in _128K_EXPLICIT_PREFIXES):
        return DEFAULT_CONTEXT_WINDOW_K
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
      2. 若会话临时切换了与档案默认不同的模型：按模型家族推断，未命中则回落默认 128K（绝不继承原模型的 1M 窗口）；
      3. 若档案保存了非默认值（!= 128）的 `context_window_k`：尊重档案值；
      4. 按模型家族推断（修复旧档案遗留的默认 128K）；
      5. 兜底返回档案值或 128K。
    """
    prof = profile if isinstance(profile, dict) else {}
    prof_model = str(prof.get("model") or "").strip()
    req_model = (model_name or "").strip()
    eff_model = req_model or prof_model
    mcw = prof.get("model_context_windows") if isinstance(prof.get("model_context_windows"), dict) else None

    if eff_model and mcw:
        hit = lookup_model_context_window_k(eff_model, mcw)
        if hit is not None:
            return hit

    is_model_override = bool(req_model and prof_model and req_model.lower() != prof_model.lower())
    stored_k = tokens_to_context_k(prof.get("context_window_k")) if prof else None

    if is_model_override:
        inferred_override = infer_model_context_window_k(eff_model, None)
        return inferred_override if inferred_override is not None else DEFAULT_CONTEXT_WINDOW_K

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


_INLINE_THINK_OPEN_RE = re.compile(r"<(think|thinking)\b[^>]*>", re.I)
_INLINE_THINK_CLOSE_RE = re.compile(r"</(think|thinking)\s*>", re.I)


def split_inline_thinking_tags(content: str, existing_thinking: str = "") -> Tuple[str, str]:
    """安全剥离正文中的 `<think>...</think>` / `<thinking>...</thinking>` 标签并并入 thinking 字段。

    单遍扫描：
    - 顶层三反引号/波浪线围栏代码块原样保留（不破坏代码片段中的示例标签）；
    - `<thinking>...</thinking>` 内部允许包含代码块（完整折叠进 thinking，不被误切）；
    - 兼容 DeepSeek/R1 类模型省略开头 `<think>`、仅输出尾部 `</think>` 的前缀思考模式。
    """
    raw_content = str(content or "")
    base_thinking = str(existing_thinking or "").strip()
    low_raw = raw_content.lower()
    if not raw_content or ("<think" not in low_raw and "</think" not in low_raw):
        return raw_content, base_thinking

    extracted: list[str] = []
    out_chars: list[str] = []
    n = len(raw_content)
    i = 0
    in_fence = False
    fence_char = ""
    fence_len = 0
    at_line_start = True

    while i < n:
        if at_line_start:
            ws = i
            while ws < n and raw_content[ws] in (" ", "\t"):
                ws += 1
            if ws < n and raw_content[ws] in ("`", "~"):
                ch = raw_content[ws]
                end_run = ws
                while end_run < n and raw_content[end_run] == ch:
                    end_run += 1
                run_len = end_run - ws
                if run_len >= 3:
                    line_end = raw_content.find("\n", end_run)
                    line_end = n if line_end == -1 else line_end + 1
                    if not in_fence:
                        in_fence = True
                        fence_char = ch
                        fence_len = run_len
                        out_chars.append(raw_content[i:line_end])
                        i = line_end
                        at_line_start = True
                        continue
                    if ch == fence_char and run_len >= fence_len and raw_content[end_run:line_end].strip() == "":
                        in_fence = False
                        fence_char = ""
                        fence_len = 0
                        out_chars.append(raw_content[i:line_end])
                        i = line_end
                        at_line_start = True
                        continue

        if not in_fence and raw_content[i] == "<":
            m_open = _INLINE_THINK_OPEN_RE.match(raw_content, i)
            if m_open:
                m_close = _INLINE_THINK_CLOSE_RE.search(raw_content, m_open.end())
                if m_close:
                    inner = raw_content[m_open.end() : m_close.start()].strip()
                    if inner:
                        extracted.append(inner)
                    i = m_close.end()
                    at_line_start = i == 0 or raw_content[i - 1] == "\n"
                    continue
            # 兼容模型省略开头 <think> 仅输出 `...思考内容...</think>` 的场景
            m_orphan_close = _INLINE_THINK_CLOSE_RE.match(raw_content, i)
            if m_orphan_close and not extracted:
                prefix_think = _INLINE_THINK_OPEN_RE.sub("", "".join(out_chars)).strip()
                if prefix_think:
                    extracted.append(prefix_think)
                out_chars.clear()
                i = m_orphan_close.end()
                at_line_start = i == 0 or raw_content[i - 1] == "\n"
                continue

        ch = raw_content[i]
        out_chars.append(ch)
        at_line_start = ch == "\n"
        i += 1

    if not extracted:
        return raw_content, base_thinking

    clean_content = re.sub(r"\n{3,}", "\n\n", "".join(out_chars)).strip()
    merged_parts: list[str] = []
    if base_thinking:
        merged_parts.append(base_thinking)
    for chunk in extracted:
        if not any(chunk == existing or chunk in existing for existing in merged_parts):
            merged_parts.append(chunk)
    merged_thinking = "\n\n".join(merged_parts).strip()
    return clean_content, merged_thinking
