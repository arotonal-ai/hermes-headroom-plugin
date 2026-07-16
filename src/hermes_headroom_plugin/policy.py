"""Admission, exactness, redaction, and bounded-header policy."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import DEFAULT_MIN_TOOL_RESULT_CHARS

MIN_TOOL_RESULT_CHARS = DEFAULT_MIN_TOOL_RESULT_CHARS
ALWAYS_TOOL_RESULT_CHARS = 120_000
MAX_RETURN_CHARS = 12_000
RAW_EDGE_CHARS = 1_200
ELIGIBLE_TOOLS = {"delegate_task", "terminal", "execute_code", "process", "read_file", "search_files", "skill_view", "fact_store", "browser_console", "browser_snapshot", "browser_get_images", "web_search", "web_extract", "session_search"}
ELIGIBLE_PREFIXES = ("browser_",)
EXACT_TOOLS = {"patch", "write_file", "skill_manage", "headroom_retrieve", "memory", "mcp_open_design_write_file"}
MACHINE_CONSUMER_EXACT_TOOLS = {"read_file", "search_files", "skill_view", "fact_store"}
READ_ONLY_ACTIONS = {"search", "probe", "related", "reason", "contradict", "list", "get", "read", "query"}
READ_ONLY_MCP_HINTS = ("get", "read", "list", "search", "query", "extract", "snapshot", "inspect", "show")
MUTATING_MCP_HINTS = ("create", "write", "patch", "edit", "update", "delete", "remove", "publish", "send")
EXACT_COMMAND_HINTS = ("git diff", "diff ", "sha256sum", "md5sum", "base64", "gpg ", "openssl ")
PROTECTED_RECOVERY_MARKER_RE = re.compile(r"(?im)\b(?:rollback(?:[_-]?(?:plan|path|target))?|checksum(?:[_-]?(?:expected|actual))?|sha(?:256)?|commit(?:[_-]?sha)?|patch(?:[_-]?id)?)\s*[:=]")
FAILURE_MARKER_RE = re.compile(r"(?i)\b(?:fail(?:ed|ure)?|error|exception|traceback|mismatch|blocked)\b")
COMPRESSED_SENTINELS = ("Headroom auto-compressed", "<<ccr:", "hash=")
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
HEADER_REQUIRED_CLASSES = {"source_readback", "browser_debug_trace", "interaction_state", "research_corpus", "orchestration_fanin", "multimodal_intermediate_text", "long_comments_history", "raw_feed_snapshot"}
KNOWN_DATA_CLASSES = HEADER_REQUIRED_CLASSES | {"diagnostic_trace", "qa_trace", "worker_trace_raw"}
EXACT_CLASSES = frozenset({"final_packet", "patch_diff", "canonical_html_css", "manifest_hashes", "claim_ledger", "final_artifact"})
BLOCKED_CLASSES = frozenset({"secret_or_sensitive", "memory_profile_instruction", "protected_contamination", "system_developer_instructions"})
COMPRESSIBLE_CLASSES = frozenset({"raw_log", "source_readback", "worker_trace_raw", "browser_debug_trace", "ocr_raw_text", "research_corpus_raw", "qa_trace", "diagnostic_intermediate"})


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
    tool = str(tool_name or "").lower()
    if tool_name in EXACT_TOOLS or tool in EXACT_TOOLS:
        return f"exact_tool:{tool_name}"
    if tool == "fact_store" and str(args.get("action") or "").lower() not in READ_ONLY_ACTIONS:
        return "exact_state_mutation:fact_store"
    if tool.startswith(("mcp__open_design__", "mcp_open_design_")):
        if any(hint in tool for hint in MUTATING_MCP_HINTS):
            return f"exact_mcp_mutation:{tool_name}"
    elif tool.startswith(("mcp__", "mcp_")):
        explicit_class = next(
            (_normalize_data_class(args.get(key)) for key in ("data_class", "headroom_data_class", "classification") if args.get(key)),
            None,
        )
        read_only_name = any(hint in tool for hint in READ_ONLY_MCP_HINTS) and not any(
            hint in tool for hint in MUTATING_MCP_HINTS
        )
        if not explicit_class and not read_only_name:
            return f"exact_mcp_default:{tool_name}"
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
    # Same-provider A/B: lossy compression removed an exact rollback target
    # from 2/2 code-trace replicates while bypass preserved it. Fail closed
    # only for failure traces carrying recovery/integrity anchors.
    if PROTECTED_RECOVERY_MARKER_RE.search(result[:160_000]) and FAILURE_MARKER_RE.search(result[:160_000]):
        return "protected_recovery_integrity_trace"
    if tool_name == "terminal":
        cmd = str(args.get("command") or "").lower()
        if any(hint in cmd for hint in EXACT_COMMAND_HINTS):
            return "exact_command"
    return None


def _lane_eligible(
    tool_name: str,
    args: dict[str, Any],
    result: str,
    *,
    min_chars: int | None = None,
) -> tuple[bool, str]:
    effective_min = MIN_TOOL_RESULT_CHARS if min_chars is None else min_chars
    if len(result) >= ALWAYS_TOOL_RESULT_CHARS:
        return True, "always_chars"
    if len(result) < effective_min:
        return False, "below_min_chars"
    tool = str(tool_name or "").lower()
    if tool in ELIGIBLE_TOOLS or any(tool.startswith(prefix) for prefix in ELIGIBLE_PREFIXES):
        return True, f"eligible_tool:{tool_name}"
    if tool.startswith(("mcp__", "mcp_")) and any(hint in tool for hint in READ_ONLY_MCP_HINTS) and not any(
        hint in tool for hint in MUTATING_MCP_HINTS
    ):
        return True, f"eligible_readonly_mcp:{tool_name}"
    task_hint = " ".join(
        str(args.get(k) or "")
        for k in ("lane", "goal", "context", "data_class", "headroom_data_class", "classification")
    )[:2_000].lower()
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

    if tool in {"read_file", "search_files", "skill_view", "fact_store"} or (
        tool.startswith(("mcp__", "mcp_"))
        and any(hint in tool for hint in READ_ONLY_MCP_HINTS)
        and not any(hint in tool for hint in MUTATING_MCP_HINTS)
    ):
        return "source_readback"
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
            "path",
            "project",
            "entry",
            "entity",
            "action",
            "offset",
            "limit",
            "pattern",
            "query",
            "target",
            "file_glob",
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
    if data_class == "source_readback":
        if not (
            identifiers
            or urls
            or any(re.search(r"(?i)(path|project|entry|entity|action|offset|limit|pattern|query|target|file_glob)", item) for item in anchors)
        ):
            missing.append("source path/query/window anchor")
    elif data_class == "orchestration_fanin":
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


def classify_data(tool: str = "", data_class: str = "", final: bool = False, sensitive: bool = False) -> str:
    """Return one of: blocked, exact, compressible, exact_bounded."""
    tool = (tool or "").strip()
    data_class = (data_class or "").strip()
    if sensitive or data_class in BLOCKED_CLASSES:
        return "blocked"
    if final or tool in EXACT_TOOLS or data_class in EXACT_CLASSES:
        return "exact"
    if data_class in COMPRESSIBLE_CLASSES:
        return "compressible"
    return "exact_bounded"


def should_compress(tool: str = "", data_class: str = "", *, final: bool = False, sensitive: bool = False) -> bool:
    return classify_data(tool=tool, data_class=data_class, final=final, sensitive=sensitive) == "compressible"
