"""Hermes Headroom installable plugin registration."""
from __future__ import annotations

from pathlib import Path

from .commands import handle_headroom_command
from .hooks import on_pre_llm_call, on_transform_llm_output, on_transform_terminal_output
from .middleware import on_llm_request, on_tool_execution
from .schemas import HEADROOM_RETRIEVE_SCHEMA
from .tools import handle_headroom_retrieve


def register(ctx) -> None:
    """Register Headroom tool, slash command, hooks, middleware, and bundled skill.

    This function intentionally does not start services or mutate Hermes config.
    Installation/setup must be explicit and reversible.
    """
    # check_fn=readyz is intentionally NOT set here.
    # headroom_retrieve remains visible to the model even when the proxy is
    # unavailable (RUNTIME_PARTIAL). The handler returns a clear error instead of
    # the tool silently disappearing from the tool list. This makes it easier
    # to diagnose install/proxy issues. Operators preferring strict service-gated
    # behavior can wrap the handler with check_fn=_readyz and register_tool.
    ctx.register_tool(
        name="headroom_retrieve",
        toolset="headroom",
        schema=HEADROOM_RETRIEVE_SCHEMA,
        handler=handle_headroom_retrieve,
        emoji="🗜️",
        description="Retrieve exact content behind a Headroom CCR marker.",
    )
    ctx.register_command(
        "headroom",
        handle_headroom_command,
        description="Headroom status/smoke/audit helpers: /headroom status|smoke|audit|on",
        args_hint="status|smoke|audit|on",
    )
    ctx.register_hook("transform_terminal_output", on_transform_terminal_output)
    ctx.register_hook("transform_llm_output", on_transform_llm_output)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)

    # Guard against older Hermes versions without register_middleware.
    register_middleware = getattr(ctx, "register_middleware", None)
    if callable(register_middleware):
        register_middleware("llm_request", on_llm_request)
        register_middleware("tool_execution", on_tool_execution)

    skill_path = Path(__file__).parent / "skills" / "headroom-token-cost-evaluation" / "SKILL.md"
    if skill_path.exists() and hasattr(ctx, "register_skill"):
        ctx.register_skill("headroom-token-cost-evaluation", skill_path, description="Operate Headroom context reduction safely.")
