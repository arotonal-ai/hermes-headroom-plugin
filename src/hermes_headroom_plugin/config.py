"""Typed effective configuration for Hermes context reduction.

Precedence is explicit overrides > environment > ``context_reduction`` YAML >
defaults. Legacy YAML aliases remain accepted at this single resolution point.
Endpoint safety is enforced by :mod:`hermes_headroom_plugin.proxy` after
resolution.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

try:
    import yaml
except Exception:  # pragma: no cover - package still works with env/defaults.
    yaml = None

DEFAULT_PROXY_URL = "http://127.0.0.1:8787"
DEFAULT_MIN_TOOL_RESULT_CHARS = 8_000
DEFAULT_EVENT_LOG_MAX_BYTES = 5_000_000
DEFAULT_LLM_REQUEST_CACHE_MAX = 2_048
DEFAULT_REPORT_RETENTION_DAYS = 14
DEFAULT_REPORT_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_REPORT_PRUNE_INTERVAL_SECONDS = 3_600
_TRUE = {"1", "true", "yes", "y", "on", "enabled", "enable", "auto", "automatic"}
_FALSE = {"0", "false", "no", "n", "off", "disabled", "disable", "manual", "on_demand", "ondemand"}


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def load_context_reduction_config(home: Path | None = None) -> dict[str, Any]:
    path = (home or hermes_home()) / "config.yaml"
    if yaml is None or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    value = data.get("context_reduction") if isinstance(data, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


def _boolish(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower().replace("-", "_")
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    return default


def _falsey(value: Any) -> bool:
    """Legacy middleware helper retained for import compatibility."""
    if isinstance(value, bool):
        return value is False
    if value is None:
        return False
    return str(value).strip().lower() in {"0", "false", "no", "n", "off", "disabled", "disable"}


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def auto_compression_enabled(config: Mapping[str, Any] | None = None) -> bool:
    """Compatibility helper backed by the single effective config resolver."""
    raw = config if isinstance(config, Mapping) else None
    return resolve_effective_config(raw_config=raw).auto_compression


def llm_request_compression_enabled(config: Mapping[str, Any] | None = None) -> bool:
    """Return whether the separate common request adapter is explicitly enabled."""
    raw = config if isinstance(config, Mapping) else None
    return resolve_effective_config(raw_config=raw).llm_request_enabled


def _first(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


@dataclass(frozen=True)
class EffectiveConfig:
    enabled: bool = True
    provider: str = "headroom"
    proxy_url: str = DEFAULT_PROXY_URL
    allow_remote_proxy: bool = False
    auto_compression: bool = True
    llm_request_enabled: bool = False
    llm_request_mode: str = "tool_results"
    min_tool_result_chars: int = DEFAULT_MIN_TOOL_RESULT_CHARS
    event_log_max_bytes: int = DEFAULT_EVENT_LOG_MAX_BYTES
    llm_request_cache_max: int = DEFAULT_LLM_REQUEST_CACHE_MAX
    visible_status_marker: bool = False
    first_turn_hint: bool = False
    experimental_below_min_terminal_aggregate: bool = False
    report_retention_days: int = DEFAULT_REPORT_RETENTION_DAYS
    report_max_bytes: int = DEFAULT_REPORT_MAX_BYTES
    report_prune_interval_seconds: int = DEFAULT_REPORT_PRUNE_INTERVAL_SECONDS
    compatibility_warnings: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


def resolve_effective_config(
    *,
    overrides: Mapping[str, Any] | None = None,
    raw_config: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> EffectiveConfig:
    """Resolve one typed authority while retaining legacy config aliases."""
    raw = dict(raw_config) if raw_config is not None else load_context_reduction_config(home)
    explicit = dict(overrides or {})
    environment: Mapping[str, str] = env if env is not None else os.environ

    def configured(*keys: str, default: Any = None) -> Any:
        value = _first(explicit, *keys, default=None)
        return value if value is not None else _first(raw, *keys, default=default)

    configured_url = str(configured("proxy_url", default=DEFAULT_PROXY_URL) or DEFAULT_PROXY_URL).strip().rstrip("/")
    parsed = urlparse(configured_url)
    base_host = str(configured("host", default=parsed.hostname or "127.0.0.1") or "127.0.0.1").strip()
    base_port = _bounded_int(configured("port", default=parsed.port or 8787), default=8787, minimum=1, maximum=65535)

    # Explicit endpoint overrides are top priority. Environment keeps the
    # historical host/port-over-URL behavior for compatibility.
    if any(key in explicit for key in ("proxy_url", "host", "port")):
        proxy_url = configured_url if "proxy_url" in explicit else f"http://{base_host}:{base_port}"
    else:
        env_host = str(environment.get("HEADROOM_HOST") or "").strip()
        env_port = str(environment.get("HEADROOM_PORT") or "").strip()
        env_url = str(environment.get("HEADROOM_PROXY_URL") or "").strip().rstrip("/")
        if env_host or env_port:
            proxy_url = f"http://{env_host or base_host}:{_bounded_int(env_port or base_port, default=base_port, minimum=1, maximum=65535)}"
        elif env_url:
            proxy_url = env_url
        else:
            proxy_url = f"http://{base_host}:{base_port}"

    mode = str(configured("mode", "compression_mode", default="") or "").strip().lower().replace("-", "_")
    yaml_auto = _first(raw, "auto_compression", "auto_compress", "auto_terminal", default=True)
    explicit_auto = _first(explicit, "auto_compression", "auto_compress", "auto_terminal", default=None)
    if explicit_auto is not None:
        auto_compression = _boolish(explicit_auto, default=True)
    elif environment.get("HEADROOM_AUTO_COMPRESSION") is not None:
        auto_compression = _boolish(environment.get("HEADROOM_AUTO_COMPRESSION"), default=True)
    elif mode:
        auto_compression = _boolish(mode, default=True)
    else:
        auto_compression = _boolish(yaml_auto, default=True)

    llm_value = configured("llm_request_middleware", default={})
    if isinstance(llm_value, Mapping):
        configured_llm_mode = str(llm_value.get("mode") or "").strip().lower().replace("-", "_")
        llm_mode = configured_llm_mode or "tool_results"
        if "enabled" in llm_value:
            llm_default = _boolish(llm_value.get("enabled"), default=False)
        else:
            llm_default = configured_llm_mode in {"tool_results", "on", "enabled", "auto"}
        if llm_mode in {"off", "disabled", "observe", "audit"}:
            llm_default = False
    else:
        llm_mode = "tool_results"
        llm_default = _boolish(llm_value, default=False)
    if environment.get("HEADROOM_LLM_REQUEST_COMPRESSION") is not None and "llm_request_middleware" not in explicit:
        llm_enabled = _boolish(environment.get("HEADROOM_LLM_REQUEST_COMPRESSION"), default=False)
    else:
        llm_enabled = llm_default

    allow_remote = _boolish(configured("allow_remote_proxy", default=False), default=False)
    if environment.get("HEADROOM_ALLOW_REMOTE_PROXY") is not None and "allow_remote_proxy" not in explicit:
        allow_remote = _boolish(environment.get("HEADROOM_ALLOW_REMOTE_PROXY"), default=False)

    min_chars_value = configured("min_tool_result_chars", default=DEFAULT_MIN_TOOL_RESULT_CHARS)
    if environment.get("HEADROOM_MIN_TOOL_RESULT_CHARS") is not None and "min_tool_result_chars" not in explicit:
        min_chars_value = environment.get("HEADROOM_MIN_TOOL_RESULT_CHARS")
    event_bytes = configured("event_log_max_bytes", "events_max_bytes", default=DEFAULT_EVENT_LOG_MAX_BYTES)

    def env_config_value(env_name: str, *keys: str, default: Any = None) -> Any:
        explicit_value = _first(explicit, *keys, default=None)
        if explicit_value is not None:
            return explicit_value
        if environment.get(env_name) is not None:
            return environment.get(env_name)
        return _first(raw, *keys, default=default)

    llm_cache_max = env_config_value(
        "HEADROOM_LLM_REQUEST_CACHE_MAX",
        "llm_request_cache_max",
        default=DEFAULT_LLM_REQUEST_CACHE_MAX,
    )
    visible_status_marker = _boolish(
        env_config_value("HEADROOM_VISIBLE_STATUS_MARKER", "visible_status_marker", default=False),
        default=False,
    )
    first_turn_hint = _boolish(
        env_config_value("HEADROOM_FIRST_TURN_HINT", "first_turn_hint", default=False),
        default=False,
    )
    experimental_below_min = _boolish(
        env_config_value(
            "HEADROOM_EXPERIMENTAL_BELOW_MIN_AGGREGATE",
            "experimental_below_min_terminal_aggregate",
            default=False,
        ),
        default=False,
    )
    report_retention_days = env_config_value(
        "HEADROOM_REPORT_RETENTION_DAYS",
        "report_retention_days",
        default=DEFAULT_REPORT_RETENTION_DAYS,
    )
    report_max_bytes = env_config_value(
        "HEADROOM_REPORT_MAX_BYTES",
        "report_max_bytes",
        default=DEFAULT_REPORT_MAX_BYTES,
    )
    report_prune_interval = env_config_value(
        "HEADROOM_REPORT_PRUNE_INTERVAL_SECONDS",
        "report_prune_interval_seconds",
        default=DEFAULT_REPORT_PRUNE_INTERVAL_SECONDS,
    )
    legacy_aliases = {
        "host": "proxy_url",
        "port": "proxy_url",
        "auto_compress": "auto_compression",
        "auto_terminal": "auto_compression",
        "compression_mode": "mode",
        "events_max_bytes": "event_log_max_bytes",
    }
    compatibility_warning_list = [
        f"legacy context_reduction.{key}; use context_reduction.{replacement}"
        for key, replacement in legacy_aliases.items()
        if key in raw or key in explicit
    ]
    if environment.get("HEADROOM_HOST") is not None:
        compatibility_warning_list.append("legacy HEADROOM_HOST; use HEADROOM_PROXY_URL")
    if environment.get("HEADROOM_PORT") is not None:
        compatibility_warning_list.append("legacy HEADROOM_PORT; use HEADROOM_PROXY_URL")
    compatibility_warnings = tuple(compatibility_warning_list)

    return EffectiveConfig(
        enabled=_boolish(configured("enabled", default=True), default=True),
        provider=str(configured("provider", default="headroom") or "headroom").strip().lower(),
        proxy_url=proxy_url,
        allow_remote_proxy=allow_remote,
        auto_compression=auto_compression,
        llm_request_enabled=llm_enabled,
        llm_request_mode=llm_mode,
        min_tool_result_chars=_bounded_int(min_chars_value, default=DEFAULT_MIN_TOOL_RESULT_CHARS, minimum=2_000, maximum=10_000_000),
        event_log_max_bytes=_bounded_int(event_bytes, default=DEFAULT_EVENT_LOG_MAX_BYTES, minimum=64_000, maximum=1_000_000_000),
        llm_request_cache_max=_bounded_int(llm_cache_max, default=DEFAULT_LLM_REQUEST_CACHE_MAX, minimum=64, maximum=100_000),
        visible_status_marker=visible_status_marker,
        first_turn_hint=first_turn_hint,
        experimental_below_min_terminal_aggregate=experimental_below_min,
        report_retention_days=_bounded_int(report_retention_days, default=DEFAULT_REPORT_RETENTION_DAYS, minimum=0, maximum=3_650),
        report_max_bytes=_bounded_int(report_max_bytes, default=DEFAULT_REPORT_MAX_BYTES, minimum=0, maximum=10_000_000_000),
        report_prune_interval_seconds=_bounded_int(
            report_prune_interval,
            default=DEFAULT_REPORT_PRUNE_INTERVAL_SECONDS,
            minimum=0,
            maximum=31_536_000,
        ),
        compatibility_warnings=compatibility_warnings,
        raw=raw,
    )
