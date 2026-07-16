"""Provider-backed reduction orchestration; no Hermes transport routing."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from .config import auto_compression_enabled, load_context_reduction_config
from .observability import ATTRIBUTION_SCHEMA_VERSION, TOKEN_ESTIMATOR, _emit_headroom_event, _report_dir, _rough_tokens_from_chars, _safe_event_text, _utc_stamp
from .policy import MIN_TOOL_RESULT_CHARS, READ_ONLY_MCP_HINTS, _build_exact_header_data, _build_trace, _compressed_excerpt, _contains_protected_control, _edge_excerpt, _exact_or_blocked_reason, _extract_markers, _format_exact_header, _lane_eligible, _redact_text, _safe_header_value, _safe_name, _shorten
from .provider_headroom import HeadroomReductionProvider

BELOW_MIN_AGGREGATE_CHARS = 28_000
BELOW_MIN_AGGREGATE_MAX_CHUNKS = 24
BELOW_MIN_AGGREGATE_MAX_BUFFER_KEYS = 128
_BELOW_MIN_AGGREGATE_BUFFERS: dict[str, dict[str, Any]] = {}


def _provider_ready(proxy_url: str | None = None) -> dict[str, Any]:
    health = HeadroomReductionProvider(proxy_url=proxy_url).ready()
    return {
        "ok": health.ready,
        "status": health.status,
        "body": health.detail,
        "proxy_url": health.endpoint or proxy_url,
    }


def _provider_compress(messages: list[dict[str, Any]], proxy_url: str | None = None) -> dict[str, Any]:
    result = HeadroomReductionProvider(proxy_url=proxy_url).compress(messages)
    payload: dict[str, Any] = {
        "ok": result.ok,
        "success": result.ok,
        "messages": result.value if result.ok else None,
        "markers": [result.marker] if result.marker else [],
        "error": result.error,
    }
    payload.update(result.metrics)
    return payload


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _below_min_aggregate_enabled() -> bool:
    if _truthy(os.environ.get("HEADROOM_EXPERIMENTAL_BELOW_MIN_AGGREGATE")):
        return True
    try:
        cfg = load_context_reduction_config()
    except Exception:
        cfg = {}
    return _truthy(cfg.get("experimental_below_min_terminal_aggregate"))


def _below_min_aggregate_key(*, session_id: str, turn_id: str, task_id: str, api_request_id: str) -> str:
    for value in (turn_id, api_request_id, task_id, session_id):
        safe = _safe_event_text(value, limit=120)
        if safe:
            return safe
    return "global"


def _prune_below_min_buffers() -> None:
    if len(_BELOW_MIN_AGGREGATE_BUFFERS) <= BELOW_MIN_AGGREGATE_MAX_BUFFER_KEYS:
        return
    for key in list(_BELOW_MIN_AGGREGATE_BUFFERS)[: len(_BELOW_MIN_AGGREGATE_BUFFERS) - BELOW_MIN_AGGREGATE_MAX_BUFFER_KEYS]:
        _BELOW_MIN_AGGREGATE_BUFFERS.pop(key, None)


def _maybe_compress_terminal_below_min_aggregate(
    *,
    tool_name: str,
    args: dict[str, Any],
    result: str,
    health: dict[str, Any],
    task_id: str = "",
    tool_call_id: str = "",
    session_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    platform: str = "",
    duration_ms: Any = None,
) -> str | None:
    """Experimental local-only prototype for repeated terminal below-min chunks.

    Default-off. When explicitly enabled, buffer small terminal chunks within a
    turn/session key and emit at most one aggregate marker when cumulative size
    is material. Exact/protected gates run before this helper, so exact commands
    and sensitive material never enter this path.
    """
    if tool_name != "terminal" or not _below_min_aggregate_enabled():
        return None
    key = _below_min_aggregate_key(session_id=session_id, turn_id=turn_id, task_id=task_id, api_request_id=api_request_id)
    redacted = _redact_text(result)
    buffer = _BELOW_MIN_AGGREGATE_BUFFERS.setdefault(
        key,
        {
            "chunks": [],
            "chars": 0,
            "first_task_id": task_id,
            "first_tool_call_id": tool_call_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "api_request_id": api_request_id,
            "platform": platform,
        },
    )
    chunks = buffer.setdefault("chunks", [])
    chunks.append(redacted)
    if len(chunks) > BELOW_MIN_AGGREGATE_MAX_CHUNKS:
        chunks.pop(0)
    buffer["chars"] = sum(len(chunk) for chunk in chunks)
    _prune_below_min_buffers()
    if len(chunks) < 2 or int(buffer.get("chars") or 0) < BELOW_MIN_AGGREGATE_CHARS:
        return None

    report_dir = _report_dir()
    stamp = _utc_stamp()
    safe_tool = _safe_name(tool_name)
    source_path = report_dir / f"auto-tool-{stamp}-{safe_tool}-below-min-aggregate.redacted.log"
    compressed_path = report_dir / f"auto-tool-{stamp}-{safe_tool}-below-min-aggregate.compressed.json"
    report_path = report_dir / f"auto-tool-{stamp}-{safe_tool}-below-min-aggregate.json"
    aggregate_body = "\n\n".join(
        [
            "===== BOUNDED TERMINAL CHUNKS =====",
            f"chunk_count={len(chunks)}",
            f"aggregate_chars={buffer['chars']}",
            "===== CHUNKS =====",
        ]
        + [f"----- chunk {idx + 1}/{len(chunks)} -----\n{chunk}" for idx, chunk in enumerate(chunks)]
    )
    source_path.write_text(aggregate_body, encoding="utf-8")
    trace = _build_trace(tool_name, args, aggregate_body, task_id=task_id, duration_ms=duration_ms)
    messages = [
        {"role": "system", "content": "Headroom intermediate tool-result compression: terminal below-min aggregate."},
        {"role": "user", "content": "Compress this bounded aggregate of repeated below-min terminal chunks. Preserve errors, warnings, paths, counts, status, and gate fields. Exact raw aggregate sidecar is retained."},
        {"role": "tool", "tool_call_id": _safe_name(tool_call_id or tool_name), "name": "worker_trace", "content": trace},
    ]
    compression_started = time.perf_counter()
    compressed = _provider_compress(messages, proxy_url=health.get("proxy_url"))
    compression_latency_ms = round((time.perf_counter() - compression_started) * 1000, 3)
    compressed_path.write_text(json.dumps(compressed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markers = _extract_markers(compressed.get("messages")) if compressed.get("ok") else []
    marker = markers[0] if markers else None
    before = compressed.get("tokens_before")
    after = compressed.get("tokens_after")
    saved = compressed.get("tokens_saved")
    useful = bool(marker) and isinstance(saved, int) and saved > 500 and isinstance(after, int) and isinstance(before, int) and after < before
    report = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "kind": "auto-tool-result-below-min-aggregate",
        "tool_name": tool_name,
        "task_id": task_id,
        "tool_call_id": tool_call_id,
        "aggregate_key": key,
        "chunk_count": len(chunks),
        "aggregate_chars": buffer.get("chars"),
        "source_path": str(source_path),
        "compressed_path": str(compressed_path),
        "marker": marker,
        "marker_count": len(markers),
        "tokens_before": before,
        "tokens_after": after,
        "tokens_saved": saved,
        "service_metric_scope": "headroom_internal_messages",
        "attribution_schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "compression_latency_ms": compression_latency_ms,
        "compression_ratio": compressed.get("compression_ratio"),
        "compression_input_shape": "terminal_below_min_per_turn_aggregate",
        "policy_mutation": False,
        "global_threshold_change": False,
        "exact_commands_relaxed": False,
        "useful": useful,
        "source_retention": "redacted_aggregate_sidecar",
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not useful:
        return None

    _BELOW_MIN_AGGREGATE_BUFFERS.pop(key, None)
    payload = (
        f"[Headroom auto-compressed below-min terminal aggregate · chunks={len(chunks)} aggregate_chars={buffer.get('chars')} "
        f"tokens_before={before} tokens_after={after} saved={saved} marker={marker}]\n"
        "[Headroom compressed intermediate]\n"
        "classification: diagnostic_trace\n"
        "surface: tool_result_below_min_aggregate\n"
        "tool_or_lane: terminal\n"
        "action: aggregate_below_min_chunks\n"
        "source_retention:\n"
        f"  report: {report_path}\n"
        "  sidecar_type: redacted_aggregate_sidecar\n"
        f"  source_path: {source_path}\n"
        f"  marker: {marker}\n"
        "contract: compressed body is intermediate only; verify material claims against exact source/authorized retrieval before final decisions.\n"
        f"Use headroom_retrieve(hash='{marker}') for the complete exact retained payload."
    )
    final_payload = _shorten(payload)
    aggregate_chars = int(buffer.get("chars") or 0)
    report.update(
        {
            "model_facing_chars_before": aggregate_chars,
            "model_facing_chars_after": len(final_payload),
            "model_facing_est_tokens_before": _rough_tokens_from_chars(aggregate_chars),
            "model_facing_est_tokens_after": _rough_tokens_from_chars(len(final_payload)),
            "model_facing_est_tokens_saved": max(
                0,
                _rough_tokens_from_chars(aggregate_chars) - _rough_tokens_from_chars(len(final_payload)),
            ),
            "model_facing_token_estimator": TOKEN_ESTIMATOR,
            "measurement_scope": "experimental_aggregate_not_request_delta",
            "new_savings_event": False,
        }
    )
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _emit_headroom_event(
        action="compressed",
        tool_name=tool_name,
        args=args,
        reason="below_min_aggregate",
        task_id=task_id,
        tool_call_id=tool_call_id,
        session_id=session_id,
        turn_id=turn_id,
        api_request_id=api_request_id,
        platform=platform,
        data_class="diagnostic_trace",
        original_chars=aggregate_chars,
        redacted_chars=aggregate_chars,
        tokens_before=before,
        tokens_after=after,
        tokens_saved=saved,
        model_facing_chars_before=aggregate_chars,
        model_facing_chars_after=len(final_payload),
        measurement_scope="experimental_aggregate_not_request_delta",
        compression_latency_ms=compression_latency_ms,
        new_savings_event=False,
        marker=marker,
        report_path=report_path,
        source_path=source_path,
        compressed_path=compressed_path,
        exact_authority="redacted_aggregate_sidecar",
    )
    return final_payload


def _compression_body_for_tool_result(tool_name: str, redacted_result: str) -> tuple[str, str]:
    """Return the payload shape to send to Headroom for compression.

    Hermes terminal results can arrive as a JSON object string such as
    ``{"output": "...large log...", "exit_code": 0}``. Sending that
    escaped JSON wrapper to Headroom can miss the runtime's log router and
    yield ``compression_not_useful`` even though the raw log is compressible.
    Keep the full redacted result as the exact sidecar authority, but feed the
    bulky output field to the runtime when this safe terminal shape is present.
    """
    if tool_name != "terminal" or not isinstance(redacted_result, str):
        return redacted_result, "original"
    stripped = redacted_result.strip()
    if not stripped.startswith("{"):
        return redacted_result, "original"
    try:
        data = json.loads(stripped)
    except Exception:
        return redacted_result, "original"
    if not isinstance(data, dict):
        return redacted_result, "original"
    output = data.get("output")
    if not isinstance(output, str) or len(output) < MIN_TOOL_RESULT_CHARS:
        return redacted_result, "original"
    metadata: list[str] = []
    if "exit_code" in data:
        metadata.append(f"exit_code={data.get('exit_code')}")
    if data.get("error"):
        metadata.append(f"error={data.get('error')}")
    prefix = ""
    if metadata:
        prefix = "[terminal result metadata: " + ", ".join(_safe_header_value(item) for item in metadata) + "]\n"
    return prefix + output, "terminal_json_output_field"


def _compression_proxy_tool_name(tool_name: str) -> str:
    """Expose semantic read-tool identity to Headroom's ContentRouter."""
    tool = str(tool_name or "").lower()
    if tool in {"read_file", "search_files", "skill_view", "fact_store", "web_search", "web_extract"}:
        return _safe_name(tool)
    if tool.startswith(("mcp__", "mcp_")) and any(hint in tool for hint in READ_ONLY_MCP_HINTS):
        return _safe_name(tool)
    return "worker_trace"


