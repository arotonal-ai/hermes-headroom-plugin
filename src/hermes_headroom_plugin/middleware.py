"""Behavior-changing middleware hooks.

The packaged plugin keeps provider/global routing off, but it may compress
eligible bulky intermediate tool/lane results when the local Headroom proxy is
healthy. Exact/edit-critical/sensitive classes fail closed to the original
result.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .proxy import compress_messages, hermes_home, readyz

MIN_TOOL_RESULT_CHARS = 28_000
ALWAYS_TOOL_RESULT_CHARS = 120_000
MAX_RETURN_CHARS = 12_000
RAW_EDGE_CHARS = 1_200

ELIGIBLE_TOOLS = {
    "delegate_task",
    "terminal",
    "execute_code",
    "process",
    "browser_console",
    "browser_snapshot",
    "browser_get_images",
    "web_extract",
    "session_search",
}
ELIGIBLE_PREFIXES = ("browser_",)
EXACT_TOOLS = {
    "read_file",
    "search_files",
    "patch",
    "write_file",
    "skill_manage",
    "headroom_retrieve",
    "memory",
    "fact_store",
    "mcp_open_design_get_file",
    "mcp_open_design_get_artifact",
    "mcp_open_design_write_file",
}
EXACT_COMMAND_HINTS = (
    "git diff",
    "diff ",
    "sha256sum",
    "md5sum",
    "base64",
    "gpg ",
    "openssl ",
)
COMPRESSED_SENTINELS = (
    "Headroom auto-compressed",
    "<<ccr:",
    "hash=",
)
SECRET_PATTERNS = [
    (r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}", r"\1[REDACTED]"),
    (r"(?i)\b((?:[A-Z0-9_]*(?:api[_-]?key|token|secret|password|authorization|client_secret)[A-Z0-9_]*|TOKEN|SECRET|PASSWORD)\s*[:=]\s*)[^\s'\"]{8,}", r"\1[REDACTED]"),
    (r"(?i)((?:OPENAI|ANTHROPIC|GEMINI|GOOGLE|GITHUB|CLOUDFLARE|TELEGRAM|SLACK|DISCORD)[A-Z0-9_]*(?:KEY|TOKEN)\s*=\s*)\S+", r"\1[REDACTED]"),
]
PROTECTED_PATTERNS = [
    r"(?i)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}",
    r"(?i)\b(?:api[_-]?key|token|secret|password|authorization|client_secret)\b\s*[=:]\s*['\"]?[^'\"\s,}]{8,}",
    r"(?i)\b(?:cookie|set-cookie)\b[^\n]{0,200}\b(?:value|session|token|secret|auth)\b[^\n]{0,80}[=:]\s*['\"]?[^'\"\s;,}]{8,}",
    r"(?i)\b(?:Network\.getAllCookies|Storage\.getCookies)\b",
]
SENSITIVE_ARG_KEY_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|client_secret|cookie)")
HEADER_REQUIRED_CLASSES = {
    "browser_debug_trace",
    "interaction_state",
    "research_corpus",
    "orchestration_fanin",
    "multimodal_intermediate_text",
    "long_comments_history",
    "raw_feed_snapshot",
}
KNOWN_DATA_CLASSES = HEADER_REQUIRED_CLASSES | {
    "diagnostic_trace",
    "qa_trace",
    "worker_trace_raw",
}


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _report_dir() -> Path:
    path = hermes_home() / "control-plane" / "headroom" / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_name(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", raw or "tool")[:60] or "tool"


def _redact_text(text: str) -> str:
    out = text
    for pattern, repl in SECRET_PATTERNS:
        out = re.sub(pattern, repl, out)
    return out


def _args_preview(args: dict[str, Any]) -> str:
    args_preview = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    args_preview = _redact_text(args_preview)
    if len(args_preview) > 5_000:
        args_preview = args_preview[:5_000] + " ...[args truncated in trace header]"
    return args_preview


def _args_contain_sensitive_value(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if SENSITIVE_ARG_KEY_RE.search(str(key)) and len(str(nested)) >= 8:
                return True
            if _args_contain_sensitive_value(nested):
                return True
    elif isinstance(value, (list, tuple, set)):
        return any(_args_contain_sensitive_value(item) for item in value)
    return False


def _contains_protected_control(tool_name: str, args: dict[str, Any], result: str) -> bool:
    """Return True when Headroom must not create sidecars or proxy calls.

    The host tool still returns its original result; this gate only prevents the
    Headroom plugin from persisting or sending protected/control payloads. Scan
    the complete already-materialized result before any sidecar/proxy call; a
    prefix-only scan can leak late cookies/keys/control blobs in large outputs.
    """
    if _args_contain_sensitive_value(args):
        return True
    try:
        args_text = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        args_text = str(args)

    metadata = f"===== TOOL =====\n{tool_name}\n===== ARGS =====\n{args_text}"
    return any(
        re.search(pattern, metadata) or re.search(pattern, result)
        for pattern in PROTECTED_PATTERNS
    )


def _extract_markers(messages: Any) -> list[str]:
    text = json.dumps(messages, ensure_ascii=False)
    markers: list[str] = []
    markers.extend(m.split()[0] for m in re.findall(r"<<ccr:([^,>]+)", text))
    markers.extend(m.split()[0] for m in re.findall(r"hash=([A-Za-z0-9_-]{8,})", text))
    markers.extend(m.split()[0] for m in re.findall(r"marker=([A-Za-z0-9_-]{8,})", text))
    seen: set[str] = set()
    out: list[str] = []
    for marker in markers:
        marker = marker.strip().strip(".,;:)]}\"")
        if marker and marker not in seen:
            seen.add(marker)
            out.append(marker)
    return out


def _shorten(text: str, limit: int = MAX_RETURN_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 32].rstrip() + "\n…[truncated by Headroom plugin]"


def _edge_excerpt(text: str) -> str:
    if len(text) <= (RAW_EDGE_CHARS * 2 + 200):
        return text
    omitted = len(text) - (RAW_EDGE_CHARS * 2)
    return text[:RAW_EDGE_CHARS] + f"\n\n... [raw middle omitted: {omitted} chars] ...\n\n" + text[-RAW_EDGE_CHARS:]


def _compressed_excerpt(data: dict[str, Any]) -> str:
    chunks: list[str] = []
    messages = data.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                chunks.append(content.strip())
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        chunks.append(part["text"].strip())
    return _shorten("\n\n".join(chunks), 1_500) if chunks else "compressed payload did not expose textual excerpt"


def _already_compressed(result: str) -> bool:
    head = result[:2_000]
    return any(sentinel in head for sentinel in COMPRESSED_SENTINELS)


def _exact_or_blocked_reason(tool_name: str, args: dict[str, Any], result: str) -> str | None:
    if tool_name in EXACT_TOOLS:
        return f"exact_tool:{tool_name}"
    if tool_name == "browser_vision":
        vision_hint = " ".join(str(args.get(k) or "") for k in ("lane", "goal", "context", "data_class"))[:2_000].lower()
        if not any(h in vision_hint for h in ("intermediate", "debug", "ocr", "diagnostic", "qa")):
            return "browser_vision_final_default_exact"
    if _already_compressed(result):
        return "already_compressed"
    lowered = result.lower()
    if "-----begin " in lowered and "private key" in lowered:
        return "secret_material"
    if "*** begin patch" in lowered or "*** end patch" in lowered:
        return "patch_diff"
    if "# worker final packet" in lowered or "claim_ledger" in lowered:
        return "final_or_claim_ledger"
    if tool_name == "terminal":
        cmd = str(args.get("command") or "").lower()
        if any(hint in cmd for hint in EXACT_COMMAND_HINTS):
            return "exact_command"
    return None


def _lane_eligible(tool_name: str, args: dict[str, Any], result: str) -> tuple[bool, str]:
    if len(result) >= ALWAYS_TOOL_RESULT_CHARS:
        return True, "always_chars"
    if len(result) < MIN_TOOL_RESULT_CHARS:
        return False, "below_min_chars"
    if tool_name in ELIGIBLE_TOOLS or any(tool_name.startswith(prefix) for prefix in ELIGIBLE_PREFIXES):
        return True, f"eligible_tool:{tool_name}"
    task_hint = " ".join(str(args.get(k) or "") for k in ("lane", "goal", "context"))[:2_000].lower()
    if any(h in task_hint for h in ("delegate", "subagent", "worker", "kanban", "background", "debug", "research", "qa", "diagnostic")):
        return True, "lane_hint"
    return False, "not_intermediate_lane"


def _scan_text(tool_name: str, args: dict[str, Any], result: str) -> str:
    try:
        args_text = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        args_text = str(args)
    return _redact_text(f"tool={tool_name}\nargs={args_text}\nresult={result[:160_000]}")


def _normalize_data_class(value: Any) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    if normalized in KNOWN_DATA_CLASSES:
        return normalized
    aliases = {
        "diagnostic": "diagnostic_trace",
        "debug": "diagnostic_trace",
        "qa": "qa_trace",
        "worker": "worker_trace_raw",
        "worker_trace": "worker_trace_raw",
        "browser_debug": "browser_debug_trace",
        "interaction": "interaction_state",
        "research": "research_corpus",
        "orchestration": "orchestration_fanin",
        "fanin": "orchestration_fanin",
        "fan_in": "orchestration_fanin",
        "multimodal_text": "multimodal_intermediate_text",
    }
    return aliases.get(normalized)


def _detect_data_class(tool_name: str, args: dict[str, Any], result: str, eligibility_reason: str) -> str:
    """Classify an already-eligible bulky intermediate for exact-header policy.

    This deliberately does not expand lane eligibility. It only classifies an
    already-eligible result so header-sensitive classes can either expose
    deterministic fields or fail closed to the original result.
    """
    for key in ("data_class", "headroom_data_class", "classification"):
        data_class = _normalize_data_class(args.get(key))
        if data_class:
            return data_class

    tool = str(tool_name or "").lower()
    scan = _scan_text(tool_name, args, result).lower()

    if tool.startswith("kanban") or re.search(
        r"\b(task[_-]?id|job[_-]?id|run[_-]?id|worker_context|acceptance_criteria|assignee|parents|children|latest_actionable_comment)\b",
        scan,
    ):
        return "orchestration_fanin"
    if tool == "browser_vision":
        return "multimodal_intermediate_text"
    if tool.startswith("browser_") or re.search(
        r"\b(frame[_-]?id|target[_-]?id|session[_-]?id|node[_-]?id|backendnodeid|selector|bounds|coordinates|dom\.|cdp\.|browser)\b",
        scan,
    ):
        return "interaction_state"
    if tool in {"web_extract", "session_search", "x_search"} or re.search(
        r"\b(citations?|inline_citations?|degraded|degraded_reason|document[_-]?id|revision|source_url|url=https?://)",
        scan,
    ):
        return "research_corpus"
    if re.search(r"\b(exit[_-]?code|traceback|assertionerror|pytest|passed|failed|warning|error)\b", scan):
        return "qa_trace" if "pytest" in scan or "passed" in scan or "failed" in scan else "diagnostic_trace"
    if tool in {"delegate_task", "process", "execute_code", "terminal"} or "lane_hint" in eligibility_reason:
        return "worker_trace_raw" if tool == "delegate_task" else "diagnostic_trace"
    return "diagnostic_trace" if eligibility_reason == "always_chars" else "worker_trace_raw"


def _safe_header_value(value: Any, *, limit: int = 220) -> str:
    text = _redact_text(str(value))
    text = re.sub(r"\s+", " ", text).strip().strip("'\"`.,;)")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _dedupe(values: list[str], *, limit: int = 12) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = _safe_header_value(value)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def _extract_labeled_values(text: str, labels: tuple[str, ...], *, limit: int = 12) -> list[str]:
    label_re = "|".join(re.escape(label) for label in labels)
    pattern = re.compile(
        rf"(?i)(?:\b|[\"'])({label_re})(?:\b|[\"'])\s*[:=]\s*(?:[\"']?)([^\s\"',;}}\]]{{1,220}})"
    )
    return _dedupe([f"{m.group(1)}={m.group(2)}" for m in pattern.finditer(text)], limit=limit)


def _extract_matching_lines(text: str, pattern: str, *, limit: int = 6) -> list[str]:
    rx = re.compile(pattern, re.IGNORECASE)
    lines: list[str] = []
    for raw_line in text.splitlines():
        if rx.search(raw_line):
            lines.append(_safe_header_value(raw_line, limit=260))
            if len(lines) >= limit:
                break
    return _dedupe(lines, limit=limit)


def _extract_urls(text: str, *, limit: int = 12) -> list[str]:
    return _dedupe(re.findall(r"https?://[^\s\"'<>),;]+", text), limit=limit)


def _build_exact_header_data(tool_name: str, args: dict[str, Any], result: str, eligibility_reason: str) -> dict[str, Any]:
    data_class = _detect_data_class(tool_name, args, result, eligibility_reason)
    scan = _scan_text(tool_name, args, result)
    lower_tool = str(tool_name or "").lower()

    identifiers = _extract_labeled_values(
        scan,
        (
            "task_id",
            "task-id",
            "job_id",
            "job-id",
            "run_id",
            "run-id",
            "session_id",
            "session-id",
            "frame_id",
            "frame-id",
            "target_id",
            "target-id",
            "node_id",
            "node-id",
            "backendNodeId",
            "comment_id",
            "comment-id",
            "thread_id",
            "thread-id",
            "message_id",
            "message-id",
            "item_id",
            "item-id",
            "feed_id",
            "feed-id",
            "source_id",
            "source-id",
            "post_id",
            "post-id",
            "document_id",
            "document-id",
            "doc_id",
            "doc-id",
        ),
    )
    status = _extract_labeled_values(
        scan,
        (
            "status",
            "state",
            "outcome",
            "exit_code",
            "exit-code",
            "error_code",
            "error-code",
            "degraded",
            "degraded_reason",
            "degraded-reason",
            "timestamp",
            "created_at",
            "created-at",
            "updated_at",
            "updated-at",
        ),
        limit=10,
    )
    anchors = _extract_labeled_values(
        scan,
        (
            "selector",
            "bounds",
            "coordinates",
            "page",
            "line",
            "section",
            "revision",
            "version",
            "range",
            "title",
            "assignee",
            "profile",
            "lane",
            "acceptance",
            "acceptance_criteria",
            "latest_actionable_comment",
            "author",
            "user",
            "cursor",
            "source",
            "source_url",
        ),
        limit=14,
    )
    urls = _extract_urls(scan)
    errors = _extract_matching_lines(scan, r"\b(error|warning|traceback|assertionerror|blocked|fail(?:ed)?)\b", limit=6)

    missing: list[str] = []
    if data_class == "orchestration_fanin":
        if not any(re.search(r"(?i)\b(task|job|run)[_-]?id=", item) for item in identifiers):
            missing.append("task/job/run id")
        if not (status or any(re.search(r"(?i)(acceptance|title|latest_actionable_comment)", item) for item in anchors)):
            missing.append("status/outcome/acceptance/title")
    elif data_class == "research_corpus":
        if not (urls or any(re.search(r"(?i)(citation|document|doc_|doc-|revision|version|degraded)", item) for item in identifiers + status + anchors)):
            missing.append("citation/url/document/degraded anchor")
    elif data_class in {"browser_debug_trace", "interaction_state"}:
        if not (urls or identifiers or any(re.search(r"(?i)(selector|bounds|coordinates|title)", item) for item in anchors)):
            missing.append("url/id/selector/bounds/error")
    elif data_class == "multimodal_intermediate_text":
        image_or_prompt = _extract_labeled_values(scan, ("image_path", "image_url", "image_hash", "question", "prompt"), limit=8)
        anchors.extend(item for item in image_or_prompt if item not in anchors)
        if lower_tool == "browser_vision" and not image_or_prompt:
            missing.append("image/prompt/question pointer")
    elif data_class == "long_comments_history":
        if not (
            identifiers
            or any(re.search(r"(?i)(latest_actionable_comment|author|user)", item) for item in anchors)
        ):
            missing.append("comment/thread/message/action anchor")
    elif data_class == "raw_feed_snapshot":
        if not (
            urls
            or identifiers
            or any(re.search(r"(?i)(cursor|source|source_url)", item) for item in anchors)
        ):
            missing.append("feed/source/item/cursor anchor")

    header_required = data_class in HEADER_REQUIRED_CLASSES
    if header_required and not any((identifiers, status, anchors, urls, errors)):
        missing.append("nonempty exact header")
    return {
        "data_class": data_class,
        "action": "needs_header" if header_required else "compress",
        "header_required": header_required,
        "header_ok": not header_required or not missing,
        "missing": missing,
        "identifiers": identifiers,
        "status": status,
        "anchors": _dedupe(anchors, limit=16),
        "urls": urls,
        "errors": errors,
    }


def _format_exact_header(
    header: dict[str, Any],
    *,
    tool_name: str,
    eligibility_reason: str,
    report_path: Path,
    source_path: Path,
    marker: str | None,
) -> str:
    def section(name: str, values: list[str]) -> list[str]:
        if not values:
            return [f"  {name}: []"]
        return [f"  {name}:"] + [f"    - {value}" for value in values]

    lines = [
        "[Headroom compressed intermediate]",
        f"classification: {header.get('data_class')}",
        "surface: tool_result",
        f"tool_or_lane: {tool_name}",
        f"action: {header.get('action')}",
        f"eligibility: {eligibility_reason}",
        "exact_header:",
    ]
    lines.extend(section("identifiers", list(header.get("identifiers") or [])))
    lines.extend(section("status", list(header.get("status") or [])))
    lines.extend(section("anchors", list(header.get("anchors") or [])))
    lines.extend(section("urls", list(header.get("urls") or [])))
    lines.extend(section("errors", list(header.get("errors") or [])))
    lines.extend(
        [
            "source_retention:",
            f"  report: {report_path}",
            "  sidecar_type: redacted_sidecar",
            f"  source_path: {source_path}",
            f"  marker: {marker or ''}",
            "contract: compressed body is intermediate only; verify material claims against exact source/authorized retrieval before final decisions.",
        ]
    )
    return "\n".join(lines)


def _build_trace(tool_name: str, args: dict[str, Any], result: str, *, task_id: str = "", duration_ms: Any = None) -> str:
    args_preview = _args_preview(args)
    return (
        f"===== TOOL =====\n{tool_name}\n"
        f"===== TASK_ID =====\n{task_id}\n"
        f"===== DURATION_MS =====\n{duration_ms if duration_ms is not None else ''}\n"
        f"===== ARGS PREVIEW =====\n{args_preview}\n"
        "===== TOOL RESULT =====\n"
        f"{result}\n"
    )


def compress_tool_result_for_context(
    *,
    tool_name: str,
    args: dict[str, Any],
    result: str,
    task_id: str = "",
    tool_call_id: str = "",
    duration_ms: Any = None,
) -> str | None:
    """Return a compressed replacement for an eligible tool result, else None."""
    if not isinstance(result, str) or not result:
        return None
    if not readyz().get("ok"):
        return None
    if _contains_protected_control(tool_name, args, result):
        return None
    exact_reason = _exact_or_blocked_reason(tool_name, args, result)
    if exact_reason:
        return None
    eligible, reason = _lane_eligible(tool_name, args, result)
    if not eligible:
        return None

    redacted = _redact_text(result)
    header_data = _build_exact_header_data(tool_name, args, redacted, reason)
    if not header_data.get("header_ok"):
        return None

    report_dir = _report_dir()
    stamp = _utc_stamp()
    safe_tool = _safe_name(tool_name)
    source_path = report_dir / f"auto-tool-{stamp}-{safe_tool}.redacted.log"
    source_path.write_text(redacted, encoding="utf-8")

    trace = _build_trace(tool_name, args, redacted, task_id=task_id, duration_ms=duration_ms)
    messages = [
        {"role": "system", "content": f"Headroom intermediate tool-result compression: {tool_name}."},
        {"role": "user", "content": f"Compress only the bulky body of this intermediate Hermes lane/tool result. Eligibility: {reason}. A deterministic exact header has already been extracted and will remain visible; do not invent identifiers or citations. Preserve errors, warnings, decisions, paths, counts, changed files, verification status, and final status indicators in the compressed body when useful."},
        {"role": "tool", "tool_call_id": _safe_name(tool_call_id or tool_name), "name": "worker_trace", "content": trace},
    ]
    compressed = compress_messages(messages)
    if not compressed.get("ok"):
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
        "tokens_before": before,
        "tokens_after": after,
        "tokens_saved": saved,
        "compression_ratio": compressed.get("compression_ratio"),
        "source_retention": "redacted_sidecar",
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    useful = bool(marker) or (isinstance(saved, int) and saved > 500 and isinstance(after, int) and isinstance(before, int) and after < before)
    if not useful:
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
            f"Use headroom_retrieve(hash='{marker}', query='<focused query>') for exact slices."
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
    return _shorten(payload)


def on_tool_execution(
    tool_name: str = "",
    args: dict[str, Any] | None = None,
    next_call: Any = None,
    task_id: str = "",
    tool_call_id: str = "",
    duration_ms: Any = None,
    **_: Any,
) -> Any:
    """Compress eligible bulky tool/lane results, including delegate_task.

    Fail-open: the original result is returned whenever Headroom is unhealthy,
    the data class is exact/blocked, compression is not useful, or any plugin
    error occurs.
    """
    if not callable(next_call):
        return None
    current_args = args if isinstance(args, dict) else {}
    result = next_call(current_args)
    try:
        if isinstance(result, str):
            transformed = compress_tool_result_for_context(
                tool_name=str(tool_name or ""),
                args=current_args,
                result=result,
                task_id=task_id or "",
                tool_call_id=tool_call_id or "",
                duration_ms=duration_ms,
            )
            if transformed:
                return transformed
    except Exception:
        return result
    return result


def on_llm_request(**kwargs):
    # This packaged plugin intentionally does not mutate provider/model routing.
    # Tool-result compression above is lane-scoped and fail-open; global/default
    # provider proxy routing remains an explicit, separate gate.
    del kwargs
    return None
