"""Slash command handlers."""
from __future__ import annotations

import json
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

from .health import audit
from .hooks import headroom_status_marker, visible_status_marker_enabled
from .proxy import hermes_home, readyz, retrieve_stats, smoke

USAGE = "Usage: /headroom status|smoke|audit|on|runtime|stats|usage [turn [turn_id]]|lanes|tail [n]|decisions [turn [turn_id]]|why [turn [turn_id]]|opportunities"
REPEATED_TERMINAL_BELOW_MIN_CANDIDATE_CHARS = 28_000




def _render_smoke(result: dict) -> str:
    if result.get("ok"):
        return (
            "Headroom smoke PASS · "
            f"tokens_before={result.get('tokens_before')} tokens_after={result.get('tokens_after')} "
            f"saved={result.get('tokens_saved')} marker={result.get('marker')} "
            f"retrieve_count={result.get('retrieve_count')} sentinel_found={result.get('sentinel_found')}"
        )
    return f"Headroom smoke FAIL · phase={result.get('phase')} · proxy={result.get('proxy_url')} · error={result.get('error', 'unknown')}"


def _render_status(health: dict) -> str:
    body = health.get("body")
    detail = ""
    if not health.get("ok") and body:
        detail_text = str(body).replace("\n", " ")
        if len(detail_text) > 180:
            detail_text = detail_text[:177] + "..."
        detail = f" · detail={detail_text}"
    marker_state = "on" if visible_status_marker_enabled() else "off"
    marker = headroom_status_marker(health) if marker_state == "on" else "disabled"
    return f"Headroom status · ok={health['ok']} · proxy={health['proxy_url']} · status={health['status']} · visible_marker={marker_state}:{marker}{detail}"



def _headroom_event_log_path() -> Path:
    return hermes_home() / "control-plane" / "headroom" / "events" / "headroom-events.jsonl"


def _read_headroom_events(*, limit: int = 2000) -> tuple[list[dict[str, Any]], Path]:
    """Read recent local Headroom observability events.

    Reader is intentionally read-only: it does not create the event directory or
    mutate runtime/proxy state. Malformed lines are skipped so a partial write
    cannot break slash command UX.
    """
    path = _headroom_event_log_path()
    if not path.exists():
        return [], path
    max_lines = max(1, min(int(limit or 2000), 10000))
    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in deque(fh, maxlen=max_lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("type") == "headroom_tool_result":
                    events.append(event)
    except OSError:
        return [], path
    return events, path


def _safe_cell(value: Any, *, limit: int = 80) -> str:
    text = str(value or "").replace("\n", " ").replace("\r", " ").strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _event_int(event: dict[str, Any], key: str) -> int:
    value = event.get(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _terminal_below_min_candidates(events: list[dict[str, Any]], *, platform: str = "telegram") -> tuple[list[dict[str, Any]], list[tuple[str, dict[str, int]]]]:
    """Group repeated terminal below-min events for S8F read-only probing.

    This is intentionally metadata-only. It does not lower middleware thresholds,
    read raw payloads, create sidecars, or call the runtime. A candidate means a
    single turn accumulated enough small terminal chunks that an adaptive,
    per-turn strategy might be worth a fixture/canary later.
    """
    scoped = [
        event for event in events
        if event.get("tool_name") == "terminal"
        and event.get("action") == "skipped"
        and event.get("reason") == "below_min_chars"
        and (not platform or _safe_cell(event.get("platform"), limit=40) == platform)
    ]
    by_turn: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "chars": 0, "max": 0, "min": 0})
    for event in scoped:
        turn = _safe_cell(event.get("turn_id"), limit=120) or "no_turn"
        size = _event_int(event, "original_chars")
        row = by_turn[turn]
        row["count"] += 1
        row["chars"] += size
        row["max"] = max(row["max"], size)
        row["min"] = size if not row["min"] else min(row["min"], size)
    candidates = sorted(
        (
            (turn, data)
            for turn, data in by_turn.items()
            if data["count"] >= 2 and data["chars"] >= REPEATED_TERMINAL_BELOW_MIN_CANDIDATE_CHARS
        ),
        key=lambda item: (item[1]["chars"], item[1]["count"]),
        reverse=True,
    )
    return scoped, candidates


