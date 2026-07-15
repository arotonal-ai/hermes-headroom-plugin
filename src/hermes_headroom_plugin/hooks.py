"""Conservative plugin hooks.

Tool-result compression is handled by middleware. Final-answer markers and
first-turn hints are optional observability extras and default off in the
portable core.
"""
from __future__ import annotations

import os
from typing import Any

from .middleware import remember_platform_context
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
    """Return whether the optional final-answer status marker is enabled."""
    env_value = os.environ.get("HEADROOM_VISIBLE_STATUS_MARKER")
    if env_value is not None:
        return _boolish(env_value, default=False)
    cfg = config if isinstance(config, dict) else load_context_reduction_config()
    return _boolish(cfg.get("visible_status_marker"), default=False)


def first_turn_hint_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return whether the optional first-turn Headroom availability hint is enabled."""
    env_value = os.environ.get("HEADROOM_FIRST_TURN_HINT")
    if env_value is not None:
        return _boolish(env_value, default=False)
    cfg = config if isinstance(config, dict) else load_context_reduction_config()
    return _boolish(cfg.get("first_turn_hint"), default=False)


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
