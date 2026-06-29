import os
import unittest
from unittest.mock import patch

from hermes_headroom_plugin import hooks


class HookStatusMarkerTest(unittest.TestCase):
    def _preserve_env(self):
        return {"HEADROOM_VISIBLE_STATUS_MARKER": os.environ.get("HEADROOM_VISIBLE_STATUS_MARKER")}

    def _restore_env(self, old):
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_visible_marker_enabled_by_default_and_reports_ready(self):
        old = self._preserve_env()
        try:
            os.environ.pop("HEADROOM_VISIBLE_STATUS_MARKER", None)
            with patch("hermes_headroom_plugin.hooks.load_context_reduction_config", return_value={}), patch(
                "hermes_headroom_plugin.hooks.readyz",
                return_value={"ok": True, "proxy_url": "http://127.0.0.1:28787", "status": 200},
            ):
                text = hooks.on_transform_llm_output("hello")
        finally:
            self._restore_env(old)
        self.assertEqual(text, "[HR✓] hello")

    def test_visible_marker_reports_unhealthy_runtime(self):
        old = self._preserve_env()
        try:
            os.environ.pop("HEADROOM_VISIBLE_STATUS_MARKER", None)
            with patch("hermes_headroom_plugin.hooks.load_context_reduction_config", return_value={}), patch(
                "hermes_headroom_plugin.hooks.readyz",
                return_value={"ok": False, "proxy_url": "http://127.0.0.1:28787", "status": None},
            ):
                text = hooks.on_transform_llm_output("hello")
        finally:
            self._restore_env(old)
        self.assertEqual(text, "[HR!] hello")

    def test_visible_marker_can_be_disabled_by_config(self):
        old = self._preserve_env()
        try:
            os.environ.pop("HEADROOM_VISIBLE_STATUS_MARKER", None)
            with patch("hermes_headroom_plugin.hooks.load_context_reduction_config", return_value={"visible_status_marker": False}):
                text = hooks.on_transform_llm_output("hello")
        finally:
            self._restore_env(old)
        self.assertIsNone(text)

    def test_visible_marker_can_be_disabled_by_env(self):
        old = self._preserve_env()
        try:
            os.environ["HEADROOM_VISIBLE_STATUS_MARKER"] = "0"
            text = hooks.on_transform_llm_output("hello")
        finally:
            self._restore_env(old)
        self.assertIsNone(text)

    def test_visible_marker_does_not_duplicate_existing_prefix(self):
        old = self._preserve_env()
        try:
            os.environ.pop("HEADROOM_VISIBLE_STATUS_MARKER", None)
            with patch("hermes_headroom_plugin.hooks.load_context_reduction_config", return_value={}), patch(
                "hermes_headroom_plugin.hooks.readyz",
                return_value={"ok": True},
            ):
                text = hooks.on_transform_llm_output("[HR✓] already marked")
        finally:
            self._restore_env(old)
        self.assertIsNone(text)


if __name__ == "__main__":
    unittest.main()