def _effective_lane(event: dict[str, Any]) -> str:
    """Return a useful display lane without rewriting the retained event."""
    lane = _safe_cell(event.get("lane"), limit=60) or "unknown"
    if lane != "unknown":
        return lane
    tool = (_safe_cell(event.get("tool_name"), limit=80) or "").lower()
    if tool in {"read_file", "search_files"}:
        return "file"
    if tool in {"web_extract", "session_search", "x_search"}:
        return "research"
    if tool in {"patch", "write_file", "mcp_open_design_write_file"}:
        return "edit"
    if tool in {"skill_view", "skill_manage"}:
        return "skill"
    if tool in {"memory", "fact_store", "todo"}:
        return "state"
    if tool == "process":
        return "process"
    if tool.startswith("mcp_open_design_"):
        return "artifact"
    if tool.startswith("browser_"):
        return "browser"
    if tool.startswith("kanban"):
        return "kanban"
    return lane


def _reason_family(action: str, reason: str) -> str:
    reason_l = (reason or "").lower()
    if action == "compressed":
        return "compressed"
    if action == "runtime_unavailable" or "proxy_not_ready" in reason_l:
        return "runtime_unavailable"
    if action == "blocked" or "protected" in reason_l or "secret" in reason_l or "header_missing" in reason_l:
        return "safety_blocked"
    if reason_l.startswith("exact_tool") or reason_l in {"patch_diff", "final_or_claim_ledger", "browser_vision_final_default_exact", "exact_command"}:
        return "safety_exact"
    if "below_min_chars" in reason_l:
        return "below_min"
    if "not_intermediate_lane" in reason_l:
        return "not_intermediate"
    if "compression_not_useful" in reason_l:
        return "compression_not_useful"
    if "already_compressed" in reason_l:
        return "already_compressed"
    return action or "unknown"


def _summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    actions = Counter(_safe_cell(event.get("action"), limit=40) or "unknown" for event in events)
    lanes: dict[str, dict[str, Any]] = defaultdict(lambda: {"events": 0, "compressed": 0, "saved": 0})
    tools = Counter()
    platforms = Counter()
    total_saved = 0
    for event in events:
        action = _safe_cell(event.get("action"), limit=40) or "unknown"
        lane = _effective_lane(event)
        tool = _safe_cell(event.get("tool_name"), limit=80) or "unknown"
        saved = _event_int(event, "tokens_saved")
        total_saved += saved
        tools[tool] += 1
        platforms[_safe_cell(event.get("platform"), limit=40) or "unknown"] += 1
        lanes[lane]["events"] += 1
        lanes[lane]["saved"] += saved
        if action == "compressed":
            lanes[lane]["compressed"] += 1
    return {"actions": actions, "lanes": dict(lanes), "tools": tools, "platforms": platforms, "tokens_saved": total_saved}


def _format_action_counts(actions: Counter) -> str:
    order = ["compressed", "exact", "blocked", "skipped", "runtime_unavailable", "error"]
    parts = [f"{name}={actions.get(name, 0)}" for name in order if actions.get(name, 0)]
    unknown = sum(count for name, count in actions.items() if name not in order)
    if unknown:
        parts.append(f"other={unknown}")
    return " ".join(parts) if parts else "no_actions"



def _format_top_platforms(platforms: Counter, *, limit: int = 3) -> str:
    if not platforms:
        return "unknown"
    ranked = platforms.most_common(limit)
    return ",".join(f"{name}:{count}" for name, count in ranked)

def _format_top_lanes(lanes: dict[str, dict[str, Any]], *, limit: int = 3) -> str:
    if not lanes:
        return "none"
    ranked = sorted(lanes.items(), key=lambda item: (int(item[1].get("saved") or 0), int(item[1].get("events") or 0)), reverse=True)
    return ", ".join(
        f"{lane}:events={data.get('events', 0)},compressed={data.get('compressed', 0)},saved={data.get('saved', 0)}"
        for lane, data in ranked[:limit]
    )



def _render_runtime_stats() -> str:
    """Render read-only Headroom runtime CCR/store stats.

    This deliberately uses only `/readyz` and `/v1/retrieve/stats`; it does not
    expose admin/debug/telemetry endpoints in owner-visible defaults.
    """
    health = readyz()
    if not health.get("ok"):
        return _render_status(health) + " · runtime_stats=unavailable"
    stats = retrieve_stats(proxy_url=health.get("proxy_url"))
    if not stats.get("success"):
        return f"Headroom runtime · proxy={health.get('proxy_url')} · retrieve_stats=FAIL · error={_safe_cell(stats.get('error'), limit=180)}"
    store = stats.get("store") if isinstance(stats.get("store"), dict) else {}
    backend = store.get("backend") if isinstance(store.get("backend"), dict) else {}
    recent = stats.get("recent_retrievals") if isinstance(stats.get("recent_retrievals"), list) else []
    fields = {
        "entries": store.get("entry_count"),
        "max": store.get("max_entries"),
        "ttl_s": store.get("default_ttl_seconds"),
        "orig_tokens": store.get("total_original_tokens"),
        "compressed_tokens": store.get("total_compressed_tokens"),
        "retrievals": store.get("total_retrievals"),
        "events": store.get("event_count"),
        "backend": backend.get("backend_type"),
        "recent": len(recent),
    }
    body = " ".join(f"{key}={_safe_cell(value, limit=80)}" for key, value in fields.items() if value is not None)
    return f"Headroom runtime · proxy={health.get('proxy_url')} · retrieve_stats=PASS · {body}"

