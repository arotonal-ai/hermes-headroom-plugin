"""Slash command handlers."""
from __future__ import annotations

import json

from .health import audit
from .proxy import readyz, resolve_proxy_url


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
        # P0 smoke is intentionally non-mutating unless a live proxy is present.
        health = readyz()
        if not health.get("ok"):
            return f"Headroom smoke BLOCKED · proxy not ready · proxy={resolve_proxy_url()}"
        return "Headroom smoke READY · proxy healthy; full compress/retrieve smoke is implemented in the health/audit harness."
    return "Usage: /headroom status|smoke|audit"
