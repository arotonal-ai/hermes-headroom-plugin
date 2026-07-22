"""Content-free request accounting and deterministic waterfall reports."""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .policy import _already_compressed, _exact_or_blocked_reason


def _chars(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    except Exception:
        return 0


def classify_request(request: dict[str, Any], api_mode: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Account for every request item without retaining request content."""
    context = context or {}
    items = request.get("input") if api_mode in {"responses", "codex_responses", "openai_responses"} else request.get("messages")
    items = items if isinstance(items, list) else []
    roles, tools, ages, classes, reasons = Counter(), Counter(), Counter(), Counter(), Counter()
    total_bytes = 0
    retained_exposure = 0
    below_min = 0
    call_names: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"function_call", "tool_call"} and item.get("call_id"):
            call_names[str(item["call_id"])] = str(item.get("name") or "unknown_tool")
        for call in item.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id"):
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                call_names[str(call["id"])] = str(fn.get("name") or call.get("name") or "unknown_tool")
    for index, item in enumerate(items):
        item = item if isinstance(item, dict) else {"value": item}
        role = str(item.get("role") or item.get("type") or "unknown")
        call_id = str(item.get("call_id") or item.get("tool_call_id") or "")
        tool = str(item.get("name") or item.get("tool_name") or call_names.get(call_id) or "")
        body = item.get("content", item.get("output", item))
        size = _chars(body)
        total_bytes += size
        age = "hot" if index >= max(0, len(items) - 2) else "cold" if index < max(0, len(items) - 8) else "warm"
        reason = ""
        if role in {"system", "developer"}:
            cls, reason = "protected", "authority_role"
        elif role in {"tool", "function_call_output"} or item.get("type") == "function_call_output":
            reason = _exact_or_blocked_reason(tool or "unknown_tool", {}, body if isinstance(body, str) else "") or ""
            cls = "exact" if reason else "deferred" if _already_compressed(str(body)) else "compressible"
            if size < 8_000 and cls == "compressible":
                below_min += size
                reasons["below_min_chars"] += 1
        else:
            cls = "exact"
        if cls in {"exact", "protected"}:
            retained_exposure += size
        roles[role] += 1; tools[tool or "none"] += 1; ages[age] += 1; classes[cls] += 1
        if reason: reasons[reason] += 1
    schemas = request.get("tools") or []
    schema_stats = Counter()
    schema_bytes = Counter()
    if isinstance(schemas, list):
        for schema in schemas:
            size = _chars(schema)
            fn = schema.get("function") if isinstance(schema, dict) and isinstance(schema.get("function"), dict) else schema
            name = str(fn.get("name") or "") if isinstance(fn, dict) else ""
            deferred = bool(isinstance(schema, dict) and schema.get("defer_loading"))
            if isinstance(fn, dict):
                deferred = deferred or bool(fn.get("defer_loading"))
            if api_mode in {"responses", "codex_responses", "openai_responses"}:
                from .request_shaper import RESIDENT_TOOLS
                deferred = deferred or name not in RESIDENT_TOOLS
            bucket = "deferable" if deferred else "resident"
            schema_stats[bucket] += 1; schema_bytes[bucket] += size
    report = {
        "schema": "headroom.waterfall.v1", "api_mode": api_mode,
        "correlation": {k: str(context.get(k) or "") for k in ("session_id", "turn_id", "task_id", "tool_call_id", "api_request_id")},
        "items": {"total": len(items), "accounted": sum(classes.values()), "bytes": total_bytes, "rough_tokens": (total_bytes + 3) // 4},
        "by_role": dict(sorted(roles.items())), "by_tool": dict(sorted(tools.items())), "by_age": dict(sorted(ages.items())),
        "by_class": dict(sorted(classes.items())), "reason_families": dict(sorted(reasons.items())), "below_min_backlog_bytes": below_min,
        "schemas": {"total": len(schemas) if isinstance(schemas, list) else 0, "bytes": _chars(schemas),
                    "resident": schema_stats["resident"], "resident_bytes": schema_bytes["resident"],
                    "deferable": schema_stats["deferable"], "deferable_bytes": schema_bytes["deferable"]},
        "unique_transforms": int(context.get("unique_transforms") or 0),
        "retained_exposure_chars": retained_exposure + int(context.get("retained_exposure_chars") or 0),
        "usage": {k: context[k] for k in ("prompt_tokens", "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "total_tokens", "provider") if k in context},
        "latency_ms": {k: context[k] for k in ("request_latency_ms", "compression_latency_ms", "retrieval_latency_ms") if k in context},
    }
    if report["items"]["accounted"] != report["items"]["total"]:
        raise AssertionError("request item accounting invariant violated")
    return report


def render_waterfall(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
