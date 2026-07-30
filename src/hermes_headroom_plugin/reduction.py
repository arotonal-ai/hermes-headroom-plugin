"""Provider-backed reduction orchestration; no Hermes transport routing."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from .config import resolve_effective_config
from .local_exact_store import retain_local_source
from .observability import ATTRIBUTION_SCHEMA_VERSION, TOKEN_ESTIMATOR, _emit_headroom_event, _report_dir, _rough_tokens_from_chars, _safe_event_text, _utc_stamp
from .policy import READ_ONLY_MCP_HINTS, _build_exact_header_data, _build_trace, _compressed_excerpt, _contains_protected_control, _edge_excerpt, _extract_markers, _format_exact_header, _lane_eligible, _redact_text, _safe_header_value, _safe_name, _shorten, semantic_admission
from .provider_headroom import HeadroomReductionProvider

BELOW_MIN_AGGREGATE_CHARS = 28_000
BELOW_MIN_AGGREGATE_MAX_CHUNKS = 24
BELOW_MIN_AGGREGATE_MAX_BUFFER_KEYS = 128
_BELOW_MIN_AGGREGATE_BUFFERS: dict[str, dict[str, Any]] = {}

NEGATIVE_OUTCOME_TTL_SECONDS = 300.0
_NEGATIVE_OUTCOME_CACHE: OrderedDict[str, float] = OrderedDict()
_NEGATIVE_OUTCOME_CACHE_LOCK = threading.RLock()


def _negative_outcome_key(
    *,
    tool_name: str,
    result: str,
    task_id: str,
    tool_call_id: str,
    session_id: str,
) -> str:
    """Identify one logical source across tool-execution and request surfaces."""
    identity = {
        "schema": "headroom.negative_outcome.v1",
        "session_id": str(session_id or ""),
        # Tool-call identity is stable across tool_execution and llm_request.
        # task_id is only a fallback because request adapters may rebind task
        # context while replaying the same canonical output.
        "task_fallback": str(task_id or "") if not session_id and not tool_call_id else "",
        "tool_call_id": str(tool_call_id or ""),
        "tool_name": str(tool_name or ""),
        "content_sha256": hashlib.sha256(result.encode("utf-8", errors="replace")).hexdigest(),
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _negative_outcome_cache_hit(key: str, *, now: float | None = None) -> bool:
    """Return true for a live compression-not-useful outcome without side effects."""
    observed = time.monotonic() if now is None else now
    with _NEGATIVE_OUTCOME_CACHE_LOCK:
        expires_at = _NEGATIVE_OUTCOME_CACHE.get(key)
        if expires_at is None:
            return False
        if expires_at <= observed:
            _NEGATIVE_OUTCOME_CACHE.pop(key, None)
            return False
        _NEGATIVE_OUTCOME_CACHE.move_to_end(key)
        return True


def _negative_outcome_cache_put(key: str, *, now: float | None = None) -> None:
    """Bound transient negative outcomes; provider failures remain retryable."""
    observed = time.monotonic() if now is None else now
    with _NEGATIVE_OUTCOME_CACHE_LOCK:
        for stale_key, expires_at in list(_NEGATIVE_OUTCOME_CACHE.items()):
            if expires_at <= observed:
                _NEGATIVE_OUTCOME_CACHE.pop(stale_key, None)
        _NEGATIVE_OUTCOME_CACHE[key] = observed + NEGATIVE_OUTCOME_TTL_SECONDS
        _NEGATIVE_OUTCOME_CACHE.move_to_end(key)
        cache_max = resolve_effective_config().llm_request_cache_max
        while len(_NEGATIVE_OUTCOME_CACHE) > cache_max:
            _NEGATIVE_OUTCOME_CACHE.popitem(last=False)


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
    markers = list(result.markers)
    for marker in _extract_markers(result.value):
        if marker not in markers:
            markers.append(marker)
    payload: dict[str, Any] = {
        "ok": result.ok,
        "success": result.ok,
        "messages": result.value if result.ok else None,
        "markers": markers,
        "error": result.error,
    }
    payload.update(result.metrics)
    return payload


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _below_min_aggregate_enabled() -> bool:
    return resolve_effective_config().experimental_below_min_terminal_aggregate


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
    marker = markers[0] if len(markers) == 1 else None
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


def _compression_body_for_tool_result(
    tool_name: str,
    redacted_result: str,
    *,
    min_tool_result_chars: int,
) -> tuple[str, str]:
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
    if not isinstance(output, str) or len(output) < min_tool_result_chars:
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
    policy_age: str = "hot",
) -> str | None:
    """Return a compressed replacement for an eligible tool result, else None."""
    event_measurement_scope = measurement_scope_override or "tool_result"
    if not isinstance(result, str) or not result:
        return None
    effective_config = resolve_effective_config()
    if not effective_config.auto_compression:
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
    admission = semantic_admission(
        tool_name,
        args,
        result,
        surface=event_surface,
        age=policy_age,
        excluded_tools=effective_config.excluded_tools,
    )
    if not admission.compress:
        _emit_headroom_event(
            action="exact",
            tool_name=tool_name,
            args=args,
            reason=f"semantic_policy:{admission.outcome}:{admission.reason}",
            task_id=task_id,
            tool_call_id=tool_call_id,
            session_id=session_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            platform=platform,
            surface=event_surface,
            data_class=admission.data_class,
            original_chars=len(result),
            measurement_scope=event_measurement_scope,
            exact_authority="original_tool_result",
        )
        return None
    negative_outcome_key = _negative_outcome_key(
        tool_name=tool_name,
        result=result,
        task_id=task_id,
        tool_call_id=tool_call_id,
        session_id=session_id,
    )
    if _negative_outcome_cache_hit(negative_outcome_key):
        # The first attempt already retained a sidecar/report and skipped event.
        # Repeated request-boundary passes must not redo provider work or grow
        # duplicate evidence for the same unchanged logical source.
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
    eligible, reason = _lane_eligible(tool_name, args, result, min_chars=effective_config.min_tool_result_chars)
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
    if redacted != result:
        _emit_headroom_event(
            action="blocked",
            tool_name=tool_name,
            args=args,
            reason="redaction_required_exact_passthrough",
            task_id=task_id,
            tool_call_id=tool_call_id,
            session_id=session_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            platform=platform,
            surface=event_surface,
            data_class=admission.data_class,
            original_chars=len(result),
            measurement_scope=event_measurement_scope,
            exact_authority="original_tool_result",
        )
        return None
    compression_body, compression_input_shape = _compression_body_for_tool_result(
        tool_name,
        redacted,
        min_tool_result_chars=effective_config.min_tool_result_chars,
    )
    header_data = _build_exact_header_data(tool_name, args, compression_body, reason)
    header_data["data_class"] = admission.data_class
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
    source_path = report_dir / f"auto-tool-{stamp}-{safe_tool}.exact.log"
    source_path.write_text(redacted, encoding="utf-8")
    source_path.chmod(0o600)

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

    markers: list[str] = []
    for candidate in [*(compressed.get("markers") or []), *_extract_markers(compressed.get("messages"))]:
        value = str(candidate or "").strip()
        if value and value not in markers:
            markers.append(value)
    marker = markers[0] if len(markers) == 1 else None
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
        "semantic_policy": admission.outcome,
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
        "source_retention": "exact_report_sidecar",
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    useful = (
        bool(marker)
        and isinstance(saved, int)
        and saved > 500
        and isinstance(after, int)
        and isinstance(before, int)
        and after < before
    )
    if not useful:
        skip_reason = (
            "multipart_marker_ambiguous"
            if len(markers) > 1
            else "missing_durable_marker"
            if not marker
            else "compression_not_useful"
        )
        _emit_headroom_event(
            action="skipped",
            tool_name=tool_name,
            args=args,
            reason=skip_reason,
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
            exact_authority="original_tool_result",
            compression_latency_ms=compression_latency_ms,
            measurement_scope=event_measurement_scope,
        )
        _negative_outcome_cache_put(negative_outcome_key)
        return None

    local_source = retain_local_source(
        str(marker),
        result,
        data_class=str(header_data.get("data_class") or ""),
        tool_name=tool_name,
        args=args,
        config=effective_config,
    )
    exact_authority = "local_exact_manifest+ccr_temporal" if local_source.exact else "ccr_temporal"
    source_bytes = len(result.encode("utf-8", errors="strict"))
    source_sha256 = local_source.sha256 or hashlib.sha256(result.encode("utf-8", errors="strict")).hexdigest()
    report["local_exact"] = local_source.as_dict(include_content=False, include_internal=True)
    report["source_sha256"] = source_sha256
    report["source_bytes"] = source_bytes

    exact_header = _format_exact_header(
        header_data,
        tool_name=tool_name,
        eligibility_reason=reason,
        report_path=report_path,
        source_path=source_path,
        marker=marker,
        source_retention_state="local_exact_manifest" if local_source.exact else "exact_report_sidecar",
        source_sha256=source_sha256,
        source_bytes=source_bytes,
        exact_authority=exact_authority,
    )
    payload = (
        f"[Headroom auto-compressed tool result · tool={tool_name} original_chars={len(result)} "
        f"tokens_before={before} tokens_after={after} saved={saved} marker={marker}]\n"
        f"{exact_header}\n"
        f"Use headroom_retrieve(hash='{marker}') for exact readback; authority={exact_authority} local_state={local_source.state}."
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
        exact_authority=exact_authority,
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
