"""Hermes tool-execution middleware adapter."""
from __future__ import annotations

import json
from typing import Any

from .observability import _emit_headroom_event
from .policy import MACHINE_CONSUMER_EXACT_TOOLS, MUTATING_MCP_HINTS, READ_ONLY_MCP_HINTS
from .reduction import _compress_structured_result_for_context, compress_tool_result_for_context


def _machine_consumer_requires_exact(
    tool_name: str,
    *,
    task_id: str = "",
    tool_call_id: str = "",
    session_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
) -> bool:
    """Protect structured tool-to-tool contracts from contextual compression.

    Programmatic Hermes tool calls carry the parent task but no model-facing
    call/session/request identifiers. Their return value is parsed by code,
    not consumed as model context, so changing its shape is unsafe.
    """
    if not task_id or any((tool_call_id, session_id, turn_id, api_request_id)):
        return False
    tool = str(tool_name or "").lower()
    if tool in MACHINE_CONSUMER_EXACT_TOOLS:
        return True
    if tool.startswith(("mcp__", "mcp_")):
        return any(hint in tool for hint in READ_ONLY_MCP_HINTS) and not any(
            hint in tool for hint in MUTATING_MCP_HINTS
        )
    return False


def on_tool_execution(
    tool_name: str = "",
    args: dict[str, Any] | None = None,
    next_call: Any = None,
    task_id: str = "",
    tool_call_id: str = "",
    duration_ms: Any = None,
    session_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    platform: str = "",
    **_: Any,
) -> Any:
    """Compress eligible bulky tool/lane results, including delegate_task.

    Fail-open: after the wrapped tool returns, the original result is returned
    whenever Headroom is unhealthy, the data class is exact/blocked,
    compression is not useful, or any plugin error occurs. Exceptions raised by
    the wrapped tool itself propagate unchanged; swallowing them would alter
    Hermes tool semantics rather than provide middleware fail-open behavior.
    """
    if not callable(next_call):
        return None
    current_args = args if isinstance(args, dict) else {}
    result = next_call(current_args)
    try:
        if _machine_consumer_requires_exact(
            str(tool_name or ""),
            task_id=task_id or "",
            tool_call_id=tool_call_id or "",
            session_id=session_id or "",
            turn_id=turn_id or "",
            api_request_id=api_request_id or "",
        ):
            _emit_headroom_event(
                action="exact",
                tool_name=str(tool_name or ""),
                args=current_args,
                reason="machine_consumer_contract",
                task_id=task_id or "",
                tool_call_id=tool_call_id or "",
                session_id=session_id or "",
                turn_id=turn_id or "",
                api_request_id=api_request_id or "",
                platform=platform or "",
                original_chars=len(result) if isinstance(result, str) else len(json.dumps(result, ensure_ascii=False, default=str)),
                exact_authority="original_machine_result",
            )
            return result
        if isinstance(result, str):
            transformed = compress_tool_result_for_context(
                tool_name=str(tool_name or ""),
                args=current_args,
                result=result,
                task_id=task_id or "",
                tool_call_id=tool_call_id or "",
                session_id=session_id or "",
                turn_id=turn_id or "",
                api_request_id=api_request_id or "",
                platform=platform or "",
                duration_ms=duration_ms,
            )
            if transformed:
                return transformed
        structured = _compress_structured_result_for_context(
            tool_name=str(tool_name or ""),
            args=current_args,
            result=result,
            task_id=task_id or "",
            tool_call_id=tool_call_id or "",
            session_id=session_id or "",
            turn_id=turn_id or "",
            api_request_id=api_request_id or "",
            platform=platform or "",
            duration_ms=duration_ms,
        )
        if structured is not None:
            return structured
    except Exception as exc:
        _emit_headroom_event(
            action="error",
            tool_name=str(tool_name or ""),
            args=current_args,
            reason="middleware_exception",
            task_id=task_id or "",
            tool_call_id=tool_call_id or "",
            session_id=session_id or "",
            turn_id=turn_id or "",
            api_request_id=api_request_id or "",
            platform=platform or "",
            original_chars=len(result) if isinstance(result, str) else None,
            error=f"{type(exc).__name__}: {exc}",
            exact_authority="original_tool_result",
        )
        return result
    return result
