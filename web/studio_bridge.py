"""Studio UI Control Plane Bridge for RegisterHub Open-Source Community Edition.

Pure Python standard library adapter layer that exposes the AutoRegister Studio
(`edition="community"`) API contract on top of RegisterHub Lite's existing
single-thread engines (`core/ai_agent.py`, `core/llm_client.py`, `core/doctor.py`,
`core/ip_scorer.py`, `core/calibrator.py`, `core/exporter.py`, `core/scaffold.py`)
with zero additional external dependencies and 100% backward compatibility.
"""

from __future__ import annotations

import datetime
import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HEALTH_CACHE_LOCK = threading.Lock()
_HEALTH_CACHE: Dict[str, Any] = {"ts": 0.0, "payload": None}
_HEALTH_CACHE_TTL_S = 30.0


COMMUNITY_META: Dict[str, Any] = {
    "edition": "community",
    "brand": "RegisterHub",
    "subtitle": "开源社区工作台",
    "version": "2.1.0",
    "features": {
        "fleet": True,
        "materials": False,
        "recycle": False,
        "mailhub_3100": False,
        "easy_proxies": False,
        "magpie": False,
        "editions_sync": False,
        "har_studio": True,
        "compound": False,
        "keys_vault": True,
        "notify": False,
        "pro_sites": True,
        "pro_adapters": True,
        "adapter_editor": False,
        "scheduler": False,
        "proxy_import": False,
        "accounts": True,
        "accounts_delete": True,
        "tools": True,
        "tools_extras": False,
        "password_change": False,
        "ai_agent": True,
        "builtin_mail": False,
        "builtin_proxies": False,
        "single_thread_only": True,
        "demo_mode": False,
    },
}


def _mask_key(val: str) -> str:
    s = str(val or "").strip()
    if not s:
        return ""
    if len(s) <= 10:
        return "••••••••"
    return f"{s[:6]}...{s[-4:]}"


def _classify_key(val: str) -> str:
    s = str(val or "").strip()
    if not s:
        return "普通账号"
    if s.startswith(("sk-", "gsk_", "rk_", "pk_", "api_")):
        return "API Key"
    if s.startswith("eyJ") and s.count(".") == 2:
        return "JWT Token"
    return "API Key"


def _infer_project_from_account(acc: Dict[str, Any], adapters: List[Dict[str, Any]]) -> str:
    return adapters[0]["name"] if adapters else "demo"


def enrich_agent_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(summary)
    usage = out.get("usage") if isinstance(out.get("usage"), dict) else {}
    in_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or out.get("input_tokens") or 0)
    out_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or out.get("output_tokens") or 0)
    out.setdefault("input_tokens", in_tok)
    out.setdefault("output_tokens", out_tok)
    out.setdefault("steps_count", int(out.get("steps") or out.get("steps_count") or 0))
    return out


def enrich_agent_detail(detail: Dict[str, Any]) -> Dict[str, Any]:
    """Add Studio AgentSessionDetail fields (`messages`, `plan_steps`, `last_seq`) while keeping Lite fields."""
    out = enrich_agent_summary(detail)
    events = out.get("events") if isinstance(out.get("events"), list) else []
    messages: List[Dict[str, Any]] = []
    tool_by_call: Dict[str, Dict[str, Any]] = {}
    max_seq = 0

    for idx, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        seq = ev.get("seq")
        if isinstance(seq, int) and seq > max_seq:
            max_seq = seq
        et = str(ev.get("type") or "")
        ts = str(ev.get("ts") or "")
        if et == "user_msg":
            messages.append({
                "id": f"m_u_{idx}",
                "role": "user",
                "content": str(ev.get("content") or ""),
                "steered": bool(ev.get("steered")),
                "ts": ts,
            })
        elif et == "assistant_msg":
            messages.append({
                "id": f"m_a_{idx}",
                "role": "assistant",
                "content": str(ev.get("content") or ""),
                "ts": ts,
            })
        elif et == "tool_start":
            cid = str(ev.get("call_id") or f"tc_{idx}")
            entry = {
                "id": f"m_t_{cid}",
                "role": "tool",
                "tool_call_id": cid,
                "call_id": cid,
                "tool_name": str(ev.get("name") or "tool"),
                "tool_args": ev.get("args") if isinstance(ev.get("args"), dict) else {},
                "status": "running",
                "ts": ts,
            }
            tool_by_call[cid] = entry
            messages.append(entry)
        elif et == "tool_end":
            cid = str(ev.get("call_id") or f"tc_{idx}")
            res_obj = ev.get("result")
            res_str = res_obj if isinstance(res_obj, str) else json.dumps(res_obj or {}, ensure_ascii=False, indent=2)
            ok = ev.get("ok", True) is not False
            existing = tool_by_call.get(cid)
            if existing is not None:
                existing["status"] = "done" if ok else "failed"
                existing["ok"] = ok
                existing["duration_ms"] = ev.get("duration_ms")
                existing["tool_result"] = res_str
                if not ok:
                    existing["error"] = res_str[:300]
            else:
                messages.append({
                    "id": f"m_t_{cid}_{idx}",
                    "role": "tool",
                    "tool_call_id": cid,
                    "tool_name": str(ev.get("name") or "tool"),
                    "status": "done" if ok else "failed",
                    "ok": ok,
                    "duration_ms": ev.get("duration_ms"),
                    "tool_result": res_str,
                    "ts": ts,
                })

    raw_plan = out.get("plan") if isinstance(out.get("plan"), list) else []
    plan_steps = []
    for i, st in enumerate(raw_plan):
        if isinstance(st, dict):
            raw_s = str(st.get("status") or "pending")
            mapped_s = "running" if raw_s == "in_progress" else raw_s
            plan_steps.append({
                "id": f"p_{i}",
                "title": str(st.get("title") or ""),
                "status": mapped_s,
            })

    usage = out.get("usage") if isinstance(out.get("usage"), dict) else {}
    in_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    out_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    out["messages"] = messages
    out["plan_steps"] = plan_steps
    out["last_seq"] = max(max_seq, int(out.get("next_seq") or len(events)))
    out["input_tokens"] = in_tok
    out["output_tokens"] = out_tok
    out["usage"] = {
        "prompt_tokens": in_tok,
        "completion_tokens": out_tok,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": in_tok + out_tok,
    }
    return out


