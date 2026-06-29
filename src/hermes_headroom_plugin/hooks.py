"""Plugin hooks.

Hooks stay conservative. Live compression is handled by tool_execution middleware for eligible bulky intermediate tool/lane results; these hooks fail closed to exact output except for a small first-turn availability hint.
"""
from __future__ import annotations

from .proxy import readyz


def on_transform_terminal_output(command: str = "", output: str = "", **kwargs):
    del command, output, kwargs
    return None


def on_transform_llm_output(response_text: str = "", **kwargs):
    del kwargs
    return None if not response_text else None


def on_pre_llm_call(is_first_turn: bool = False, task_id: str = "", platform: str = "", **kwargs):
    del task_id, platform, kwargs
    if not is_first_turn:
        return None
    if not readyz().get("ok"):
        return None
    return {"context": "Headroom is available for eligible bulky intermediate/diagnostic traces; final/edit-critical/sensitive content remains exact or blocked."}
