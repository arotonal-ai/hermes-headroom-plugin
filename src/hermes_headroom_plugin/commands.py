"""Slash command handlers."""
from __future__ import annotations

import json

from .health import audit
from .proxy import readyz, smoke


def _render_smoke(result: dict) -> str:
    if result.get("ok"):
        return (
            "Headroom smoke PASS · "
            f"tokens_before={result.get('tokens_before')} tokens_after={result.get('tokens_after')} "
            f"saved={result.get('tokens_saved')} marker={result.get('marker')} "
            f"retrieve_count={result.get('retrieve_count')} sentinel_found={result.get('sentinel_found')}"
        )
    return f"Headroom smoke FAIL · phase={result.get('phase')} · proxy={result.get('proxy_url')} · error={result.get('error', 'unknown')}"


def handle_headroom_command(raw_args: str = "") -> str:
    parts = (raw_args or "").strip().split()
    action = (parts[0].lower() if parts else "status")
    if action == "status":
        health = readyz()
        return f"Headroom status · ok={health['ok']} · proxy={health['proxy_url']} · status={health['status']}"
    if action == "audit":
        result = audit()
        return "Headroom audit " + ("PASS" if result.get("ok") else "FAIL") + " · " + json.dumps(result, ensure_ascii=False, sort_keys=True)
    if action == "smoke":
        return _render_smoke(smoke())
    return "Usage: /headroom status|smoke|audit"
