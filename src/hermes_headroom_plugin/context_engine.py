"""Native composite ContextEngine, inert until selected and enabled."""
from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any, Callable

from agent.context_engine import ContextEngine

from .config import EffectiveConfig, resolve_effective_config
from .lifecycle import transform_history
from .reduction import compress_tool_result_for_context


def _history_digest(messages: list[dict[str, Any]]) -> str:
    return hashlib.sha256(repr(messages).encode("utf-8", errors="replace")).hexdigest()


def _valid(original: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> bool:
    if len(original) != len(candidate):
        return False
    for old, new in zip(original, candidate):
        if not isinstance(old, dict) or not isinstance(new, dict):
            return False
        if old.keys() != new.keys() or old.get("role") != new.get("role") or old.get("tool_call_id") != new.get("tool_call_id"):
            return False
        for key in old:
            if key != "content" and old[key] != new[key]:
                return False
        if old.get("role") != "tool" and old.get("content") != new.get("content"):
            return False
    return True


class HeadroomCompositeEngine(ContextEngine):
    def __init__(self, *, model: str = "", context_length: int = 200_000, threshold_percent: float = .75,
                 protect_first_n: int = 3, protect_last_n: int = 6, materiality_chars: int | None = None,
                 builtin: Any = None, lifecycle_transform: Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]] = transform_history,
                 lifecycle_compressor: Callable[..., str | None] | None = None,
                 effective_config: EffectiveConfig | None = None):
        cfg = effective_config or resolve_effective_config()
        self.model = model; self.context_length = context_length; self.threshold_percent = threshold_percent
        self.threshold_tokens = int(context_length * threshold_percent); self.protect_first_n = protect_first_n
        self.protect_last_n = protect_last_n; self.materiality_chars = cfg.lifecycle_materiality_chars if materiality_chars is None else materiality_chars
        self.lifecycle_enabled = cfg.lifecycle_enabled
        self.hot_tool_results = cfg.lifecycle_hot_tool_results; self.warm_tool_results = cfg.lifecycle_warm_tool_results
        self.aggregate_budget_chars = cfg.lifecycle_aggregate_budget_chars; self.min_item_chars = cfg.min_tool_result_chars
        self._builtin = builtin; self._lifecycle_transform = lifecycle_transform
        self._uses_default_compressor = lifecycle_compressor is None
        self._lifecycle_compressor = lifecycle_compressor or self._default_compressor
        self.last_prompt_tokens = self.last_completion_tokens = self.last_total_tokens = self.compression_count = 0
        self._session_id = ""; self._no_op: dict[str, int] = {}

    @property
    def name(self) -> str: return "headroom-composite"

    def _default_compressor(self, tool: str, text: str, digest: str, *, aggregate: bool = False) -> str | None:
        return compress_tool_result_for_context(tool_name=tool, args={}, result=text,
            session_id=self._session_id, event_surface="context_engine", logical_source_id=digest,
            allow_below_min_aggregate=False)

    def __deepcopy__(self, memo):
        cfg = EffectiveConfig(lifecycle_enabled=self.lifecycle_enabled,
            lifecycle_materiality_chars=self.materiality_chars, lifecycle_hot_tool_results=self.hot_tool_results,
            lifecycle_warm_tool_results=self.warm_tool_results, lifecycle_aggregate_budget_chars=self.aggregate_budget_chars,
            min_tool_result_chars=self.min_item_chars)
        compressor = None if self._uses_default_compressor else self._lifecycle_compressor
        clone = type(self)(model=self.model, context_length=self.context_length, threshold_percent=self.threshold_percent,
            protect_first_n=self.protect_first_n, protect_last_n=self.protect_last_n, builtin=deepcopy(self._builtin, memo),
            lifecycle_transform=self._lifecycle_transform, lifecycle_compressor=compressor, effective_config=cfg)
        clone.update_from_response({"prompt_tokens": self.last_prompt_tokens, "completion_tokens": self.last_completion_tokens, "total_tokens": self.last_total_tokens})
        clone.compression_count = self.compression_count; clone._session_id = self._session_id; clone._no_op = dict(self._no_op)
        return clone

    def update_from_response(self, usage: dict[str, Any]) -> None:
        self.last_prompt_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
        self.last_completion_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
        self.last_total_tokens = int(usage.get("total_tokens", self.last_prompt_tokens + self.last_completion_tokens) or 0)
        if self._builtin is not None: self._builtin.update_from_response(usage)

    def should_compress(self, prompt_tokens: int = None) -> bool:
        value = self.last_prompt_tokens if prompt_tokens is None else prompt_tokens
        return bool(value and value >= self.threshold_tokens)

    def should_compress_preflight(self, messages):
        return sum(len(str(m.get("content") or "")) for m in messages) // 4 >= self.threshold_tokens

    def has_content_to_compress(self, messages):
        if not self.lifecycle_enabled and self._builtin is not None:
            checker = getattr(self._builtin, "has_content_to_compress", None)
            if callable(checker):
                return bool(checker(messages))
        protected_tail_start = max(0, len(messages) - self.protect_last_n)
        non_system_seen = 0
        for index, message in enumerate(messages):
            if message.get("role") == "system":
                continue
            if non_system_seen < self.protect_first_n:
                non_system_seen += 1
                continue
            non_system_seen += 1
            if index >= protected_tail_start:
                continue
            if message.get("role") == "tool" and isinstance(message.get("content"), str):
                return True
        return False

    def should_defer_preflight_to_real_usage(self, rough_tokens: int) -> bool:
        if self._builtin is not None:
            check = getattr(self._builtin, "should_defer_preflight_to_real_usage", None)
            if callable(check):
                return bool(check(rough_tokens))
        return False

    def _fallback(self, messages, **kwargs):
        if self._builtin is not None:
            return self._builtin.compress(deepcopy(messages), **kwargs)
        return deepcopy(messages)

    def compress(self, messages, current_tokens=None, focus_topic=None, force=False, memory_context=""):
        original = deepcopy(messages)
        fallback_args = dict(current_tokens=current_tokens, focus_topic=focus_topic, force=force, memory_context=memory_context)
        if not self.lifecycle_enabled:
            return self._fallback(original, **fallback_args)
        digest = _history_digest(original)
        if digest in self._no_op:
            self._no_op[digest] += 1
            return self._fallback(original, **fallback_args)
        try:
            candidate, info = self._lifecycle_transform(original, protect_first_n=self.protect_first_n,
                protect_last_n=self.protect_last_n, hot_tool_results=self.hot_tool_results,
                warm_tool_results=self.warm_tool_results, aggregate_budget_chars=self.aggregate_budget_chars,
                min_item_chars=self.min_item_chars, compressor=self._lifecycle_compressor)
        except Exception:
            candidate, info = original, {"changed": 0}
        before = sum(len(str(m.get("content") or "")) for m in original)
        after = sum(len(str(m.get("content") or "")) for m in candidate)
        if info.get("changed") and _valid(original, candidate) and before - after >= self.materiality_chars:
            self.compression_count += 1
            return candidate
        self._no_op[digest] = 1
        while len(self._no_op) > 256: self._no_op.pop(next(iter(self._no_op)))
        return self._fallback(original, **fallback_args)

    def on_session_start(self, session_id: str, **kwargs):
        self._session_id = session_id
        if self._builtin is not None: self._builtin.on_session_start(session_id, **kwargs)
    def on_session_end(self, session_id, messages):
        if self._builtin is not None: self._builtin.on_session_end(session_id, messages)
    def on_session_reset(self):
        super().on_session_reset(); self._session_id = ""; self._no_op.clear()
        if self._builtin is not None: self._builtin.on_session_reset()
    def update_model(self, model, context_length, base_url="", api_key="", provider="", api_mode=""):
        self.model = model; self._no_op.clear(); super().update_model(model, context_length, base_url, api_key, provider, api_mode)
        if self._builtin is None:
            try:
                from agent.context_compressor import ContextCompressor
                self._builtin = ContextCompressor(model=model, threshold_percent=self.threshold_percent,
                    protect_first_n=self.protect_first_n, protect_last_n=self.protect_last_n, base_url=base_url,
                    api_key=api_key, config_context_length=context_length, provider=provider, api_mode=api_mode)
            except Exception: self._builtin = None
        if self._builtin is not None: self._builtin.update_model(model, context_length, base_url, api_key, provider, api_mode)
    def get_tool_schemas(self): return []


HeadroomCompositeContextEngine = HeadroomCompositeEngine
