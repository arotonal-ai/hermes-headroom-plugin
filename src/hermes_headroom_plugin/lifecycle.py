"""Fail-open, tool-result-age lifecycle for model history."""
from __future__ import annotations

import hashlib
import re
from collections import OrderedDict, defaultdict
from copy import deepcopy
from typing import Any, Callable

from .policy import _already_compressed, _contains_protected_control, _exact_or_blocked_reason, _extract_markers

_CACHE: OrderedDict[str, str] = OrderedDict()
_CACHE_BYTES = 0
_CACHE_MAX = 512
_CACHE_MAX_BYTES = 8_000_000
_CRITICAL = re.compile(r"(?im)^.*(?:error|failed|rollback|recover|path|sha256|hash|status|\+\+\+|---|@@).*$")
_MUTATIONS = {"patch", "write_file", "mcp_open_design_write_file"}
_SAFE_AGGREGATE = {"terminal", "read_file", "search_files", "session_search", "web_search", "web_extract"}


def source_hash(tool: str, text: str) -> str:
    return hashlib.sha256((tool + "\0" + text).encode("utf-8", errors="replace")).hexdigest()


def retrieve_local_source(digest: str) -> str | None:
    # Kept as an import-compatible hook. Lifecycle never claims volatile memory
    # as durable recovery; exact recovery is owned by a real CCR marker.
    return None


