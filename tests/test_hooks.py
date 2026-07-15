import os
import unittest
from unittest.mock import patch

from hermes_headroom_plugin import hooks


class HookStatusMarkerTest(unittest.TestCase):
    ENV_KEYS = ("HEADROOM_VISIBLE_STATUS_MARKER", "HEADROOM_FIRST_TURN_HINT")

    def _preserve_env(self):
        return {key: os.environ.get(key) for key in self.ENV_KEYS}

    def _restore_env(self, old):
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_visible_marker_disabled_by_default(self):
        old = self._preserve_env()
        try:
            os.environ.pop("HEADROOM_VISIBLE_STATUS_MARKER", None)
            with patch("hermes_headroom_plugin.hooks.load_context_reduction_config", return_value={}), patch(
                "hermes_headroom_plugin.hooks.readyz"
            ) as ready:
                text = hooks.on_transform_llm_output("hello")
        finally:
            self._restore_env(old)
        self.assertIsNone(text)
        ready.assert_not_called()

    def test_visible_marker_can_be_enabled_and_reports_ready(self):
        old = self._preserve_env()
        try:
            os.environ["HEADROOM_VISIBLE_STATUS_MARKER"] = "1"
            with patch(
                "hermes_headroom_plugin.hooks.readyz",
                return_value={"ok": True, "proxy_url": "http://127.0.0.1:28787", "status": 200},
            ):
                text = hooks.on_transform_llm_output("hello")
        finally:
            self._restore_env(old)
        self.assertEqual(text, "[HR✓] hello")

    def test_visible_marker_reports_unhealthy_runtime_when_enabled(self):
        old = self._preserve_env()
        try:
            os.environ["HEADROOM_VISIBLE_STATUS_MARKER"] = "1"
            with patch(
                "hermes_headroom_plugin.hooks.readyz",
                return_value={"ok": False, "proxy_url": "http://127.0.0.1:28787", "status": None},
            ):
                text = hooks.on_transform_llm_output("hello")
        finally:
            self._restore_env(old)
        self.assertEqual(text, "[HR!] hello")

    def test_visible_marker_can_be_enabled_by_config(self):
        old = self._preserve_env()
        try:
            os.environ.pop("HEADROOM_VISIBLE_STATUS_MARKER", None)
            with patch("hermes_headroom_plugin.hooks.load_context_reduction_config", return_value={"visible_status_marker": True}), patch(
                "hermes_headroom_plugin.hooks.readyz", return_value={"ok": True}
            ):
                text = hooks.on_transform_llm_output("hello")
        finally:
            self._restore_env(old)
        self.assertEqual(text, "[HR✓] hello")

    def test_visible_marker_does_not_duplicate_existing_prefix(self):
        old = self._preserve_env()
        try:
            os.environ["HEADROOM_VISIBLE_STATUS_MARKER"] = "1"
            text = hooks.on_transform_llm_output("[HR✓] already marked")
        finally:
            self._restore_env(old)
        self.assertIsNone(text)

    def test_first_turn_hint_disabled_by_default(self):
        old = self._preserve_env()
        try:
            os.environ.pop("HEADROOM_FIRST_TURN_HINT", None)
            with patch("hermes_headroom_plugin.hooks.load_context_reduction_config", return_value={}), patch(
                "hermes_headroom_plugin.hooks.readyz"
            ) as ready, patch("hermes_headroom_plugin.hooks.remember_platform_context"):
                result = hooks.on_pre_llm_call(is_first_turn=True, platform="telegram")
        finally:
            self._restore_env(old)
        self.assertIsNone(result)
        ready.assert_not_called()

    def test_first_turn_hint_can_be_enabled_by_config(self):
        old = self._preserve_env()
        try:
            os.environ.pop("HEADROOM_FIRST_TURN_HINT", None)
            with patch("hermes_headroom_plugin.hooks.load_context_reduction_config", return_value={"first_turn_hint": True}), patch(
                "hermes_headroom_plugin.hooks.readyz", return_value={"ok": True}
            ), patch("hermes_headroom_plugin.hooks.remember_platform_context"):
                result = hooks.on_pre_llm_call(is_first_turn=True, platform="telegram")
        finally:
            self._restore_env(old)
        self.assertIsInstance(result, dict)
        assert isinstance(result, dict)
        self.assertIn("eligible bulky intermediate tool results", result["context"])


if __name__ == "__main__":
    unittest.main()
