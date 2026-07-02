"""Slash command handlers."""
from __future__ import annotations

import json
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

from .health import audit
from .hooks import headroom_status_marker, visible_status_marker_enabled
from .proxy import hermes_home, readyz, smoke

USAGE = "Usage: /headroom status|smoke|audit|on|usage [turn [turn_id]]|lanes|tail [n]"


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


def _summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    actions = Counter(_safe_cell(event.get("action"), limit=40) or "unknown" for event in events)
    lanes: dict[str, dict[str, Any]] = defaultdict(lambda: {"events": 0, "compressed": 0, "saved": 0})
    tools = Counter()
    platforms = Counter()
    total_saved = 0
    for event in events:
        action = _safe_cell(event.get("action"), limit=40) or "unknown"
        lane = _safe_cell(event.get("lane"), limit=60) or "unknown"
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
            f"lane={_safe_cell(event.get('lane'), limit=40)} "
            f"platform={_safe_cell(event.get('platform'), limit=24) or 'unknown'} "
            f"saved={saved}{marker_part} "
            f"reason={_safe_cell(event.get('reason'), limit=72)}"
        )
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
    if action == "usage":
        return _render_usage(parts)
    if action == "lanes":
        return _render_lanes()
    if action == "tail":
        return _render_tail(parts)
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
    elif command == "tail":
        text = _render_tail(["tail", str(getattr(args, "lines", 5))])
    else:
        parser.print_help(sys.stderr)
        return 2
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(events_summary_main())