def _tool_names(messages: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                if call.get("id"):
                    out[str(call["id"])] = str(fn.get("name") or call.get("name") or "unknown_tool")
    return out


def _marker(text: str) -> str | None:
    values = _extract_markers(text)
    return values[0] if values else None


def _call(compressor: Callable[..., str | None] | None, tool: str, text: str, digest: str, *, aggregate: bool) -> str | None:
    if compressor is None:
        return None
    try:
        return compressor(tool, text, digest, aggregate=aggregate)
    except TypeError:
        try:
            return compressor(tool, text, digest)
        except Exception:
            return None
    except Exception:
        return None


def _anchors(text: str) -> str:
    return "\n".join(dict.fromkeys(m.group(0)[:500] for m in _CRITICAL.finditer(text)))[:2400]


def _cache_put(key: str, value: str) -> None:
    global _CACHE_BYTES
    previous = _CACHE.pop(key, None)
    if previous is not None:
        _CACHE_BYTES -= len(previous)
    _CACHE[key] = value
    _CACHE_BYTES += len(value)
    while len(_CACHE) > _CACHE_MAX or _CACHE_BYTES > _CACHE_MAX_BYTES:
        _, removed = _CACHE.popitem(last=False)
        _CACHE_BYTES -= len(removed)


def _warm(tool: str, text: str, digest: str, replacement: str, marker: str) -> str:
    key = "warm:" + digest + ":" + marker
    cached = _CACHE.get(key)
    if cached is not None:
        _CACHE.move_to_end(key)
        return cached
    header = "\n".join(text.splitlines()[:4])[:1200]
    anchors = _anchors(text)
    form = f"[Headroom warm tool result tool={tool} source_sha256={digest}]\n{header}"
    if anchors:
        form += "\nCritical anchors:\n" + anchors
    form += f"\nCompressed body:\n{replacement[:4000]}\nDurable recovery: headroom_retrieve(hash='{marker}')"
    _cache_put(key, form)
    return form


def _cold(tool: str, text: str, digest: str, marker: str) -> str:
    key = "cold:" + digest + ":" + marker
    cached = _CACHE.get(key)
    if cached is not None:
        _CACHE.move_to_end(key)
        return cached
    form = f"[Headroom cold tool result tool={tool} source_sha256={digest}]"
    anchors = _anchors(text)
    if anchors:
        form += "\nCritical anchors:\n" + anchors
    form += f"\nDurable recovery: headroom_retrieve(hash='{marker}')"
    _cache_put(key, form)
    return form


def transform_history(messages: list[dict[str, Any]], *, protect_first_n: int = 0, protect_last_n: int = 0,
                      hot_tool_results: int = 4, warm_tool_results: int = 8,
                      aggregate_budget_chars: int = 16_000, min_item_chars: int = 8_000,
                      compressor: Callable[..., str | None] | None = None,
                      retain: Callable[[str, str], None] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Transform eligible tool results without changing history structure.

    ``retain`` remains accepted for API compatibility but is intentionally not
    called: only the compressor's durable CCR store may own exact sources.
    """
    del retain
    original = deepcopy(messages)
    result = deepcopy(messages)
    names = _tool_names(result)
    # Match the host contract: system messages are always exact, then the
    # first N non-system messages and final N messages are exact as well.
    non_system = [i for i, message in enumerate(result) if message.get("role") != "system"]
    protected_indices = {
        i for i, message in enumerate(result) if message.get("role") == "system"
    }
    protected_indices.update(non_system[: max(0, protect_first_n)])
    if protect_last_n > 0:
        protected_indices.update(range(max(0, len(result) - protect_last_n), len(result)))
    candidates: list[dict[str, Any]] = []
    for index, message in enumerate(result):
        if message.get("role") != "tool" or not isinstance(message.get("content"), str):
            continue
        if index in protected_indices:
            continue
        tool = names.get(str(message.get("tool_call_id") or ""), str(message.get("name") or "unknown_tool"))
        candidates.append({"index": index, "message": message, "tool": tool, "text": message["content"]})
    total = len(candidates)
    for ordinal, item in enumerate(candidates):
        newest_rank = total - 1 - ordinal
        item["age"] = "hot" if newest_rank < hot_tool_results else "warm" if newest_rank < hot_tool_results + warm_tool_results else "cold"

    changed = blocked = backlog = compressor_calls = 0
    eligible_small: list[dict[str, Any]] = []
    for item in candidates:
        text, tool, age = item["text"], item["tool"], item["age"]
        if _contains_protected_control(tool, {}, text):
            blocked += 1
            continue
        if age == "hot" or tool in _MUTATIONS or tool == "headroom_retrieve":
            continue
        reason = _exact_or_blocked_reason(tool, {}, text)
        if reason and tool not in {"read_file", "search_files", "session_search"}:
            continue
        existing = _marker(text)
        if _already_compressed(text):
            # Reuse the real marker. Never nest or synthesize recovery handles.
            if age == "cold" and existing:
                digest = source_hash(tool, text)
                item["message"]["content"] = _cold(tool, text, digest, existing)
                changed += 1
            continue
        if len(text) < min_item_chars:
            backlog += len(text)
            if tool in _SAFE_AGGREGATE:
                eligible_small.append(item)
            continue
        digest = source_hash(tool, text)
        replacement = _call(compressor, tool, text, digest, aggregate=False)
        compressor_calls += int(compressor is not None)
        marker = _marker(replacement or "")
        if not replacement or not marker or len(replacement) >= len(text):
            continue
        item["message"]["content"] = (
            _cold(tool, text, digest, marker)
            if age == "cold"
            else _warm(tool, text, digest, replacement, marker)
        )
        changed += 1

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in eligible_small:
        groups[(item["tool"], item["age"])].append(item)
    for (tool, age), group in sorted(groups.items()):
        cumulative = sum(len(x["text"]) for x in group)
        if len(group) < 2 or cumulative <= aggregate_budget_chars:
            continue
        joined = "\n\n".join(f"source_sha256={source_hash(tool, x['text'])}\n{x['text']}" for x in group)
        group_digest = source_hash(tool + ":" + age, joined)
        replacement = _call(compressor, tool, joined, group_digest, aggregate=True)
        compressor_calls += int(compressor is not None)
        marker = _marker(replacement or "")
        if not replacement or not marker or len(replacement) >= len(joined):
            continue
        for position, item in enumerate(group, 1):
            digest = source_hash(tool, item["text"])
            anchors = _anchors(item["text"])
            body = f"[Headroom aggregated {age} tool result tool={tool} source_sha256={digest} group_sha256={group_digest} part={position}/{len(group)}]"
            if anchors:
                body += "\nCritical anchors:\n" + anchors
            # Include the summary once; every paired result retains the same
            # durable aggregate handle without multiplying model exposure.
            if position == 1:
                body += f"\nShared compressed summary:\n{replacement[:3000]}"
            body += f"\nDurable recovery: headroom_retrieve(hash='{marker}')"
            item["message"]["content"] = body
            changed += 1
    return result, {"changed": changed, "below_min_backlog_bytes": backlog, "blocked": blocked,
                    "compressor_calls": compressor_calls, "tool_results": total}
