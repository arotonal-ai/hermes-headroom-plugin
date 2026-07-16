"""Conservative plugin hooks.

Tool-result compression is handled by middleware. Final-answer markers and
first-turn hints are optional observability extras and default off in the
portable core.
"""
from __future__ import annotations

from typing import Any

from .config import load_context_reduction_config, resolve_effective_config
from .observability import remember_platform_context
from .proxy import readyz

_STATUS_PREFIXES = ("[HR✓]", "[HR!]", "[HR?]")


def visible_status_marker_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return whether the optional final-answer status marker is enabled."""
    cfg = config if isinstance(config, dict) else load_context_reduction_config()
    return resolve_effective_config(raw_config=cfg).visible_status_marker


def first_turn_hint_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return whether the optional first-turn Headroom availability hint is enabled."""
    cfg = config if isinstance(config, dict) else load_context_reduction_config()
    return resolve_effective_config(raw_config=cfg).first_turn_hint


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
    remember_platform_context(
        session_id=kwargs.get("session_id", ""),
        task_id=task_id,
        turn_id=kwargs.get("turn_id", ""),
        platform=platform,
    )
    if not is_first_turn or not first_turn_hint_enabled():
        return None
    if not readyz().get("ok"):
        return None
    return {"context": "Headroom is available for eligible bulky intermediate tool results; final/edit-critical/sensitive content remains exact or blocked."}
