"""Provider/cache/retrieval-aware net context ledger.

The ledger never adds non-additive authorities. Character-level middleware deltas
produce a labelled provider-neutral estimate; provider usage/cache and billing
remain separate exact observations keyed by ``api_request_id``.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .observability import TOKEN_ESTIMATOR, append_metadata_event, _rough_tokens_from_chars

NET_LEDGER_SCHEMA = "headroom.net_ledger.v1"


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _source_id(event: Mapping[str, Any]) -> str:
    for key in ("logical_source_id", "source_id", "dedupe_key", "marker", "hash"):
        value = str(event.get(key) or "").strip()
        if value:
            return value
    canonical = json.dumps(
        {
            "session_id": event.get("session_id"),
            "tool_call_id": event.get("tool_call_id"),
            "tool_name": event.get("tool_name"),
            "original_chars": event.get("original_chars"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def append_retrieval_event(
    *,
    marker: str,
    model_facing_chars: int,
    success: bool,
    source: str,
    state: str,
    session_id: str = "",
    turn_id: str = "",
    task_id: str = "",
    tool_call_id: str = "",
    api_request_id: str = "",
) -> None:
    """Record exact retrieval reintroduction without retaining retrieved text."""
    identity = {
        "schema": NET_LEDGER_SCHEMA,
        "marker": str(marker),
        "session_id": str(session_id),
        "turn_id": str(turn_id),
        "tool_call_id": str(tool_call_id),
        "api_request_id": str(api_request_id),
    }
    dedupe_key = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    append_metadata_event(
        {
            "type": "headroom_retrieval",
            "schema": NET_LEDGER_SCHEMA,
            "event_id": uuid.uuid4().hex,
            "dedupe_key": dedupe_key,
            "marker": str(marker),
            "session_id": str(session_id),
            "turn_id": str(turn_id),
            "task_id": str(task_id),
            "tool_call_id": str(tool_call_id),
            "api_request_id": str(api_request_id),
            "success": bool(success),
            "source": str(source),
            "state": str(state),
            "model_facing_chars": _int(model_facing_chars),
            "reintroduced_est_tokens": _rough_tokens_from_chars(model_facing_chars),
        }
    )


def build_net_ledger(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Join bounded attribution events into deduplicated task/source rows."""
    source_rows: dict[str, dict[str, Any]] = {}
    marker_to_source: dict[str, str] = {}
    pending_retrievals: list[Mapping[str, Any]] = []
    provider_requests: dict[str, dict[str, Any]] = {}
    pending_overheads: list[Mapping[str, Any]] = []
    seen_event_ids: set[str] = set()
    seen_source_keys: set[str] = set()
    seen_retrieval_keys: set[str] = set()
    task_results: dict[str, dict[str, Any]] = {}

    for raw in events:
        event = dict(raw)
        event_id = str(event.get("event_id") or "")
        if event_id and event_id in seen_event_ids:
            continue
        if event_id:
            seen_event_ids.add(event_id)
        event_type = str(event.get("type") or "")
        action = str(event.get("action") or "")

        if event_type == "headroom_tool_result" and action == "compressed":
            source_key = str(event.get("logical_source_id") or event.get("dedupe_key") or _source_id(event))
            if source_key in seen_source_keys:
                continue
            seen_source_keys.add(source_key)
            marker = str(event.get("marker") or "")
            before_chars = _int(event.get("model_facing_chars_before", event.get("original_chars")))
            after_chars = _int(event.get("model_facing_chars_after", before_chars))
            row = {
                "source_id": source_key,
                "session_id": str(event.get("session_id") or ""),
                "turn_id": str(event.get("turn_id") or ""),
                "task_id": str(event.get("task_id") or ""),
                "tool_call_id": str(event.get("tool_call_id") or ""),
                "tool_name": str(event.get("tool_name") or ""),
                "marker": marker,
                "eligible_source_chars": before_chars,
                "transformed_chars": after_chars,
                "gross_saved_chars": max(0, before_chars - after_chars),
                "retrieval_reintroduced_chars": 0,
                "extra_call_input_tokens": 0,
                "retry_input_tokens": 0,
                "quality_correction_input_tokens": 0,
                "compression_latency_ms": event.get("compression_latency_ms"),
                "provider_request_ids": [],
                "task_success": None,
            }
            source_rows[source_key] = row
            if marker:
                marker_to_source[marker] = source_key
            continue

        if event_type == "headroom_retrieval":
            dedupe = str(event.get("dedupe_key") or event_id or _source_id(event))
            if dedupe in seen_retrieval_keys:
                continue
            seen_retrieval_keys.add(dedupe)
            pending_retrievals.append(event)
            continue

        if event_type in {"provider_usage", "headroom_provider_usage"}:
            request_id = str(event.get("api_request_id") or "").strip()
            if not request_id or request_id in provider_requests:
                continue
            prompt_total = _int(event.get("prompt_tokens")) or _int(event.get("input_tokens"))
            provider_requests[request_id] = {
                "api_request_id": request_id,
                "provider": str(event.get("provider") or ""),
                "model": str(event.get("model") or ""),
                "prompt_or_input_tokens": prompt_total,
                "cache_read_tokens": _int(event.get("cache_read_tokens")),
                "cache_write_tokens": _int(event.get("cache_write_tokens")),
                "uncached_input_tokens": _int(event.get("uncached_input_tokens")),
                "output_tokens": _int(event.get("output_tokens", event.get("completion_tokens"))),
                "billing_amount": event.get("billing_amount"),
                "billing_currency": event.get("billing_currency"),
                "billing_authority": str(event.get("billing_authority") or "unavailable"),
                "logical_source_id": str(event.get("logical_source_id") or ""),
                "cache_semantics": "non_additive_component",
            }
            continue

        if event_type in {"headroom_retry", "headroom_extra_call"}:
            pending_overheads.append(event)
            continue

        if event_type == "headroom_task_result":
            task_key = str(event.get("task_id") or event.get("turn_id") or "")
            if task_key:
                task_results[task_key] = {
                    "task_id": task_key,
                    "success": event.get("success"),
                    "latency_ms": event.get("latency_ms"),
                    "critical_failure": bool(event.get("critical_failure")),
                }

    unmatched_retrievals = 0
    for event in pending_retrievals:
        marker = str(event.get("marker") or event.get("hash") or "")
        source_key = marker_to_source.get(marker)
        if not source_key:
            unmatched_retrievals += 1
            continue
        source_rows[source_key]["retrieval_reintroduced_chars"] += _int(event.get("model_facing_chars"))

    unmatched_overheads = 0
    for event in pending_overheads:
        source_key = _source_id(event)
        if source_key not in source_rows:
            unmatched_overheads += 1
            continue
        source_rows[source_key]["extra_call_input_tokens"] += _int(event.get("extra_call_input_tokens"))
        source_rows[source_key]["retry_input_tokens"] += _int(event.get("retry_input_tokens"))
        source_rows[source_key]["quality_correction_input_tokens"] += _int(event.get("quality_correction_input_tokens"))

    for request_id, request in provider_requests.items():
        source_key = str(request.get("logical_source_id") or "")
        if source_key in source_rows:
            source_rows[source_key]["provider_request_ids"].append(request_id)

    rows: list[dict[str, Any]] = []
    for row in source_rows.values():
        gross_tokens = _rough_tokens_from_chars(row["gross_saved_chars"])
        retrieval_tokens = _rough_tokens_from_chars(row["retrieval_reintroduced_chars"])
        explicit_overhead = (
            row["extra_call_input_tokens"]
            + row["retry_input_tokens"]
            + row["quality_correction_input_tokens"]
        )
        row["gross_est_tokens_saved"] = gross_tokens
        row["retrieval_reintroduced_est_tokens"] = retrieval_tokens
        row["net_est_tokens_saved"] = gross_tokens - retrieval_tokens - explicit_overhead
        row["token_estimator"] = TOKEN_ESTIMATOR
        row["metric_authority"] = "middleware_exact_chars_provider_neutral_estimate"
        task_key = row["task_id"] or row["turn_id"]
        if task_key in task_results:
            row["task_success"] = task_results[task_key]
        rows.append(row)
    rows.sort(key=lambda item: item["source_id"])

    return {
        "schema": NET_LEDGER_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "rows": rows,
        "provider_requests": sorted(provider_requests.values(), key=lambda item: item["api_request_id"]),
        "task_results": sorted(task_results.values(), key=lambda item: item["task_id"]),
        "unmatched_retrieval_events": unmatched_retrievals,
        "unmatched_overhead_events": unmatched_overheads,
        "authorities": {
            "middleware": "exact_chars_plus_labelled_estimate",
            "provider_usage_cache": "provider_reported_non_additive",
            "billing": "separate_when_available",
        },
        "summary": {
            "logical_sources": len(rows),
            "gross_est_tokens_saved": sum(item["gross_est_tokens_saved"] for item in rows),
            "retrieval_reintroduced_est_tokens": sum(item["retrieval_reintroduced_est_tokens"] for item in rows),
            "net_est_tokens_saved": sum(item["net_est_tokens_saved"] for item in rows),
            "provider_request_count": len(provider_requests),
            "provider_prompt_or_input_tokens": sum(item["prompt_or_input_tokens"] for item in provider_requests.values()),
            "provider_cache_read_tokens": sum(item["cache_read_tokens"] for item in provider_requests.values()),
            "provider_cache_write_tokens": sum(item["cache_write_tokens"] for item in provider_requests.values()),
        },
    }
