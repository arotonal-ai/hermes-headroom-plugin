"""Opt-in common LLM-request tool-result adapter."""
from __future__ import annotations

import hashlib
import json

import threading
from collections import OrderedDict
from copy import deepcopy
from typing import Any

from .config import llm_request_compression_enabled, resolve_effective_config
from .observability import _emit_headroom_event
from .policy import _already_compressed, _extract_markers
from .reduction import compress_tool_result_for_context

_LLM_REQUEST_CACHE_MAX = resolve_effective_config().llm_request_cache_max
_LLM_REQUEST_TRANSFORM_CACHE: OrderedDict[str, str] = OrderedDict()
_LLM_REQUEST_CACHE_LOCK = threading.RLock()


def _request_tool_info(
    *,
    tool_name: Any,
    tool_args: Any,
) -> tuple[str, dict[str, Any]]:
    name = str(tool_name or "").strip() or "unknown_tool"
    if isinstance(tool_args, dict):
        return name, tool_args
    if isinstance(tool_args, str):
        try:
            parsed = json.loads(tool_args)
        except Exception:
            parsed = {}
        return name, parsed if isinstance(parsed, dict) else {}
    return name, {}


def _request_source_fingerprint(
    *,
    text: str,
    tool_name: str,
    tool_call_id: Any,
    api_mode: str,
    context: dict[str, Any],
) -> str:
    identity = {
        "schema": "headroom.llm_request_source.v1",
        "session_id": str(context.get("session_id") or ""),
        "tool_call_id": str(tool_call_id or ""),
        "api_mode": api_mode,
        "tool_name": tool_name,
        "content_sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _request_cache_get(fingerprint: str) -> str | None:
    with _LLM_REQUEST_CACHE_LOCK:
        value = _LLM_REQUEST_TRANSFORM_CACHE.get(fingerprint)
        if value is not None:
            _LLM_REQUEST_TRANSFORM_CACHE.move_to_end(fingerprint)
        return value


def _request_cache_put(fingerprint: str, transformed: str) -> None:
    with _LLM_REQUEST_CACHE_LOCK:
        _LLM_REQUEST_TRANSFORM_CACHE[fingerprint] = transformed
        _LLM_REQUEST_TRANSFORM_CACHE.move_to_end(fingerprint)
        while len(_LLM_REQUEST_TRANSFORM_CACHE) > _LLM_REQUEST_CACHE_MAX:
            _LLM_REQUEST_TRANSFORM_CACHE.popitem(last=False)


def _emit_request_cache_reuse(
    *,
    original: str,
    transformed: str,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_call_id: Any,
    api_mode: str,
    context: dict[str, Any],
    logical_source_id: str,
) -> None:
    markers = _extract_markers([transformed])
    _emit_headroom_event(
        action="retained",
        tool_name=tool_name,
        args=tool_args,
        reason="request_cache_reuse",
        task_id=str(context.get("task_id") or ""),
        tool_call_id=str(tool_call_id or ""),
        session_id=str(context.get("session_id") or ""),
        turn_id=str(context.get("turn_id") or ""),
        api_request_id=str(context.get("api_request_id") or ""),
        platform=str(context.get("platform") or ""),
        surface="llm_request",
        original_chars=len(original),
        model_facing_chars_before=len(original),
        model_facing_chars_after=len(transformed),
        measurement_scope=f"llm_request_tool_result:{api_mode}",
        new_savings_event=False,
        marker=markers[0] if markers else "",
        exact_authority="request_transform_cache",
        logical_source_id=logical_source_id,
    )


def _compress_request_text(
    text: Any,
    *,
    tool_name: Any,
    tool_args: Any,
    tool_call_id: Any,
    api_mode: str,
    context: dict[str, Any],
) -> tuple[Any, bool]:
    """Compress one textual tool result at the common request boundary."""
    if not isinstance(text, str) or len(text) < resolve_effective_config().min_tool_result_chars or _already_compressed(text):
        return text, False
    name, args = _request_tool_info(tool_name=tool_name, tool_args=tool_args)
    logical_source_id = _request_source_fingerprint(
        text=text,
        tool_name=name,
        tool_call_id=tool_call_id,
        api_mode=api_mode,
        context=context,
    )
    cached = _request_cache_get(logical_source_id)
    if cached is not None:
        _emit_request_cache_reuse(
            original=text,
            transformed=cached,
            tool_name=name,
            tool_args=args,
            tool_call_id=tool_call_id,
            api_mode=api_mode,
            context=context,
            logical_source_id=logical_source_id,
        )
        return cached, True
    transformed = compress_tool_result_for_context(
        tool_name=name,
        args=args,
        result=text,
        task_id=str(context.get("task_id") or ""),
        tool_call_id=str(tool_call_id or ""),
        session_id=str(context.get("session_id") or ""),
        turn_id=str(context.get("turn_id") or ""),
        api_request_id=str(context.get("api_request_id") or ""),
        platform=str(context.get("platform") or ""),
        event_surface="llm_request",
        measurement_scope_override=f"llm_request_tool_result:{api_mode}",
        allow_below_min_aggregate=False,
        logical_source_id=logical_source_id,
    )
    if transformed:
        _request_cache_put(logical_source_id, transformed)
        return transformed, True
    return text, False


def _compress_text_parts(
    parts: Any,
    *,
    accepted_types: set[str],
    text_key: str,
    tool_name: Any,
    tool_args: Any,
    tool_call_id: Any,
    api_mode: str,
    context: dict[str, Any],
) -> int:
    """Compress text blocks in place while preserving image/metadata blocks."""
    if not isinstance(parts, list):
        return 0
    changed = 0
    for part in parts:
        if not isinstance(part, dict) or str(part.get("type") or "") not in accepted_types:
            continue
        replacement, did_change = _compress_request_text(
            part.get(text_key),
            tool_name=tool_name,
            tool_args=tool_args,
            tool_call_id=tool_call_id,
            api_mode=api_mode,
            context=context,
        )
        if did_change:
            part[text_key] = replacement
            changed += 1
    return changed


def _adapt_chat_completions_request(request: dict[str, Any], context: dict[str, Any]) -> int:
    messages = request.get("messages")
    if not isinstance(messages, list):
        return 0
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    changed = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                raw_function = call.get("function")
                function: dict[str, Any] = raw_function if isinstance(raw_function, dict) else {}
                call_id = str(call.get("id") or call.get("call_id") or "")
                if call_id:
                    calls[call_id] = _request_tool_info(
                        tool_name=function.get("name"),
                        tool_args=function.get("arguments"),
                    )
            continue
        if message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id") or "")
        name, args = calls.get(
            call_id,
            _request_tool_info(
                tool_name=message.get("name") or message.get("tool_name"),
                tool_args={},
            ),
        )
        content = message.get("content")
        if isinstance(content, str):
            replacement, did_change = _compress_request_text(
                content,
                tool_name=name,
                tool_args=args,
                tool_call_id=call_id,
                api_mode="chat_completions",
                context=context,
            )
            if did_change:
                message["content"] = replacement
                changed += 1
        else:
            changed += _compress_text_parts(
                content,
                accepted_types={"text", "input_text", "output_text"},
                text_key="text",
                tool_name=name,
                tool_args=args,
                tool_call_id=call_id,
                api_mode="chat_completions",
                context=context,
            )
    return changed


def _adapt_responses_request(request: dict[str, Any], context: dict[str, Any]) -> int:
    items = request.get("input")
    if not isinstance(items, list):
        return 0
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    changed = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        call_id = str(item.get("call_id") or "")
        if item_type == "function_call" and call_id:
            calls[call_id] = _request_tool_info(
                tool_name=item.get("name"),
                tool_args=item.get("arguments"),
            )
            continue
        if item_type != "function_call_output" or not call_id:
            continue
        name, args = calls.get(call_id, ("unknown_tool", {}))
        output = item.get("output")
        if isinstance(output, str):
            replacement, did_change = _compress_request_text(
                output,
                tool_name=name,
                tool_args=args,
                tool_call_id=call_id,
                api_mode="codex_responses",
                context=context,
            )
            if did_change:
                item["output"] = replacement
                changed += 1
        else:
            changed += _compress_text_parts(
                output,
                accepted_types={"input_text"},
                text_key="text",
                tool_name=name,
                tool_args=args,
                tool_call_id=call_id,
                api_mode="codex_responses",
                context=context,
            )
    return changed


def _adapt_anthropic_request(request: dict[str, Any], context: dict[str, Any]) -> int:
    messages = request.get("messages")
    if not isinstance(messages, list):
        return 0
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    changed = 0
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                call_id = str(block.get("id") or "")
                if call_id:
                    calls[call_id] = _request_tool_info(
                        tool_name=block.get("name"),
                        tool_args=block.get("input"),
                    )
                continue
            if block_type != "tool_result":
                continue
            call_id = str(block.get("tool_use_id") or "")
            name, args = calls.get(call_id, ("unknown_tool", {}))
            content = block.get("content")
            if isinstance(content, str):
                replacement, did_change = _compress_request_text(
                    content,
                    tool_name=name,
                    tool_args=args,
                    tool_call_id=call_id,
                    api_mode="anthropic_messages",
                    context=context,
                )
                if did_change:
                    block["content"] = replacement
                    changed += 1
            else:
                changed += _compress_text_parts(
                    content,
                    accepted_types={"text"},
                    text_key="text",
                    tool_name=name,
                    tool_args=args,
                    tool_call_id=call_id,
                    api_mode="anthropic_messages",
                    context=context,
                )
    return changed


def _adapt_bedrock_request(request: dict[str, Any], context: dict[str, Any]) -> int:
    messages = request.get("messages")
    if not isinstance(messages, list):
        return 0
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    changed = 0
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            tool_use = block.get("toolUse")
            if isinstance(tool_use, dict):
                call_id = str(tool_use.get("toolUseId") or "")
                if call_id:
                    calls[call_id] = _request_tool_info(
                        tool_name=tool_use.get("name"),
                        tool_args=tool_use.get("input"),
                    )
                continue
            tool_result = block.get("toolResult")
            if not isinstance(tool_result, dict):
                continue
            call_id = str(tool_result.get("toolUseId") or "")
            name, args = calls.get(call_id, ("unknown_tool", {}))
            content = tool_result.get("content")
            if isinstance(content, str):
                replacement, did_change = _compress_request_text(
                    content,
                    tool_name=name,
                    tool_args=args,
                    tool_call_id=call_id,
                    api_mode="bedrock_converse",
                    context=context,
                )
                if did_change:
                    tool_result["content"] = replacement
                    changed += 1
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                        continue
                    replacement, did_change = _compress_request_text(
                        part.get("text"),
                        tool_name=name,
                        tool_args=args,
                        tool_call_id=call_id,
                        api_mode="bedrock_converse",
                        context=context,
                    )
                    if did_change:
                        part["text"] = replacement
                        changed += 1
    return changed


def on_llm_request(request: dict[str, Any] | None = None, api_mode: str = "", **context: Any):
    """Compress eligible tool results at Hermes's common pre-transport boundary.

    Provider/model routing is never changed. The adapter is opt-in, copy-on-write,
    protocol-aware, and fail-open. Unsupported shapes return no middleware result.
    """
    if not isinstance(request, dict) or not llm_request_compression_enabled():
        return None
    normalized_mode = str(api_mode or "").strip().lower()
    aliases = {
        "responses": "codex_responses",
        "openai_responses": "codex_responses",
        "anthropic": "anthropic_messages",
        "bedrock": "bedrock_converse",
        "openai_chat": "chat_completions",
    }
    normalized_mode = aliases.get(normalized_mode, normalized_mode)
    adapters = {
        "chat_completions": _adapt_chat_completions_request,
        "codex_responses": _adapt_responses_request,
        "anthropic_messages": _adapt_anthropic_request,
        "bedrock_converse": _adapt_bedrock_request,
    }
    adapter = adapters.get(normalized_mode)
    if adapter is None:
        return None
    try:
        effective_request = deepcopy(request)
        changed = adapter(effective_request, {**context, "api_mode": normalized_mode})
    except Exception:
        return None
    if changed <= 0:
        return None
    return {
        "request": effective_request,
        "source": "headroom_retrieve",
        "reason": f"compressed_tool_results:{normalized_mode}:{changed}",
    }