def compress_tool_result_for_context(
    *,
    tool_name: str,
    args: dict[str, Any],
    result: str,
    task_id: str = "",
    tool_call_id: str = "",
    session_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    platform: str = "",
    duration_ms: Any = None,
    structured_result: dict[str, Any] | None = None,
    structured_field_key: str = "",
    event_surface: str = "tool_execution",
    measurement_scope_override: str = "",
    allow_below_min_aggregate: bool = True,
    logical_source_id: str = "",
) -> str | None:
    """Return a compressed replacement for an eligible tool result, else None."""
    event_measurement_scope = measurement_scope_override or "tool_result"
    if not isinstance(result, str) or not result:
        return None
    if not auto_compression_enabled():
        _emit_headroom_event(
            action="skipped",
            tool_name=tool_name,
            args=args,
            reason="auto_compression_disabled",
            task_id=task_id,
            tool_call_id=tool_call_id,
            session_id=session_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            platform=platform,
            surface=event_surface,
            original_chars=len(result),
            measurement_scope=event_measurement_scope,
            exact_authority="original_tool_result",
        )
        return None
    health = _provider_ready()
    if not health.get("ok"):
        _emit_headroom_event(
            action="runtime_unavailable",
            tool_name=tool_name,
            args=args,
            reason="proxy_not_ready",
            task_id=task_id,
            tool_call_id=tool_call_id,
            session_id=session_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            platform=platform,
            surface=event_surface,
            original_chars=len(result),
            measurement_scope=event_measurement_scope,
            error=health.get("body") or health.get("error"),
        )
        return None
    if _contains_protected_control(tool_name, args, result):
        _emit_headroom_event(
            action="blocked",
            tool_name=tool_name,
            args=args,
            reason="protected_control_or_sensitive_material",
            task_id=task_id,
            tool_call_id=tool_call_id,
            session_id=session_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            platform=platform,
            surface=event_surface,
            original_chars=len(result),
            measurement_scope=event_measurement_scope,
            exact_authority="original_tool_result",
        )
        return None
    exact_reason = _exact_or_blocked_reason(tool_name, args, result)
    if exact_reason:
        _emit_headroom_event(
            action="exact",
            tool_name=tool_name,
            args=args,
            reason=exact_reason,
            task_id=task_id,
            tool_call_id=tool_call_id,
            session_id=session_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            platform=platform,
            surface=event_surface,
            original_chars=len(result),
            measurement_scope=event_measurement_scope,
            exact_authority="original_tool_result",
        )
        return None
    eligible, reason = _lane_eligible(tool_name, args, result, min_chars=MIN_TOOL_RESULT_CHARS)
    if not eligible:
        if allow_below_min_aggregate and reason == "below_min_chars":
            aggregate = _maybe_compress_terminal_below_min_aggregate(
                tool_name=tool_name,
                args=args,
                result=result,
                health=health,
                task_id=task_id,
                tool_call_id=tool_call_id,
                session_id=session_id,
                turn_id=turn_id,
                api_request_id=api_request_id,
                platform=platform,
                duration_ms=duration_ms,
            )
            if aggregate:
                return aggregate
        _emit_headroom_event(
            action="skipped",
            tool_name=tool_name,
            args=args,
            reason=reason,
            task_id=task_id,
            tool_call_id=tool_call_id,
            session_id=session_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            platform=platform,
            surface=event_surface,
            original_chars=len(result),
            measurement_scope=event_measurement_scope,
            exact_authority="original_tool_result",
        )
        return None

    redacted = _redact_text(result)
    compression_body, compression_input_shape = _compression_body_for_tool_result(tool_name, redacted)
    header_data = _build_exact_header_data(tool_name, args, compression_body, reason)
    if not header_data.get("header_ok"):
        _emit_headroom_event(
            action="blocked",
            tool_name=tool_name,
            args=args,
            reason="header_missing:" + ",".join(str(x) for x in (header_data.get("missing") or [])),
            task_id=task_id,
            tool_call_id=tool_call_id,
            session_id=session_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            platform=platform,
            surface=event_surface,
            data_class=header_data.get("data_class"),
            original_chars=len(result),
            redacted_chars=len(redacted),
            measurement_scope=event_measurement_scope,
            exact_authority="original_tool_result",
        )
        return None

    report_dir = _report_dir()
    stamp = _utc_stamp()
    safe_tool = _safe_name(tool_name)
    source_path = report_dir / f"auto-tool-{stamp}-{safe_tool}.redacted.log"
    source_path.write_text(redacted, encoding="utf-8")

    trace = (
        compression_body
        if header_data.get("data_class") == "source_readback"
        else _build_trace(tool_name, args, compression_body, task_id=task_id, duration_ms=duration_ms)
    )
    messages = [
        {"role": "system", "content": f"Headroom intermediate tool-result compression: {tool_name}."},
        {"role": "user", "content": f"Compress only the bulky body of this intermediate Hermes lane/tool result. Eligibility: {reason}. A deterministic exact header has already been extracted and will remain visible; do not invent identifiers or citations. Preserve errors, warnings, decisions, paths, counts, changed files, verification status, and final status indicators in the compressed body when useful."},
        {"role": "tool", "tool_call_id": _safe_name(tool_call_id or tool_name), "name": _compression_proxy_tool_name(tool_name), "content": trace},
    ]
    compression_started = time.perf_counter()
    compressed = _provider_compress(messages)
    compression_latency_ms = round((time.perf_counter() - compression_started) * 1000, 3)
    if not compressed.get("ok"):
        _emit_headroom_event(
            action="error",
            tool_name=tool_name,
            args=args,
            reason="compress_failed",
            task_id=task_id,
            tool_call_id=tool_call_id,
            session_id=session_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            platform=platform,
            surface=event_surface,
            data_class=header_data.get("data_class"),
            original_chars=len(result),
            redacted_chars=len(redacted),
            measurement_scope=event_measurement_scope,
            exact_authority="redacted_sidecar",
            source_path=source_path,
            compression_latency_ms=compression_latency_ms,
            error=compressed.get("error"),
        )
        return None

    markers = _extract_markers(compressed.get("messages"))
    marker = markers[0] if markers else None
    compressed_path = report_dir / f"auto-tool-{stamp}-{safe_tool}.compressed.json"
    compressed_path.write_text(json.dumps(compressed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    before = compressed.get("tokens_before")
    after = compressed.get("tokens_after")
    saved = compressed.get("tokens_saved")
    report_path = report_dir / f"auto-tool-{stamp}-{safe_tool}.json"
    report = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "kind": "auto-tool-result",
        "surface": event_surface,
        "tool_name": tool_name,
        "task_id": task_id,
        "tool_call_id": tool_call_id,
        "eligibility_reason": reason,
        "data_class": header_data.get("data_class"),
        "header_action": header_data.get("action"),
        "header_required": header_data.get("header_required"),
        "exact_header": {
            "identifiers": header_data.get("identifiers"),
            "status": header_data.get("status"),
            "anchors": header_data.get("anchors"),
            "urls": header_data.get("urls"),
            "errors": header_data.get("errors"),
        },
        "source_path": str(source_path),
        "compressed_path": str(compressed_path),
        "marker": marker,
        "marker_count": len(markers),
        "original_chars": len(result),
        "redacted_chars": len(redacted),
        "compression_input_shape": compression_input_shape,
        "compression_input_chars": len(compression_body),
        "tokens_before": before,
        "tokens_after": after,
        "tokens_saved": saved,
        "service_metric_scope": "headroom_internal_messages",
        "attribution_schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "compression_latency_ms": compression_latency_ms,
        "compression_ratio": compressed.get("compression_ratio"),
        "source_retention": "redacted_sidecar",
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    useful = bool(marker) or (isinstance(saved, int) and saved > 500 and isinstance(after, int) and isinstance(before, int) and after < before)
    if not useful:
        _emit_headroom_event(
            action="skipped",
            tool_name=tool_name,
            args=args,
            reason="compression_not_useful",
            task_id=task_id,
            tool_call_id=tool_call_id,
            session_id=session_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            platform=platform,
            surface=event_surface,
            data_class=header_data.get("data_class"),
            original_chars=len(result),
            redacted_chars=len(redacted),
            tokens_before=before,
            tokens_after=after,
            tokens_saved=saved,
            marker=marker,
            report_path=report_path,
            source_path=source_path,
            compressed_path=compressed_path,
            exact_authority="redacted_sidecar",
            compression_latency_ms=compression_latency_ms,
            measurement_scope=event_measurement_scope,
        )
        return None

    exact_header = _format_exact_header(
        header_data,
        tool_name=tool_name,
        eligibility_reason=reason,
        report_path=report_path,
        source_path=source_path,
        marker=marker,
    )
    if marker:
        payload = (
            f"[Headroom auto-compressed tool result · tool={tool_name} original_chars={len(result)} "
            f"tokens_before={before} tokens_after={after} saved={saved} marker={marker}]\n"
            f"{exact_header}\n"
            f"Use headroom_retrieve(hash='{marker}') for the complete exact retained payload."
        )
    else:
        payload = (
            f"[Headroom auto-compressed tool result · tool={tool_name} original_chars={len(result)} "
            f"tokens_before={before} tokens_after={after} saved={saved} direct_compression=true]\n"
            f"{exact_header}\n\n"
            f"Compressed payload: {compressed_path}\n\n"
            f"Compressed excerpt:\n{_compressed_excerpt(compressed)}\n\n"
            f"Raw edge excerpt:\n{_edge_excerpt(redacted)}"
        )
    final_payload = _shorten(payload)

    measurement_scope = event_measurement_scope
    model_before_chars = len(result)
    model_after_chars = len(final_payload)
    if isinstance(structured_result, dict) and structured_field_key:
        before_structured = json.dumps(structured_result, ensure_ascii=False, default=str)
        after_structured = dict(structured_result)
        after_structured[structured_field_key] = final_payload
        after_structured.setdefault("headroom_auto_compressed", True)
        after_structured.setdefault("headroom_compressed_field", structured_field_key)
        after_serialized = json.dumps(after_structured, ensure_ascii=False, default=str)
        model_before_chars = len(before_structured)
        model_after_chars = len(after_serialized)
        measurement_scope = f"structured_tool_result:{structured_field_key}"

    report.update(
        {
            "model_facing_chars_before": model_before_chars,
            "model_facing_chars_after": model_after_chars,
            "model_facing_est_tokens_before": _rough_tokens_from_chars(model_before_chars),
            "model_facing_est_tokens_after": _rough_tokens_from_chars(model_after_chars),
            "model_facing_est_tokens_saved": max(
                0,
                _rough_tokens_from_chars(model_before_chars) - _rough_tokens_from_chars(model_after_chars),
            ),
            "model_facing_token_estimator": TOKEN_ESTIMATOR,
            "measurement_scope": measurement_scope,
        }
    )
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    _emit_headroom_event(
        action="compressed",
        tool_name=tool_name,
        args=args,
        reason=reason,
        task_id=task_id,
        tool_call_id=tool_call_id,
        session_id=session_id,
        turn_id=turn_id,
        api_request_id=api_request_id,
        platform=platform,
        surface=event_surface,
        data_class=header_data.get("data_class"),
        original_chars=len(result),
        redacted_chars=len(redacted),
        tokens_before=before,
        tokens_after=after,
        tokens_saved=saved,
        model_facing_chars_before=model_before_chars,
        model_facing_chars_after=model_after_chars,
        measurement_scope=measurement_scope,
        compression_latency_ms=compression_latency_ms,
        marker=marker,
        report_path=report_path,
        source_path=source_path,
        compressed_path=compressed_path,
        exact_authority="redacted_sidecar",
        logical_source_id=logical_source_id,
    )
    return final_payload


def _compress_structured_result_for_context(
    *,
    tool_name: str,
    args: dict[str, Any],
    result: Any,
    task_id: str = "",
    tool_call_id: str = "",
    session_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    platform: str = "",
    duration_ms: Any = None,
) -> Any | None:
    """Compress bulky string fields embedded in structured tool results.

    Some Hermes tools, notably ``execute_code``, return dictionaries such as
    ``{"status": "success", "output": "..."}`` instead of a bare string. Keep
    the structured metadata exact and replace only the bulky intermediate text.
    """
    if not isinstance(result, dict):
        return None
    for key in ("output", "content", "result", "text"):
        value = result.get(key)
        if not isinstance(value, str) or not value:
            continue
        transformed = compress_tool_result_for_context(
            tool_name=tool_name,
            args=args,
            result=value,
            task_id=task_id,
            tool_call_id=tool_call_id,
            session_id=session_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            platform=platform,
            duration_ms=duration_ms,
            structured_result=result,
            structured_field_key=key,
        )
        if transformed:
            out = dict(result)
            out[key] = transformed
            out.setdefault("headroom_auto_compressed", True)
            out.setdefault("headroom_compressed_field", key)
            return out
    return None