def _render_usage(parts: list[str]) -> str:
    events, path = _read_headroom_events()
    if len(parts) >= 2 and parts[1].lower() == "turn":
        if not events:
            return f"Headroom usage turn · no events yet · path={path}"
        requested = parts[2] if len(parts) >= 3 else ""
        if requested:
            turn_id = requested
        else:
            turn_id = next((_safe_cell(event.get("turn_id"), limit=120) for event in reversed(events) if _safe_cell(event.get("turn_id"), limit=120)), "")
        if turn_id:
            scoped = [event for event in events if _safe_cell(event.get("turn_id"), limit=120) == turn_id]
            label = f"turn_id={turn_id}"
        else:
            scoped = events[-1:]
            label = "latest_event_no_turn_id"
        if not scoped:
            return f"Headroom usage turn · {label} · events=0 · path={path}"
        summary = _summarize_events(scoped)
        return (
            f"Headroom usage turn · {label} · events={len(scoped)} · "
            f"{_format_action_counts(summary['actions'])} · saved={summary['tokens_saved']} · "
            f"lanes={_format_top_lanes(summary['lanes'])} · platforms={_format_top_platforms(summary['platforms'])} · path={path}"
        )

    if not events:
        return f"Headroom usage · no events yet · path={path}"
    summary = _summarize_events(events)
    return (
        f"Headroom usage · events={len(events)} · {_format_action_counts(summary['actions'])} · "
        f"saved={summary['tokens_saved']} · lanes={_format_top_lanes(summary['lanes'])} · platforms={_format_top_platforms(summary['platforms'])} · path={path}"
    )


def _render_lanes() -> str:
    events, path = _read_headroom_events()
    if not events:
        return f"Headroom lanes · no events yet · path={path}"
    summary = _summarize_events(events)
    lanes = summary["lanes"]
    if not lanes:
        return f"Headroom lanes · none · path={path}"
    ranked = sorted(lanes.items(), key=lambda item: (int(item[1].get("saved") or 0), int(item[1].get("events") or 0)), reverse=True)
    body = " | ".join(
        f"{lane}: events={data.get('events', 0)} compressed={data.get('compressed', 0)} saved={data.get('saved', 0)}"
        for lane, data in ranked[:8]
    )
    return f"Headroom lanes · {body} · path={path}"


def _render_tail(parts: list[str]) -> str:
    try:
        requested = int(parts[1]) if len(parts) >= 2 else 5
    except ValueError:
        requested = 5
    n = max(1, min(requested, 20))
    events, path = _read_headroom_events(limit=max(n, 50))
    if not events:
        return f"Headroom tail · no events yet · path={path}"
    lines = [f"Headroom tail · n={min(n, len(events))} · path={path}"]
    for event in events[-n:]:
        saved = _event_int(event, "tokens_saved")
        marker = _safe_cell(event.get("marker"), limit=36)
        marker_part = f" marker={marker}" if marker else ""
        lines.append(
            "- "
            f"{_safe_cell(event.get('ts'), limit=24)} "
            f"{_safe_cell(event.get('action'), limit=32)} "
            f"tool={_safe_cell(event.get('tool_name'), limit=40)} "
            f"lane={_effective_lane(event)} "
            f"platform={_safe_cell(event.get('platform'), limit=24) or 'unknown'} "
            f"saved={saved}{marker_part} "
            f"reason={_safe_cell(event.get('reason'), limit=72)}"
        )
    return "\n".join(lines)

