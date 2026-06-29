import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_headroom_plugin import middleware


class ToolExecutionMiddlewareTest(unittest.TestCase):
    def _large_result(self, lines=1200):
        return "".join(
            f"delegate line {i} WARNING verification PASS path=/tmp/delegate/{i}\n"
            for i in range(lines)
        )

    def test_delegate_task_large_result_is_compressed_when_proxy_ready(self):
        with tempfile.TemporaryDirectory() as td:
            compressed = {
                "ok": True,
                "tokens_before": 30000,
                "tokens_after": 300,
                "tokens_saved": 29700,
                "compression_ratio": 0.01,
                "messages": [
                    {
                        "role": "tool",
                        "name": "worker_trace",
                        "content": "[1200 lines compressed. Retrieve more: hash=abc123def456]",
                    }
                ],
            }
            with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
                "hermes_headroom_plugin.middleware.compress_messages", return_value=compressed
            ), patch("hermes_headroom_plugin.middleware.hermes_home", return_value=Path(td)):
                out = middleware.on_tool_execution(
                    tool_name="delegate_task",
                    args={"goal": "fan-in worker diagnostics"},
                    next_call=lambda args: self._large_result(),
                    task_id="t1",
                    tool_call_id="tc1",
                )
        self.assertIn("Headroom auto-compressed tool result", out)
        self.assertIn("tool=delegate_task", out)
        self.assertIn("marker=abc123def456", out)
        self.assertIn("headroom_retrieve", out)

    def test_exact_tools_remain_exact_even_when_large(self):
        large = self._large_result()
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages"
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="read_file",
                args={"path": "important.py"},
                next_call=lambda args: large,
            )
        self.assertEqual(out, large)
        compress.assert_not_called()

    def test_git_diff_terminal_result_remains_exact(self):
        large = self._large_result()
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages"
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="terminal",
                args={"command": "git diff"},
                next_call=lambda args: large,
            )
        self.assertEqual(out, large)
        compress.assert_not_called()

    def test_small_delegate_result_remains_exact(self):
        small = "short final packet"
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages"
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="delegate_task",
                args={"goal": "small"},
                next_call=lambda args: small,
            )
        self.assertEqual(out, small)
        compress.assert_not_called()

    def test_unhealthy_proxy_fails_open(self):
        large = self._large_result()
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": False}), patch(
            "hermes_headroom_plugin.middleware.compress_messages"
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="delegate_task",
                args={"goal": "large"},
                next_call=lambda args: large,
            )
        self.assertEqual(out, large)
        compress.assert_not_called()

    def test_extract_markers_supports_ccr_and_hash_forms(self):
        messages = [{"content": "<<ccr:abc123,base64,4KB>> and Retrieve more: hash=def4567890."}]
        self.assertEqual(middleware._extract_markers(messages), ["abc123", "def4567890"])


if __name__ == "__main__":
    unittest.main()
