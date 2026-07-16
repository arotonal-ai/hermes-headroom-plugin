"""Owner-local LLM monitor for Hermes.

Minimal observer plugin:
- /llm-monitor on|off|status|tail
- writes one JSONL trace file per turn under ~/.hermes/control-plane/llm-monitor/traces/
- does not rewrite LLM requests or modify Hermes core behavior
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import urlopen

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover - clean package tests may import companion without Hermes core
    def get_hermes_home() -> str:
        return os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")

try:
    from agent.redact import redact_sensitive_text
except Exception:  # pragma: no cover - defensive for unusual plugin load contexts
    def redact_sensitive_text(text: str) -> str:  # type: ignore
        return text

_MONITOR_DIR = Path(get_hermes_home()) / "control-plane" / "llm-monitor"
_TRACE_DIR = _MONITOR_DIR / "traces"
_REPORT_DIR = _MONITOR_DIR / "reports"
_CONTEXT_REPORT_SCRIPT = _MONITOR_DIR / "scripts" / "turn_attribution.py"
_STATE_FILE = _MONITOR_DIR / "state.json"
_LOCK = threading.RLock()
_TURN_FILES: Dict[str, Path] = {}
_TURN_ROOT_WRITTEN: set[str] = set()
_LAST_RETENTION_CHECK = 0.0

_OWNER_DEFAULT_LIGHT = [
    "terminal", "file", "code_execution", "web", "skills", "memory",
    "session_search", "todo", "delegation", "clarify", "no_mcp",
]

_CAPABILITY_PRESETS: Dict[str, Dict[str, Any]] = {
    "micro": {
        "intent": "Tiny answers / cost canaries. Use a fresh session with the smallest practical tool surface.",
        "risk": "Too small for complex tasks; must escalate before acting.",
    },
    "light": {
        "intent": "Default owner work: real local capability with bounded schema surface.",
        "toolsets": _OWNER_DEFAULT_LIGHT,
        "risk": "Specialized media/browser/MCP tools require explicit escalation.",
    },
    "browser": {
        "intent": "Interactive web/browser automation tasks.",
        "toolsets": _OWNER_DEFAULT_LIGHT + ["browser"],
        "risk": "Higher schema/cost; use only for web interaction, not normal chat.",
    },
    "creative": {
        "intent": "Image/video/audio/visual generation workflows.",
        "toolsets": _OWNER_DEFAULT_LIGHT + ["vision", "image_gen", "video", "tts"],
        "risk": "Potential paid/media actions still require normal approval gates.",
    },
    "full": {
        "intent": "Temporary escape hatch when task scope is unknown or broad.",
        "risk": "Most expensive/default-noisiest mode; should be bounded to one session/task.",
    },
}

_WARNING_PROMPT_TOKENS = 40_000
_RESET_PROMPT_TOKENS = 60_000
_WARNING_TOOL_COUNT = 50
_WARNING_API_CALLS = 3

_LAST_INSTRUCTIONS_CHARS = 0
_LAST_TOOL_SCHEMA_CHARS_BY_COUNT: Dict[int, int] = {}

_DEFAULT_STATE = {
    "enabled": False,
    "mode": "full",  # full = sanitized request/response payloads; metadata = counters only
    "strict_metadata": True,
    "retention_days": 14,
    "max_trace_files": 2000,
    "retention_max_deletes_per_check": 200,
    "visible_pre_call": False,
    # final_overlay prepends text into the assistant answer itself. Keep off by
    # default: visible status should live in platform status/draft rails, not
    # pollute the final answer or create apparent loops.
    "final_overlay": False,
    # local_visibility edits a tiny status bubble with request/response previews
    # derived from already-local hook data. No additional LLM calls.
    "local_visibility": True,
    # turn_summary edits one per-turn gateway status bubble instead of emitting a
    # separate visible marker per API request. This keeps observability useful
    # without chat clutter like repeated [LLM CALL 1/N] cards.
    "visible_status_mode": "turn_summary",
    # Guardrails: visibility must never become a chat loop/cost amplifier.
    "max_visible_calls_per_turn": 3,
    "max_visible_calls_per_minute": 10,
    "fallback_send_message": False,
    "disable_visibility_on_error": True,
    # Include Headroom per-turn event summary in the editable turn-summary
    # status bubble when local Headroom events exist. Read-only, no LLM calls.
    "headroom_summary": True,
}

_PRECALL_NOTIFY_SENT: set[str] = set()
_REQUEST_PREVIEWS: Dict[str, str] = {}
_VISIBLE_API_IDS: set[str] = set()
_VISIBLE_CALLS_BY_TURN: Dict[str, int] = {}
_VISIBLE_CALL_TIMES: List[float] = []
_VISIBLE_TURN_STATS: Dict[str, Dict[str, Any]] = {}
_HEADROOM_MARKERS_SEEN_BY_SESSION: Dict[str, set[str]] = {}
_HEADROOM_MARKER_RE = re.compile(
    r"(?:<<ccr:([A-Za-z0-9._-]{6,160})(?:,[^>]*)?>>|\b(?:hash|marker)=([A-Za-z0-9._-]{6,160}))"
)


def _format_pre_call_message(*, provider: Any, model: Any, call_no: Any, tokens: Any) -> str:
    """Minimal owner-visible LLM activity line; no prompt/response previews."""
    return f"LLM activo · llamada `{call_no}` · in≈`{tokens}` · `{provider}/{model}`"


def _visible_mode(state: Dict[str, Any]) -> str:
    mode = str(state.get("visible_status_mode") or "turn_summary").strip().lower()
    return "per_call" if mode in {"per_call", "per-call", "call"} else "turn_summary"


def _turn_status_key(kwargs: Dict[str, Any]) -> str:
    turn = _visible_turn_key(kwargs)
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", "-", turn).strip("-._:") or "unknown"
    return f"llm-monitor-turn:{safe[:96]}"


def _turn_stats_key(kwargs: Dict[str, Any]) -> str:
    return _visible_turn_key(kwargs)


def _update_turn_stats_pre(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    key = _turn_stats_key(kwargs)
    provider = str(kwargs.get("provider") or "unknown-provider")
    model = str(kwargs.get("model") or "unknown-model")
    tokens = _safe_int(kwargs.get("approx_input_tokens"))
    with _LOCK:
        stats = _VISIBLE_TURN_STATS.setdefault(key, {"started": 0, "completed": 0, "errors": 0, "max_input_tokens": 0, "duration_s": 0.0})
        stats["started"] = _safe_int(stats.get("started")) + 1
        stats["max_input_tokens"] = max(_safe_int(stats.get("max_input_tokens")), tokens)
        stats["provider"] = provider
        stats["model"] = model
        stats["session_id"] = kwargs.get("session_id") or stats.get("session_id") or ""
        stats["turn_id"] = kwargs.get("turn_id") or stats.get("turn_id") or ""
        stats["task_id"] = kwargs.get("task_id") or stats.get("task_id") or ""
        stats["platform"] = kwargs.get("platform") or stats.get("platform") or ""
        # Bound memory for long-running gateway processes.
        if len(_VISIBLE_TURN_STATS) > 300:
            for stale in list(_VISIBLE_TURN_STATS.keys())[:80]:
                if stale != key:
                    _VISIBLE_TURN_STATS.pop(stale, None)
        return dict(stats)


def _update_turn_stats_post(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    key = _turn_stats_key(kwargs)
    provider = str(kwargs.get("provider") or "unknown-provider")
    model = str(kwargs.get("model") or kwargs.get("response_model") or "unknown-model")
    duration = kwargs.get("api_duration")
    with _LOCK:
        stats = _VISIBLE_TURN_STATS.setdefault(key, {"started": 0, "completed": 0, "errors": 0, "max_input_tokens": 0, "duration_s": 0.0})
        stats["completed"] = _safe_int(stats.get("completed")) + 1
        stats["provider"] = provider
        stats["model"] = model
        stats["session_id"] = kwargs.get("session_id") or stats.get("session_id") or ""
        stats["turn_id"] = kwargs.get("turn_id") or stats.get("turn_id") or ""
        stats["task_id"] = kwargs.get("task_id") or stats.get("task_id") or ""
        stats["platform"] = kwargs.get("platform") or stats.get("platform") or ""
        stats["finish_reason"] = kwargs.get("finish_reason") or stats.get("finish_reason") or "?"
        if isinstance(duration, (int, float)):
            stats["duration_s"] = round(float(stats.get("duration_s") or 0.0) + float(duration), 3)
        usage = kwargs.get("usage") if isinstance(kwargs.get("usage"), dict) else {}
        prompt = _safe_int(usage.get("prompt_tokens") or usage.get("input_tokens"))
        output = _safe_int(usage.get("completion_tokens") or usage.get("output_tokens"))
        cache = _safe_int(usage.get("cache_read_tokens"))
        if prompt:
            stats["prompt_tokens_sum"] = _safe_int(stats.get("prompt_tokens_sum")) + prompt
        if output:
            stats["output_tokens_sum"] = _safe_int(stats.get("output_tokens_sum")) + output
        if cache:
            stats["cache_read_tokens_sum"] = _safe_int(stats.get("cache_read_tokens_sum")) + cache
        return dict(stats)


def _human_count(value: Any) -> str:
    """Compact owner-visible integer formatting for token/status counts."""
    num = _safe_int(value)
    if not num:
        return "?"
    if num >= 1_000_000:
        return f"{num / 1_000_000:.1f}M".replace(".0M", "M")
    if num >= 10_000:
        return f"{num / 1_000:.1f}k".replace(".0k", "k")
    return str(num)




def _headroom_event_log_path() -> Path:
    return Path(get_hermes_home()) / "control-plane" / "headroom" / "events" / "headroom-events.jsonl"


def _headroom_event_tail(limit: int = 2000) -> List[Dict[str, Any]]:
    path = _headroom_event_log_path()
    if not path.exists():
        return []
    try:
        from collections import deque
        events: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in deque(fh, maxlen=max(1, min(int(limit or 2000), 10000))):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("type") == "headroom_tool_result":
                    events.append(event)
        return events
    except Exception:
        return []


def _legacy_headroom_dedupe_key(event: Dict[str, Any]) -> str:
    """Best-effort key for v1 rows; avoid merging unattributed repeated calls."""
    if not any(str(event.get(key) or "").strip() for key in ("tool_call_id", "api_request_id", "turn_id")):
        return ""
    values = (
        event.get("session_id"),
        event.get("turn_id"),
        event.get("task_id"),
        event.get("tool_call_id"),
        event.get("api_request_id"),
        event.get("tool_name"),
        event.get("action"),
        event.get("reason"),
        event.get("original_chars"),
        event.get("marker"),
    )
    return "legacy:" + "\x1f".join(str(value or "") for value in values)


def _dedupe_headroom_events(events: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Keep one row per logical transform while preserving unkeyed events."""
    unique: List[Dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    for event in events:
        key = str(event.get("dedupe_key") or "").strip() or _legacy_headroom_dedupe_key(event)
        if key and key in seen:
            duplicates += 1
            continue
        if key:
            seen.add(key)
        unique.append(event)
    return unique, duplicates


def _headroom_tool_payload_texts(request: Any, request_messages: Any = None) -> List[str]:
    """Collect only model-facing tool-result payloads across common protocols."""
    roots: List[Any] = []
    if isinstance(request_messages, list):
        roots.append(request_messages)
    if isinstance(request, dict):
        candidate_body = request.get("body")
        body: Dict[str, Any] = candidate_body if isinstance(candidate_body, dict) else request
        for key in ("messages", "input"):
            if isinstance(body.get(key), list):
                roots.append(body.get(key))

    payloads: List[str] = []
    tool_types = {"tool_result", "function_call_output", "computer_tool_result", "mcp_tool_result"}

    def walk(node: Any, inherited_tool: bool = False) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, inherited_tool)
            return
        if not isinstance(node, dict):
            if inherited_tool and isinstance(node, str):
                payloads.append(node)
            return
        role = str(node.get("role") or "").lower()
        item_type = str(node.get("type") or "").lower()
        is_tool = inherited_tool or role == "tool" or item_type in tool_types
        if is_tool:
            try:
                payloads.append(json.dumps(node, ensure_ascii=False, default=str))
            except Exception:
                payloads.append(str(node))
            return
        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value, False)

    for root in roots:
        walk(root)
    return payloads