def _render_decisions(parts: list[str]) -> str:
    """Render a read-only decision matrix from local Headroom events."""
    events, path = _read_headroom_events()
    if not events:
        return f"Headroom decisions · no events yet · path={path}"

    scoped = events
    label = "recent"
    if len(parts) >= 2 and parts[1].lower() == "turn":
        requested = parts[2] if len(parts) >= 3 else ""
        turn_id = requested or next((_safe_cell(event.get("turn_id"), limit=120) for event in reversed(events) if _safe_cell(event.get("turn_id"), limit=120)), "")
        if turn_id:
            scoped = [event for event in events if _safe_cell(event.get("turn_id"), limit=120) == turn_id]
            label = f"turn_id={turn_id}"
        else:
            scoped = events[-1:]
            label = "latest_event_no_turn_id"
    if not scoped:
        return f"Headroom decisions · {label} · events=0 · path={path}"

    groups: dict[tuple[str, str, str, str, str], dict[str, Any]] = defaultdict(lambda: {"count": 0, "saved": 0, "before": 0, "chars": 0})
    family_counts: Counter = Counter()
    for event in scoped:
        action = _safe_cell(event.get("action"), limit=40) or "unknown"
        reason = _safe_cell(event.get("reason"), limit=100) or "unknown"
        family = _reason_family(action, reason)
        key = (
            _safe_cell(event.get("tool_name"), limit=50) or "unknown_tool",
            _effective_lane(event),
            _safe_cell(event.get("platform"), limit=30) or "unknown",
            action,
            reason,
        )
        row = groups[key]
        row["count"] += 1
        row["saved"] += _event_int(event, "tokens_saved")
        row["before"] += _event_int(event, "tokens_before")
        row["chars"] += _event_int(event, "original_chars")
        family_counts[family] += 1

    ranked = sorted(groups.items(), key=lambda item: (int(item[1]["count"]), int(item[1]["saved"]), int(item[1]["chars"])), reverse=True)
    lines = [
        f"Headroom decisions · {label} · events={len(scoped)} · path={path}",
        "families=" + ",".join(f"{name}:{count}" for name, count in family_counts.most_common()),
    ]
    for (tool, lane, platform, action, reason), row in ranked[:12]:
        family = _reason_family(action, reason)
        lines.append(
            f"- {tool} lane={lane} platform={platform} action={action} family={family} "
            f"count={row['count']} saved={row['saved']} before={row['before']} chars={row['chars']} reason={reason}"
        )
    if len(ranked) > 12:
        lines.append(f"… {len(ranked) - 12} more groups omitted")
    return "\n".join(lines)




def _render_opportunities() -> str:
    """Render read-only savings opportunities from retained event metadata.

    This deliberately uses event metadata only: no raw payload reads, no sidecar
    creation, no policy mutation. It separates live Telegram evidence from
    local/synthetic/unknown-platform events so we do not overfit tests into
    owner-facing tuning decisions.
    """
    events, path = _read_headroom_events(limit=10000)
    if not events:
        return f"Headroom opportunities · no events yet · path={path}"

    telegram = [event for event in events if _safe_cell(event.get("platform"), limit=40) == "telegram"]
    total_saved = sum(_event_int(event, "tokens_saved") for event in events)
    telegram_saved = sum(_event_int(event, "tokens_saved") for event in telegram)

    def chars(items: list[dict[str, Any]]) -> int:
        return sum(_event_int(event, "original_chars") for event in items)

    compressed_terminal = [
        event for event in telegram
        if event.get("tool_name") == "terminal" and event.get("action") == "compressed"
    ]
    terminal_not_useful = [
        event for event in telegram
        if event.get("tool_name") == "terminal" and event.get("reason") == "compression_not_useful"
    ]
    terminal_below, repeated_below = _terminal_below_min_candidates(events, platform="telegram")

    exact_reads = [
        event for event in telegram
        if event.get("action") == "exact" and event.get("tool_name") in {"read_file", "search_files"}
    ]
    header_missing_all = [event for event in events if str(event.get("reason") or "").startswith("header_missing")]
    header_missing_telegram = [event for event in telegram if str(event.get("reason") or "").startswith("header_missing")]

    lines = [
        f"Headroom opportunities · events={len(events)} saved={total_saved} · telegram_events={len(telegram)} telegram_saved={telegram_saved} · path={path}",
        f"1. terminal/build logs: live_compressed={len(compressed_terminal)} saved={sum(_event_int(e, 'tokens_saved') for e in compressed_terminal)} chars={chars(compressed_terminal)}; keep exact-command exclusions",
        f"2. repeated below-min terminal chunks: candidate_turns={len(repeated_below)} chunks={len(terminal_below)} chars={chars(terminal_below)}; probe adaptive per-turn threshold, not global lowering",
        f"3. exact read discipline: events={len(exact_reads)} chars={chars(exact_reads)}; prefer narrower offsets/limits/shallow bundles, do not compress source truth by default",
        f"4. header-missing audit: telegram={len(header_missing_telegram)} all={len(header_missing_all)} chars_all={chars(header_missing_all)}; patch only with raw sample + parity fixtures",
        f"5. compression_not_useful terminal: events={len(terminal_not_useful)} chars={chars(terminal_not_useful)}; inspect shape before policy tuning",
    ]
    if repeated_below:
        turn, data = repeated_below[0]
        lines.append(f"top_below_min_turn: turn_id={turn} count={data['count']} chars={data['chars']} max={data['max']}")
    return "\n".join(lines)

