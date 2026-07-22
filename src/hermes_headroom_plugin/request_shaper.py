"""Copy-on-write provider schema shaping; never changes message bodies."""
from __future__ import annotations
from copy import deepcopy
from typing import Any

RESIDENT_TOOLS = frozenset({"terminal", "read_file", "search_files", "patch", "write_file", "todo", "headroom_retrieve", "innocent"})
SUPPORTED_MODES = frozenset({"responses", "codex_responses", "openai_responses"})

def shape_request(request: dict[str, Any], api_mode: str, *, owner: str, native_tool_search: bool = False,
                  proxy_deferral: bool = False, compatibility_test: bool = False) -> dict[str, Any] | None:
    # The current local Hermes Codex preflight rejects ``tool_search`` and strips
    # ``defer_loading`` from function tools. Keep provider-owned shaping behind
    # an explicit compatibility fixture until the host contract supports both.
    if not compatibility_test: return None
    if api_mode not in SUPPORTED_MODES: return None
    owners = {x for x, enabled in (("hermes", native_tool_search), ("provider", owner == "provider"), ("proxy", proxy_deferral or owner == "proxy")) if enabled}
    if len(owners) != 1: return None
    if owner != "provider": return None
    tools = request.get("tools")
    if not isinstance(tools, list): return None
    shaped = deepcopy(request); changed = False
    deferred = 0
    has_search = any(isinstance(schema, dict) and schema.get("type") == "tool_search" for schema in shaped["tools"])
    for schema in shaped["tools"]:
        if not isinstance(schema, dict): continue
        if schema.get("type") == "tool_search":
            continue
        fn = schema.get("function") if isinstance(schema.get("function"), dict) else schema
        name = str(fn.get("name") or "")
        desired = False if name in RESIDENT_TOOLS else True
        # Responses accepts defer_loading on the tool object, not inside the
        # function definition used by chat-completions schemas.
        if schema.get("defer_loading") != desired: schema["defer_loading"] = desired; changed = True
        deferred += int(desired)
    # OpenAI Responses only honors deferred definitions when a provider-native
    # tool_search tool is present. This plugin is the sole disclosure owner in
    # this branch, so it must supply the complete protocol pair exactly once.
    if deferred and not has_search:
        shaped["tools"].insert(0, {"type": "tool_search"})
        changed = True
    return shaped if changed else request
