"""Compatibility facade for context-reduction middleware hooks.

Implementation lives in focused policy, observability, reduction, tool, and
request modules. This module preserves the existing import/call surface. Tests
and extensions must patch the module that owns a dependency, not this facade.
"""

from .config import (
    _falsey,
    auto_compression_enabled,
    hermes_home,
    llm_request_compression_enabled,
    load_context_reduction_config,
)
from .middleware_request import *  # noqa: F403 - deliberate legacy re-export
from .middleware_request import (
    _LLM_REQUEST_CACHE_LOCK,
    _LLM_REQUEST_CACHE_MAX,
    _LLM_REQUEST_TRANSFORM_CACHE,
    _adapt_anthropic_request,
    _adapt_bedrock_request,
    _adapt_chat_completions_request,
    _adapt_responses_request,
    _compress_request_text,
    _compress_text_parts,
    _emit_request_cache_reuse,
    _request_cache_get,
    _request_cache_put,
    _request_source_fingerprint,
    _request_tool_info,
    on_llm_request,
)
from .middleware_tool import _machine_consumer_requires_exact, on_tool_execution
from .observability import *  # noqa: F403 - deliberate legacy re-export
from .observability import (
    _EVENT_WRITE_LOCK,
    _PLATFORM_BY_KEY,
    _PLATFORM_CONTEXT_MAX,
    _emit_headroom_event,
    _event_dedupe_key,
    _event_log_contains_dedupe_key,
    _event_log_max_bytes,
    _event_log_path,
    _infer_lane,
    _normalize_platform,
    _report_dir,
    _resolve_event_platform,
    _rotate_event_log_if_needed,
    _rough_tokens_from_chars,
    _safe_event_text,
    _utc_stamp,
)
from .policy import *  # noqa: F403 - deliberate legacy re-export
from .policy import (
    _already_compressed,
    _args_contain_sensitive_value,
    _args_preview,
    _build_exact_header_data,
    _build_trace,
    _compressed_excerpt,
    _contains_protected_control,
    _dedupe,
    _detect_data_class,
    _edge_excerpt,
    _exact_or_blocked_reason,
    _extract_labeled_values,
    _extract_markers,
    _extract_matching_lines,
    _extract_urls,
    _format_exact_header,
    _lane_eligible,
    _normalize_data_class,
    _redact_text,
    _safe_header_value,
    _safe_name,
    _scan_text,
    _shorten,
)
from .proxy import compress_messages, readyz
from .reduction import *  # noqa: F403 - deliberate legacy re-export
from .reduction import (
    _BELOW_MIN_AGGREGATE_BUFFERS,
    _below_min_aggregate_enabled,
    _below_min_aggregate_key,
    _compress_structured_result_for_context,
    _compression_body_for_tool_result,
    _compression_proxy_tool_name,
    _maybe_compress_terminal_below_min_aggregate,
    _prune_below_min_buffers,
    _truthy,
    compress_tool_result_for_context,
)