def _render_on() -> str:
    """Compatibility response for owner-local `/headroom on` muscle memory.

    The packaged plugin does not toggle itself from a slash command. Plugin
    enablement is handled by `hermes plugins install ... --enable`; runtime
    startup is handled by the production runtime installer or service manager.
    This command is intentionally read-only and reports whether Headroom is
    already usable through the current proxy.
    """
    health = readyz()
    if health.get("ok"):
        return (
            "Headroom on · already active · "
            f"proxy={health['proxy_url']} · status={health['status']} · "
            f"visible_marker={'on:' + headroom_status_marker(health) if visible_status_marker_enabled() else 'off:disabled'} · "
            "use /headroom smoke for compress→retrieve verification"
        )
    return (
        "Headroom on · no slash-side toggle in the packaged plugin · "
        f"proxy={health['proxy_url']} not ready · status={health['status']} · "
        f"visible_marker={'on:' + headroom_status_marker(health) if visible_status_marker_enabled() else 'off:disabled'} · "
        "run the production runtime installer or restart the external Headroom service, then /headroom smoke"
    )


def handle_headroom_command(raw_args: str = "") -> str:
    parts = (raw_args or "").strip().split()
    action = (parts[0].lower() if parts else "status")
    if action == "status":
        return _render_status(readyz())
    if action == "audit":
        result = audit()
        return "Headroom audit " + ("PASS" if result.get("ok") else "FAIL") + " · " + json.dumps(result, ensure_ascii=False, sort_keys=True)
    if action == "smoke":
        return _render_smoke(smoke())
    if action in {"runtime", "stats"}:
        return _render_runtime_stats()
    if action == "usage":
        return _render_usage(parts)
    if action == "lanes":
        return _render_lanes()
    if action == "tail":
        return _render_tail(parts)
    if action in {"decisions", "why"}:
        return _render_decisions(parts)
    if action in {"opportunities", "opp"}:
        return _render_opportunities()
    if action in {"on", "enable"}:
        return _render_on()
    if action in {"off", "disable"}:
        return "Headroom off · not supported from slash command; use hermes plugins disable headroom_retrieve or stop the external runtime service explicitly"
    return USAGE


def events_summary_main(argv: list[str] | None = None) -> int:
    """CLI renderer for local Headroom observability events.

    This is the terminal/cron-friendly companion to `/headroom usage|lanes|tail`.
    It is read-only and does not start the proxy, mutate runtime config, or
    require a Hermes gateway restart.
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Render local Headroom event summaries from $HERMES_HOME/control-plane/headroom/events/headroom-events.jsonl")
    sub = parser.add_subparsers(dest="command")
    usage = sub.add_parser("usage", help="summarize recent Headroom events")
    usage.add_argument("--turn", dest="turn_id", default="", help="optional turn_id to scope usage summary")
    sub.add_parser("lanes", help="summarize lane-level Headroom activity")
    sub.add_parser("runtime", help="show read-only Headroom runtime retrieval/store stats")
    decisions = sub.add_parser("decisions", help="show grouped Headroom compression/exact/skip/block decisions")
    decisions.add_argument("--turn", dest="turn_id", default="", help="optional turn_id to scope decision matrix")
    sub.add_parser("opportunities", help="show ranked read-only savings opportunities from event metadata")
    tail = sub.add_parser("tail", help="show recent Headroom events")
    tail.add_argument("-n", "--lines", type=int, default=5, help="number of events to show, clamped to 1..20")
    args = parser.parse_args(argv)

    command = args.command or "usage"
    if command == "usage":
        parts = ["usage"]
        if getattr(args, "turn_id", ""):
            parts.extend(["turn", str(args.turn_id)])
        text = _render_usage(parts)
    elif command == "lanes":
        text = _render_lanes()
    elif command == "runtime":
        text = _render_runtime_stats()
    elif command == "tail":
        text = _render_tail(["tail", str(getattr(args, "lines", 5))])
    elif command == "opportunities":
        text = _render_opportunities()
    elif command == "decisions":
        parts = ["decisions"]
        if getattr(args, "turn_id", ""):
            parts.extend(["turn", str(args.turn_id)])
        text = _render_decisions(parts)
    else:
        parser.print_help(sys.stderr)
        return 2
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(events_summary_main())
