"""Default-on embedded llm-monitor registration for the Headroom plugin.

Hermes does not currently support declarative dependencies between user plugins.
This adapter exposes the bundled metadata-only monitor from the primary plugin
without mutating Hermes config or installing a second plugin at import time.
An explicitly disabled or enabled standalone ``llm-monitor`` always wins.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Literal

from .config import hermes_home

try:
    import yaml
except Exception:  # pragma: no cover - PyYAML is a package dependency
    yaml = None  # type: ignore


_LOG = logging.getLogger(__name__)
_MODULE_NAME = "hermes_headroom_plugin._embedded_llm_monitor"
MonitorMode = Literal["embedded", "standalone", "disabled", "error"]


def _plugin_config(home: Path) -> tuple[set[str], set[str], bool]:
    path = home / "config.yaml"
    if not path.exists():
        return set(), set(), True
    if yaml is None:
        return set(), set(), False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        plugins = data.get("plugins") if isinstance(data, dict) else {}
        if not isinstance(plugins, dict):
            return set(), set(), False
        raw_enabled = plugins.get("enabled", [])
        raw_disabled = plugins.get("disabled", [])
        if not isinstance(raw_enabled, list) or not isinstance(raw_disabled, list):
            return set(), set(), False
        return (
            {str(value) for value in raw_enabled},
            {str(value) for value in raw_disabled},
            True,
        )
    except Exception:
        return set(), set(), False


def embedded_monitor_mode(home: Path | None = None) -> MonitorMode:
    """Choose one monitor owner without changing files or configuration."""
    target_home = (home or hermes_home()).expanduser().resolve()
    enabled, disabled, config_ok = _plugin_config(target_home)
    standalone = target_home / "plugins" / "llm-monitor"
    standalone_present = (
        (standalone / "plugin.yaml").is_file()
        and (standalone / "__init__.py").is_file()
    )

    if "llm-monitor" in disabled:
        return "disabled"
    if standalone_present and ("llm-monitor" in enabled or not config_ok):
        return "standalone"
    return "embedded"


def load_embedded_monitor() -> ModuleType:
    existing = sys.modules.get(_MODULE_NAME)
    if isinstance(existing, ModuleType):
        return existing

    source = Path(__file__).parent / "companions" / "llm-monitor" / "__init__.py"
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load bundled llm-monitor from {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_MODULE_NAME, None)
        raise
    return module


def register_embedded_monitor(ctx, *, home: Path | None = None) -> MonitorMode:
    """Register metadata-only monitoring unless standalone/disable owns it."""
    mode = embedded_monitor_mode(home)
    if mode != "embedded":
        return mode
    try:
        module = load_embedded_monitor()
        module.register(ctx)
        return "embedded"
    except Exception as exc:  # Headroom must remain fail-open if observability fails.
        _LOG.warning("Bundled llm-monitor registration failed: %s", exc)
        return "error"
