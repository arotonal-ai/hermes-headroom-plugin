"""Behavior-changing middleware hooks.

The packaged plugin keeps provider/global routing off, but it may compress
eligible bulky intermediate tool/lane results when the local Headroom proxy is
healthy. An explicitly enabled LLM-request adapter can also catch eligible
legacy or bypassed tool results at Hermes's common pre-transport boundary.
Exact/edit-critical/sensitive classes fail closed to the original result.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import resolve_effective_config
from .proxy import compress_messages, hermes_home, load_context_reduction_config, readyz
from .retention import maybe_prune_reports

MIN_TOOL_RESULT_CHARS = max(2_000, int(os.environ.get("HEADROOM_MIN_TOOL_RESULT_CHARS", "8000")))
ALWAYS_TOOL_RESULT_CHARS = 120_000
MAX_RETURN_CHARS = 12_000
RAW_EDGE_CHARS = 1_200
DEFAULT_EVENT_LOG_MAX_BYTES = 5_000_000
EVENT_LOG_ROTATIONS = 3
_PLATFORM_CONTEXT_MAX = 512
_PLATFORM_BY_KEY: dict[str, str] = {}
_BELOW_MIN_AGGREGATE_BUFFERS: dict[str, dict[str, Any]] = {}
_LLM_REQUEST_CACHE_MAX = max(64, int(os.environ.get("HEADROOM_LLM_REQUEST_CACHE_MAX", "2048")))
_LLM_REQUEST_TRANSFORM_CACHE: OrderedDict[str, str] = OrderedDict()
_LLM_REQUEST_CACHE_LOCK = threading.RLock()
_EVENT_WRITE_LOCK = threading.RLock()
BELOW_MIN_AGGREGATE_CHARS = 28_000
BELOW_MIN_AGGREGATE_MAX_CHUNKS = 24
BELOW_MIN_AGGREGATE_MAX_BUFFER_KEYS = 128
ATTRIBUTION_SCHEMA_VERSION = "headroom.attribution.v2"
TOKEN_ESTIMATOR = "chars_div4_ceil"

ELIGIBLE_TOOLS = {
    "delegate_task",
    "terminal",
    "execute_code",
    "process",
    "read_file",
    "search_files",
    "skill_view",
    "fact_store",
    "browser_console",
    "browser_snapshot",
    "browser_get_images",
    "web_search",
    "web_extract",
    "session_search",
}
ELIGIBLE_PREFIXES = ("browser_",)
EXACT_TOOLS = {
    "patch",
    "write_file",
    "skill_manage",
    "headroom_retrieve",
    "memory",
    "mcp_open_design_write_file",
}
MACHINE_CONSUMER_EXACT_TOOLS = {"read_file", "search_files", "skill_view", "fact_store"}
READ_ONLY_ACTIONS = {"search", "probe", "related", "reason", "contradict", "list", "get", "read", "query"}
READ_ONLY_MCP_HINTS = ("get", "read", "list", "search", "query", "extract", "snapshot", "inspect", "show")
MUTATING_MCP_HINTS = ("create", "write", "patch", "edit", "update", "delete", "remove", "publish", "send")
EXACT_COMMAND_HINTS = (
    "git diff",
    "diff ",
    "sha256sum",
    "md5sum",
    "base64",
    "gpg ",
    "openssl ",
)
PROTECTED_RECOVERY_MARKER_RE = re.compile(
    r"(?im)\b(?:rollback(?:[_-]?(?:plan|path|target))?|checksum(?:[_-]?(?:expected|actual))?|sha(?:256)?|commit(?:[_-]?sha)?|patch(?:[_-]?id)?)\s*[:=]"
)
FAILURE_MARKER_RE = re.compile(r"(?i)\b(?:fail(?:ed|ure)?|error|exception|traceback|mismatch|blocked)\b")
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
    "source_readback",
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
    try:
        path.chmod(0o700)
    except Exception:
        pass
    maybe_prune_reports(path, config=load_context_reduction_config())
    return path


def _event_log_path() -> Path:
    path = hermes_home() / "control-plane" / "headroom" / "events"
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except Exception:
        pass
    return path / "headroom-events.jsonl"


def _event_log_max_bytes() -> int:
    try:
        max_bytes = resolve_effective_config(raw_config=load_context_reduction_config()).event_log_max_bytes
    except Exception:
        max_bytes = DEFAULT_EVENT_LOG_MAX_BYTES
    return max(64_000, max_bytes)


def _falsey(value: Any) -> bool:
    if isinstance(value, bool):
        return value is False
    if value is None:
        return False
    return str(value).strip().lower() in {"0", "false", "no", "n", "off", "disabled", "disable"}


def auto_compression_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return whether middleware may auto-compress tool outputs.

    Runtime/status/smoke/retrieve remain available when this is false. This is
    the lightweight on-demand mode for expensive owner-cockpit improvement loops.
    """
    raw = config if isinstance(config, dict) else None
    return resolve_effective_config(raw_config=raw).auto_compression


