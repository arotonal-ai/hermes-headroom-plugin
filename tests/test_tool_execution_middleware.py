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

    def _events(self, root: str):
        path = Path(root) / "control-plane" / "headroom" / "events" / "headroom-events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

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
                    session_id="s1",
                    turn_id="turn1",
                    api_request_id="api1",
                    platform="telegram",
                )
                events = self._events(td)
        self.assertIn("Headroom auto-compressed tool result", out)
        self.assertIn("tool=delegate_task", out)
        self.assertIn("marker=abc123def456", out)
        self.assertIn("headroom_retrieve", out)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["action"], "compressed")
        self.assertEqual(event["tool_name"], "delegate_task")
        self.assertEqual(event["lane"], "delegate")
        self.assertEqual(event["session_id"], "s1")
        self.assertEqual(event["turn_id"], "turn1")
        self.assertEqual(event["api_request_id"], "api1")
        self.assertEqual(event["platform"], "telegram")
        self.assertEqual(event["tokens_saved"], 29700)
        self.assertEqual(event["marker"], "abc123def456")
        self.assertIn("auto-tool-", event["report_path"])
        self.assertNotIn("delegate line", json.dumps(event, ensure_ascii=False))

    def test_structured_execute_code_output_compresses_output_field(self):
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
                        "content": "[execute_code output compressed. Retrieve more: hash=exec123def456]",
                    }
                ],
            }
            structured = {"status": "success", "output": self._large_result(), "duration_seconds": 0.1}
            with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
                "hermes_headroom_plugin.middleware.compress_messages", return_value=compressed
            ), patch("hermes_headroom_plugin.middleware.hermes_home", return_value=Path(td)):
                out = middleware.on_tool_execution(
                    tool_name="execute_code",
                    args={"code": "print synthetic diagnostics"},
                    next_call=lambda args: structured,
                    task_id="t-exec",
                    tool_call_id="tc-exec",
                )
        self.assertIsInstance(out, dict)
        self.assertEqual(out["status"], "success")
        self.assertTrue(out["headroom_auto_compressed"])
        self.assertEqual(out["headroom_compressed_field"], "output")
        self.assertIn("Headroom auto-compressed tool result", out["output"])
        self.assertIn("tool=execute_code", out["output"])
        self.assertIn("marker=exec123def456", out["output"])

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
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.middleware.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": False, "body": "down"}), patch(
            "hermes_headroom_plugin.middleware.compress_messages"
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="delegate_task",
                args={"goal": "large"},
                next_call=lambda args: large,
            )
            events = self._events(td)
        self.assertEqual(out, large)
        compress.assert_not_called()
        self.assertEqual(events[-1]["action"], "runtime_unavailable")
        self.assertEqual(events[-1]["reason"], "proxy_not_ready")

    def test_event_log_records_exact_blocked_and_skipped_without_raw_payloads(self):
        large = self._large_result()
        small = "short final packet"
        protected = "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nSYNTHETIC_PRIVATE_KEY_BODY\n" + large
        synthetic_secret = "SYNTHETIC_SECRET_1234567890"
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.middleware.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages"
        ) as compress:
            self.assertEqual(
                middleware.on_tool_execution(
                    tool_name="read_file",
                    args={"path": "important.py"},
                    next_call=lambda args: large,
                ),
                large,
            )
            self.assertEqual(
                middleware.on_tool_execution(
                    tool_name="terminal",
                    args={"command": "journalctl --user", "api_token": synthetic_secret},
                    next_call=lambda args: protected,
                ),
                protected,
            )
            self.assertEqual(
                middleware.on_tool_execution(
                    tool_name="delegate_task",
                    args={"goal": "small"},
                    next_call=lambda args: small,
                ),
                small,
            )
            events = self._events(td)

        compress.assert_not_called()
        actions = [event["action"] for event in events]
        self.assertEqual(actions, ["exact", "blocked", "skipped"])
        self.assertEqual(events[0]["reason"], "exact_tool:read_file")
        self.assertEqual(events[1]["reason"], "protected_control_or_sensitive_material")
        self.assertEqual(events[1]["protected_hits"], 1)
        self.assertEqual(events[2]["reason"], "below_min_chars")
        serialized = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("delegate line", serialized)
        self.assertNotIn("PRIVATE KEY", serialized)
        self.assertNotIn(synthetic_secret, serialized)

    def test_pre_llm_platform_context_fills_tool_event_platform(self):
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.middleware.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages"
        ) as compress:
            middleware.remember_platform_context(session_id="s-platform", task_id="task-platform", turn_id="turn-platform", platform="telegram")
            out = middleware.on_tool_execution(
                tool_name="delegate_task",
                args={"goal": "small"},
                next_call=lambda args: "short exact result",
                session_id="s-platform",
                task_id="task-platform",
                turn_id="turn-platform",
            )
            events = self._events(td)
        self.assertEqual(out, "short exact result")
        compress.assert_not_called()
        self.assertEqual(events[-1]["platform"], "telegram")

    def test_event_log_rotates_when_size_limit_is_exceeded(self):
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.middleware.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.load_context_reduction_config", return_value={"event_log_max_bytes": 64000}
        ), patch("hermes_headroom_plugin.middleware.compress_messages"):
            path = Path(td) / "control-plane" / "headroom" / "events" / "headroom-events.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text("x" * 65000, encoding="utf-8")
            out = middleware.on_tool_execution(
                tool_name="delegate_task",
                args={"goal": "small"},
                next_call=lambda args: "short exact result",
                session_id="s-rotate",
                task_id="task-rotate",
                turn_id="turn-rotate",
            )
            events = self._events(td)
            rotated = path.with_name(path.name + ".1")
            rotated_exists = rotated.exists()
            rotated_size = rotated.stat().st_size if rotated_exists else 0
        self.assertEqual(out, "short exact result")
        self.assertEqual(events[-1]["turn_id"], "turn-rotate")
        self.assertTrue(rotated_exists)
        self.assertGreater(rotated_size, 64000)

    def test_extract_markers_supports_ccr_hash_and_marker_forms(self):
        messages = [{"content": "<<ccr:abc123,base64,4KB>> and Retrieve more: hash=def4567890 marker=feedface1234."}]
        self.assertEqual(middleware._extract_markers(messages), ["abc123", "def4567890", "feedface1234"])


if __name__ == "__main__":
    unittest.main()
