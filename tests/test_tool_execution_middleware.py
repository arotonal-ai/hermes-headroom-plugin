import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_headroom_plugin import middleware


class ToolExecutionMiddlewareTest(unittest.TestCase):
    def setUp(self):
        self._auto_compression_env = patch.dict(os.environ, {"HEADROOM_AUTO_COMPRESSION": "1"})
        self._auto_compression_env.start()
        self._hermes_home_tmp = tempfile.TemporaryDirectory()
        self._hermes_home_patch = patch(
            "hermes_headroom_plugin.observability.hermes_home",
            return_value=Path(self._hermes_home_tmp.name),
        )
        self._hermes_home_patch.start()

    def tearDown(self):
        self._hermes_home_patch.stop()
        self._hermes_home_tmp.cleanup()
        self._auto_compression_env.stop()

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

    def test_wrapped_tool_exception_propagates_unchanged(self):
        error = RuntimeError("tool failed before middleware transformation")

        def fail(_args):
            raise error

        with self.assertRaises(RuntimeError) as caught:
            middleware.on_tool_execution(tool_name="terminal", args={}, next_call=fail)
        self.assertIs(caught.exception, error)

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
            with patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}), patch(
                "hermes_headroom_plugin.provider_headroom.compress_messages", return_value=compressed
            ), patch("hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)):
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
                report = json.loads(Path(events[0]["report_path"]).read_text(encoding="utf-8"))
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
        self.assertEqual(event["telemetry_schema_version"], "headroom.attribution.v2")
        self.assertEqual(len(event["event_id"]), 32)
        self.assertEqual(len(event["dedupe_key"]), 64)
        self.assertEqual(event["service_metric_scope"], "headroom_internal_messages")
        self.assertEqual(event["model_facing_chars_before"], len(self._large_result()))
        self.assertEqual(event["model_facing_chars_after"], len(out))
        self.assertEqual(
            event["model_facing_est_tokens_saved"],
            middleware._rough_tokens_from_chars(len(self._large_result()))
            - middleware._rough_tokens_from_chars(len(out)),
        )
        self.assertEqual(event["measurement_scope"], "tool_result")
        self.assertTrue(event["new_savings_event"])
        self.assertGreaterEqual(event["compression_latency_ms"], 0)
        self.assertIn("auto-tool-", event["report_path"])
        self.assertEqual(report["model_facing_chars_before"], len(self._large_result()))
        self.assertEqual(report["model_facing_chars_after"], len(out))
        self.assertNotIn("delegate line", json.dumps(event, ensure_ascii=False))

    def test_event_ids_are_unique_while_logical_dedupe_keys_are_stable(self):
        large = self._large_result()
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}):
            for _ in range(2):
                middleware.on_tool_execution(
                    tool_name="write_file",
                    args={"path": "same.py", "content": "replacement"},
                    next_call=lambda args: large,
                    task_id="task-dedupe",
                    tool_call_id="tool-call-dedupe",
                    session_id="session-dedupe",
                    turn_id="turn-dedupe",
                    api_request_id="api-dedupe",
                )
            events = self._events(td)
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0]["event_id"], events[1]["event_id"])
        self.assertEqual(events[0]["dedupe_key"], events[1]["dedupe_key"])
        self.assertEqual(events[0]["model_facing_est_tokens_saved"], 0)

    def test_auto_compression_can_be_disabled_for_on_demand_mode(self):
        large = self._large_result()
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HEADROOM_AUTO_COMPRESSION": "0"}), patch(
            "hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.provider_headroom.readyz") as ready, patch(
            "hermes_headroom_plugin.provider_headroom.compress_messages"
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="delegate_task",
                args={"goal": "diagnostics"},
                next_call=lambda args: large,
                turn_id="turn-manual",
                platform="telegram",
            )
            events = self._events(td)
        self.assertEqual(out, large)
        ready.assert_not_called()
        compress.assert_not_called()
        self.assertEqual(events[-1]["action"], "skipped")
        self.assertEqual(events[-1]["reason"], "auto_compression_disabled")

    def test_auto_compression_can_be_disabled_by_boolean_config(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(middleware.auto_compression_enabled({"auto_compression": False}))
            self.assertFalse(middleware.auto_compression_enabled({"mode": "manual", "auto_terminal": True}))
            self.assertTrue(middleware.auto_compression_enabled({"auto_compression": True}))

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
            with patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}), patch(
                "hermes_headroom_plugin.provider_headroom.compress_messages", return_value=compressed
            ), patch("hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)):
                out = middleware.on_tool_execution(
                    tool_name="execute_code",
                    args={"code": "print synthetic diagnostics"},
                    next_call=lambda args: structured,
                    task_id="t-exec",
                    tool_call_id="tc-exec",
                )
                events = self._events(td)
        self.assertIsInstance(out, dict)
        self.assertEqual(out["status"], "success")
        self.assertTrue(out["headroom_auto_compressed"])
        self.assertEqual(out["headroom_compressed_field"], "output")
        self.assertIn("Headroom auto-compressed tool result", out["output"])
        self.assertIn("tool=execute_code", out["output"])
        self.assertIn("marker=exec123def456", out["output"])
        self.assertEqual(events[-1]["measurement_scope"], "structured_tool_result:output")
        self.assertEqual(
            events[-1]["model_facing_chars_before"],
            len(json.dumps(structured, ensure_ascii=False, default=str)),
        )
        self.assertEqual(
            events[-1]["model_facing_chars_after"],
            len(json.dumps(out, ensure_ascii=False, default=str)),
        )

    def test_large_read_file_is_compressible_with_exact_source_header(self):
        source = "".join(f"{i}|def function_{i}(): return {i}\n" for i in range(900))
        compressed = {
            "ok": True,
            "tokens_before": 9000,
            "tokens_after": 1500,
            "tokens_saved": 7500,
            "compression_ratio": 0.166,
            "messages": [{"role": "tool", "content": "source outline hash=read123def456"}],
        }
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.provider_headroom.compress_messages", return_value=compressed
        ):
            out = middleware.on_tool_execution(
                tool_name="read_file",
                args={"path": "/tmp/source.py", "offset": 1, "limit": 900},
                next_call=lambda args: source,
                session_id="s-read",
            )
            events = self._events(td)
        self.assertIn("classification: source_readback", out)
        self.assertIn("path=/tmp/source.py", out)
        self.assertIn("offset=1", out)
        self.assertIn("marker=read123def456", out)
        self.assertEqual(events[-1]["action"], "compressed")
        self.assertEqual(events[-1]["lane"], "file")

    def test_machine_consumer_read_file_keeps_structured_contract_exact(self):
        structured = {
            "content": self._large_result(lines=700),
            "total_lines": 700,
            "truncated": False,
        }
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.provider_headroom.compress_messages") as compress:
            out = middleware.on_tool_execution(
                tool_name="read_file",
                args={"path": "/tmp/machine-input.json", "offset": 1, "limit": 700},
                next_call=lambda args: structured,
                task_id="parent-execute-code-task",
                platform="telegram",
            )
            events = self._events(td)
        self.assertEqual(out, structured)
        compress.assert_not_called()
        self.assertEqual(events[-1]["action"], "exact")
        self.assertEqual(events[-1]["reason"], "machine_consumer_contract")
        self.assertEqual(events[-1]["exact_authority"], "original_machine_result")

    def test_fact_store_reads_compress_but_mutations_remain_exact(self):
        large = json.dumps({"results": [{"id": i, "content": "durable fact " + ("x" * 80)} for i in range(300)]})
        compressed = {
            "ok": True,
            "tokens_before": 10000,
            "tokens_after": 2000,
            "tokens_saved": 8000,
            "messages": [{"role": "tool", "content": "fact rows hash=fact123def456"}],
        }
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.provider_headroom.compress_messages", return_value=compressed
        ) as compress:
            read_out = middleware.on_tool_execution(
                tool_name="fact_store",
                args={"action": "probe", "entity": "architecture"},
                next_call=lambda args: large,
            )
            mutation_out = middleware.on_tool_execution(
                tool_name="fact_store",
                args={"action": "add", "content": "new durable fact"},
                next_call=lambda args: large,
            )
        self.assertIn("classification: source_readback", read_out)
        self.assertEqual(mutation_out, large)
        self.assertEqual(compress.call_count, 1)

    def test_readonly_mcp_is_compressible_and_mcp_write_is_exact(self):
        large = self._large_result(lines=500)
        compressed = {
            "ok": True,
            "tokens_before": 9000,
            "tokens_after": 1200,
            "tokens_saved": 7800,
            "messages": [{"role": "tool", "content": "artifact outline hash=mcp123def456"}],
        }
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.provider_headroom.compress_messages", return_value=compressed
        ) as compress:
            read_out = middleware.on_tool_execution(
                tool_name="mcp__open_design__get_artifact",
                args={"project": "demo", "entry": "index.html"},
                next_call=lambda args: large,
            )
            write_out = middleware.on_tool_execution(
                tool_name="mcp__open_design__write_file",
                args={"project": "demo", "path": "index.html", "content": "replacement"},
                next_call=lambda args: large,
            )
        self.assertIn("classification: source_readback", read_out)
        self.assertEqual(write_out, large)
        self.assertEqual(compress.call_count, 1)

    def test_mutating_tools_remain_exact_even_when_large(self):
        large = self._large_result()
        with patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.provider_headroom.compress_messages"
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="write_file",
                args={"path": "important.py", "content": "replacement"},
                next_call=lambda args: large,
            )
        self.assertEqual(out, large)
        compress.assert_not_called()


    def test_exact_mutation_events_use_specific_lanes_for_owner_attribution(self):
        large = self._large_result()
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.provider_headroom.compress_messages"
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="write_file",
                args={"path": "important.py", "content": "replacement"},
                next_call=lambda args: large,
            )
            events = self._events(td)
        self.assertEqual(out, large)
        compress.assert_not_called()
        self.assertEqual(events[-1]["action"], "exact")
        self.assertEqual(events[-1]["lane"], "edit")


    def test_terminal_json_output_field_is_unwrapped_for_compression_shape(self):
        captured = {}

        def fake_compress(messages):
            captured["messages"] = messages
            return {
                "ok": True,
                "tokens_before": 30000,
                "tokens_after": 300,
                "tokens_saved": 29700,
                "compression_ratio": 0.01,
                "messages": [
                    {
                        "role": "tool",
                        "name": "worker_trace",
                        "content": "[terminal log compressed. Retrieve more: hash=term123def456]",
                    }
                ],
            }

        raw_output = self._large_result(lines=1400)
        terminal_result = json.dumps({"output": raw_output, "exit_code": 0, "error": None})
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}
        ), patch("hermes_headroom_plugin.provider_headroom.compress_messages", side_effect=fake_compress), patch(
            "hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)
        ):
            out = middleware.on_tool_execution(
                tool_name="terminal",
                args={"command": "pytest -q"},
                next_call=lambda args: terminal_result,
                task_id="t-terminal-json",
                tool_call_id="tc-terminal-json",
            )
            reports = list((Path(td) / "control-plane" / "headroom" / "reports").glob("auto-tool-*-terminal.json"))
            report = json.loads(reports[0].read_text(encoding="utf-8"))

        tool_message = captured["messages"][-1]["content"]
        self.assertIn("delegate line 1399", tool_message)
        self.assertIn("exit_code=0", tool_message)
        self.assertNotIn('"output":', tool_message)
        self.assertIn("Headroom auto-compressed tool result", out)
        self.assertEqual(report["compression_input_shape"], "terminal_json_output_field")
        self.assertGreater(report["compression_input_chars"], 28000)

    def test_git_diff_terminal_result_remains_exact(self):
        large = self._large_result()
        with patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.provider_headroom.compress_messages"
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="terminal",
                args={"command": "git diff"},
                next_call=lambda args: large,
            )
        self.assertEqual(out, large)
        compress.assert_not_called()

    def test_terminal_below_min_aggregate_is_default_off(self):
        first = "terminal chunk A\n" * 1000
        second = "terminal chunk B\n" * 1000
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"HEADROOM_EXPERIMENTAL_BELOW_MIN_AGGREGATE": ""}
        ), patch(
            "hermes_headroom_plugin.reduction.MIN_TOOL_RESULT_CHARS", 28_000
        ), patch(
            "hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.provider_headroom.compress_messages"
        ) as compress:
            middleware._BELOW_MIN_AGGREGATE_BUFFERS.clear()
            out1 = middleware.on_tool_execution(
                tool_name="terminal",
                args={"command": "pytest -q"},
                next_call=lambda args: first,
                turn_id="turn-default-off",
            )
            out2 = middleware.on_tool_execution(
                tool_name="terminal",
                args={"command": "pytest -q"},
                next_call=lambda args: second,
                turn_id="turn-default-off",
            )
            events = self._events(td)
        self.assertEqual(out1, first)
        self.assertEqual(out2, second)
        compress.assert_not_called()
        self.assertEqual([event["reason"] for event in events], ["below_min_chars", "below_min_chars"])

    def test_terminal_below_min_aggregate_requires_opt_in_and_emits_one_marker(self):
        captured = {}

        def fake_compress(messages, proxy_url=None):
            captured["messages"] = messages
            return {
                "ok": True,
                "tokens_before": 12000,
                "tokens_after": 900,
                "tokens_saved": 11100,
                "compression_ratio": 0.075,
                "messages": [
                    {"role": "tool", "content": "[aggregate compressed. Retrieve more: hash=belowagg123456]"}
                ],
            }

        first = "tests/test_a.py::test_a FAILED warning chunk A\n" * 360
        second = "tests/test_b.py::test_b FAILED warning chunk B\n" * 360
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HEADROOM_EXPERIMENTAL_BELOW_MIN_AGGREGATE": "1"}), patch(
            "hermes_headroom_plugin.reduction.MIN_TOOL_RESULT_CHARS", 28_000
        ), patch(
            "hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True, "proxy_url": "http://127.0.0.1:28787"}), patch(
            "hermes_headroom_plugin.provider_headroom.compress_messages", side_effect=fake_compress
        ):
            middleware._BELOW_MIN_AGGREGATE_BUFFERS.clear()
            out1 = middleware.on_tool_execution(
                tool_name="terminal",
                args={"command": "pytest -q"},
                next_call=lambda args: first,
                turn_id="turn-aggregate",
                platform="telegram",
            )
            out2 = middleware.on_tool_execution(
                tool_name="terminal",
                args={"command": "pytest -q"},
                next_call=lambda args: second,
                turn_id="turn-aggregate",
                platform="telegram",
            )
            events = self._events(td)
            reports = list((Path(td) / "control-plane" / "headroom" / "reports").glob("auto-tool-*-terminal-below-min-aggregate.json"))
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            source_path = Path(report["source_path"])
            source_text = source_path.read_text(encoding="utf-8")
        self.assertEqual(out1, first)
        self.assertIn("Headroom auto-compressed below-min terminal aggregate", out2)
        self.assertIn("marker=belowagg123456", out2)
        self.assertIn("chunk 1/2", source_text)
        self.assertIn("chunk 2/2", source_text)
        self.assertIn("terminal_below_min_per_turn_aggregate", json.dumps(report))
        compression_body = captured["messages"][-1]["content"]
        self.assertIn("===== BOUNDED TERMINAL CHUNKS =====", compression_body)
        self.assertNotIn("policy_mutation", compression_body)
        self.assertNotIn("global_threshold_change", compression_body)
        self.assertNotIn("exact_commands_relaxed", compression_body)
        self.assertEqual(events[0]["reason"], "below_min_chars")
        self.assertEqual(events[1]["action"], "compressed")
        self.assertEqual(events[1]["reason"], "below_min_aggregate")
        self.assertEqual(events[1]["marker"], "belowagg123456")

    def test_terminal_below_min_aggregate_does_not_override_exact_commands(self):
        first = "diff --git a/file b/file\n" * 2000
        with patch.dict(os.environ, {"HEADROOM_EXPERIMENTAL_BELOW_MIN_AGGREGATE": "1"}), patch(
            "hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True, "proxy_url": "http://127.0.0.1:28787"}
        ), patch("hermes_headroom_plugin.provider_headroom.compress_messages") as compress:
            middleware._BELOW_MIN_AGGREGATE_BUFFERS.clear()
            out = middleware.on_tool_execution(
                tool_name="terminal",
                args={"command": "git diff"},
                next_call=lambda args: first,
                turn_id="turn-exact-command",
            )
        self.assertEqual(out, first)
        compress.assert_not_called()
        self.assertEqual(middleware._BELOW_MIN_AGGREGATE_BUFFERS, {})

    def test_failure_trace_with_rollback_anchor_remains_exact(self):
        trace = (
            "frame=0173 state=failed exception=ChecksumMismatch rollback=segment_42\n"
            + "synthetic trace detail\n" * 2500
        )
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.provider_headroom.compress_messages"
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="terminal",
                args={"command": "python synthetic_trace.py"},
                next_call=lambda args: trace,
            )
            events = self._events(td)
        self.assertEqual(out, trace)
        compress.assert_not_called()
        self.assertEqual(events[-1]["action"], "exact")
        self.assertEqual(events[-1]["reason"], "protected_recovery_integrity_trace")


    def test_small_delegate_result_remains_exact(self):
        small = "short final packet"
        with patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.provider_headroom.compress_messages"
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
            "hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": False, "body": "down"}), patch(
            "hermes_headroom_plugin.provider_headroom.compress_messages"
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
            "hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.provider_headroom.compress_messages"
        ) as compress:
            self.assertEqual(
                middleware.on_tool_execution(
                    tool_name="write_file",
                    args={"path": "important.py", "content": "replacement"},
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
        self.assertEqual(events[0]["reason"], "exact_tool:write_file")
        self.assertEqual(events[1]["reason"], "protected_control_or_sensitive_material")
        self.assertEqual(events[1]["protected_hits"], 1)
        self.assertEqual(events[2]["reason"], "below_min_chars")
        serialized = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("delegate line", serialized)
        self.assertNotIn("PRIVATE KEY", serialized)
        self.assertNotIn(synthetic_secret, serialized)

    def test_pre_llm_platform_context_fills_tool_event_platform(self):
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.provider_headroom.compress_messages"
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
            "hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.observability.load_context_reduction_config", return_value={"event_log_max_bytes": 64000}
        ), patch("hermes_headroom_plugin.provider_headroom.compress_messages"):
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

    def test_request_surface_and_scope_are_recorded_on_compression_event(self):
        compressed = {
            "ok": True,
            "tokens_before": 20000,
            "tokens_after": 200,
            "tokens_saved": 19800,
            "compression_ratio": 0.01,
            "messages": [{"role": "tool", "content": "Retrieve more: hash=requestsurface123"}],
        }
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.observability.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.provider_headroom.compress_messages", return_value=compressed
        ):
            out = middleware.compress_tool_result_for_context(
                tool_name="terminal",
                args={"command": "pytest -q"},
                result=self._large_result(),
                task_id="task-request",
                tool_call_id="call-request",
                session_id="session-request",
                turn_id="turn-request",
                api_request_id="api-request",
                event_surface="llm_request",
                measurement_scope_override="llm_request_tool_result:chat_completions",
                allow_below_min_aggregate=False,
            )
            events = self._events(td)
            report = json.loads(Path(events[-1]["report_path"]).read_text(encoding="utf-8"))
        self.assertIsInstance(out, str)
        self.assertEqual(events[-1]["surface"], "llm_request")
        self.assertEqual(events[-1]["measurement_scope"], "llm_request_tool_result:chat_completions")
        self.assertEqual(report["surface"], "llm_request")


if __name__ == "__main__":
    unittest.main()