def llm_request_compression_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return whether the common pre-transport tool-result adapter is enabled.

    This is deliberately opt-in even when tool-execution compression is active.
    It mutates only tool-result text, never routing, credentials, system/user
    prompts, tool schemas, tool arguments, or streaming controls.
    """
    raw = config if isinstance(config, dict) else None
    return resolve_effective_config(raw_config=raw).llm_request_enabled


def _rotate_event_log_if_needed(path: Path) -> None:
    """Bound local observability JSONL growth before appending a new event."""
    try:
        if not path.exists() or path.stat().st_size < _event_log_max_bytes():
            return
        oldest = path.with_name(path.name + f".{EVENT_LOG_ROTATIONS}")
        if oldest.exists():
            oldest.unlink()
        for idx in range(EVENT_LOG_ROTATIONS - 1, 0, -1):
            src = path.with_name(path.name + f".{idx}")
            dst = path.with_name(path.name + f".{idx + 1}")
            if src.exists():
                src.replace(dst)
        path.replace(path.with_name(path.name + ".1"))
        for rotated in path.parent.glob(path.name + ".*"):
            try:
                rotated.chmod(0o600)
            except Exception:
                pass
    except Exception:
        return


def _safe_event_text(value: Any, *, limit: int = 240) -> str:
    text = _redact_text(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _normalize_platform(value: Any) -> str:
    text = _safe_event_text(value, limit=80).lower().replace("-", "_")
    aliases = {
        "tg": "telegram",
        "telegram_dm": "telegram",
        "telegram_group": "telegram",
        "api": "api_server",
        "api-server": "api_server",
        "local": "cli",
    }
    text = aliases.get(text, text)
    allowed = {
        "telegram",
        "cli",
        "tui",
        "desktop",
        "api_server",
        "cron",
        "webhook",
        "discord",
        "slack",
        "whatsapp",
        "signal",
        "matrix",
        "email",
    }
    return text if text in allowed else ""


def remember_platform_context(*, session_id: Any = "", task_id: Any = "", turn_id: Any = "", platform: Any = "", **_: Any) -> None:
    """Remember platform from pre-LLM hooks for later tool middleware events.

    Hermes core currently passes platform to pre-LLM hooks, while some
    tool-execution middleware paths may omit it. Keep this plugin-local and
    bounded instead of patching core just for observability metadata.
    """
    normalized = _normalize_platform(platform)
    if not normalized:
        return
    for raw in (turn_id, task_id, session_id):
        key = _safe_event_text(raw, limit=160)
        if key:
            _PLATFORM_BY_KEY[key] = normalized
    if len(_PLATFORM_BY_KEY) > _PLATFORM_CONTEXT_MAX:
        for key in list(_PLATFORM_BY_KEY)[: len(_PLATFORM_BY_KEY) - _PLATFORM_CONTEXT_MAX]:
            _PLATFORM_BY_KEY.pop(key, None)


def _resolve_event_platform(platform: Any, *, session_id: Any = "", task_id: Any = "", turn_id: Any = "") -> str:
    explicit = _normalize_platform(platform)
    if explicit:
        return explicit
    for raw in (turn_id, task_id, session_id):
        key = _safe_event_text(raw, limit=160)
        if key and _PLATFORM_BY_KEY.get(key):
            return _PLATFORM_BY_KEY[key]
    for env_name in ("HERMES_PLATFORM", "HERMES_SESSION_SOURCE"):
        env_platform = _normalize_platform(os.environ.get(env_name))
        if env_platform:
            return env_platform
    return "unknown"


def _infer_lane(tool_name: str, args: dict[str, Any], data_class: Any = None) -> str:
    explicit = _safe_event_text(args.get("lane") or args.get("headroom_lane"), limit=80)
    if explicit:
        return explicit
    tool = str(tool_name or "").lower()
    if tool == "delegate_task":
        return "delegate"
    if tool == "terminal":
        return "terminal"
    if tool == "execute_code":
        return "code_execution"
    if tool == "process":
        return "process"
    if tool.startswith("browser_"):
        return "browser"
    if tool in {"web_extract", "session_search", "x_search"}:
        return "research"
    if tool in {"read_file", "search_files"}:
        return "file"
    if tool in {"patch", "write_file", "mcp_open_design_write_file"}:
        return "edit"
    if tool in {"skill_view", "skill_manage"}:
        return "skill"
    if tool in {"memory", "fact_store", "todo"}:
        return "state"
    if tool.startswith("mcp_open_design_"):
        return "artifact"
    if tool.startswith("kanban"):
        return "kanban"
    if str(data_class or "") in {"qa_trace", "diagnostic_trace"}:
        return "qa"
    return "unknown"


def _rough_tokens_from_chars(value: Any) -> int:
    """Cheap provider-neutral estimate; exact character counts remain authoritative."""
    try:
        chars = max(0, int(value or 0))
    except Exception:
        return 0
    return (chars + 3) // 4 if chars else 0


def _event_dedupe_key(
    *,
    session_id: Any,
    turn_id: Any,
    task_id: Any,
    tool_call_id: Any,
    api_request_id: Any,
    tool_name: Any,
    action: Any,
    reason: Any,
    original_chars: Any,
    marker: Any,
    logical_source_id: Any = "",
) -> str:
    """Stable logical-event key; blank when Hermes supplied no call identity."""
    if str(logical_source_id or "").strip():
        identity = {
            "telemetry_schema_version": ATTRIBUTION_SCHEMA_VERSION,
            "session_id": str(session_id or ""),
            "tool_call_id": str(tool_call_id or ""),
            "tool_name": str(tool_name or ""),
            "action": str(action or ""),
            "logical_source_id": str(logical_source_id),
        }
        canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if not any(str(value or "").strip() for value in (tool_call_id, api_request_id, turn_id)):
        return ""
    identity = {
        "session_id": str(session_id or ""),
        "turn_id": str(turn_id or ""),
        "task_id": str(task_id or ""),
        "tool_call_id": str(tool_call_id or ""),
        "api_request_id": str(api_request_id or ""),
        "tool_name": str(tool_name or ""),
        "action": str(action or ""),
        "reason": str(reason or ""),
        "original_chars": original_chars,
        "marker": str(marker or ""),
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _event_log_contains_dedupe_key(path: Path, dedupe_key: str) -> bool:
    """Best-effort cross-restart idempotence over the bounded event-log rotations."""
    if not dedupe_key:
        return False
    for candidate in (path, *(path.with_name(f"{path.name}.{idx}") for idx in range(1, EVENT_LOG_ROTATIONS + 1))):
        if not candidate.exists():
            continue
        try:
            with candidate.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        if str(json.loads(line).get("dedupe_key") or "") == dedupe_key:
                            return True
                    except (json.JSONDecodeError, AttributeError):
                        continue
        except OSError:
            continue
    return False


def _emit_headroom_event(
    *,
    action: str,
    tool_name: str,
    args: dict[str, Any],
    reason: str,
    task_id: str = "",
    tool_call_id: str = "",
    session_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    platform: str = "",
    surface: str = "tool_execution",
    data_class: Any = None,
    original_chars: int | None = None,
    redacted_chars: int | None = None,
    tokens_before: Any = None,
    tokens_after: Any = None,
    tokens_saved: Any = None,
    model_facing_chars_before: int | None = None,
    model_facing_chars_after: int | None = None,
    measurement_scope: str = "tool_result",
    compression_latency_ms: Any = None,
    new_savings_event: bool | None = None,
    marker: str | None = None,
    report_path: Path | None = None,
    source_path: Path | None = None,
    compressed_path: Path | None = None,
    exact_authority: str = "none",
    logical_source_id: str = "",
    error: Any = None,
) -> None:
    """Append one local attribution event without changing tool behavior."""
    try:
        before_chars = original_chars if model_facing_chars_before is None else model_facing_chars_before
        after_chars = before_chars if model_facing_chars_after is None else model_facing_chars_after
        before_est = _rough_tokens_from_chars(before_chars)
        after_est = _rough_tokens_from_chars(after_chars)
        saved_est = max(0, before_est - after_est)
        dedupe_key = _event_dedupe_key(
            session_id=session_id,
            turn_id=turn_id,
            task_id=task_id,
            tool_call_id=tool_call_id,
            api_request_id=api_request_id,
            tool_name=tool_name,
            action=action,
            reason=reason,
            original_chars=original_chars,
            marker=marker,
            logical_source_id=logical_source_id,
        )
        event: dict[str, Any] = {
            "type": "headroom_tool_result",
            "telemetry_schema_version": ATTRIBUTION_SCHEMA_VERSION,
            "event_id": uuid.uuid4().hex,
            "dedupe_key": dedupe_key,
            "logical_source_id": _safe_event_text(logical_source_id, limit=80),
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "session_id": _safe_event_text(session_id, limit=120),
            "turn_id": _safe_event_text(turn_id, limit=120),
            "task_id": _safe_event_text(task_id, limit=120),
            "tool_call_id": _safe_event_text(tool_call_id, limit=120),
            "api_request_id": _safe_event_text(api_request_id, limit=120),
            "platform": _resolve_event_platform(platform, session_id=session_id, task_id=task_id, turn_id=turn_id),
            "surface": surface,
            "tool_name": _safe_event_text(tool_name, limit=120),
            "lane": _infer_lane(tool_name, args, data_class),
            "data_class": _safe_event_text(data_class, limit=120),
            "action": action,
            "reason": _safe_event_text(reason, limit=240),
            "original_chars": original_chars,
            "redacted_chars": redacted_chars,
            # Back-compatible internal Headroom counters. These do not describe
            # the final payload returned to the model.
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "tokens_saved": tokens_saved,
            "service_tokens_before": tokens_before,
            "service_tokens_after": tokens_after,
            "service_tokens_saved": tokens_saved,
            "service_metric_scope": "headroom_internal_messages",
            # Attribution v2: exact chars plus a labelled provider-neutral estimate
            # for the payload fragment actually returned by this middleware.
            "model_facing_chars_before": before_chars,
            "model_facing_chars_after": after_chars,
            "model_facing_est_tokens_before": before_est,
            "model_facing_est_tokens_after": after_est,
            "model_facing_est_tokens_saved": saved_est,
            "model_facing_token_estimator": TOKEN_ESTIMATOR,
            "measurement_scope": _safe_event_text(measurement_scope, limit=120),
            "new_savings_event": (
                bool(action == "compressed" and saved_est > 0)
                if new_savings_event is None
                else bool(new_savings_event)
            ),
            "compression_latency_ms": compression_latency_ms,
            "marker": _safe_event_text(marker, limit=160),
            "report_path": str(report_path) if report_path else "",
            "source_path": str(source_path) if source_path else "",
            "compressed_path": str(compressed_path) if compressed_path else "",
            "exact_authority": exact_authority,
            "sensitive_hits": 0,
            "protected_hits": 1 if action == "blocked" and "protected" in str(reason).lower() else 0,
        }
        if error:
            event["error"] = _safe_event_text(error, limit=500)
        path = _event_log_path()
        with _EVENT_WRITE_LOCK:
            if event["new_savings_event"] and logical_source_id and _event_log_contains_dedupe_key(path, dedupe_key):
                event["new_savings_event"] = False
                event["attribution_duplicate"] = True
            _rotate_event_log_if_needed(path)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        try:
            path.chmod(0o600)
        except Exception:
            pass
    except Exception:
        return


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


def _lane_eligible(tool_name: str, args: dict[str, Any], result: str) -> tuple[bool, str]:
    if len(result) >= ALWAYS_TOOL_RESULT_CHARS:
        return True, "always_chars"
    if len(result) < MIN_TOOL_RESULT_CHARS:
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
    compressed = compress_messages(messages, proxy_url=health.get("proxy_url"))
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
    health = readyz()
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
    eligible, reason = _lane_eligible(tool_name, args, result)
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
    compressed = compress_messages(messages)
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

    Fail-open: the original result is returned whenever Headroom is unhealthy,
    the data class is exact/blocked, compression is not useful, or any plugin
    error occurs.
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
    if not isinstance(text, str) or len(text) < MIN_TOOL_RESULT_CHARS or _already_compressed(text):
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