def _headroom_markers_in_request(request: Any, request_messages: Any = None) -> List[str]:
    markers: List[str] = []
    seen: set[str] = set()
    for payload in _headroom_tool_payload_texts(request, request_messages):
        for match in _HEADROOM_MARKER_RE.finditer(payload):
            marker = next((group for group in match.groups() if group), "")
            if marker and marker not in seen:
                seen.add(marker)
                markers.append(marker)
    return markers


def _headroom_request_attribution(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Correlate retained transforms; never count them as new request savings."""
    markers = _headroom_markers_in_request(kwargs.get("request"), kwargs.get("request_messages"))
    raw_events = _headroom_event_tail(limit=10000)
    marker_events = [
        event
        for event in raw_events
        if event.get("action") == "compressed" and str(event.get("marker") or "").strip() in markers
    ]
    events, duplicates = _dedupe_headroom_events(marker_events)
    by_marker: Dict[str, Dict[str, Any]] = {}
    for event in events:
        marker = str(event.get("marker") or "").strip()
        if marker and event.get("action") == "compressed":
            by_marker[marker] = event
    correlated = [by_marker[marker] for marker in markers if marker in by_marker]
    metric_events = [
        event
        for event in correlated
        if event.get("model_facing_est_tokens_before") is not None
        and event.get("model_facing_est_tokens_after") is not None
    ]
    before = sum(_safe_int(event.get("model_facing_est_tokens_before")) for event in metric_events)
    after = sum(_safe_int(event.get("model_facing_est_tokens_after")) for event in metric_events)
    saved = sum(_safe_int(event.get("model_facing_est_tokens_saved")) for event in metric_events)
    service_saved = sum(
        _safe_int(event.get("service_tokens_saved") or event.get("tokens_saved")) for event in correlated
    )
    session_id = str(kwargs.get("session_id") or "")
    with _LOCK:
        seen = _HEADROOM_MARKERS_SEEN_BY_SESSION.setdefault(session_id, set())
        first_observed = [marker for marker in markers if marker not in seen]
        seen.update(markers)
    if not markers:
        coverage = "no_markers"
    elif len(correlated) == len(markers):
        coverage = "correlated"
    elif correlated:
        coverage = "partial"
    else:
        coverage = "unmatched"
    if not correlated:
        metric_coverage = "none"
    elif len(metric_events) == len(correlated):
        metric_coverage = "full"
    elif metric_events:
        metric_coverage = "partial"
    else:
        metric_coverage = "legacy_only"
    marker_completeness = (len(correlated) / len(markers) * 100.0) if markers else 100.0
    metric_completeness = (len(metric_events) / len(correlated) * 100.0) if correlated else 0.0
    return {
        "schema_version": "headroom.attribution.v2",
        "coverage": coverage,
        "marker_count": len(markers),
        "correlated_event_count": len(correlated),
        "metric_event_count": len(metric_events),
        "legacy_metric_event_count": max(0, len(correlated) - len(metric_events)),
        "unmatched_marker_count": max(0, len(markers) - len(correlated)),
        "marker_correlation_completeness_pct": round(marker_completeness, 2),
        "model_facing_metric_coverage": metric_coverage,
        "model_facing_metric_completeness_pct": round(metric_completeness, 2),
        "first_observed_in_process_count": len(first_observed),
        "duplicate_events_ignored": duplicates,
        "retained_transform_est_tokens_before": before,
        "retained_transform_est_tokens_after": after,
        "retained_transform_est_tokens_saved": saved,
        "service_internal_tokens_saved": service_saved,
        "metric_scope": "retained_tool_transforms_in_request",
        "token_estimator": "chars_div4_ceil",
        "counts_as_new_savings": False,
        "full_request_counterfactual_available": False,
    }


def _headroom_proxy_summary_line() -> str:
    """Compact Headroom proxy savings line for llm-monitor.

    Local loopback read only; fail-silent so monitor never blocks model turns.
    This reports provider-route/proxy compression, distinct from tool-output
    middleware events in `_headroom_turn_summary_line`.
    """
    try:
        if not _read_state().get("headroom_summary", True):
            return ""
        proxy = os.getenv("HEADROOM_PROXY_URL") or "http://127.0.0.1:28787"
        proxy = proxy.rstrip("/")
        with urlopen(proxy + "/stats", timeout=0.75) as resp:  # nosec B310 owner-local loopback/read-only
            data = json.loads(resp.read(200_000).decode("utf-8", errors="replace"))
        tokens = data.get("tokens") or {}
        display = (data.get("persistent_savings") or {}).get("display_session") or {}
        requests = data.get("requests") or {}
        saved = _safe_int(display.get("tokens_saved") or tokens.get("saved") or tokens.get("proxy_compression_saved"))
        pct = display.get("savings_percent", tokens.get("savings_percent", tokens.get("proxy_savings_percent")))
        reqs = _safe_int(display.get("requests") or requests.get("total"))
        failed = _safe_int(requests.get("failed"))
        if not saved and pct in (None, "", 0, 0.0):
            return ""
        try:
            pct_txt = f"{float(pct):.1f}%"
        except Exception:
            pct_txt = "?%"
        parts = [f"proxy saved `{_human_count(saved)}`", f"rate `{pct_txt}`"]
        if reqs:
            parts.append(f"req `{reqs}`")
        if failed:
            parts.append(f"failed `{failed}`")
        return "**HR proxy:** " + " · ".join(parts)
    except Exception:
        return ""


def _headroom_turn_summary_line(stats: Dict[str, Any]) -> str:
    """Compact Headroom line for llm-monitor turn-summary rail.

    Reads local JSONL only; never calls the LLM, proxy, gateway, or external telemetry.
    """
    try:
        if not _read_state().get("headroom_summary", True):
            return ""
        turn_id = str(stats.get("turn_id") or "").strip()
        session_id = str(stats.get("session_id") or "").strip()
        task_id = str(stats.get("task_id") or "").strip()
        events = _headroom_event_tail()
        if not events:
            return ""
        scoped: List[Dict[str, Any]] = []
        for event in events:
            event_turn = str(event.get("turn_id") or "").strip()
            event_session = str(event.get("session_id") or "").strip()
            event_task = str(event.get("task_id") or "").strip()
            # Prefer the precise turn id. In live gateway sessions ``task_id``
            # can be session-wide, so OR-ing task matches with a real turn id
            # leaks earlier-turn events into the current bubble.
            if turn_id:
                turn_match = event_turn == turn_id
            else:
                turn_match = bool(task_id and event_task == task_id)
            if not turn_match:
                continue
            if session_id and event_session and event_session != session_id:
                continue
            scoped.append(event)
        if not scoped:
            return ""
        scoped, duplicate_count = _dedupe_headroom_events(scoped)
        counts: Dict[str, int] = {}
        lanes: Dict[str, int] = {}
        service_saved = 0
        model_before = 0
        model_after = 0
        model_saved = 0
        for event in scoped:
            action = str(event.get("action") or "unknown")
            lane = str(event.get("lane") or "unknown")
            counts[action] = counts.get(action, 0) + 1
            lanes[lane] = lanes.get(lane, 0) + 1
            if action != "compressed":
                continue
            service_saved += _safe_int(event.get("service_tokens_saved") or event.get("tokens_saved"))
            if event.get("new_savings_event") is False:
                continue
            if event.get("model_facing_est_tokens_before") is not None:
                model_before += _safe_int(event.get("model_facing_est_tokens_before"))
                model_after += _safe_int(event.get("model_facing_est_tokens_after"))
                model_saved += _safe_int(event.get("model_facing_est_tokens_saved"))
        lane_txt = ",".join(lane for lane, _ in sorted(lanes.items(), key=lambda item: item[1], reverse=True)[:3]) or "—"
        compressed_count = counts.get("compressed", 0)
        exact_count = counts.get("exact", 0)
        blocked_count = counts.get("blocked", 0)
        skipped_count = counts.get("skipped", 0)
        issue_count = counts.get("runtime_unavailable", 0) + counts.get("error", 0)
        if model_before:
            pct = (model_saved / model_before * 100.0) if model_before else 0.0
            parts = [
                "tool-output used",
                f"`{_human_count(model_before)}→{_human_count(model_after) if model_after else '0'}`",
                f"saved `{_human_count(model_saved) if model_saved else '0'}` (`{pct:.1f}%`, est.)",
                f"compressed `{compressed_count}`",
            ]
        elif compressed_count or service_saved:
            parts = [
                "tool-output used",
                f"internal saved `{_human_count(service_saved) if service_saved else '0'}`",
                f"compressed `{compressed_count}`",
            ]
        elif issue_count:
            parts = [f"tool-output issues `{issue_count}`", f"exact/skipped `{exact_count + skipped_count}`"]
        elif blocked_count:
            parts = ["tool-output no compression", f"safety-blocked `{blocked_count}`"]
        else:
            safe_count = exact_count + skipped_count
            parts = ["tool-output ready", f"exact/skipped `{safe_count}`", "no compressed eligible output"]
        if compressed_count or model_before or service_saved:
            if exact_count or skipped_count:
                parts.append(f"exact/skipped `{exact_count + skipped_count}`")
            if blocked_count:
                parts.append(f"safety-blocked `{blocked_count}`")
        if issue_count and not parts[0].startswith("tool-output issues"):
            parts.append(f"issues `{issue_count}`")
        if duplicate_count:
            parts.append(f"dupes ignored `{duplicate_count}`")
        parts.append(f"lanes `{lane_txt}`")
        return "**HR:** " + " · ".join(parts)
    except Exception:
        return ""


def _turn_summary_message(stats: Dict[str, Any], *, phase: str) -> str:
    started = _safe_int(stats.get("started"))
    completed = _safe_int(stats.get("completed"))
    max_input = _safe_int(stats.get("max_input_tokens"))
    provider = stats.get("provider") or "unknown-provider"
    model = stats.get("model") or "unknown-model"
    duration = stats.get("duration_s")
    duration_txt = f"{float(duration):.1f}s" if isinstance(duration, (int, float)) and duration else "—"
    output_sum = _safe_int(stats.get("output_tokens_sum"))
    cache_sum = _safe_int(stats.get("cache_read_tokens_sum"))
    finish = stats.get("finish_reason") or "—"
    done = phase == "post" and started and completed >= started
    title = "✅ **LLM completado**" if done else "⏳ **LLM activo**"
    calls = f"{completed}/{started or '?'}"
    message = (
        f"{title} · `{calls}` · `{duration_txt}`\n"
        f"**Tokens:** in `{_human_count(max_input)}` · outΣ `{_human_count(output_sum) if output_sum else '—'}` · cacheΣ `{_human_count(cache_sum) if cache_sum else '—'}`\n"
        f"**Estado:** finish `{finish}`\n"
        f"**Modelo:** `{provider}/{model}`"
    )
    headroom_proxy_line = _headroom_proxy_summary_line()
    if headroom_proxy_line:
        message += "\n" + headroom_proxy_line
    headroom_line = _headroom_turn_summary_line(stats)
    if headroom_line:
        message += "\n" + headroom_line
    return message



def _strip_runtime_noise(text: str) -> str:
    """Remove gateway/runtime wrapper notes before owner-facing previews."""
    if not text:
        return ""
    # Telegram/gateway injects this after restarts; it is operational noise, not
    # the owner ask we want to surface in the [LLM CALL] card.
    text = re.sub(
        r"^\[System note: Your previous turn in this session was interrupted by a gateway shutdown\..*?message below\.\]\s*",
        "",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"^\[OUT-OF-BAND USER MESSAGE —.*?\[/OUT-OF-BAND USER MESSAGE\]\s*",
        "",
        text,
        flags=re.DOTALL,
    )
    return text.strip()


def _preview_text(value: Any, limit: int = 220) -> str:
    """Compact single-line preview from already-local data; no LLM calls."""
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = str(value)
    text = redact_sensitive_text(_strip_runtime_noise(value))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _content_from_message(msg: Any) -> str:
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                val = item.get("text") or item.get("content") or item.get("input_text")
                if isinstance(val, str):
                    parts.append(val)
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(parts)
    return ""


def _request_prompt_preview(kwargs: Dict[str, Any], limit: int = 240) -> str:
    """Return what matters to the owner: the latest user/developer ask preview."""
    api_id = str(kwargs.get("api_request_id") or "")
    if api_id and api_id in _REQUEST_PREVIEWS:
        return _preview_text(_REQUEST_PREVIEWS.get(api_id, ""), limit)
    direct = kwargs.get("user_message")
    if isinstance(direct, str) and direct.strip():
        return _preview_text(direct, limit)
    msgs = kwargs.get("request_messages")
    if not isinstance(msgs, list):
        req = kwargs.get("request")
        body = req.get("body") if isinstance(req, dict) and isinstance(req.get("body"), dict) else {}
        msgs = body.get("messages") if isinstance(body.get("messages"), list) else []
    for msg in reversed(msgs or []):
        if isinstance(msg, dict) and msg.get("role") in {"user", "developer", "system"}:
            preview = _content_from_message(msg)
            if preview.strip():
                return _preview_text(preview, limit)
    return ""


def _tool_call_preview_from_message(msg: Any, limit: int = 180) -> str:
    tool_calls = None
    if isinstance(msg, dict):
        tool_calls = msg.get("tool_calls")
    else:
        tool_calls = getattr(msg, "tool_calls", None)
    if not tool_calls:
        return ""
    names: List[str] = []
    for call in tool_calls[:4] if isinstance(tool_calls, list) else []:
        name = ""
        if isinstance(call, dict):
            fn = call.get("function") or {}
            name = fn.get("name") if isinstance(fn, dict) else ""
            name = name or call.get("name") or call.get("type") or "tool"
        else:
            fn = getattr(call, "function", None)
            name = getattr(fn, "name", "") or getattr(call, "name", "") or getattr(call, "type", "") or "tool"
        names.append(str(name))
    extra = "…" if isinstance(tool_calls, list) and len(tool_calls) > 4 else ""
    return _preview_text("tool_calls=" + ", ".join(names) + extra, limit)


def _response_preview(kwargs: Dict[str, Any], limit: int = 260) -> str:
    """Extract a small assistant preview from post_api_request kwargs locally."""
    response = kwargs.get("response")
    candidates: List[Any] = []
    tool_preview = ""
    if isinstance(response, dict):
        candidates.extend([
            response.get("content"),
            response.get("text"),
            response.get("output_text"),
        ])
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict):
                candidates.append(msg.get("content"))
                tool_preview = tool_preview or _tool_call_preview_from_message(msg)
        output = response.get("output")
        if isinstance(output, list):
            for item in output:
                if isinstance(item, dict):
                    candidates.append(item.get("content") or item.get("text"))
    else:
        for attr in ("content", "text", "output_text"):
            candidates.append(getattr(response, attr, None))
        choices = getattr(response, "choices", None)
        if isinstance(choices, list) and choices:
            msg = getattr(choices[0], "message", None)
            candidates.append(getattr(msg, "content", None))
            tool_preview = tool_preview or _tool_call_preview_from_message(msg)
    for val in candidates:
        if isinstance(val, str) and val.strip():
            return _preview_text(val, limit)
        if isinstance(val, list):
            joined = " ".join(_content_from_message(x) if isinstance(x, dict) else str(x) for x in val)
            if joined.strip():
                return _preview_text(joined, limit)
    if tool_preview:
        return tool_preview
    tool_calls = kwargs.get("assistant_tool_call_count")
    if tool_calls:
        return f"tool_calls={tool_calls}"
    return ""


def _api_status_key(kwargs: Dict[str, Any], api_request_id: str = "") -> str:
    """Status key per model call; retained for opt-in per-call visibility mode."""
    api_id = api_request_id or str(kwargs.get("api_request_id") or "")
    if api_id:
        safe = re.sub(r"[^A-Za-z0-9_.:-]+", "-", api_id).strip("-._:")
        return f"llm-monitor-call:{safe[:96]}"
    turn = str(kwargs.get("turn_id") or "unknown")
    call_no = str(kwargs.get("api_call_count") or "?")
    safe_turn = re.sub(r"[^A-Za-z0-9_.:-]+", "-", turn).strip("-._:")
    return f"llm-monitor-call:{safe_turn}:api:{call_no}"


def _visible_turn_key(kwargs: Dict[str, Any]) -> str:
    return _turn_key(kwargs.get("session_id"), kwargs.get("turn_id"), kwargs.get("task_id"))


def _safe_positive_int(value: Any, default: int) -> int:
    try:
        out = int(value)
        return out if out > 0 else default
    except Exception:
        return default


def _visible_pre_call_allowed(state: Dict[str, Any], kwargs: Dict[str, Any]) -> Tuple[bool, str]:
    """Local-only guardrail for visible cards; never calls the LLM."""
    if not state.get("enabled") or not state.get("visible_pre_call"):
        return False, "disabled"
    if not state.get("local_visibility", True):
        return False, "local_visibility_off"
    turn_key = _visible_turn_key(kwargs)
    max_turn = _safe_positive_int(state.get("max_visible_calls_per_turn"), 3)
    max_minute = _safe_positive_int(state.get("max_visible_calls_per_minute"), 10)
    now = time.time()
    with _LOCK:
        # Keep one-minute window bounded.
        cutoff = now - 60.0
        while _VISIBLE_CALL_TIMES and _VISIBLE_CALL_TIMES[0] < cutoff:
            _VISIBLE_CALL_TIMES.pop(0)
        if _visible_mode(state) != "turn_summary":
            if _VISIBLE_CALLS_BY_TURN.get(turn_key, 0) >= max_turn:
                return False, f"turn_limit_{max_turn}"
            _VISIBLE_CALLS_BY_TURN[turn_key] = _VISIBLE_CALLS_BY_TURN.get(turn_key, 0) + 1
        if len(_VISIBLE_CALL_TIMES) >= max_minute:
            return False, f"minute_limit_{max_minute}"
        _VISIBLE_CALL_TIMES.append(now)
    return True, "ok"


def _remember_visible_api(api_request_id: str) -> None:
    if not api_request_id:
        return
    with _LOCK:
        _VISIBLE_API_IDS.add(api_request_id)
        if len(_VISIBLE_API_IDS) > 300:
            for key in list(_VISIBLE_API_IDS)[:80]:
                _VISIBLE_API_IDS.discard(key)


def _visible_api_was_sent(api_request_id: str) -> bool:
    if not api_request_id:
        return False
    with _LOCK:
        return api_request_id in _VISIBLE_API_IDS


def _disable_visible_due_to_error(reason: str) -> None:
    state = _read_state()
    if not state.get("disable_visibility_on_error", True):
        return
    state.update({
        "visible_pre_call": False,
        "local_visibility": False,
        "visible_disabled_at": _utc_now(),
        "visible_disabled_reason": reason,
    })
    _write_state(state)


def _notify_visible_post_call(**kwargs: Any) -> None:
    state = _read_state()
    if not state.get("enabled") or not state.get("visible_pre_call") or not state.get("local_visibility", True):
        return
    platform = str(kwargs.get("platform") or "").strip().lower()
    if not platform or platform in {"cli", "local"}:
        return
    try:
        from gateway.session_context import get_session_env
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID") or ""
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID") or ""
    except Exception:
        chat_id = ""
        thread_id = ""
    if not chat_id:
        return
    api_request_id = str(kwargs.get("api_request_id") or "")
    mode = _visible_mode(state)
    if not _visible_api_was_sent(api_request_id) and mode != "turn_summary":
        return
    call_no = kwargs.get("api_call_count") or "?"
    model = kwargs.get("model") or kwargs.get("response_model") or "unknown-model"
    provider = kwargs.get("provider") or "unknown-provider"
    duration = kwargs.get("api_duration")
    finish = kwargs.get("finish_reason") or "?"
    duration_txt = f" · {float(duration):.1f}s" if isinstance(duration, (int, float)) else ""
    if mode == "turn_summary":
        key = _turn_stats_key(kwargs)
        with _LOCK:
            has_turn_marker = key in _VISIBLE_TURN_STATS
        if not has_turn_marker and not _visible_api_was_sent(api_request_id):
            return
        stats = _update_turn_stats_post(kwargs)
        message = _turn_summary_message(stats, phase="post")
        status_key = _turn_status_key(kwargs)
    else:
        message = f"LLM completado · llamada `{call_no}` · finish `{finish}`{duration_txt} · `{provider}/{model}`"
        status_key = _api_status_key(kwargs, api_request_id)
    ok = _schedule_gateway_status_marker(
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
        status_key=status_key,
        message=message,
    )
    if not ok:
        _disable_visible_due_to_error("post_status_unavailable")


def _schedule_gateway_status_marker(
    *,
    platform: str,
    chat_id: str,
    thread_id: str,
    status_key: str,
    message: str,
) -> bool:
    """Best-effort editable status delivery through the live gateway adapter.

    Telegram supports ``send_or_update_status``: the first call sends a small
    bubble and later calls for the same key edit it in place.  That preserves
    pre-call visibility without flooding the chat with one standalone message
    per API call.  If the live gateway/adapter is unavailable, the caller can
    fall back to the generic ``send_message`` path.
    """
    try:
        from agent.async_utils import safe_schedule_threadsafe
        from gateway.config import Platform
        from gateway.run import _gateway_runner_ref, _send_or_update_status_coro

        runner = _gateway_runner_ref()
        if runner is None:
            return False
        loop = getattr(runner, "_gateway_loop", None)
        if loop is None or loop.is_closed():
            return False
        try:
            platform_key = Platform(platform)
        except Exception:
            return False
        adapter = getattr(runner, "adapters", {}).get(platform_key)
        if adapter is None:
            return False
        metadata: Optional[Dict[str, Any]] = {"thread_id": thread_id} if thread_id else None
        fut = safe_schedule_threadsafe(
            _send_or_update_status_coro(adapter, chat_id, status_key, message, metadata),
            loop,
        )
        return fut is not None
    except Exception:
        return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")


def _safe_part(value: Any, max_len: int = 32) -> str:
    text = str(value or "unknown").strip() or "unknown"
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-._") or "unknown"
    return text[:max_len]


def _ensure_dirs() -> None:
    _TRACE_DIR.mkdir(parents=True, exist_ok=True)
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_MONITOR_DIR, 0o700)
        os.chmod(_TRACE_DIR, 0o700)
        os.chmod(_REPORT_DIR, 0o700)
    except Exception:
        pass


def _maybe_prune_traces(state: Dict[str, Any], *, now_ts: float | None = None, force: bool = False) -> Dict[str, int]:
    """Bounded inline retention; never spawns a watcher/background loop."""
    global _LAST_RETENTION_CHECK
    now_value = float(now_ts if now_ts is not None else time.time())
    with _LOCK:
        if not force and now_value - _LAST_RETENTION_CHECK < 3600:
            return {"checked": 0, "deleted": 0}
        _LAST_RETENTION_CHECK = now_value
        _ensure_dirs()
        retention_days = _safe_positive_int(state.get("retention_days"), 14)
        max_files = _safe_positive_int(state.get("max_trace_files"), 2000)
        delete_cap = _safe_positive_int(state.get("retention_max_deletes_per_check"), 200)
        protected = {path.resolve() for path in _TURN_FILES.values()}
        files = sorted(
            (path for path in _TRACE_DIR.glob("*.jsonl") if path.resolve() not in protected),
            key=lambda path: path.stat().st_mtime if path.exists() else now_value,
        )
        cutoff = now_value - retention_days * 86400
        victims = [path for path in files if path.stat().st_mtime < cutoff]
        victim_set = set(victims)
        remaining = [path for path in files if path not in victim_set]
        if len(remaining) > max_files:
            victims.extend(remaining[:len(remaining) - max_files])
        deleted = 0
        for path in victims[:delete_cap]:
            try:
                path.unlink()
                deleted += 1
            except (FileNotFoundError, OSError):
                continue
        return {"checked": len(files), "eligible": len(victims), "deleted": deleted}


def _read_state() -> Dict[str, Any]:
    with _LOCK:
        if not _STATE_FILE.exists():
            return dict(_DEFAULT_STATE)
        try:
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return dict(_DEFAULT_STATE)
            state = dict(_DEFAULT_STATE)
            state.update(data)
            if state.get("local_visibility_mode") == "minimal_call_and_tokens_only_pending_gateway_restart":
                state["local_visibility"] = True
                state["local_visibility_mode"] = "minimal_call_and_tokens_only"
            mode = str(state.get("mode") or "full").lower()
            state["mode"] = "metadata" if mode == "metadata" else "full"
            state["enabled"] = bool(state.get("enabled"))
            for key in ("visible_pre_call", "final_overlay", "local_visibility", "fallback_send_message", "disable_visibility_on_error", "headroom_summary", "strict_metadata"):
                state[key] = bool(state.get(key))
            state["max_visible_calls_per_turn"] = _safe_positive_int(state.get("max_visible_calls_per_turn"), 3)
            state["max_visible_calls_per_minute"] = _safe_positive_int(state.get("max_visible_calls_per_minute"), 10)
            state["retention_days"] = _safe_positive_int(state.get("retention_days"), 14)
            state["max_trace_files"] = _safe_positive_int(state.get("max_trace_files"), 2000)
            state["retention_max_deletes_per_check"] = _safe_positive_int(state.get("retention_max_deletes_per_check"), 200)
            return state
        except Exception:
            return dict(_DEFAULT_STATE)


def _write_state(state: Dict[str, Any]) -> None:
    with _LOCK:
        _ensure_dirs()
        payload = dict(_DEFAULT_STATE)
        payload.update(state)
        tmp = _STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, _STATE_FILE)
        try:
            os.chmod(_STATE_FILE, 0o600)
        except Exception:
            pass


def _json_redacted(value: Any) -> Any:
    """Best-effort JSON-safe redaction for local traces."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    text = redact_sensitive_text(text)
    try:
        return json.loads(text)
    except Exception:
        return text


def _compact_request(request: Any) -> Dict[str, Any]:
    if not isinstance(request, dict):
        return {"present": bool(request)}
    body = request.get("body") if isinstance(request.get("body"), dict) else {}
    out: Dict[str, Any] = {
        "method": request.get("method"),
        "body_keys": sorted([str(k) for k in body.keys()])[:50],
    }
    if isinstance(body, dict):
        for key in ("model", "max_tokens", "temperature", "stream"):
            if key in body:
                out[key] = body.get(key)
        messages = body.get("messages") or body.get("input")
        if isinstance(messages, list):
            out["message_count"] = len(messages)
    return out


def _turn_key(session_id: Any, turn_id: Any, task_id: Any = "") -> str:
    return f"{session_id or 'no-session'}::{turn_id or task_id or 'no-turn'}"


def _trace_file(session_id: Any, turn_id: Any, platform: Any, task_id: Any = "") -> Path:
    key = _turn_key(session_id, turn_id, task_id)
    with _LOCK:
        existing = _TURN_FILES.get(key)
        if existing is not None:
            return existing
        _ensure_dirs()
        filename = (
            f"{_stamp()}-"
            f"{_safe_part(platform, 16)}-"
            f"{_safe_part(session_id, 18)}-"
            f"{_safe_part(turn_id or task_id, 18)}-"
            "llm-monitor.jsonl"
        )
        path = _TRACE_DIR / filename
        _TURN_FILES[key] = path
        return path


def _append_event(path: Path, event: Dict[str, Any]) -> None:
    with _LOCK:
        _ensure_dirs()
        event.setdefault("ts", _utc_now())
        line = json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass


def _notify_visible_pre_call(**kwargs: Any) -> None:
    """Best-effort owner-visible marker emitted before the provider request.

    The final `[LLM CALL]` overlay is necessarily post-hoc because it is added
    to the completed response. For messaging gateway sessions, this optional
    path emits a tiny side-channel message before the API request starts.
    Failures are recorded locally and never interrupt the LLM call.
    """
    state = _read_state()
    if not state.get("enabled") or not state.get("visible_pre_call"):
        return

    platform = str(kwargs.get("platform") or "").strip().lower()
    if not platform or platform in {"cli", "local"}:
        return

    api_request_id = str(kwargs.get("api_request_id") or "")
    if not api_request_id:
        return
    allowed, guard_reason = _visible_pre_call_allowed(state, kwargs)
    if not allowed:
        try:
            path = _trace_file(kwargs.get("session_id"), kwargs.get("turn_id"), platform, kwargs.get("task_id"))
            _append_event(path, {
                "type": "llm_visible_marker_suppressed",
                "api_request_id": api_request_id,
                "platform": platform,
                "reason": guard_reason,
            })
        except Exception:
            pass
        return
    with _LOCK:
        if api_request_id in _PRECALL_NOTIFY_SENT:
            return
        _PRECALL_NOTIFY_SENT.add(api_request_id)

    try:
        from gateway.session_context import get_session_env
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID") or ""
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID") or ""
    except Exception:
        chat_id = ""
        thread_id = ""

    if not chat_id:
        return

    try:
        call_no = kwargs.get("api_call_count") or "?"
        model = kwargs.get("model") or "unknown-model"
        provider = kwargs.get("provider") or "unknown-provider"
        tokens = kwargs.get("approx_input_tokens") or "?"
        mode = _visible_mode(state)
        if mode == "turn_summary":
            stats = _update_turn_stats_pre(kwargs)
            message = _turn_summary_message(stats, phase="pre")
            status_key = _turn_status_key(kwargs)
            delivery_name = "gateway_status_turn_summary"
        else:
            message = _format_pre_call_message(provider=provider, model=model, call_no=call_no, tokens=tokens)
            status_key = _api_status_key(kwargs, api_request_id)
            delivery_name = "gateway_status_per_call"
        target = f"{platform}:{chat_id}:{thread_id}" if thread_id else f"{platform}:{chat_id}"
        delivered_as_status = _schedule_gateway_status_marker(
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            status_key=status_key,
            message=message,
        )
        if delivered_as_status:
            _remember_visible_api(api_request_id)
            result = {"scheduled": True, "delivery": delivery_name, "status_key": status_key}
        elif state.get("fallback_send_message", False) and mode != "turn_summary":
            from tools.send_message_tool import send_message_tool
            fallback_text = f"LLM activo · llamada {call_no} · in≈{tokens} · {provider}/{model}"
            result_raw = send_message_tool({"action": "send", "target": target, "message": fallback_text})
            _remember_visible_api(api_request_id)
            try:
                result = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
            except Exception:
                result = {"raw": str(result_raw)[:500]}
        else:
            result = {"scheduled": False, "delivery": "suppressed_no_status", "status_key": status_key}
            _disable_visible_due_to_error("pre_status_unavailable")
        path = _trace_file(kwargs.get("session_id"), kwargs.get("turn_id"), platform, kwargs.get("task_id"))
        visibility_event: Dict[str, Any] = {
            "type": "llm_pre_call_visible_marker",
            "api_request_id": api_request_id,
            "session_id": kwargs.get("session_id"),
            "turn_id": kwargs.get("turn_id"),
            "platform": platform,
        }
        if state.get("mode") == "metadata" and state.get("strict_metadata"):
            visibility_event["delivery"] = result.get("delivery") if isinstance(result, dict) else "unknown"
            visibility_event["scheduled"] = bool(result.get("scheduled")) if isinstance(result, dict) else False
        else:
            visibility_event["target"] = target
            visibility_event["send_result"] = _json_redacted(result)
        _append_event(path, visibility_event)
    except Exception as exc:
        try:
            path = _trace_file(kwargs.get("session_id"), kwargs.get("turn_id"), platform, kwargs.get("task_id"))
            _append_event(path, {
                "type": "llm_pre_call_visible_marker_error",
                "api_request_id": api_request_id,
                "platform": platform,
                "error": f"{type(exc).__name__}: {redact_sensitive_text(str(exc))}",
            })
        except Exception:
            pass


def _maybe_write_root(path: Path, *, session_id: Any, task_id: Any, turn_id: Any, platform: Any, user_message: Any, strict_metadata: bool = False) -> None:
    key = _turn_key(session_id, turn_id, task_id)
    with _LOCK:
        if key in _TURN_ROOT_WRITTEN:
            return
        _TURN_ROOT_WRITTEN.add(key)
    event: Dict[str, Any] = {
        "type": "owner_request",
        "session_id": session_id,
        "task_id": task_id,
        "turn_id": turn_id,
        "platform": platform,
    }
    if strict_metadata:
        event["user_message_chars"] = len(str(user_message or ""))
    else:
        event["user_message"] = redact_sensitive_text(str(user_message or ""))
    _append_event(path, event)


def _is_enabled() -> bool:
    return bool(_read_state().get("enabled"))


def _rough_tokens_from_chars(char_count: int) -> int:
    if char_count <= 0:
        return 0
    return max(1, (char_count + 3) // 4)


def _json_char_len(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")))
    except Exception:
        return len(str(value or ""))


def _remember_static_bucket_sizes(instructions_chars: int, tools_chars: int, tool_count: Any = None) -> None:
    global _LAST_INSTRUCTIONS_CHARS
    with _LOCK:
        if instructions_chars > 0:
            _LAST_INSTRUCTIONS_CHARS = max(_LAST_INSTRUCTIONS_CHARS, instructions_chars)
        count = _safe_int(tool_count)
        if count > 0 and tools_chars > 2:
            _LAST_TOOL_SCHEMA_CHARS_BY_COUNT[count] = max(
                _LAST_TOOL_SCHEMA_CHARS_BY_COUNT.get(count, 0),
                tools_chars,
            )


def _fallback_static_bucket_sizes(tool_count: Any = None) -> Tuple[int, int, str]:
    with _LOCK:
        instructions_chars = _LAST_INSTRUCTIONS_CHARS
        count = _safe_int(tool_count)
        tools_chars = 0
        if count > 0:
            tools_chars = _LAST_TOOL_SCHEMA_CHARS_BY_COUNT.get(count, 0)
        if not tools_chars and _LAST_TOOL_SCHEMA_CHARS_BY_COUNT:
            tools_chars = max(_LAST_TOOL_SCHEMA_CHARS_BY_COUNT.values())
    source = "cached_static" if (instructions_chars or tools_chars) else "unavailable_static"
    return instructions_chars, tools_chars, source


def _request_bucket_estimates(
    request: Any,
    request_char_count: Any = None,
    *,
    request_messages: Any = None,
    tool_count: Any = None,
) -> Dict[str, Any]:
    """Approximate request token attribution without storing raw prompt content."""
    if not isinstance(request, dict):
        return {"total_est_tokens": _rough_tokens_from_chars(_safe_int(request_char_count))}

    body = request.get("body") if isinstance(request.get("body"), dict) else {}
    total_chars = _safe_int(request_char_count)
    if not total_chars:
        total_chars = _json_char_len(request)

    instructions_chars = 0
    messages_chars = 0
    tools_chars = 0
    body_chars = total_chars
    source = "request_body"
    if isinstance(body, dict):
        instructions = body.get("instructions")
        if instructions:
            instructions_chars += len(str(instructions))
        messages = body.get("messages") or body.get("input") or []
        messages_chars = _json_char_len(messages)
        tools_chars = _json_char_len(body.get("tools") or [])
        body_chars = _json_char_len(body)

    if isinstance(request_messages, list) and messages_chars <= 2:
        messages_chars = _json_char_len(request_messages)
        source = "raw_request_messages"

    if instructions_chars or tools_chars > 2:
        _remember_static_bucket_sizes(instructions_chars, tools_chars, tool_count)
    elif messages_chars > 2:
        instructions_chars, tools_chars, static_source = _fallback_static_bucket_sizes(tool_count)
        source = f"{source}+{static_source}"

    known_chars = instructions_chars + messages_chars + tools_chars
    total_chars = max(total_chars, body_chars, known_chars)
    other_chars = max(0, body_chars - known_chars)
    return {
        "total_est_tokens": _rough_tokens_from_chars(total_chars),
        "instructions_est_tokens": _rough_tokens_from_chars(instructions_chars),
        "conversation_est_tokens": _rough_tokens_from_chars(messages_chars),
        "tool_schema_est_tokens": _rough_tokens_from_chars(tools_chars),
        "other_body_est_tokens": _rough_tokens_from_chars(other_chars),
        "total_chars": total_chars,
        "tool_schema_chars": tools_chars,
        "conversation_chars": messages_chars,
        "source": source,
    }


def _parse_limit(args: List[str], default: int = 10, maximum: int = 200) -> int:
    for i, arg in enumerate(args):
        if arg in {"--last", "-n"} and i + 1 < len(args):
            return max(1, min(maximum, _safe_int(args[i + 1]) or default))
        if arg.isdigit():
            return max(1, min(maximum, int(arg)))
    return default


def _latest_trace_files(limit: int) -> List[Path]:
    _ensure_dirs()
    return sorted(
        _TRACE_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )[:limit]


def _read_events(path: Path) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue
            if isinstance(data, dict):
                events.append(data)
    except Exception:
        return []
    return events


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _summarize_trace(path: Path) -> Dict[str, Any]:
    events = _read_events(path)
    owner = next((e for e in events if e.get("type") == "owner_request"), {})
    requests = [e for e in events if e.get("type") == "llm_request"]
    responses = [e for e in events if e.get("type") == "llm_response"]
    errors = [e for e in events if e.get("type") == "llm_error"]

    prompt_tokens = [_safe_int((e.get("usage") or {}).get("prompt_tokens")) for e in responses]
    input_tokens = [_safe_int((e.get("usage") or {}).get("input_tokens")) for e in responses]
    output_tokens = [_safe_int((e.get("usage") or {}).get("output_tokens")) for e in responses]
    cache_read = [_safe_int((e.get("usage") or {}).get("cache_read_tokens")) for e in responses]
    approx = [_safe_int(e.get("approx_input_tokens")) for e in requests]
    tool_counts = [_safe_int(e.get("tool_count")) for e in requests]
    message_counts = [_safe_int(e.get("message_count")) for e in requests]
    bucket_events = [e.get("bucket_estimates") or {} for e in requests if isinstance(e.get("bucket_estimates"), dict)]

    warnings: List[str] = []
    max_prompt = max(prompt_tokens or approx or [0])
    max_tools = max(tool_counts or [0])
    api_calls = len(requests)
    max_messages = max(message_counts or [0])
    if max_prompt >= _RESET_PROMPT_TOKENS:
        warnings.append(f"reset-recommended: prompt_tokens>={_RESET_PROMPT_TOKENS}")
    elif max_prompt >= _WARNING_PROMPT_TOKENS:
        warnings.append(f"warning: prompt_tokens>={_WARNING_PROMPT_TOKENS}")
    if max_tools >= _WARNING_TOOL_COUNT:
        warnings.append(f"broad-tool-surface: tool_count>={_WARNING_TOOL_COUNT}")
    if api_calls > _WARNING_API_CALLS:
        warnings.append(f"tool-loop-cost: api_calls>{_WARNING_API_CALLS}")
    if max_messages >= 80:
        warnings.append("long-session-history: message_count>=80")
    if errors:
        warnings.append("provider-errors-present")

    user_message = str(owner.get("user_message") or "")
    if len(user_message) > 120:
        user_message = user_message[:117] + "..."

    return {
        "trace": str(path),
        "session_id": owner.get("session_id") or (requests[0].get("session_id") if requests else None),
        "turn_id": owner.get("turn_id") or (requests[0].get("turn_id") if requests else None),
        "platform": owner.get("platform") or (requests[0].get("platform") if requests else None),
        "user_preview": user_message,
        "api_calls": api_calls,
        "responses": len(responses),
        "errors": len(errors),
        "first_approx_input_tokens": approx[0] if approx else 0,
        "last_approx_input_tokens": approx[-1] if approx else 0,
        "max_approx_input_tokens": max(approx or [0]),
        "max_prompt_tokens": max(prompt_tokens or [0]),
        "sum_prompt_tokens": sum(prompt_tokens),
        "sum_input_tokens": sum(input_tokens),
        "sum_output_tokens": sum(output_tokens),
        "sum_cache_read_tokens": sum(cache_read),
        "max_tool_count": max_tools,
        "max_message_count": max_messages,
        "bucket_max": {
            "tool_schema_est_tokens": max([_safe_int(b.get("tool_schema_est_tokens")) for b in bucket_events] or [0]),
            "conversation_est_tokens": max([_safe_int(b.get("conversation_est_tokens")) for b in bucket_events] or [0]),
            "instructions_est_tokens": max([_safe_int(b.get("instructions_est_tokens")) for b in bucket_events] or [0]),
            "other_body_est_tokens": max([_safe_int(b.get("other_body_est_tokens")) for b in bucket_events] or [0]),
        },
        "warnings": warnings,
    }


def _write_report(summaries: List[Dict[str, Any]]) -> Path:
    _ensure_dirs()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    path = _REPORT_DIR / f"{ts}-summary.json"
    payload = {
        "generated_at": _utc_now(),
        "trace_count": len(summaries),
        "summaries": summaries,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return path


def _render_report(args: List[str]) -> str:
    limit = _parse_limit(args, default=10)
    summaries = [_summarize_trace(p) for p in _latest_trace_files(limit)]
    if not summaries:
        return "LLM monitor report: no traces yet"
    report_path = _write_report(summaries)
    total_api_calls = sum(_safe_int(s.get("api_calls")) for s in summaries)
    max_prompt = max(_safe_int(s.get("max_prompt_tokens")) for s in summaries)
    max_tools = max(_safe_int(s.get("max_tool_count")) for s in summaries)
    max_messages = max(_safe_int(s.get("max_message_count")) for s in summaries)
    warn_counts: Dict[str, int] = {}
    for s in summaries:
        for warning in s.get("warnings") or []:
            warn_counts[warning] = warn_counts.get(warning, 0) + 1

    lines = [
        f"LLM monitor report · traces={len(summaries)} · api_calls={total_api_calls}",
        f"max_prompt_tokens={max_prompt} · max_tool_count={max_tools} · max_message_count={max_messages}",
        f"saved={report_path}",
    ]
    if warn_counts:
        lines.append("warnings:")
        for warning, count in sorted(warn_counts.items()):
            lines.append(f"- {warning}: {count}")
    lines.append("recent turns:")
    for s in summaries[:5]:
        bucket = s.get("bucket_max") or {}
        lines.append(
            "- "
            f"api={s.get('api_calls')} prompt_max={s.get('max_prompt_tokens')} "
            f"tools={s.get('max_tool_count')} msgs={s.get('max_message_count')} "
            f"buckets(tool≈{bucket.get('tool_schema_est_tokens', 0)}, "
            f"conv≈{bucket.get('conversation_est_tokens', 0)}, "
            f"instr≈{bucket.get('instructions_est_tokens', 0)}) "
            f"user={s.get('user_preview')!r}"
        )
    return "\n".join(lines)



def _render_context_economy(args: List[str]) -> str:
    """Generate a compact llm-monitor → context-economy report.

    Read-only. Uses the richer turn_attribution sidecar script so the slash
    command stays thin and the owner sees only decision-grade signals.
    """
    limit = _parse_limit(args, default=80, maximum=300)
    if not _CONTEXT_REPORT_SCRIPT.exists():
        return f"Context economy report unavailable: missing {_CONTEXT_REPORT_SCRIPT}"
    try:
        proc = subprocess.run(
            [sys.executable, str(_CONTEXT_REPORT_SCRIPT), "--limit", str(limit), "--top-messages", "20"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    except Exception as exc:
        return f"Context economy report failed: {exc}"
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().splitlines()[-8:]
        return "Context economy report failed:\n" + "\n".join(stderr)
    try:
        summary = json.loads(proc.stdout or "{}")
    except Exception:
        return "Context economy report wrote output but summary JSON was unreadable."
    json_path = Path(str(summary.get("json") or ""))
    md_path = Path(str(summary.get("md") or ""))
    payload: Dict[str, Any] = {}
    if json_path.exists():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    rankings = payload.get("rankings") if isinstance(payload.get("rankings"), dict) else {}
    top_turns = rankings.get("by_max_actual_input_tokens") if isinstance(rankings.get("by_max_actual_input_tokens"), list) else []
    top = top_turns[0] if top_turns and isinstance(top_turns[0], dict) else (summary.get("top_turn") if isinstance(summary.get("top_turn"), dict) else {})
    bucket = top.get("max_bucket") if isinstance(top.get("max_bucket"), dict) else {}
    conv = _safe_int(bucket.get("conversation_est_tokens"))
    schema = _safe_int(bucket.get("tool_schema_est_tokens"))
    instr = _safe_int(bucket.get("instructions_est_tokens"))
    max_actual = _safe_int(top.get("max_actual_input_tokens"))
    api_calls = _safe_int(top.get("api_calls"))

    sessions = payload.get("sessions") if isinstance(payload.get("sessions"), dict) else {}
    tool_counter: Dict[str, int] = {}
    call_counter: Dict[str, int] = {}
    for sdata in sessions.values():
        if not isinstance(sdata, dict) or sdata.get("error"):
            continue
        for k, v in (sdata.get("tool_chars") or {}).items():
            tool_counter[str(k)] = tool_counter.get(str(k), 0) + _safe_int(v)
        for k, v in (sdata.get("assistant_tool_calls") or {}).items():
            call_counter[str(k)] = call_counter.get(str(k), 0) + _safe_int(v)
    top_tool_chars = sorted(tool_counter.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top_calls = sorted(call_counter.items(), key=lambda kv: kv[1], reverse=True)[:5]

    if conv > max(schema, instr) * 2:
        primary = "retained conversation/tool outputs"
        next_action = "compress/summarize bulky intermediate outputs; keep exact files for edit-critical work."
    elif schema >= max(conv, instr):
        primary = "fixed tool schema surface"
        next_action = "use scoped worker/fresh-session capability presets only for bounded tasks; do not shrink cockpit globally."
    elif instr >= max(conv, schema):
        primary = "instructions/loaded skills"
        next_action = "prefer compact skill hot paths and avoid loading heavy references unless needed."
    else:
        primary = "mixed"
        next_action = "inspect the generated Markdown before changing runtime config."

    lines = [
        f"Context economy report · traces={summary.get('turn_count')} · sessions={summary.get('session_count')}",
        f"primary_driver={primary}",
        f"top_turn=max_actual≈{max_actual} · api_calls={api_calls} · conv≈{conv} · schema≈{schema} · instr≈{instr}",
    ]
    if top_tool_chars:
        lines.append("top_retained_tool_outputs=" + ", ".join(f"{k}:{v//4}t" for k, v in top_tool_chars))
    if top_calls:
        lines.append("top_tool_calls=" + ", ".join(f"{k}:{v}" for k, v in top_calls))
    lines.extend([
        f"next={next_action}",
        f"md={md_path}",
        f"json={json_path}",
    ])
    return "\n".join(lines)

def _read_platform_toolsets() -> Dict[str, Any]:
    cfg_path = Path(get_hermes_home()) / "config.yaml"
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return data.get("platform_toolsets") or {}
    except Exception as exc:
        return {"_error": str(exc)}


def _detect_capability_mode(toolsets: Any) -> str:
    if not isinstance(toolsets, list):
        return "unknown"
    values = [str(x) for x in toolsets]
    if values == _OWNER_DEFAULT_LIGHT:
        return "owner-default-light"
    if "no_mcp" in values and len(values) <= len(_OWNER_DEFAULT_LIGHT):
        return "light-custom"
    if "browser" in values:
        return "browser"
    if any(x in values for x in ("image_gen", "vision", "video", "tts")):
        return "creative/media"
    return "custom/full-ish"


def handle_capabilities_command(raw_args: str = "") -> str:
    args = (raw_args or "").strip().split()
    action = (args[0].lower() if args else "status")
    if action in {"help", "modes"}:
        lines = ["Capability modes are routing presets, not uninstallers:"]
        for name, meta in _CAPABILITY_PRESETS.items():
            lines.append(f"- {name}: {meta['intent']} Risk: {meta['risk']}")
        lines.append("apply is intentionally gated: this command reports/recommends but does not shrink capabilities automatically.")
        return "\n".join(lines)

    if action == "recommend":
        text = " ".join(args[1:]).lower()
        mode = "light"
        if any(k in text for k in ("browser", "web app", "login", "click", "navigate", "chrome")):
            mode = "browser"
        elif any(k in text for k in ("image", "video", "audio", "tts", "vision", "visual", "higgsfield")):
            mode = "creative"
        elif any(k in text for k in ("mcp", "unknown", "todo", "end-to-end", "full")):
            mode = "full"
        elif any(k in text for k in ("solo responde", "ok", "costo", "canary", "prueba")):
            mode = "micro"
        meta = _CAPABILITY_PRESETS[mode]
        return f"recommended={mode}\nintent={meta['intent']}\nrisk={meta['risk']}\nNote: escalate for the task session; do not make micro the global default."

    if action == "apply":
        return (
            "HOLD: automatic capability mutation is intentionally disabled in this owner-local command. "
            "Changing platform_toolsets affects future sessions and can hide needed tools. "
            "Use this command for status/recommendation, then apply config with an explicit scoped change + gateway restart."
        )

    pt = _read_platform_toolsets()
    if "_error" in pt:
        return f"Capabilities status unavailable: {pt['_error']}"
    platforms = ["telegram", "cli", "api_server", "cron", "webhook"]
    lines = ["Capabilities status · reversible config surface · no tools uninstalled"]
    for platform in platforms:
        tools = pt.get(platform)
        mode = _detect_capability_mode(tools)
        count = len(tools) if isinstance(tools, list) else "?"
        lines.append(f"- {platform}: {mode} · toolsets={count} · {tools}")
    lines.append("Use /capabilities modes or /capabilities recommend <task>. Complex tasks should escalate, not struggle in micro mode.")
    return "\n".join(lines)


def handle_command(raw_args: str = "") -> str:
    args = (raw_args or "").strip().split()
    action = (args[0].lower() if args else "status")

    if action == "on":
        mode = "full" if len(args) > 1 and args[1].lower() == "full" else "metadata"
        state = _read_state()
        state.update({
            "enabled": True,
            "mode": mode,
            "activated_at": _utc_now(),
            "final_overlay": False,
            "fallback_send_message": False,
            "disable_visibility_on_error": True,
        })
        _write_state(state)
        return f"[LLM monitor] ON · mode={mode} · traces={_TRACE_DIR}"

    if action == "off":
        state = _read_state()
        state.update({
            "enabled": False,
            "visible_pre_call": False,
            "local_visibility": False,
            "final_overlay": False,
            "deactivated_at": _utc_now(),
        })
        _write_state(state)
        return "LLM monitor OFF"

    if action in {"precall", "visible-precall", "pre-call"}:
        state = _read_state()
        if len(args) > 1 and args[1].lower() in {"on", "true", "1", "yes"}:
            state["visible_pre_call"] = True
            state["local_visibility"] = True
            state["local_visibility_mode"] = "rich_turn_summary"
            state["visible_pre_call_updated_at"] = _utc_now()
            _write_state(state)
            return "LLM monitor visible local status ON · edits one rich in-place turn summary before/after gateway LLM requests"
        if len(args) > 1 and args[1].lower() in {"off", "false", "0", "no"}:
            state["visible_pre_call"] = False
            state["visible_pre_call_updated_at"] = _utc_now()
            _write_state(state)
            return "LLM monitor visible pre-call marker OFF"
        return f"LLM monitor visible local status {'ON' if state.get('visible_pre_call') else 'OFF'} · final_overlay={'ON' if state.get('final_overlay') else 'OFF'}"


    if action in {"visible-mode", "visibility-mode", "status-mode"}:
        state = _read_state()
        if len(args) > 1:
            requested = args[1].lower().strip()
            if requested in {"summary", "turn", "turn-summary", "turn_summary"}:
                state["visible_status_mode"] = "turn_summary"
                _write_state(state)
                return "LLM monitor visible status mode: turn_summary · one editable bubble per turn with final call count"
            if requested in {"per-call", "per_call", "call"}:
                state["visible_status_mode"] = "per_call"
                _write_state(state)
                return "LLM monitor visible status mode: per_call · legacy one marker per API call"
            return "Usage: /llm-monitor visible-mode [summary|per-call]"
        return f"LLM monitor visible status mode: {_visible_mode(state)}"

    if action in {"guardrails", "guards"}:
        state = _read_state()
        return (
            "LLM monitor guardrails\n"
            f"- visible_status_mode={_visible_mode(state)}\n"
            f"- max_visible_calls_per_turn={state.get('max_visible_calls_per_turn')}\n"
            f"- max_visible_calls_per_minute={state.get('max_visible_calls_per_minute')}\n"
            f"- fallback_send_message={state.get('fallback_send_message')}\n"
            f"- disable_visibility_on_error={state.get('disable_visibility_on_error')}\n"
            f"- final_overlay={state.get('final_overlay')}"
        )

    if action == "canary":
        return (
            "[LLM monitor canary] local only · no LLM call. "
            "Safe path: Web-compatible Markdown/edit; native rich/draft remains opt-in only."
        )

    if action == "tail":
        limit = _parse_limit(args[1:], default=5, maximum=50)
        files = _latest_trace_files(limit)
        if not files:
            return "LLM monitor: no traces yet"
        return "LLM monitor recent traces:\n" + "\n".join(str(p) for p in files)

    if action == "report":
        return _render_report(args[1:])

    if action in {"context", "economy", "context-economy", "cost"}:
        return _render_context_economy(args[1:])

    state = _read_state()
    status = "ON" if state.get("enabled") else "OFF"
    return (
        f"LLM monitor {status} · mode={state.get('mode', 'full')} · "
        f"strict_metadata={state.get('strict_metadata')} · retention_days={state.get('retention_days')} · "
        f"visible={state.get('visible_pre_call')} · local_visibility={state.get('local_visibility')} · visible_status_mode={_visible_mode(state)} · "
        f"final_overlay={state.get('final_overlay')} · guardrails=turn:{state.get('max_visible_calls_per_turn')}/min:{state.get('max_visible_calls_per_minute')} · "
        f"traces={_TRACE_DIR} · reports={_REPORT_DIR}"
    )

def on_pre_api_request(**kwargs: Any) -> None:
    state = _read_state()
    if not state.get("enabled"):
        return

    _maybe_prune_traces(state)
    strict_metadata = state.get("mode") == "metadata" and bool(state.get("strict_metadata"))
    path = _trace_file(kwargs.get("session_id"), kwargs.get("turn_id"), kwargs.get("platform"), kwargs.get("task_id"))
    _maybe_write_root(
        path,
        session_id=kwargs.get("session_id"),
        task_id=kwargs.get("task_id"),
        turn_id=kwargs.get("turn_id"),
        platform=kwargs.get("platform"),
        user_message=kwargs.get("user_message"),
        strict_metadata=strict_metadata,
    )

    request = kwargs.get("request")
    event: Dict[str, Any] = {
        "type": "llm_request",
        "api_request_id": kwargs.get("api_request_id"),
        "session_id": kwargs.get("session_id"),
        "task_id": kwargs.get("task_id"),
        "turn_id": kwargs.get("turn_id"),
        "platform": kwargs.get("platform"),
        "provider": kwargs.get("provider"),
        "model": kwargs.get("model"),
        "api_mode": kwargs.get("api_mode"),
        "api_call_count": kwargs.get("api_call_count"),
        "approx_input_tokens": kwargs.get("approx_input_tokens"),
        "request_char_count": kwargs.get("request_char_count"),
        "message_count": kwargs.get("message_count"),
        "tool_count": kwargs.get("tool_count"),
        "max_tokens": kwargs.get("max_tokens"),
        "bucket_estimates": _request_bucket_estimates(
            request,
            kwargs.get("request_char_count"),
            request_messages=kwargs.get("request_messages"),
            tool_count=kwargs.get("tool_count"),
        ),
    }
    if state.get("headroom_summary", True):
        try:
            event["headroom_attribution"] = _headroom_request_attribution(kwargs)
        except Exception:
            event["headroom_attribution"] = {
                "schema_version": "headroom.attribution.v2",
                "coverage": "collector_error",
                "counts_as_new_savings": False,
            }
    prompt_preview = _request_prompt_preview(kwargs)
    if kwargs.get("api_request_id") and prompt_preview:
        with _LOCK:
            _REQUEST_PREVIEWS[str(kwargs.get("api_request_id"))] = prompt_preview
    if strict_metadata:
        event["prompt_preview_chars"] = len(prompt_preview)
    else:
        event["prompt_preview"] = prompt_preview
    if state.get("mode") == "metadata":
        event["request_summary"] = _compact_request(request)
    else:
        event["request"] = _json_redacted(request)
    _append_event(path, event)
    _notify_visible_pre_call(**kwargs)


def on_post_api_request(**kwargs: Any) -> None:
    state = _read_state()
    if not state.get("enabled"):
        return
    strict_metadata = state.get("mode") == "metadata" and bool(state.get("strict_metadata"))
    path = _trace_file(kwargs.get("session_id"), kwargs.get("turn_id"), kwargs.get("platform"), kwargs.get("task_id"))
    event: Dict[str, Any] = {
        "type": "llm_response",
        "api_request_id": kwargs.get("api_request_id"),
        "session_id": kwargs.get("session_id"),
        "task_id": kwargs.get("task_id"),
        "turn_id": kwargs.get("turn_id"),
        "provider": kwargs.get("provider"),
        "model": kwargs.get("model"),
        "response_model": kwargs.get("response_model"),
        "api_call_count": kwargs.get("api_call_count"),
        "api_duration": kwargs.get("api_duration"),
        "finish_reason": kwargs.get("finish_reason"),
        "usage": kwargs.get("usage"),
        "assistant_content_chars": kwargs.get("assistant_content_chars"),
        "assistant_tool_call_count": kwargs.get("assistant_tool_call_count"),
    }
    response_preview = _response_preview(kwargs)
    if strict_metadata:
        event["response_preview_chars"] = len(response_preview)
    else:
        event["response_preview"] = response_preview
    if state.get("mode") != "metadata":
        event["response"] = _json_redacted(kwargs.get("response"))
    _append_event(path, event)
    _notify_visible_post_call(**kwargs)


def on_api_request_error(**kwargs: Any) -> None:
    state = _read_state()
    if not state.get("enabled"):
        return
    path = _trace_file(kwargs.get("session_id"), kwargs.get("turn_id"), kwargs.get("platform"), kwargs.get("task_id"))
    event = {
        "type": "llm_error",
        "api_request_id": kwargs.get("api_request_id"),
        "session_id": kwargs.get("session_id"),
        "task_id": kwargs.get("task_id"),
        "turn_id": kwargs.get("turn_id"),
        "provider": kwargs.get("provider"),
        "model": kwargs.get("model"),
        "api_call_count": kwargs.get("api_call_count"),
        "api_duration": kwargs.get("api_duration"),
        "status_code": kwargs.get("status_code"),
        "retryable": kwargs.get("retryable"),
        "reason": kwargs.get("reason"),
        "error": _json_redacted(kwargs.get("error")),
    }
    _append_event(path, event)


def _llm_call_marker_for_latest_turn(session_id: Any = "") -> str:
    try:
        trace_dir = _TRACE_DIR
        if not trace_dir.exists():
            return "[LLM CALL]"
        session_part = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(session_id or "").strip()).strip("-._")[:18]
        candidates = list(trace_dir.glob(f"*-{session_part}-*-llm-monitor.jsonl")) if session_part else []
        if not candidates:
            candidates = list(trace_dir.glob("*-llm-monitor.jsonl"))
        if not candidates:
            return "[LLM CALL]"
        latest = max(candidates, key=lambda p: p.stat().st_mtime)
        calls = 0
        for line in latest.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                if json.loads(line).get("type") == "llm_request":
                    calls += 1
            except Exception:
                continue
        calls = max(1, min(calls, 8))
        if calls == 1:
            return "[LLM CALL]"
        return "\n".join(f"[LLM CALL {idx}/{calls}]" for idx in range(1, calls + 1))
    except Exception:
        return "[LLM CALL]"


def on_transform_llm_output(response_text: str = "", **kwargs: Any) -> Optional[str]:
    state = _read_state()
    if not response_text or not state.get("enabled") or not state.get("final_overlay"):
        return None
    if response_text.startswith("[LLM CALL"):
        return response_text
    marker = _llm_call_marker_for_latest_turn(kwargs.get("session_id"))
    return f"{marker}\n{response_text}"


def register(ctx) -> None:
    ctx.register_command(
        "llm-monitor",
        handle_command,
        description="Toggle/report owner-local LLM request monitor: /llm-monitor on|off|status|tail|report|context",
        args_hint="on|off|status|tail|report|context [limit]",
    )
    ctx.register_command(
        "capabilities",
        handle_capabilities_command,
        description="Inspect/recommend reversible capability modes without auto-shrinking tools",
        args_hint="status|modes|recommend <task>|apply",
    )
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("api_request_error", on_api_request_error)
    ctx.register_hook("transform_llm_output", on_transform_llm_output)
