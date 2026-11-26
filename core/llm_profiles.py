"""Multi-profile LLM provider & model catalog store for RegisterHub Community Edition.

Pure Python stdlib persistence backed by `results/llm_profiles.json` (derived
dynamically from `core.llm_client.LLM_CONFIG_FILE` so test monkeypatches stay
100% isolated). Automatically mirrors the active profile into `llm_config.json`
for full backward compatibility with legacy Lite endpoints and `core.ai_agent`.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import llm_client
from core.locks import atomic_write_text_safe, interprocess_file_lock
from core.secrets import mask_proxy_url

_STORE_LOCK = threading.Lock()


def _profiles_file() -> Path:
    return Path(llm_client.LLM_CONFIG_FILE).with_name("llm_profiles.json")


def _clean_str_list(raw: Any, limit: int = 500) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    seen = set()
    for item in raw:
        s = str(item or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
            if len(out) >= limit:
                break
    return out


def _clean_ctx_map(raw: Any) -> Dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, int] = {}
    for k, v in raw.items():
        ks = str(k or "").strip()
        if not ks:
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if 4 <= iv <= 4000:
            out[ks] = iv
    return out


def _normalize_key_entry(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, str):
        val = raw.strip()
        if not val:
            return None
        return {
            "id": f"k_{secrets.token_hex(4)}",
            "key": val,
            "state": "ok",
            "fails": 0,
            "last_error": "",
        }
    if isinstance(raw, dict):
        val = str(raw.get("key") or "").strip()
        if not val:
            return None
        try:
            fails_val = max(0, int(raw.get("fails") or 0))
        except (TypeError, ValueError):
            fails_val = 0
        return {
            "id": str(raw.get("id") or f"k_{secrets.token_hex(4)}"),
            "key": val,
            "state": "resting" if raw.get("state") == "resting" else "ok",
            "fails": fails_val,
            "last_error": str(raw.get("last_error") or ""),
        }
    return None


def _normalize_profile(raw: Dict[str, Any], default_id: str = "") -> Dict[str, Any]:
    pid = str(raw.get("id") or default_id or f"prof_{secrets.token_hex(4)}").strip()
    fmt_raw = str(raw.get("format") or "openai").strip().lower()
    fmt = fmt_raw if fmt_raw in ("openai", "anthropic", "openai-responses") else "openai"

    raw_base = str(raw.get("base_url") or "").strip()
    base_url = llm_client._normalize_base_url(raw_base) if raw_base else llm_client.DEFAULT_BASE_URL

    model = str(raw.get("model") or "").strip() or llm_client.DEFAULT_MODEL
    name = str(raw.get("name") or "").strip() or model

    keys: List[Dict[str, Any]] = []
    seen_keys = set()
    raw_keys = raw.get("keys")
    if isinstance(raw_keys, list):
        for item in raw_keys:
            ent = _normalize_key_entry(item)
            if ent and ent["key"] not in seen_keys:
                seen_keys.add(ent["key"])
                keys.append(ent)
    elif raw.get("api_key"):
        ent = _normalize_key_entry(str(raw.get("api_key")))
        if ent:
            keys.append(ent)

    try:
        mt_raw = int(raw.get("max_tokens") or 0)
    except (TypeError, ValueError):
        mt_raw = 0
    max_tokens = 0 if mt_raw <= 0 else max(64, min(128000, mt_raw))

    mtf = str(raw.get("max_tokens_field") or llm_client.DEFAULT_MAX_TOKENS_FIELD).strip()
    if mtf not in llm_client.VALID_MAX_TOKENS_FIELDS:
        mtf = llm_client.DEFAULT_MAX_TOKENS_FIELD

    temp = llm_client._clamp_float(raw.get("temperature"), llm_client.DEFAULT_TEMPERATURE, 0.0, 2.0)
    ctx_k = llm_client._clamp_int(raw.get("context_window_k"), 128, 16, 2000)
    prio = llm_client._clamp_int(raw.get("priority"), 10, 0, 1000)

    tm = str(raw.get("thinking_mode") or "none").strip()
    if tm not in ("none", "enabled", "disabled"):
        tm = "none"
    re_val = str(raw.get("reasoning_effort") or "none").strip()
    if re_val not in ("none", "low", "medium", "high", "xhigh", "max"):
        re_val = "none"

    health = raw.get("health") if isinstance(raw.get("health"), dict) else {}
    return {
        "id": pid,
        "name": name,
        "format": fmt,
        "model": model,
        "base_url": base_url,
        "proxy_url": str(raw.get("proxy_url") or "").strip(),
        "keys": keys,
        "max_tokens": max_tokens,
        "max_tokens_field": mtf,
        "temperature": temp,
        "context_window_k": ctx_k,
        "model_context_windows": _clean_ctx_map(raw.get("model_context_windows")),
        "priority": prio,
        "enabled": raw.get("enabled") is not False,
        "excluded": bool(raw.get("excluded", False)),
        "icon": str(raw.get("icon") or "").strip(),
        "pinned_models": _clean_str_list(raw.get("pinned_models"), 100),
        "catalog_models": _clean_str_list(raw.get("catalog_models"), 1000),
        "rate_per_second": llm_client._clamp_float(raw.get("rate_per_second"), 0.0, 0.0, 1000.0),
        "rate_per_minute": llm_client._clamp_int(raw.get("rate_per_minute"), 0, 0, 100000),
        "session_header_key": str(raw.get("session_header_key") or "").strip(),
        "bind_fallback": raw.get("bind_fallback") if isinstance(raw.get("bind_fallback"), bool) else None,
        "thinking_mode": tm,
        "reasoning_effort": re_val,
        "streaming": raw.get("streaming") is not False,
        "retry_policy": raw.get("retry_policy") if isinstance(raw.get("retry_policy"), dict) else None,
        "health": health,
    }


def _primary_key(prof: Dict[str, Any], key_id: str = "") -> str:
    keys = prof.get("keys") or []
    if key_id:
        for k in keys:
            if k.get("id") == key_id and k.get("key"):
                return str(k["key"])
    for k in keys:
        if k.get("state") != "resting" and k.get("key"):
            return str(k["key"])
    if keys and keys[0].get("key"):
        return str(keys[0]["key"])
    return ""


def to_public_profile(prof: Dict[str, Any], active_id: str) -> Dict[str, Any]:
    keys = prof.get("keys") or []
    first_key = _primary_key(prof)
    hint = llm_client.mask_api_key(first_key)
    healthy_cnt = sum(1 for k in keys if k.get("state") != "resting")
    health = dict(prof.get("health") or {})
    if not health.get("status"):
        health["status"] = "ok" if first_key else "unconfigured"
    return {
        "id": prof["id"],
        "name": prof["name"],
        "format": prof["format"],
        "model": prof["model"],
        "base_url": prof["base_url"],
        "proxy_url": mask_proxy_url(prof["proxy_url"]) if prof.get("proxy_url") else "",
        "api_key_hint": hint,
        "api_key_masked": hint,
        "has_key": bool(first_key),
        "has_api_key": bool(first_key),
        "key_count": len(keys),
        "healthy_key_count": healthy_cnt,
        "key_pool": [
            {
                "id": k["id"],
                "hint": llm_client.mask_api_key(k["key"]),
                "state": k.get("state", "ok"),
                "fails": llm_client._safe_nonneg_int(k.get("fails")),
                "last_error": str(k.get("last_error", "")),
            }
            for k in keys
        ],
        "max_tokens": prof["max_tokens"],
        "max_tokens_field": prof["max_tokens_field"],
        "temperature": prof["temperature"],
        "context_window_k": prof["context_window_k"],
        "model_context_windows": prof["model_context_windows"],
        "priority": prof["priority"],
        "enabled": prof["enabled"],
        "excluded": prof["excluded"],
        "icon": prof["icon"],
        "pinned_models": prof["pinned_models"],
        "catalog_models": prof["catalog_models"],
        "rate_per_second": prof["rate_per_second"],
        "rate_per_minute": prof["rate_per_minute"],
        "session_header_key": prof["session_header_key"],
        "bind_fallback": prof["bind_fallback"],
        "thinking_mode": prof["thinking_mode"],
        "reasoning_effort": prof["reasoning_effort"],
        "streaming": prof["streaming"],
        "retry_policy": prof["retry_policy"],
        "active": prof["id"] == active_id,
        "is_active": prof["id"] == active_id,
        "health": health,
    }


def _seed_from_legacy() -> Dict[str, Any]:
    try:
        raw = llm_client.load_raw_llm_config()
    except Exception:
        raw = llm_client._default_config()
    raw_mt = int(raw.get("max_tokens") or 0)
    seed_mt = 0 if raw_mt in (0, 64) else raw_mt
    seed_prof = _normalize_profile(
        {
            "id": "community_default",
            "name": "OpenAI 兼容主力接口",
            "format": "openai",
            "base_url": raw.get("base_url"),
            "model": raw.get("model"),
            "api_key": raw.get("api_key"),
            "proxy_url": raw.get("proxy_url"),
            "max_tokens": seed_mt,
            "max_tokens_field": raw.get("max_tokens_field"),
            "temperature": raw.get("temperature"),
            "pinned_models": [raw.get("model")] if raw.get("model") else [],
        },
        default_id="community_default",
    )
    return {"active_id": seed_prof["id"], "profiles": [seed_prof]}


def _load_store_unlocked() -> Dict[str, Any]:
    fp = _profiles_file()
    if not fp.exists():
        return _seed_from_legacy()
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as exc:
        raise llm_client.LLMClientError(f"模型供应商配置文件损坏 ({fp.name}): {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
        raise llm_client.LLMClientError(f"模型供应商配置文件格式非法 ({fp.name})")
    profs = [_normalize_profile(p) for p in data["profiles"] if isinstance(p, dict)]
    if not profs:
        return _seed_from_legacy()
    active_id = str(data.get("active_id") or "")
    if not any(p["id"] == active_id for p in profs):
        active_id = profs[0]["id"]
    return {"active_id": active_id, "profiles": profs}


def _sync_active_to_legacy(store: Dict[str, Any]) -> None:
    active_id = store.get("active_id") or ""
    profs = store.get("profiles") or []
    active = next((p for p in profs if p["id"] == active_id), profs[0] if profs else None)
    if not active:
        return
    key = _primary_key(active)
    mt = active.get("max_tokens") or llm_client.DEFAULT_MAX_TOKENS
    if mt <= 0:
        mt = llm_client.DEFAULT_MAX_TOKENS
    payload: Dict[str, Any] = {
        "base_url": active["base_url"],
        "model": active["model"],
        "proxy_url": active["proxy_url"],
        "max_tokens": mt,
        "max_tokens_field": active["max_tokens_field"],
        "temperature": active["temperature"],
    }
    if key:
        payload["api_key"] = key
    else:
        payload["clear_api_key"] = True
    try:
        llm_client.save_llm_config(payload)
    except Exception:
        pass


def _save_store_unlocked(store: Dict[str, Any]) -> None:
    fp = _profiles_file()
    fp.parent.mkdir(parents=True, exist_ok=True)
    with interprocess_file_lock(fp):
        atomic_write_text_safe(fp, json.dumps(store, ensure_ascii=False, indent=2) + "\n")
    _sync_active_to_legacy(store)


def list_profiles_response() -> Dict[str, Any]:
    with _STORE_LOCK:
        try:
            store = _load_store_unlocked()
        except llm_client.LLMClientError:
            store = _seed_from_legacy()
        active_id = store["active_id"]
        pub_list = [to_public_profile(p, active_id) for p in store["profiles"]]
        health_map = {p["id"]: p["health"] for p in pub_list}
    return {
        "active_id": active_id,
        "active_profile_id": active_id,
        "profiles": pub_list,
        "pool": {"enabled": False},
        "health": health_map,
        "recent_calls": llm_client.list_llm_calls(limit=50),
    }


def upsert_profile(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("供应商配置必须为 JSON 对象")
    if "base_url" in payload and str(payload.get("base_url") or "").strip():
        llm_client._validate_base_url(str(payload.get("base_url") or ""))
    if "proxy_url" in payload:
        llm_client._validate_proxy_url(payload.get("proxy_url"))

    with _STORE_LOCK:
        store = _load_store_unlocked()
        profs: List[Dict[str, Any]] = store["profiles"]
        req_id = str(payload.get("id") or "").strip()
        existing_idx = next((i for i, p in enumerate(profs) if p["id"] == req_id), -1) if req_id else -1

        if existing_idx >= 0:
            merged = dict(profs[existing_idx])
            existing_keys = list(merged.get("keys") or [])
        else:
            new_id = req_id or f"prof_{secrets.token_hex(4)}"
            merged = _normalize_profile({"id": new_id}, default_id=new_id)
            existing_keys = []

        for field in (
            "name",
            "format",
            "model",
            "base_url",
            "proxy_url",
            "max_tokens",
            "max_tokens_field",
            "temperature",
            "context_window_k",
            "model_context_windows",
            "priority",
            "enabled",
            "excluded",
            "icon",
            "pinned_models",
            "catalog_models",
            "rate_per_second",
            "rate_per_minute",
            "session_header_key",
            "bind_fallback",
            "thinking_mode",
            "reasoning_effort",
            "streaming",
            "retry_policy",
        ):
            if field in payload:
                merged[field] = payload[field]

        # Key handling: preserve existing keys unless explicitly adding/replacing
        incoming_keys: List[str] = []
        if isinstance(payload.get("api_keys"), list):
            for k in payload["api_keys"]:
                ks = str(k or "").strip()
                if ks and ks != "***":
                    incoming_keys.append(ks)
        elif payload.get("api_key"):
            ks = str(payload.get("api_key") or "").strip()
            existing_hint = llm_client.mask_api_key(_primary_key({"keys": existing_keys}))
            if ks and ks != "***" and ks != existing_hint:
                incoming_keys.append(ks)

        if bool(payload.get("clear_api_key")):
            merged["keys"] = []
        elif incoming_keys:
            if bool(payload.get("replace_keys")):
                seen = set()
                new_list = []
            else:
                seen = {k["key"] for k in existing_keys}
                new_list = list(existing_keys)
            for ks in incoming_keys:
                if ks not in seen:
                    seen.add(ks)
                    ent = _normalize_key_entry(ks)
                    if ent:
                        new_list.append(ent)
            merged["keys"] = new_list
        else:
            merged["keys"] = existing_keys

        # Ensure default model stays in pinned_models if user hasn't explicitly emptied it on first create
        normalized = _normalize_profile(merged, default_id=merged["id"])
        if existing_idx >= 0:
            profs[existing_idx] = normalized
        else:
            profs.append(normalized)
            # Auto-activate newly created profile if current active has no key yet
            cur_active = next((p for p in profs if p["id"] == store["active_id"]), None)
            if cur_active is None or (not _primary_key(cur_active) and _primary_key(normalized)):
                store["active_id"] = normalized["id"]

        _save_store_unlocked(store)
        return to_public_profile(normalized, store["active_id"])


def delete_profile(profile_id: str) -> bool:
    pid = str(profile_id or "").strip()
    if not pid:
        return False
    with _STORE_LOCK:
        store = _load_store_unlocked()
        before = len(store["profiles"])
        store["profiles"] = [p for p in store["profiles"] if p["id"] != pid]
        if len(store["profiles"]) == before:
            return False
        if not store["profiles"]:
            store = _seed_from_legacy()
            store["profiles"][0]["keys"] = []
        if store["active_id"] == pid or not any(p["id"] == store["active_id"] for p in store["profiles"]):
            store["active_id"] = store["profiles"][0]["id"]
        _save_store_unlocked(store)
        return True


def activate_profile(profile_id: str) -> Optional[Dict[str, Any]]:
    pid = str(profile_id or "").strip()
    with _STORE_LOCK:
        store = _load_store_unlocked()
        target = next((p for p in store["profiles"] if p["id"] == pid), None)
        if not target:
            return None
        store["active_id"] = pid
        _save_store_unlocked(store)
        return to_public_profile(target, pid)


def mutate_profile_keys(
    profile_id: str,
    action: str,
    *,
    keys: Optional[List[str]] = None,
    key_id: str = "",
) -> Tuple[Optional[Dict[str, Any]], int]:
    pid = str(profile_id or "").strip()
    act = str(action or "").strip().lower()
    with _STORE_LOCK:
        store = _load_store_unlocked()
        idx = next((i for i, p in enumerate(store["profiles"]) if p["id"] == pid), -1)
        if idx < 0:
            return None, 0
        prof = store["profiles"][idx]
        cur_keys: List[Dict[str, Any]] = list(prof.get("keys") or [])
        delta = 0
        if act == "add":
            seen = {k["key"] for k in cur_keys}
            for raw_k in (keys or []):
                ks = str(raw_k or "").strip()
                if ks and ks != "***" and ks not in seen:
                    seen.add(ks)
                    ent = _normalize_key_entry(ks)
                    if ent:
                        cur_keys.append(ent)
                        delta += 1
            prof["keys"] = cur_keys
        elif act == "remove":
            before = len(cur_keys)
            cur_keys = [k for k in cur_keys if k.get("id") != key_id]
            delta = before - len(cur_keys)
            prof["keys"] = cur_keys
        elif act == "reset_key":
            for k in cur_keys:
                if (not key_id) or k.get("id") == key_id:
                    k["state"] = "ok"
                    k["fails"] = 0
                    k["last_error"] = ""
                    delta += 1
            prof["keys"] = cur_keys
        else:
            raise ValueError(f"不支持的 Key 池操作: {action}")
        store["profiles"][idx] = prof
        _save_store_unlocked(store)
        return to_public_profile(prof, store["active_id"]), delta


def reset_pool_cooldown(profile_id: str = "") -> int:
    pid = str(profile_id or "").strip()
    with _STORE_LOCK:
        store = _load_store_unlocked()
        count = 0
        for p in store["profiles"]:
            if pid and p["id"] != pid:
                continue
            p["health"] = {"status": "ok" if _primary_key(p) else "unconfigured", "fails": 0}
            for k in p.get("keys") or []:
                k["state"] = "ok"
                k["fails"] = 0
                k["last_error"] = ""
            count += 1
        _save_store_unlocked(store)
        return count


def save_catalog_models(
    profile_id: str,
    models: List[str],
    ctx_map: Optional[Dict[str, int]] = None,
) -> Optional[Dict[str, Any]]:
    pid = str(profile_id or "").strip()
    if not pid or not models:
        return None
    with _STORE_LOCK:
        store = _load_store_unlocked()
        idx = next((i for i, p in enumerate(store["profiles"]) if p["id"] == pid), -1)
        if idx < 0:
            return None
        prof = store["profiles"][idx]
        prof["catalog_models"] = _clean_str_list(models, 1000)
        if ctx_map:
            merged_ctx = dict(prof.get("model_context_windows") or {})
            merged_ctx.update(_clean_ctx_map(ctx_map))
            prof["model_context_windows"] = merged_ctx
        store["profiles"][idx] = prof
        _save_store_unlocked(store)
        return to_public_profile(prof, store["active_id"])


def record_profile_test_result(profile_id: str, ok: bool, latency_ms: float = 0.0, error: str = "") -> None:
    pid = str(profile_id or "").strip()
    if not pid:
        return
    with _STORE_LOCK:
        try:
            store = _load_store_unlocked()
        except Exception:
            return
        idx = next((i for i, p in enumerate(store["profiles"]) if p["id"] == pid), -1)
        if idx < 0:
            return
        prof = store["profiles"][idx]
        if ok:
            prof["health"] = {"status": "ok", "state": "ok", "ok": True, "fails": 0, "latency_ms": latency_ms, "checked_at": time.time()}
        else:
            prev_fails = int((prof.get("health") or {}).get("fails") or 0)
            prof["health"] = {"status": "error", "state": "degraded", "ok": False, "fails": prev_fails + 1, "last_error": str(error or "")[:240], "checked_at": time.time()}
        store["profiles"][idx] = prof
        try:
            _save_store_unlocked(store)
        except Exception:
            pass


def resolve_context_window(profile_id: str = "", model_name: str = "") -> int:
    """Resolve effective context window tokens for a profile/model (defaults to heuristic or 128K)."""
    with _STORE_LOCK:
        try:
            store = _load_store_unlocked()
        except Exception:
            store = _seed_from_legacy()
        profs = store["profiles"]
        pid = str(profile_id or "").strip() or store["active_id"]
        prof = next((p for p in profs if p["id"] == pid), None)
        if prof is None:
            prof = next((p for p in profs if p["id"] == store["active_id"]), profs[0] if profs else None)

    if prof:
        mcw = prof.get("model_context_windows")
        if isinstance(mcw, dict) and model_name:
            if model_name in mcw and int(mcw[model_name]) > 0:
                return int(mcw[model_name]) * 1000
            short_m = model_name.rsplit("/", 1)[-1]
            if short_m in mcw and int(mcw[short_m]) > 0:
                return int(mcw[short_m]) * 1000
        ctx_k = int(prof.get("context_window_k") or 0)
        if ctx_k > 0:
            return ctx_k * 1000

    low = str(model_name or "").lower()
    if any(k in low for k in ("gemini-2", "gemini-1.5", "gemini-2.5", "claude-sonnet-4", "claude-opus-4", "gpt-4.1")):
        return 1_000_000
    if "claude-3" in low or "claude-4" in low:
        return 200_000
    return 128_000


def resolve_runtime_override(
    profile_id: str = "",
    *,
    model_override: str = "",
    key_id: str = "",
    extra_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve raw runtime LLM config (including raw api_key) for a specific profile or active profile."""
    with _STORE_LOCK:
        try:
            store = _load_store_unlocked()
        except Exception:
            store = _seed_from_legacy()
        profs = store["profiles"]
        pid = str(profile_id or "").strip() or store["active_id"]
        prof = next((p for p in profs if p["id"] == pid), None)
        if prof is None:
            prof = next((p for p in profs if p["id"] == store["active_id"]), profs[0] if profs else None)

    out: Dict[str, Any] = {}
    if prof:
        mt = int(prof.get("max_tokens") or 0)
        out = {
            "profile_id": prof["id"],
            "profile_name": prof["name"],
            "format": prof.get("format") or "openai",
            "base_url": prof["base_url"],
            "model": prof["model"],
            "api_key": _primary_key(prof, key_id=key_id),
            "proxy_url": prof.get("proxy_url") or "",
            "max_tokens": mt if mt > 0 else llm_client.DEFAULT_MAX_TOKENS,
            "max_tokens_field": prof.get("max_tokens_field") or llm_client.DEFAULT_MAX_TOKENS_FIELD,
            "temperature": prof.get("temperature", llm_client.DEFAULT_TEMPERATURE),
            "thinking_mode": prof.get("thinking_mode") or "none",
            "reasoning_effort": prof.get("reasoning_effort") or "none",
            "streaming": prof.get("streaming") is not False,
            "context_window_k": int(prof.get("context_window_k") or 128),
            "model_context_windows": dict(prof.get("model_context_windows") or {}),
        }
    if model_override and model_override != "__follow_profile__":
        out["model"] = str(model_override).strip()
    if isinstance(extra_overrides, dict):
        prof_origin = llm_client._origin_tuple(out.get("base_url") or "")
        base_overridden = False
        explicit_key = False
        for k in ("format", "base_url", "model", "max_tokens", "max_tokens_field", "temperature", "thinking_mode", "reasoning_effort"):
            v = extra_overrides.get(k)
            if v is not None and str(v).strip() != "":
                if k == "max_tokens":
                    try:
                        if int(v) <= 0:
                            continue
                    except Exception:
                        continue
                if k == "base_url":
                    base_overridden = True
                out[k] = v
        if "streaming" in extra_overrides and isinstance(extra_overrides.get("streaming"), bool):
            out["streaming"] = extra_overrides["streaming"]
        if "proxy_url" in extra_overrides and extra_overrides.get("proxy_url") is not None:
            out["proxy_url"] = str(extra_overrides.get("proxy_url") or "").strip()
        raw_k = str(extra_overrides.get("api_key") or "").strip()
        if raw_k and raw_k != "***":
            out["api_key"] = raw_k
            explicit_key = True
        if base_overridden and not explicit_key and prof_origin != llm_client._origin_tuple(out.get("base_url") or ""):
            out["api_key"] = ""
    return out
