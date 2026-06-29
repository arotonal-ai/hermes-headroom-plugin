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


def _extract_markers(messages: Any) -> list[str]:
    text = json.dumps(messages, ensure_ascii=False)
    markers: list[str] = []
    markers.extend(m.split()[0] for m in re.findall(r"<<ccr:([^,>]+)", text))
    markers.extend(m.split()[0] for m in re.findall(r"hash=([A-Za-z0-9_-]{8,})", text))
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
    if _already_compressed(result):
        return "already_compressed"
    lowered = result[:12_000].lower()
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
    task_hint = " ".join(str(args.get(k) or "") for k in ("lane", "goal", "context", "query"))[:2_000].lower()
    if any(h in task_hint for h in ("delegate", "subagent", "worker", "kanban", "background", "debug", "research", "qa", "diagnostic")):
        return True, "lane_hint"
    return False, "not_intermediate_lane"


def _build_trace(tool_name: str, args: dict[str, Any], result: str, *, task_id: str = "", duration_ms: Any = None) -> str:
    args_preview = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    if len(args_preview) > 5_000:
        args_preview = args_preview[:5_000] + " ...[args truncated in trace header]"
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
    exact_reason = _exact_or_blocked_reason(tool_name, args, result)
    if exact_reason:
        return None
    eligible, reason = _lane_eligible(tool_name, args, result)
    if not eligible:
        return None

    redacted = _redact_text(result)
    report_dir = _report_dir()
    stamp = _utc_stamp()
    safe_tool = _safe_name(tool_name)
    source_path = report_dir / f"auto-tool-{stamp}-{safe_tool}.redacted.log"
    source_path.write_text(redacted, encoding="utf-8")

    trace = _build_trace(tool_name, args, redacted, task_id=task_id, duration_ms=duration_ms)
    messages = [
        {"role": "system", "content": f"Headroom intermediate tool-result compression: {tool_name}."},
        {"role": "user", "content": f"Compress this bulky intermediate Hermes lane/tool result for downstream reasoning. Eligibility: {reason}. Preserve errors, warnings, decisions, paths, counts, changed files, verification status, and final status indicators. Exact source is retained separately."},
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
    report = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "kind": "auto-tool-result",
        "tool_name": tool_name,
        "task_id": task_id,
        "tool_call_id": tool_call_id,
        "eligibility_reason": reason,
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
    }
    report_path = report_dir / f"auto-tool-{stamp}-{safe_tool}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    useful = bool(marker) or (isinstance(saved, int) and saved > 500 and isinstance(after, int) and isinstance(before, int) and after < before)
    if not useful:
        return None

    if marker:
        payload = (
            f"[Headroom auto-compressed tool result · tool={tool_name} original_chars={len(result)} "
            f"tokens_before={before} tokens_after={after} saved={saved} marker={marker}]\n"
            f"Use headroom_retrieve(hash='{marker}', query='<focused query>') for exact slices.\n"
            f"Report: {report_path}\n"
            f"Redacted exact source sidecar: {source_path}\n"
            "Contract: compressed view is for intermediate fan-in; verify material claims against exact source/retrieval before final decisions."
        )
    else:
        payload = (
            f"[Headroom auto-compressed tool result · tool={tool_name} original_chars={len(result)} "
            f"tokens_before={before} tokens_after={after} saved={saved} direct_compression=true]\n"
            f"Compressed payload: {compressed_path}\n"
            f"Report: {report_path}\n\n"
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
