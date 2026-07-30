import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_headroom_plugin.embedded_monitor import (
    _MODULE_NAME,
    embedded_monitor_mode,
    load_embedded_monitor,
    register_embedded_monitor,
)


class FakeCtx:
    def __init__(self):
        self.commands = []
        self.hooks = []

    def register_command(self, *args, **kwargs):
        self.commands.append((args, kwargs))

    def register_hook(self, *args):
        self.hooks.append(args)


class EmbeddedMonitorTest(unittest.TestCase):
    def test_clean_home_uses_embedded_monitor(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(embedded_monitor_mode(Path(td)), "embedded")

    def test_enabled_standalone_monitor_wins(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            plugin = home / "plugins" / "llm-monitor"
            plugin.mkdir(parents=True)
            (plugin / "plugin.yaml").write_text("name: llm-monitor\n", encoding="utf-8")
            (plugin / "__init__.py").write_text("def register(ctx): pass\n", encoding="utf-8")
            (home / "config.yaml").write_text(
                '{"plugins": {"enabled": ["llm-monitor"], "disabled": []}}\n',
                encoding="utf-8",
            )
            self.assertEqual(embedded_monitor_mode(home), "standalone")

    def test_explicit_disable_suppresses_embedded_monitor(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config.yaml").write_text(
                '{"plugins": {"enabled": [], "disabled": ["llm-monitor"]}}\n',
                encoding="utf-8",
            )
            self.assertEqual(embedded_monitor_mode(home), "disabled")

    def test_embedded_monitor_registers_default_on_metadata_surface(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HERMES_HOME": td}):
            sys.modules.pop(_MODULE_NAME, None)
            ctx = FakeCtx()
            try:
                self.assertEqual(register_embedded_monitor(ctx, home=Path(td)), "embedded")
                command_names = [args[0] for args, _kwargs in ctx.commands]
                hook_names = [args[0] for args in ctx.hooks]
                self.assertIn("llm-monitor", command_names)
                self.assertIn("pre_api_request", hook_names)
                self.assertIn("post_api_request", hook_names)
                self.assertIn("api_request_error", hook_names)
                status = load_embedded_monitor().handle_command("status")
                self.assertTrue(status.startswith("LLM monitor ON · mode=metadata"), status)
            finally:
                sys.modules.pop(_MODULE_NAME, None)


if __name__ == "__main__":
    unittest.main()