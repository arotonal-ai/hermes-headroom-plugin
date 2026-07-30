"""Tool handlers."""
from __future__ import annotations

import json

from .contracts import normalize_ccr_hash
from .proxy import retrieve
from .local_exact_store import MISSING, retrieve_local_source_result
from .net_ledger import append_retrieval_event


def handle_headroom_retrieve(args: dict, **kwargs) -> str:
    hash_key = normalize_ccr_hash(args.get("hash"))
    if not hash_key:
        return json.dumps({"success": False, "error": "missing or invalid Headroom hash"}, ensure_ascii=False)
    local = retrieve_local_source_result(hash_key)
    if local.exact:
        rendered = json.dumps(local.as_dict(), ensure_ascii=False)
        append_retrieval_event(
            marker=hash_key,
            model_facing_chars=len(rendered),
            success=True,
            source=local.source,
            state=local.state,
            session_id=str(kwargs.get("session_id") or ""),
            turn_id=str(kwargs.get("turn_id") or ""),
            task_id=str(kwargs.get("task_id") or ""),
            tool_call_id=str(kwargs.get("tool_call_id") or ""),
            api_request_id=str(kwargs.get("api_request_id") or ""),
        )
        return rendered
    result = retrieve(hash_key)
    result["local_fallback_state"] = local.state
    if local.state != MISSING:
        result["local_fallback"] = local.as_dict(include_content=False)
    rendered = json.dumps(result, ensure_ascii=False)
    success = bool(result.get("success", "error" not in result)) and not result.get("error")
    append_retrieval_event(
        marker=hash_key,
        model_facing_chars=len(rendered),
        success=success,
        source="headroom_ccr_proxy",
        state="exact" if success else local.state,
        session_id=str(kwargs.get("session_id") or ""),
        turn_id=str(kwargs.get("turn_id") or ""),
        task_id=str(kwargs.get("task_id") or ""),
        tool_call_id=str(kwargs.get("tool_call_id") or ""),
        api_request_id=str(kwargs.get("api_request_id") or ""),
    )
    return rendered
