import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hermes_headroom_plugin.commands import _render_status


class CommandStatusTest(unittest.TestCase):
    def test_status_surfaces_legacy_config_warnings(self):
        effective = SimpleNamespace(
            auto_compression=True,
            compatibility_warnings=(
                "legacy context_reduction.auto_terminal; use context_reduction.auto_compression",
            ),
        )
        with patch(
            "hermes_headroom_plugin.commands.visible_status_marker_enabled", return_value=False
        ), patch(
            "hermes_headroom_plugin.commands.resolve_effective_config", return_value=effective
        ):
            rendered = _render_status(
                {"ok": True, "proxy_url": "http://127.0.0.1:28787", "status": 200}
            )
        self.assertIn("auto_compression=on", rendered)
        self.assertIn("config_warnings=legacy context_reduction.auto_terminal", rendered)

    def test_status_omits_warning_segment_for_canonical_config(self):
        effective = SimpleNamespace(auto_compression=False, compatibility_warnings=())
        with patch(
            "hermes_headroom_plugin.commands.visible_status_marker_enabled", return_value=False
        ), patch(
            "hermes_headroom_plugin.commands.resolve_effective_config", return_value=effective
        ):
            rendered = _render_status(
                {"ok": True, "proxy_url": "http://127.0.0.1:28787", "status": 200}
            )
        self.assertIn("auto_compression=manual", rendered)
        self.assertNotIn("config_warnings=", rendered)


if __name__ == "__main__":
    unittest.main()
