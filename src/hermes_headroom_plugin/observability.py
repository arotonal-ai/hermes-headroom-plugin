"""Local redacted observability and bounded report paths."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DEFAULT_EVENT_LOG_MAX_BYTES, hermes_home, load_context_reduction_config, resolve_effective_config
from .policy import _redact_text
from .retention import maybe_prune_reports

EVENT_LOG_ROTATIONS = 3
_PLATFORM_CONTEXT_MAX = 512
_PLATFORM_BY_KEY: dict[str, str] = {}
_EVENT_WRITE_LOCK = threading.RLock()
ATTRIBUTION_SCHEMA_VERSION = "headroom.attribution.v2"
TOKEN_ESTIMATOR = "chars_div4_ceil"


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


def append_metadata_event(event: dict[str, Any]) -> None:
    """Append a caller-sanitized metadata-only event through the bounded log."""
    try:
        event = dict(event)
        event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        event.setdefault("action", "shadow_classified")
        event.setdefault("surface", "llm_request")
        path = _event_log_path()
        with _EVENT_WRITE_LOCK:
            _rotate_event_log_if_needed(path)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        try:
            path.chmod(0o600)
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