def handle_studio_get(handler: Any, srv: Any, path: str, qs: Dict[str, List[str]]) -> bool:
    """Handle Studio GET routes. Returns True if handled, False to fall through."""
    if path == "/api/console/meta":
        handler._send_json(COMMUNITY_META)
        return True

    if path == "/api/dashboard":
        from core.exporter import parse_accounts_file

        adapters = srv.list_community_adapters()
        accounts = parse_accounts_file(srv.ACCOUNTS_FILE)
        fails = srv.list_failures()
        jobs = srv.job_manager.list_jobs()
        act = srv.job_manager.active_job()

        with_key_rows = [a for a in accounts if str(a.get("api_key") or "").strip()]
        dead_cnt = sum(1 for f in fails if f.get("category") == "dead")
        soft_cnt = sum(1 for f in fails if f.get("category") in ("soft", "captcha"))

        # 14-day trend
        today = datetime.date.today()
        day_map: Dict[str, Dict[str, int]] = {}
        for offset in range(13, -1, -1):
            d = today - datetime.timedelta(days=offset)
            day_map[d.strftime("%Y-%m-%d")] = {"success": 0, "failed": 0}
        for j in jobs:
            day_k = str(j.get("started_at") or "")[:10]
            if day_k in day_map:
                if j.get("status") == "done":
                    day_map[day_k]["success"] += int(j.get("count") or 1)
                elif j.get("status") == "failed":
                    day_map[day_k]["failed"] += 1
        for f in fails:
            day_k = str(f.get("time") or "")[:10]
            if day_k in day_map:
                day_map[day_k]["failed"] += 1

        trend = [
            {"date": k[5:], "full_date": k, "success": v["success"], "failed": v["failed"]}
            for k, v in day_map.items()
        ]
        today_k = today.strftime("%Y-%m-%d")

        key_dist: Dict[str, int] = {}
        for a in accounts:
            kind = _classify_key(str(a.get("api_key") or ""))
            key_dist[kind] = key_dist.get(kind, 0) + 1

        durations = [float(j.get("duration_s") or 0) for j in jobs if j.get("status") == "done" and j.get("duration_s")]
        avg_dur = round(sum(durations) / len(durations), 1) if durations else 0.0

        recent_jobs = [
            {**j, "project": j.get("adapter") or "demo", "concurrency": 1}
            for j in jobs[:10]
        ]
        recent_accounts = [
            {
                "project": _infer_project_from_account(a, adapters),
                "email": a.get("email", ""),
                "has_key": bool(str(a.get("api_key") or "").strip()),
                "kind": _classify_key(str(a.get("api_key") or "")),
                "masked_key": _mask_key(str(a.get("api_key") or "")),
                "credential": str(a.get("api_key") or ""),
                "created_at": "",
            }
            for a in accounts[::-1][:8]
        ]

        top_projects = [
            {
                "project": ad["name"],
                "ok_total": sum(1 for a in accounts if _infer_project_from_account(a, adapters) == ad["name"]),
                "valid_keys": sum(
                    1 for a in with_key_rows if _infer_project_from_account(a, adapters) == ad["name"]
                ),
                "retriable": 0,
                "dead": 0,
            }
            for ad in adapters
        ]

        handler._send_json({
            "kpi": {
                "projects_total": len(adapters),
                "active_jobs": 1 if act else 0,
                "total_ok": len(accounts),
                "valid_keys": len(with_key_rows),
                "retriable_count": soft_cnt,
                "dead_count": dead_cnt,
                "avg_duration_s": avg_dur,
                "today_ok": day_map[today_k]["success"],
                "today_failed": day_map[today_k]["failed"],
            },
            "daily_trend": trend,
            "key_distribution": key_dist,
            "top_projects": top_projects,
            "all_projects": [ad["name"] for ad in adapters],
            "recent_jobs": recent_jobs,
            "recent_accounts": recent_accounts,
            "external_running": [],
        })
        return True

    if path == "/api/services/health":
        force = (qs.get("force") or ["0"])[0] in ("1", "true", "yes")
        now = time.time()
        if not force:
            with _HEALTH_CACHE_LOCK:
                cached = _HEALTH_CACHE.get("payload")
                if cached is not None and (now - float(_HEALTH_CACHE.get("ts") or 0.0)) < _HEALTH_CACHE_TTL_S:
                    handler._send_json(cached)
                    return True
        report = srv.collect_doctor_report(include_network=force)
        checks = report.get("checks") or []
        services = []
        online_cnt = 0
        for idx, c in enumerate(checks):
            st = str(c.get("state") or "pass")
            mapped = "online" if st == "pass" else "degraded" if st == "warn" else "offline"
            if mapped == "online":
                online_cnt += 1
            detail = str(c.get("detail") or "")
            hint = str(c.get("hint") or "")
            services.append({
                "id": f"doc_{idx}",
                "name": str(c.get("name") or f"Check #{idx + 1}"),
                "category": "环境探针",
                "endpoint": "local",
                "status": mapped,
                "latency_ms": 0,
                "recoverable": False,
                "metrics": {},
                "detail": f"{detail} ({hint})" if hint else detail,
            })
        payload = {
            "checked_at": report.get("checked_at") or time.strftime("%H:%M:%S"),
            "summary": {
                "total": len(services),
                "online": online_cnt,
                "offline": sum(1 for s in services if s["status"] == "offline"),
                "all_core_ready": bool(report.get("ready")),
            },
            "services": services,
        }
        with _HEALTH_CACHE_LOCK:
            _HEALTH_CACHE["ts"] = time.time()
            _HEALTH_CACHE["payload"] = payload
        handler._send_json(payload)
        return True

    if path == "/api/fleet":
        from core.exporter import parse_accounts_file

        adapters = srv.list_community_adapters()
        accounts = parse_accounts_file(srv.ACCOUNTS_FILE)
        fails = srv.list_failures()
        snaps = srv.list_snapshots()

        recent_errors = [
            {
                "dir": f.get("snapshot") or f"fail_{f.get('id', 0)}",
                "reason": str(f.get("reason") or ""),
                "has_screenshot": bool(f.get("snapshot_url")),
                "screenshot_url": str(f.get("snapshot_url") or ""),
            }
            for f in fails[:8]
        ]
        if not recent_errors and snaps:
            recent_errors = [
                {
                    "dir": s["name"],
                    "reason": f"失败现场截图 ({s['mtime']})",
                    "has_screenshot": True,
                    "screenshot_url": s["url"],
                }
                for s in snaps[:8]
            ]

        projects = []
        for idx, ad in enumerate(adapters):
            name = ad["name"]
            matched = [a for a in accounts if _infer_project_from_account(a, adapters) == name]
            valid_k = sum(1 for a in matched if str(a.get("api_key") or "").strip())
            projects.append({
                "project": name,
                "target_url": ad.get("target_url") or "",
                "project_dir": str(srv.ROOT_DIR / "sites"),
                "queue_count": 0,
                "ok_total": len(matched),
                "valid_keys": valid_k,
                "failed_lines": len(fails) if idx == 0 else 0,
                "retriable_count": 0,
                "dead_count": sum(1 for f in fails if f.get("category") == "dead") if idx == 0 else 0,
                "unknown_count": 0,
                "top_reasons": [],
                "metrics": {"count": len(matched), "success_rate": None, "avg_duration_s": None},
                "recent_errors": recent_errors if idx == 0 else [],
            })
        handler._send_json({"projects": projects, "total": len(projects)})
        return True

    if path == "/api/fleet/external-running":
        handler._send_json({"items": []})
        return True

    if path == "/api/fleet/queue":
        handler._send_json({"queue_text": "", "count": 0})
        return True

    m_act = re.fullmatch(r"/api/fleet/([^/]+)/activity", path)
    if m_act:
        from core.exporter import parse_accounts_file

        proj = m_act.group(1)
        adapters = srv.list_community_adapters()
        accounts = parse_accounts_file(srv.ACCOUNTS_FILE)
        fails = srv.list_failures()
        rows = [
            {
                "email": a.get("email", ""),
                "password": a.get("password", ""),
                "has_key": bool(str(a.get("api_key") or "").strip()),
                "kind": _classify_key(str(a.get("api_key") or "")),
                "masked_key": _mask_key(str(a.get("api_key") or "")),
                "credential": str(a.get("api_key") or ""),
                "ip": "",
                "created_at": "",
            }
            for a in accounts[::-1][:20]
        ]
        handler._send_json({
            "project": proj,
            "ok_total": len(accounts),
            "valid_keys": sum(1 for a in accounts if str(a.get("api_key") or "").strip()),
            "recent_accounts": rows,
            "recent_failures": [
                {"line": f"{f.get('time', '')} | {f.get('reason', '')}", "category": f.get("category") or "unknown"}
                for f in fails[:20]
            ],
            "has_layout": srv.GEOMETRY_FILE.exists(),
            "notify_enabled": False,
            "notify_platform": "",
        })
        return True

    if path == "/api/keys/vault":
        from core.exporter import parse_accounts_file

        adapters = srv.list_community_adapters()
        accounts = parse_accounts_file(srv.ACCOUNTS_FILE)
        items = []
        by_project: Dict[str, int] = {}
        by_kind: Dict[str, int] = {}
        for a in accounts[::-1]:
            key = str(a.get("api_key") or "").strip()
            if not key:
                continue
            proj = _infer_project_from_account(a, adapters)
            kind = _classify_key(key)
            by_project[proj] = by_project.get(proj, 0) + 1
            by_kind[kind] = by_kind.get(kind, 0) + 1
            items.append({
                "project": proj,
                "email": a.get("email", ""),
                "password": a.get("password", ""),
                "key": key,
                "masked_key": _mask_key(key),
                "kind": kind,
                "created_at": "",
                "status": "unchecked",
                "detail": "",
            })
        handler._send_json({"total": len(items), "by_project": by_project, "by_kind": by_kind, "items": items})
        return True

    if path == "/api/accounts/ledger":
        from core.exporter import parse_accounts_file

        adapters = srv.list_community_adapters()
        accounts = parse_accounts_file(srv.ACCOUNTS_FILE)
        items = []
        by_project: Dict[str, int] = {}
        with_key = 0
        for idx, a in enumerate(accounts[::-1]):
            key = str(a.get("api_key") or "").strip()
            has_k = bool(key)
            if has_k:
                with_key += 1
            proj = _infer_project_from_account(a, adapters)
            by_project[proj] = by_project.get(proj, 0) + 1
            items.append({
                "id": f"acc_{idx}_{a.get('email', '')}",
                "project": proj,
                "email": a.get("email", ""),
                "password": a.get("password", ""),
                "credential": key,
                "has_key": has_k,
                "kind": _classify_key(key),
                "ip": "",
                "created_at": "",
            })
        handler._send_json({
            "total": len(items),
            "with_key": with_key,
            "without_key": len(items) - with_key,
            "by_project": by_project,
            "items": items,
        })
        return True

    if path == "/api/services/proxies":
        handler._send_json({
            "source": "community",
            "online": True,
            "latency_ms": 0,
            "total": 0,
            "available": 0,
            "clean_count": 0,
            "nodes": [],
        })
        return True

    if path == "/api/services/ip-score/current":
        from core.ip_scorer import score_ip

        force = (qs.get("force") or ["0"])[0] in ("1", "true", "yes")
        now = time.time()
        with srv._IP_SCORE_CACHE_LOCK:
            if not force and srv._IP_SCORE_CACHE["data"] is not None and (now - float(srv._IP_SCORE_CACHE["ts"])) < srv._IP_SCORE_TTL_S:
                handler._send_json({**srv._IP_SCORE_CACHE["data"], "cached": True})
                return True
        cfg = srv._load_raw_config_dict()
        px = str((cfg.get("browser") or {}).get("proxy") or "").strip()
        res = score_ip(ip="", proxy_url=px, target_url="")
        with srv._IP_SCORE_CACHE_LOCK:
            srv._IP_SCORE_CACHE["ts"] = time.time()
            srv._IP_SCORE_CACHE["data"] = res
        handler._send_json({**res, "cached": False})
        return True

    if path == "/api/studio/adapters":
        adapters = srv.list_community_adapters()
        mapped = []
        for a in adapters:
            tier_raw = str(a.get("tier") or "template")
            tier = "verified" if tier_raw == "verified" else "partial" if tier_raw == "oauth" else "template"
            cls = "".join(p.capitalize() for p in str(a["name"]).split("_")) + "Adapter"
            mapped.append({
                "name": a["name"],
                "class_name": cls,
                "target_url": a.get("target_url") or "",
                "file": a.get("file") or f"sites/{a['name']}.py",
                "lines": int(a.get("lines") or 0),
                "tier": tier,
            })
        handler._send_json({"total": len(mapped), "adapters": mapped})
        return True

    m_st_code = re.fullmatch(r"/api/studio/adapters/([A-Za-z0-9_-]+)/code", path)
    if m_st_code:
        name = m_st_code.group(1)
        ad = next((a for a in srv.list_community_adapters() if a["name"] == name), None)
        if not ad:
            handler._send_json({"error": f"Adapter not found: {name}"}, status=404)
            return True
        fp = (srv.ROOT_DIR / ad["file"]).resolve()
        code = fp.read_text(encoding="utf-8", errors="replace") if fp.is_file() else ""
        handler._send_json({"ok": True, "name": name, "file": ad["file"], "code": code})
        return True

    if path == "/api/layout/presets":
        if srv.GEOMETRY_FILE.exists():
            try:
                data = json.loads(srv.GEOMETRY_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        else:
            from core.calibrator import auto_generate_layout

            data = auto_generate_layout(save_path=srv.GEOMETRY_FILE)
        handler._send_json(data)
        return True

    if path == "/api/settings/config":
        cfg = srv.load_config_dict()
        b = cfg.get("browser") or {}
        d = cfg.get("defaults") or {}
        m = cfg.get("mail") or {}
        has_proxy = bool(str(b.get("proxy") or "").strip())
        has_imap = bool(str(m.get("imap_server") or "").strip() and not d.get("use_disposable_mail", True))
        handler._send_json({
            "edition": "community",
            "proxy_mode": "proxy" if has_proxy else "direct",
            "proxy_residential_url": str(b.get("proxy") or ""),
            "proxy_cdp_endpoint": str(b.get("cdp_endpoint") or ""),
            "mail_provider": "imap" if has_imap else "tempmail",
            "mail_hub_api": "",
            "mail_cf_wildcard_domain": "",
            "mail_imap_server": str(m.get("imap_server") or ""),
            "mail_imap_port": int(m.get("imap_port") or 993),
            "mail_imap_user": str(m.get("imap_user") or ""),
            "mail_imap_password": str(m.get("imap_password") or ""),
            "run_headless": bool(b.get("headless", False)),
            "run_delay_between_s": round(float(b.get("slow_mo") or 0) / 1000.0, 2),
        })
        return True

    if path == "/api/llm/profiles":
        from core import llm_profiles

        handler._send_json(llm_profiles.list_profiles_response())
        return True

    m_err_det = re.fullmatch(r"/api/fleet/([^/]+)/error-detail/([^/]+)", path)
    if m_err_det:
        err_dir = m_err_det.group(2)
        fails = srv.list_failures()
        matched = next(
            (f for f in fails if f.get("snapshot") == err_dir or f"fail_{f.get('id', 0)}" == err_dir),
            None,
        )
        handler._send_json({
            "dir": err_dir,
            "detail": matched or {"reason": f"失败快照 {err_dir}"},
            "html_excerpt": "",
        })
        return True

    if path == "/api/llm/calls":
        try:
            lim = max(1, min(500, int((qs.get("limit") or ["200"])[0])))
        except Exception:
            lim = 200
        calls = srv.llm_client.list_llm_calls(limit=lim)
        handler._send_json({"ok": True, "calls": calls, "total": len(calls)})
        return True

    m_call_id = re.fullmatch(r"/api/llm/calls/([^/]+)", path)
    if m_call_id:
        item = srv.llm_client.get_llm_call(m_call_id.group(1))
        if item is None:
            handler._send_json({"ok": False, "error": "Call log not found"}, status=404)
            return True
        handler._send_json({"ok": True, "call": item})
        return True

    if path == "/api/llm/magpie/status":
        handler._send_json({"available": False, "providers": []})
        return True

    if path == "/api/agent/config":
        from core.ai_agent import TOOL_SCHEMAS, load_agent_config

        tools = [
            {
                "name": t["function"]["name"],
                "label": t["function"]["name"],
                "description": t["function"]["description"],
                "risk": "exec" if "run_" in t["function"]["name"] else "write" if "write_" in t["function"]["name"] else "read",
            }
            for t in TOOL_SCHEMAS
        ]
        handler._send_json({
            "agent": load_agent_config(),
            "available_tools": tools,
            "skills": [],
            "guard_rules": [],
            "approvals": [],
        })
        return True

    m_ag_evs = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/events", path)
    if m_ag_evs:
        sess = srv.agent_manager.get_session(m_ag_evs.group(1))
        if not sess:
            handler._send_json({"ok": False, "error": "Agent session not found"}, status=404)
            return True
        try:
            after_seq = max(0, int((qs.get("after_seq") or ["0"])[0]))
        except Exception:
            after_seq = 0
        detail = sess.to_detail()
        evs = [e for e in (detail.get("events") or []) if isinstance(e, dict) and int(e.get("seq") or 0) > after_seq]
        handler._send_json({"ok": True, "events": evs, "last_seq": detail.get("next_seq", 0), "has_more": False})
        return True

    return False


def handle_studio_post(handler: Any, srv: Any, path: str, body: Dict[str, Any]) -> bool:
    """Handle Studio POST routes. Returns True if handled, False to fall through."""
    if path == "/api/jobs/start":
        adapter = str(body.get("project") or body.get("site") or body.get("adapter") or "").strip().lower()
        try:
            count = max(1, min(20, int(body.get("count") or 1)))
        except Exception:
            count = 1
        raw_hf = body.get("headful")
        hl_override: Optional[bool] = (not raw_hf) if isinstance(raw_hf, bool) else None
        known = {a["name"] for a in srv.list_community_adapters()}
        if adapter not in known:
            handler._send_json({"ok": False, "error": f"Unknown adapter: {adapter}"}, status=400)
            return True
        if srv.agent_manager.active_session_id():
            handler._send_json({"ok": False, "error": "单线程模式已有运行中的 AI 会话"}, status=409)
            return True
        try:
            job = srv.job_manager.start(adapter, count=count, headless_override=hl_override)
            jd = job.to_dict()
            jd["project"] = adapter
            jd["concurrency"] = 1
            handler._send_json({"ok": True, "job": jd})
        except RuntimeError as e:
            handler._send_json({"ok": False, "error": str(e)}, status=409)
        return True

    if path == "/api/settings/config":
        updates: Dict[str, Any] = {}
        if "proxy_mode" in body or "proxy_residential_url" in body:
            mode = str(body.get("proxy_mode") or "direct")
            px = str(body.get("proxy_residential_url") or "").strip()
            updates["proxy"] = px if mode != "direct" else ""
        if "proxy_cdp_endpoint" in body:
            updates["cdp_endpoint"] = str(body.get("proxy_cdp_endpoint") or "").strip()
        if "mail_provider" in body:
            updates["use_disposable_mail"] = str(body.get("mail_provider") or "tempmail") != "imap"
        for k in ("mail_imap_server", "mail_imap_port", "mail_imap_user", "mail_imap_password"):
            if k in body:
                updates[k.replace("mail_", "")] = body[k]
        if "run_headless" in body:
            updates["headless"] = bool(body["run_headless"])
        if "run_delay_between_s" in body:
            try:
                updates["slow_mo"] = int(max(0.0, min(2.0, float(body["run_delay_between_s"]))) * 1000)
            except Exception:
                pass
        srv.save_config_dict(updates)
        handle_studio_get(handler, srv, "/api/settings/config", {})
        return True

    if path == "/api/services/ip-score":
        from core.ip_scorer import score_ip
        from core.net_guard import PrivateAddressBlockedError, validate_proxy_url, validate_public_http_url

        ip_val = str(body.get("ip") or "").strip()
        px = str(body.get("proxy_url") or "").strip()
        target_url = str(body.get("target_url") or "").strip()
        try:
            if target_url:
                target_url = validate_public_http_url(target_url, resolve_dns=True)
            if px:
                px = validate_proxy_url(px)
        except (PrivateAddressBlockedError, ValueError) as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
            return True
        if not ip_val and not px:
            cfg = srv._load_raw_config_dict()
            px = str((cfg.get("browser") or {}).get("proxy") or "").strip()
        handler._send_json(score_ip(ip=ip_val, proxy_url=px, target_url=target_url))
        return True

    if path == "/api/services/ip-score/batch":
        from core.ip_scorer import score_ip

        raw_items = body.get("items") if isinstance(body.get("items"), list) else []
        target_url = str(body.get("target_url") or "").strip()
        results = []
        for raw in raw_items[:10]:
            s = str(raw or "").strip()
            if not s:
                continue
            is_px = "://" in s
            results.append(score_ip(ip="" if is_px else s, proxy_url=s if is_px else "", target_url=target_url))
        handler._send_json({"ok": True, "total": len(results), "results": results})
        return True

    if path == "/api/services/test-proxy":
        cfg = srv._load_raw_config_dict()
        px = str(body.get("proxy_url") or (cfg.get("browser") or {}).get("proxy") or "").strip()
        target = str(body.get("target_url") or "https://api.ipify.org?format=json").strip()
        from core.net_guard import PrivateAddressBlockedError, build_safe_urllib_opener, validate_proxy_url, validate_public_http_url

        t0 = time.perf_counter()
        try:
            clean_target = validate_public_http_url(target, resolve_dns=True)
            clean_px = validate_proxy_url(px) if px else ""
            opener = build_safe_urllib_opener(proxy=clean_px or "")
            req = urllib.request.Request(clean_target, headers={"User-Agent": "RegisterHub/2.1"})
            with opener.open(req, timeout=8) as r:
                txt = r.read(512).decode("utf-8", errors="ignore").strip()
                handler._send_json({
                    "ok": True,
                    "status_code": r.status,
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "body": txt[:200],
                })
        except (PrivateAddressBlockedError, ValueError, Exception) as e:
            handler._send_json({"ok": False, "error": str(e), "latency_ms": round((time.perf_counter() - t0) * 1000, 1)})
        return True

    if path == "/api/services/test-mail":
        from core.imap_mail import SingleImapMailClient

        cfg = srv._load_raw_config_dict()
        mc = cfg.get("mail") or {}
        raw_pw = str(body.get("imap_password") or "")
        eff_pw = str(mc.get("imap_password") or "") if (not raw_pw or raw_pw == "***") else raw_pw
        try:
            eff_port = int(body.get("imap_port") or mc.get("imap_port") or 993)
        except Exception:
            eff_port = 993
        client = SingleImapMailClient(
            server=str(body.get("imap_server") or mc.get("imap_server") or "").strip(),
            port=max(1, min(65535, eff_port)),
            user=str(body.get("imap_user") or mc.get("imap_user") or "").strip(),
            password=eff_pw,
        )
        handler._send_json(client.test_connection())
        return True

    if path == "/api/tools/profile":
        from core.identity import generate_identity

        loc = str(body.get("country") or "US").strip().upper()
        if loc not in ("US", "UK", "DE"):
            loc = "US"
        ident = generate_identity(locale=loc, use_disposable=False)
        first = ident.get("first_name") or str(ident.get("full_name", "Alex Morgan")).split(" ")[0]
        last = ident.get("last_name") or (str(ident.get("full_name", "Alex Morgan")).split(" ")[-1])
        handler._send_json({
            "first_name": first,
            "last_name": last,
            "full_name": ident.get("full_name") or f"{first} {last}",
            "country": loc,
            "country_code": loc,
            "state": ident.get("state") or "California",
            "state_code": ident.get("state") or "CA",
            "city": ident.get("city") or "Austin",
            "address": ident.get("address") or f"{random.randint(100, 999)} Innovation Ave",
            "zip_code": ident.get("postal_code") or ident.get("zip_code") or "78704",
            "phone": ident.get("phone") or "+1 (512) 555-0148",
            "email": ident.get("email") or "",
            "birthday": ident.get("birth_date") or "1995-06-14",
            "organization": "Independent Developer",
            "job_title": "Software Engineer",
        })
        return True

    if path == "/api/studio/har":
        har_text = str(body.get("har_text") or "")
        site_name = str(body.get("site_name") or "").strip()
        try:
            res = srv._parse_har_community(har_text, site_name=site_name)
            handler._send_json({
                "har_file": "upload.har",
                "site": res.get("site_name") or site_name or "custom_site",
                "origin": res.get("origin") or "",
                "total_entries": int(res.get("total_entries") or len(res.get("api_steps") or [])),
                "api_steps": res.get("api_steps") or [],
                "signals": res.get("signals") or [],
                "skeleton": res.get("skeleton") or "",
                "written_path": "",
            })
        except Exception as e:
            handler._send_json({"error": f"HAR 解析失败: {e}"}, status=400)
        return True

    if path == "/api/keys/verify":
        from core.net_guard import (
            PrivateAddressBlockedError,
            build_safe_urllib_opener,
            validate_proxy_url,
            validate_public_http_url,
        )

        items = body.get("keys") if isinstance(body.get("keys"), list) else []
        base_url = str(body.get("base_url") or "").strip().rstrip("/")
        cfg = srv._load_raw_config_dict()
        raw_px = str((cfg.get("browser") or {}).get("proxy") or "").strip()

        clean_base = ""
        base_err = ""
        opener = None
        if base_url:
            try:
                clean_base = validate_public_http_url(base_url, resolve_dns=True).rstrip("/")
                px = validate_proxy_url(raw_px) if raw_px else ""
                opener = build_safe_urllib_opener(proxy=px or "")
            except (PrivateAddressBlockedError, ValueError) as e:
                base_err = str(e)

        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()

        live_cnt = 0
        dead_cnt = 0
        batch = items[:50]
        try:
            for idx, it in enumerate(batch, 1):
                k = str((it.get("key") if isinstance(it, dict) else it) or "").strip()
                proj = str((it.get("project") if isinstance(it, dict) else "") or "demo")
                if not base_url:
                    ev = {
                        "type": "item",
                        "index": idx,
                        "key": k,
                        "project": proj,
                        "ok": False,
                        "status": "unverifiable",
                        "detail": "请先填写 Base URL 再执行在线验活",
                    }
                elif base_err or opener is None:
                    dead_cnt += 1
                    ev = {
                        "type": "item",
                        "index": idx,
                        "key": k,
                        "project": proj,
                        "ok": False,
                        "status": "invalid",
                        "detail": base_err or "Base URL 校验失败",
                    }
                else:
                    models_url = f"{clean_base}/models" if clean_base.endswith("/v1") else f"{clean_base}/v1/models"
                    req = urllib.request.Request(
                        models_url,
                        headers={"Authorization": f"Bearer {k}", "Accept": "application/json", "User-Agent": "RegisterHub/2.1"},
                    )
                    t0 = time.perf_counter()
                    try:
                        with opener.open(req, timeout=10) as r:
                            raw = r.read(65536).decode("utf-8", errors="ignore")
                            cnt = 0
                            try:
                                j = json.loads(raw)
                                if isinstance(j, dict) and isinstance(j.get("data"), list):
                                    cnt = len(j["data"])
                            except Exception:
                                pass
                            ms = round((time.perf_counter() - t0) * 1000, 1)
                            live_cnt += 1
                            ev = {
                                "type": "item",
                                "index": idx,
                                "key": k,
                                "project": proj,
                                "ok": True,
                                "status": "valid",
                                "detail": f"HTTP {r.status} · {cnt} 模型 · {ms}ms",
                            }
                    except urllib.error.HTTPError as he:
                        dead_cnt += 1
                        ev = {
                            "type": "item",
                            "index": idx,
                            "key": k,
                            "project": proj,
                            "ok": False,
                            "status": "invalid",
                            "detail": f"HTTP {he.code}",
                        }
                    except Exception as e:
                        dead_cnt += 1
                        ev = {
                            "type": "item",
                            "index": idx,
                            "key": k,
                            "project": proj,
                            "ok": False,
                            "status": "invalid",
                            "detail": str(e)[:120],
                        }
                handler.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                handler.wfile.flush()
            done_ev = {"type": "done", "total": len(batch), "live": live_cnt, "dead": dead_cnt}
            handler.wfile.write(f"data: {json.dumps(done_ev, ensure_ascii=False)}\n\n".encode("utf-8"))
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        return True

    if path == "/api/keys/oneapi":
        keys = body.get("keys") if isinstance(body.get("keys"), list) else []
        prefix = str(body.get("name_prefix") or "RegisterHub").strip()
        base = str(body.get("base_url") or "https://api.openai.com").strip()
        channels = [
            {"name": f"{prefix}-{i + 1}", "type": 1, "key": str(k), "base_url": base, "models": "gpt-4o-mini"}
            for i, k in enumerate(keys)
            if str(k).strip()
        ]
        handler._send_json({"ok": True, "count": len(channels), "channels": channels})
        return True

    if path == "/api/keys/prune":
        items = body.get("items") if isinstance(body.get("items"), list) else []
        keys_to_rm = {str(it.get("key") or "").strip() for it in items if isinstance(it, dict) and it.get("key")}
        removed = 0
        if keys_to_rm and srv.ACCOUNTS_FILE.exists():
            from core.locks import atomic_write_text_safe, interprocess_file_lock

            with interprocess_file_lock(srv.ACCOUNTS_FILE):
                lines = srv.ACCOUNTS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
                kept = []
                for ln in lines:
                    if any(k in ln for k in keys_to_rm):
                        removed += 1
                    else:
                        kept.append(ln)
                if removed:
                    srv.ACCOUNTS_FILE.with_suffix(".txt.bak").write_text("\n".join(lines) + "\n", encoding="utf-8")
                    atomic_write_text_safe(srv.ACCOUNTS_FILE, "\n".join(kept) + ("\n" if kept else ""))
        handler._send_json({"ok": True, "removed": removed, "skipped_projects": []})
        return True

    if path == "/api/accounts/export":
        fmt = str(body.get("format") or "csv").strip().lower()
        if fmt == "txt":
            raw = srv.ACCOUNTS_FILE.read_text(encoding="utf-8", errors="replace") if srv.ACCOUNTS_FILE.exists() else ""
            handler._send_json({"ok": True, "filename": "registerhub_accounts.txt", "content": raw})
            return True
        if fmt in ("csv", "json", "bitwarden"):
            from core.exporter import run_export

            try:
                out_path = run_export(fmt=fmt, input_file=str(srv.ACCOUNTS_FILE), out_dir=str(srv.RESULTS_DIR))
                handler._send_json({"ok": True, "filename": out_path.name, "content": out_path.read_text(encoding="utf-8", errors="replace")})
            except Exception as e:
                handler._send_json({"ok": False, "error": str(e)}, status=400)
            return True
        handler._send_json({"ok": False, "error": f"Unsupported format: {fmt}"}, status=400)
        return True

    if path == "/api/llm/profiles":
        from core import llm_profiles

        try:
            prof = llm_profiles.upsert_profile(body)
            handler._send_json({"ok": True, "id": prof["id"], "profile": prof})
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True

    if path == "/api/llm/models":
        from core import llm_profiles

        pid = str(body.get("profile_id") or "").strip()
        ov = llm_profiles.resolve_runtime_override(profile_id=pid, extra_overrides=body) if pid else (body if body else None)
        res = srv.llm_client.discover_models(overrides=ov)
        saved_flag = False
        if res.get("ok") and bool(body.get("save_catalog")) and pid and res.get("models"):
            if llm_profiles.save_catalog_models(pid, res["models"], ctx_map=res.get("model_context_windows")):
                saved_flag = True
        handler._send_json({**res, "saved": saved_flag})
        return True

    if path == "/api/llm/import-vault":
        from core import llm_profiles
        from core.exporter import parse_accounts_file

        accounts = parse_accounts_file(srv.ACCOUNTS_FILE)
        keys = [str(a.get("api_key") or "").strip() for a in accounts if str(a.get("api_key") or "").strip()]
        keys = list(dict.fromkeys(keys))
        if not keys:
            handler._send_json({"ok": False, "error": "本地账号台账中暂无可导入的 API Key"}, status=400)
            return True
        pid = str(body.get("profile_id") or "").strip()
        try:
            if pid and not bool(body.get("replace_keys")):
                prof, added = llm_profiles.mutate_profile_keys(pid, "add", keys=keys)
                if prof is None:
                    upsert_body = dict(body)
                    upsert_body["id"] = pid
                    upsert_body["api_keys"] = keys
                    prof = llm_profiles.upsert_profile(upsert_body)
                    added = len(keys)
            else:
                upsert_body = dict(body)
                if pid:
                    upsert_body["id"] = pid
                upsert_body["api_keys"] = keys
                if bool(body.get("replace_keys")):
                    upsert_body["clear_api_key"] = False
                prof = llm_profiles.upsert_profile(upsert_body)
                added = len(keys)
            handler._send_json({"ok": True, "imported_keys": added, "total_keys": prof.get("key_count", added), "profile": prof})
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True

    if path == "/api/llm/pool/config":
        handler._send_json({"ok": True, "pool": {"enabled": False}, "retry_policy": body.get("retry_policy") or {}})
        return True

    if path == "/api/llm/pool/reset":
        from core import llm_profiles

        cnt = llm_profiles.reset_pool_cooldown(str(body.get("profile_id") or ""))
        handler._send_json({"ok": True, "reset": cnt})
        return True

    m_prof_keys = re.fullmatch(r"/api/llm/profiles/([^/]+)/keys", path)
    if m_prof_keys:
        from core import llm_profiles

        pid = m_prof_keys.group(1)
        action = str(body.get("action") or "add").strip().lower()
        raw_keys = body.get("keys") if isinstance(body.get("keys"), list) else ([body["key"]] if body.get("key") else [])
        try:
            prof, delta = llm_profiles.mutate_profile_keys(
                pid,
                action,
                keys=[str(k) for k in raw_keys],
                key_id=str(body.get("key_id") or ""),
            )
            if prof is None:
                handler._send_json({"ok": False, "error": f"供应商不存在: {pid}"}, status=404)
                return True
            handler._send_json({"ok": True, "added": delta, "removed": delta, "profile": prof})
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True

    m_prof_act = re.fullmatch(r"/api/llm/profiles/([^/]+)/(test|activate)", path)
    if m_prof_act:
        from core import llm_profiles

        pid = m_prof_act.group(1)
        action = m_prof_act.group(2)
        if action == "activate":
            prof = llm_profiles.activate_profile(pid)
            if prof is None:
                handler._send_json({"ok": False, "error": f"供应商不存在: {pid}"}, status=404)
                return True
            handler._send_json({"ok": True, "active_id": pid, "profile": prof})
        else:
            ov = llm_profiles.resolve_runtime_override(
                profile_id=pid,
                model_override=str(body.get("model") or ""),
                key_id=str(body.get("key_id") or ""),
                extra_overrides=body if body else None,
            )
            res = srv.llm_client.test_connection(overrides=ov)
            llm_profiles.record_profile_test_result(
                pid,
                ok=bool(res.get("ok")),
                latency_ms=float(res.get("latency_ms") or 0.0),
                error=str(res.get("error") or ""),
            )
            handler._send_json(res)
        return True

    m_ag_btw = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/side-question/stream", path)
    if m_ag_btw:
        import secrets
        from core import llm_profiles

        sid = m_ag_btw.group(1)
        sess = srv.agent_manager.get_session(sid)
        if not sess:
            handler._send_json({"ok": False, "error": "Agent session not found"}, status=404)
            return True
        question = str(body.get("question") or "").strip()
        if not question:
            handler._send_json({"ok": False, "error": "提问内容不能为空"}, status=400)
            return True

        with sess._lock:
            prof_id = str(body.get("llm_profile_id") or sess.llm_profile_id or "").strip()
            mod_ov = str(body.get("llm_model") or sess.llm_model or "").strip()
            recent = [
                f"{m.get('role')}: {str(m.get('content') or '')[:400]}"
                for m in sess.messages[-8:]
                if isinstance(m, dict) and m.get("content")
            ]
        ov = llm_profiles.resolve_runtime_override(profile_id=prof_id, model_override=mod_ov)
        ctx_blob = "\n".join(recent)
        prompt = f"以下是当前任务会话的最近上下文：\n{ctx_blob}\n\n用户旁路提问（不中断主任务，简洁中文作答）：\n{question}" if ctx_blob else question
        sq_id = f"sq_{int(time.time() * 1000)}_{secrets.token_hex(3)}"

        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        t0 = time.perf_counter()
        try:
            handler.wfile.write(f"data: {json.dumps({'type': 'start', 'id': sq_id}, ensure_ascii=False)}\n\n".encode("utf-8"))
            handler.wfile.flush()
            resp = srv.llm_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                system_prompt="你是 RegisterHub 旁路答疑助手，请基于上下文用简洁中文直接回答用户问题。",
                config_override=ov,
                timeout=45.0,
            )
            answer = str(resp.get("content") or "").strip() or "（模型未返回正文）"
            thinking = str(resp.get("thinking") or "").strip()
            ms = round((time.perf_counter() - t0) * 1000, 1)
            usage = resp.get("usage") or {}
            item_obj = {
                "id": sq_id,
                "question": question,
                "answer": answer,
                "thinking": thinking,
                "model": resp.get("model", ""),
                "latency_ms": ms,
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            if thinking:
                handler.wfile.write(f"data: {json.dumps({'type': 'thinking_delta', 'delta': thinking}, ensure_ascii=False)}\n\n".encode("utf-8"))
            handler.wfile.write(f"data: {json.dumps({'type': 'delta', 'delta': answer}, ensure_ascii=False)}\n\n".encode("utf-8"))
            handler.wfile.write(
                f"data: {json.dumps({'type': 'done', 'ok': True, 'item': item_obj, 'answer': answer, 'model': item_obj['model'], 'latency_ms': ms}, ensure_ascii=False)}\n\n".encode("utf-8")
            )
            handler.wfile.flush()
        except Exception as e:
            handler.wfile.write(f"data: {json.dumps({'type': 'error', 'error': str(e)}, ensure_ascii=False)}\n\n".encode("utf-8"))
            handler.wfile.flush()
        return True

    m_ag_up = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/upload", path)
    if m_ag_up:
        import base64

        sid = m_ag_up.group(1)
        sess = srv.agent_manager.get_session(sid)
        if not sess:
            handler._send_json({"ok": False, "error": "Agent session not found"}, status=404)
            return True
        raw_name = Path(str(body.get("filename") or "upload.bin")).name
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("._") or "upload.bin"
        b64 = str(body.get("content_b64") or "")
        try:
            data = base64.b64decode(b64, validate=False)
        except Exception as e:
            handler._send_json({"ok": False, "error": f"附件编码非法: {e}"}, status=400)
            return True
        if len(data) > 5 * 1024 * 1024:
            handler._send_json({"ok": False, "error": "附件超过 5MB 上限"}, status=400)
            return True
        up_dir = (srv.RESULTS_DIR / "agent_uploads" / sid).resolve()
        up_dir.mkdir(parents=True, exist_ok=True)
        dest = (up_dir / safe_name).resolve()
        if dest.parent != up_dir:
            handler._send_json({"ok": False, "error": "非法附件路径"}, status=400)
            return True
        dest.write_bytes(data)
        rel_path = f"results/agent_uploads/{sid}/{safe_name}"
        file_meta = {
            "name": safe_name,
            "path": rel_path,
            "size_bytes": len(data),
            "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        handler._send_json({"ok": True, "file": file_meta})
        return True

    m_ag_compact = re.fullmatch(r"/api/agent/sessions/([A-Za-z0-9_-]+)/compact", path)
    if m_ag_compact:
        sess = srv.agent_manager.get_session(m_ag_compact.group(1))
        if not sess:
            handler._send_json({"ok": False, "error": "Agent session not found"}, status=404)
            return True
        with sess._lock:
            before = len(sess.messages)
            if before > 16:
                sess.messages = sess.messages[-16:]
            after = len(sess.messages)
        sess.persist()
        handler._send_json({"ok": True, "before": before, "after": after, "message": f"已保留最近 {after} 条核心上下文"})
        return True

    if path == "/api/agent/config":
        from core.ai_agent import save_agent_config

        try:
            saved = save_agent_config(body)
            handler._send_json({"ok": True, "agent": saved})
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True

    return False


def handle_studio_put(handler: Any, srv: Any, path: str, body: Dict[str, Any]) -> bool:
    """Handle Studio PUT routes."""
    m_prof = re.fullmatch(r"/api/llm/profiles/([^/]+)", path)
    if m_prof:
        from core import llm_profiles

        pid = m_prof.group(1)
        try:
            prof = llm_profiles.upsert_profile({**body, "id": pid})
            handler._send_json({"ok": True, "id": prof["id"], "profile": prof})
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True
    return False


def handle_studio_patch(handler: Any, srv: Any, path: str, body: Dict[str, Any]) -> bool:
    """Handle Studio PATCH routes."""
    m_prof = re.fullmatch(r"/api/llm/profiles/([^/]+)", path)
    if m_prof:
        from core import llm_profiles

        pid = m_prof.group(1)
        try:
            prof = llm_profiles.upsert_profile({**body, "id": pid})
            handler._send_json({"ok": True, "id": prof["id"], "profile": prof})
        except Exception as e:
            handler._send_json({"ok": False, "error": str(e)}, status=400)
        return True
    return False


def handle_studio_delete(handler: Any, srv: Any, path: str) -> bool:
    """Handle Studio DELETE routes."""
    m_prof = re.fullmatch(r"/api/llm/profiles/([^/]+)", path)
    if m_prof:
        from core import llm_profiles

        pid = m_prof.group(1)
        ok = llm_profiles.delete_profile(pid)
        if not ok:
            handler._send_json({"ok": False, "error": f"供应商不存在: {pid}"}, status=404)
            return True
        handler._send_json({"ok": True, "deleted": pid})
        return True

    m_fl_err = re.fullmatch(r"/api/fleet/([^/]+)/errors", path)
    if m_fl_err:
        from core.locks import atomic_write_text_safe, interprocess_file_lock

        removed = 0
        if srv.FAILED_FILE.exists():
            with interprocess_file_lock(srv.FAILED_FILE):
                if srv.FAILED_FILE.exists():
                    atomic_write_text_safe(srv.FAILED_FILE, "")
                    removed += 1
        if srv.SNAPSHOTS_DIR.exists():
            for fp in srv.SNAPSHOTS_DIR.glob("*.png"):
                try:
                    fp.unlink()
                    removed += 1
                except OSError:
                    pass
        handler._send_json({"ok": True, "removed": removed})
        return True

    return False
