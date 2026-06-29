"""Slash command handlers."""
from __future__ import annotations

import json

from .health import audit
from .hooks import headroom_status_marker, visible_status_marker_enabled
from .proxy import readyz, smoke

USAGE = "Usage: /headroom status|smoke|audit|on"


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
    if action in {"on", "enable"}:
        return _render_on()
    if action in {"off", "disable"}:
        return "Headroom off · not supported from slash command; use hermes plugins disable headroom_retrieve or stop the external runtime service explicitly"
    return USAGE
