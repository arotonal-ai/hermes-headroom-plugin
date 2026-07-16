"""Read-only Headroom cache effectiveness report.

This module measures the cache layers the Hermes plugin can safely observe without
mutating runtime cache, Hermes model routing, or provider credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import yaml
except Exception:  # pragma: no cover - package still works without config insight.
    yaml = None

from .config import auto_compression_enabled, hermes_home, load_context_reduction_config
from .proxy import readyz, resolve_proxy_url, retrieve_stats, utc_now

DECISIONS = {"KEEP_PROXY_HOT_PATH", "ADD_CACHE_UX", "TEST_PROVIDER_CACHE_LANE", "DO_NOT_USE_PROVIDER_CACHE"}


@dataclass(frozen=True)
class CacheEffectivenessConfig:
    event_limit: int = 2000
    min_tokens_saved: int = 50_000
    min_compressed_events: int = 3
    high_ttl_risk_seconds: int = 900
    proxy_url: str | None = None
    hermes_home_path: Path | None = None


def _event_log_path(home: Path | None = None) -> Path:
    return (home or hermes_home()) / "control-plane" / "headroom" / "events" / "headroom-events.jsonl"


def _read_events(*, limit: int, home: Path | None = None) -> tuple[list[dict[str, Any]], Path]:
    path = _event_log_path(home)
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
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get("type") == "headroom_tool_result":
                    events.append(item)
    except OSError:
        return [], path
    return events, path


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_text(value: Any, *, limit: int = 160) -> str:
    text = str(value or "").replace("\n", " ").replace("\r", " ").strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _load_hermes_config(home: Path) -> dict[str, Any]:
    path = home / "config.yaml"
    if yaml is None or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _active_model_path(home: Path, proxy_url: str | None) -> dict[str, Any]:
    cfg = _load_hermes_config(home)
    model = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    provider = _safe_text(model.get("provider"), limit=80) or "unknown"
    base_url = _safe_text(model.get("base_url"), limit=240)
    proxy_host = urlparse(proxy_url or "").netloc
    base_host = urlparse(base_url).netloc if base_url else ""
    routed_through_headroom = bool(proxy_host and base_host and proxy_host == base_host)
    return {
        "provider": provider,
        "api_mode": _safe_text(model.get("api_mode"), limit=80) or "unknown",
        "base_url_host": base_host or "unknown",
        "routed_through_headroom_proxy": routed_through_headroom,
        "provider_cache_observable": routed_through_headroom,
        "note": "Active model base_url matches Headroom proxy host." if routed_through_headroom else "Active Hermes model path is not routed through the Headroom proxy; provider prompt/KV cache is not observable by this plugin report.",
    }


def _summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    actions = Counter(_safe_text(e.get("action"), limit=40) or "unknown" for e in events)
    reasons = Counter(_safe_text(e.get("reason"), limit=80) or "none" for e in events)
    tools = Counter(_safe_text(e.get("tool_name"), limit=80) or "unknown" for e in events)
    lanes: dict[str, dict[str, int]] = defaultdict(lambda: {"events": 0, "compressed": 0, "saved": 0})
    tokens_saved = 0
    original_chars = 0
    compressed_chars = 0
    for event in events:
        action = _safe_text(event.get("action"), limit=40) or "unknown"
        lane = _safe_text(event.get("lane"), limit=60) or "unknown"
        saved = _safe_int(event.get("tokens_saved"))
        tokens_saved += saved
        original_chars += _safe_int(event.get("original_chars"))
        compressed_chars += _safe_int(event.get("compressed_chars"))
        lanes[lane]["events"] += 1
        lanes[lane]["saved"] += saved
        if action == "compressed":
            lanes[lane]["compressed"] += 1
    compressed = actions.get("compressed", 0)
    eligible_decisions = compressed + actions.get("skipped", 0) + actions.get("runtime_unavailable", 0)
    compression_rate = round(compressed / eligible_decisions, 4) if eligible_decisions else 0.0
    return {
        "events": len(events),
        "actions": dict(actions),
        "top_reasons": dict(reasons.most_common(8)),
        "top_tools": dict(tools.most_common(8)),
        "lanes": dict(lanes),
        "tokens_saved": tokens_saved,
        "original_chars_seen": original_chars,
        "compressed_chars_seen": compressed_chars,
        "compressed_events": compressed,
        "compression_rate_over_candidate_actions": compression_rate,
    }


def _summarize_store(stats: dict[str, Any]) -> dict[str, Any]:
    store = stats.get("store") if isinstance(stats.get("store"), dict) else {}
    backend = store.get("backend") if isinstance(store.get("backend"), dict) else {}
    recent = stats.get("recent_retrievals") if isinstance(stats.get("recent_retrievals"), list) else []
    retrieved_nonzero = sum(1 for r in recent if _safe_int(r.get("items_retrieved")) > 0)
    recent_success_rate = round(retrieved_nonzero / len(recent), 4) if recent else None
    entries = _safe_int(store.get("entry_count"))
    max_entries = _safe_int(store.get("max_entries"))
    usage_pct = round((entries / max_entries) * 100, 2) if max_entries else None
    original_tokens = _safe_int(store.get("total_original_tokens"))
    compressed_tokens = _safe_int(store.get("total_compressed_tokens"))
    ccr_savings_ratio = round(max(0, original_tokens - compressed_tokens) / original_tokens, 4) if original_tokens else None
    return {
        "available": bool(stats.get("success")),
        "entry_count": entries,
        "max_entries": max_entries,
        "usage_pct": usage_pct,
        "ttl_seconds": _safe_int(store.get("default_ttl_seconds")),
        "total_original_tokens": original_tokens,
        "total_compressed_tokens": compressed_tokens,
        "ccr_savings_ratio": ccr_savings_ratio,
        "total_retrievals": _safe_int(store.get("total_retrievals")),
        "event_count": _safe_int(store.get("event_count")),
        "recent_retrievals": len(recent),
        "recent_nonzero_retrievals": retrieved_nonzero,
        "recent_success_rate": recent_success_rate,
        "backend_type": backend.get("backend_type") or "unknown",
        "bytes_used": _safe_int(backend.get("bytes_used")),
    }


def _ttl_risk(store: dict[str, Any], cfg: CacheEffectivenessConfig) -> str:
    ttl = _safe_int(store.get("ttl_seconds"))
    entries = _safe_int(store.get("entry_count"))
    retrievals = _safe_int(store.get("total_retrievals"))
    if ttl <= 0 or not store.get("available"):
        return "unknown"
    if ttl < cfg.high_ttl_risk_seconds and entries > 0:
        return "high"
    if entries > 0 and retrievals == 0:
        return "medium"
    return "low"


def _decision(*, runtime_ok: bool, auto_on: bool, store: dict[str, Any], events: dict[str, Any], model_path: dict[str, Any], cfg: CacheEffectivenessConfig) -> str:
    if not runtime_ok or not store.get("available"):
        return "DO_NOT_USE_PROVIDER_CACHE"
    # Provider cache lane should only be proposed when the active model path is
    # not already proxied and local compression is healthy enough to justify a
    # separate, isolated experiment.
    strong_compression = events.get("tokens_saved", 0) >= cfg.min_tokens_saved and events.get("compressed_events", 0) >= cfg.min_compressed_events
    if model_path.get("routed_through_headroom_proxy"):
        return "ADD_CACHE_UX"
    if strong_compression and auto_on:
        return "KEEP_PROXY_HOT_PATH"
    if store.get("recent_retrievals", 0) > 0 or store.get("entry_count", 0) > 0:
        return "ADD_CACHE_UX"
    return "DO_NOT_USE_PROVIDER_CACHE"


def run_report(config: CacheEffectivenessConfig | None = None) -> dict[str, Any]:
    cfg = config or CacheEffectivenessConfig()
    home = cfg.hermes_home_path or hermes_home()
    try:
        proxy_url = cfg.proxy_url or resolve_proxy_url(load_context_reduction_config(home))
    except Exception as exc:
        return {
            "schema": "headroom-cache-effectiveness/v1",
            "ts": utc_now(),
            "decision": "DO_NOT_USE_PROVIDER_CACHE",
            "status": "RUNTIME_PARTIAL",
            "runtime_ok": False,
            "error": f"proxy configuration failed: {type(exc).__name__}: {exc}",
            "next": "Fix loopback proxy configuration before evaluating provider/prompt cache lanes.",
        }

    health = readyz(proxy_url)
    stats = retrieve_stats(proxy_url=proxy_url) if health.get("ok") else {"success": False, "error": "runtime not ready"}
    events, event_path = _read_events(limit=cfg.event_limit, home=home)
    event_summary = _summarize_events(events)
    store_summary = _summarize_store(stats)
    auto_on = bool(auto_compression_enabled())
    model_path = _active_model_path(home, proxy_url)
    runtime_ok = bool(health.get("ok"))
    decision = _decision(runtime_ok=runtime_ok, auto_on=auto_on, store=store_summary, events=event_summary, model_path=model_path, cfg=cfg)
    ttl_risk = _ttl_risk(store_summary, cfg)
    next_by_decision = {
        "KEEP_PROXY_HOT_PATH": "Keep loopback proxy as compression/retrieval hot path; do not move LLM provider routing yet. Revisit provider-cache lane only with isolated benchmark evidence.",
        "ADD_CACHE_UX": "Improve read-only cache UX/metrics before changing routing; expose hit/miss/TTL/recompression signals where available.",
        "TEST_PROVIDER_CACHE_LANE": "Run an isolated provider-cache lane benchmark with explicit auth/routing rollback before promotion.",
        "DO_NOT_USE_PROVIDER_CACHE": "Do not route LLM calls through Headroom for cache yet; fix runtime/store observability or keep compression-only behavior.",
    }
    provider_cache = {
        "active": bool(model_path.get("routed_through_headroom_proxy")),
        "observable": bool(model_path.get("provider_cache_observable")),
        "cache_read_tokens_observed": 0,
        "cache_write_tokens_observed": 0,
        "note": model_path.get("note"),
    }
    return {
        "schema": "headroom-cache-effectiveness/v1",
        "ts": utc_now(),
        "decision": decision,
        "status": "RUNTIME_FULL" if runtime_ok else "RUNTIME_PARTIAL",
        "runtime_ok": runtime_ok,
        "auto_compression": "on" if auto_on else "manual",
        "proxy_url": proxy_url,
        "event_path": str(event_path),
        "ccr_store": store_summary | {"ttl_risk": ttl_risk},
        "middleware": event_summary,
        "model_path": model_path,
        "provider_cache": provider_cache,
        "thresholds": {
            "event_limit": cfg.event_limit,
            "min_tokens_saved": cfg.min_tokens_saved,
            "min_compressed_events": cfg.min_compressed_events,
            "high_ttl_risk_seconds": cfg.high_ttl_risk_seconds,
        },
        "limits": [
            "Read-only report; no runtime/cache/provider/model mutations.",
            "CCR store is runtime-owned and TTL-bound; final/canonical sources must remain exact.",
            "Provider prompt/KV cache can only be confirmed from a provider-routed path with cache telemetry.",
        ],
        "next": next_by_decision[decision],
    }


def _format_text(report: dict[str, Any]) -> str:
    store = report.get("ccr_store") if isinstance(report.get("ccr_store"), dict) else {}
    middleware = report.get("middleware") if isinstance(report.get("middleware"), dict) else {}
    provider_cache = report.get("provider_cache") if isinstance(report.get("provider_cache"), dict) else {}
    model_path = report.get("model_path") if isinstance(report.get("model_path"), dict) else {}
    return " · ".join(
        [
            f"Headroom cache effectiveness {report.get('decision')}",
            f"status={report.get('status')}",
            f"auto={report.get('auto_compression')}",
            f"ccr_entries={store.get('entry_count')}/{store.get('max_entries')}",
            f"ttl={store.get('ttl_seconds')}s risk={store.get('ttl_risk')}",
            f"retrievals={store.get('total_retrievals')} recent_success={store.get('recent_success_rate')}",
            f"middleware_saved={middleware.get('tokens_saved')} compressed={middleware.get('compressed_events')}/{middleware.get('events')}",
            f"provider_cache_active={provider_cache.get('active')}",
            f"model_provider={model_path.get('provider')}",
            f"next={report.get('next')}",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a read-only Headroom cache effectiveness report.")
    parser.add_argument("--event-limit", type=int, default=2000, help="Recent Headroom events to inspect (default: 2000, max: 10000).")
    parser.add_argument("--min-tokens-saved", type=int, default=50_000)
    parser.add_argument("--min-compressed-events", type=int, default=3)
    parser.add_argument("--high-ttl-risk-seconds", type=int, default=900)
    parser.add_argument("--proxy-url", default=None)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument("--output", help="Optional path to write the JSON report.")
    args = parser.parse_args(argv)
    report = run_report(
        CacheEffectivenessConfig(
            event_limit=args.event_limit,
            min_tokens_saved=args.min_tokens_saved,
            min_compressed_events=args.min_compressed_events,
            high_ttl_risk_seconds=args.high_ttl_risk_seconds,
            proxy_url=args.proxy_url,
        )
    )
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True) if args.format == "json" else _format_text(report))
    return 0 if report.get("decision") in DECISIONS else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
