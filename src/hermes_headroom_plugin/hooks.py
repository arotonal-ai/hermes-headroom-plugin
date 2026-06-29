"""Plugin hooks.

Live compression is handled by tool_execution middleware for eligible bulky
intermediate tool/lane results. These hooks stay conservative: they add a small
first-turn availability hint and, by default, a compact visible status marker to
final assistant messages so operators can distinguish plugin/runtime posture
without reading slash-command output every turn.
"""
from __future__ import annotations

import os
from typing import Any

from .proxy import load_context_reduction_config, readyz

_TRUTHY = {"1", "true", "yes", "y", "on"}
_FALSEY = {"0", "false", "no", "n", "off"}
_STATUS_PREFIXES = ("[HR✓]", "[HR!]", "[HR?]")


def _boolish(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSEY:
        return False
    return default


def visible_status_marker_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return whether final-answer `[HR✓]`/`[HR!]` marker is enabled.

    The marker is cosmetic/status-only. It reports Headroom runtime readiness,
    not whether a particular final answer was compressed. Product default is on
    for consistency with the historical owner-local contract; operators may opt
    out via `context_reduction.visible_status_marker: false` or
    `HEADROOM_VISIBLE_STATUS_MARKER=0`.
    """
    env_value = os.environ.get("HEADROOM_VISIBLE_STATUS_MARKER")
    if env_value is not None:
        return _boolish(env_value, default=True)
    cfg = config if isinstance(config, dict) else load_context_reduction_config()
    return _boolish(cfg.get("visible_status_marker"), default=True)


def headroom_status_marker(health: dict[str, Any] | None = None) -> str:
    """Compact visible marker for final assistant messages."""
    status = health if isinstance(health, dict) else readyz()
    return "[HR✓]" if status.get("ok") else "[HR!]"


def on_transform_terminal_output(command: str = "", output: str = "", **kwargs):
    del command, output, kwargs
    return None


def on_transform_llm_output(response_text: str = "", **kwargs):
    del kwargs
    if not response_text or not visible_status_marker_enabled():
        return None
    stripped = response_text.lstrip()
    if stripped.startswith(_STATUS_PREFIXES):
        return None
    return f"{headroom_status_marker()} {response_text}"


def on_pre_llm_call(is_first_turn: bool = False, task_id: str = "", platform: str = "", **kwargs):
    del task_id, platform, kwargs
    if not is_first_turn:
        return None
    if not readyz().get("ok"):
        return None
    return {"context": "Headroom is available for eligible bulky intermediate/diagnostic traces; final/edit-critical/sensitive content remains exact or blocked. Visible [HR✓]/[HR!] marker reports proxy readiness only, not per-message compression."}
