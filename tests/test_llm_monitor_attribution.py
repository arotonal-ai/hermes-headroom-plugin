import importlib.util
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "hermes_headroom_plugin" / "companions" / "llm-monitor" / "__init__.py"


class LlmMonitorAttributionTest(unittest.TestCase):
    def _load(self, home: str):
        old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = home
        self.addCleanup(self._restore_home, old_home)
        name = f"llm_monitor_attribution_test_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {MODULE_PATH}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        setattr(
            module,
            "_read_state",
            lambda: {**module._DEFAULT_STATE, "enabled": True, "headroom_summary": True},
        )
        return module

    @staticmethod
    def _restore_home(old_home):
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home

    @staticmethod
    def _write_events(home: str, rows):
        path = Path(home) / "control-plane" / "headroom" / "events" / "headroom-events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _compressed_event(**overrides):
        row = {
            "type": "headroom_tool_result",
            "telemetry_schema_version": "headroom.attribution.v2",
            "event_id": "event-a",
            "dedupe_key": "logical-transform-a",
            "session_id": "s1",
            "turn_id": "turn1",
            "task_id": "task1",
            "tool_call_id": "tool1",
            "api_request_id": "api0",
            "tool_name": "read_file",
            "lane": "file",
            "action": "compressed",
            "reason": "eligible_tool:read_file",
            "marker": "abc123def456",
            "tokens_saved": 700,
            "service_tokens_saved": 700,
            "model_facing_est_tokens_before": 1000,
            "model_facing_est_tokens_after": 200,
            "model_facing_est_tokens_saved": 800,
            "new_savings_event": True,
        }
        row.update(overrides)
        return row

    def test_dedupe_prefers_v2_logical_key_and_preserves_unkeyed_rows(self):
        with tempfile.TemporaryDirectory() as td:
            module = self._load(td)
            first = self._compressed_event()
            duplicate = self._compressed_event(event_id="event-b")
            unkeyed_a = {"type": "headroom_tool_result", "action": "skipped"}
            unkeyed_b = {"type": "headroom_tool_result", "action": "skipped"}
            unique, duplicates = module._dedupe_headroom_events([first, duplicate, unkeyed_a, unkeyed_b])
        self.assertEqual(duplicates, 1)
        self.assertEqual(len(unique), 3)

    def test_turn_summary_uses_model_facing_delta_and_ignores_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            module = self._load(td)
            rows = [
                self._compressed_event(),
                self._compressed_event(event_id="event-b"),
                {
                    "type": "headroom_tool_result",
                    "event_id": "event-exact",
                    "dedupe_key": "logical-exact",
                    "session_id": "s1",
                    "turn_id": "turn1",
                    "task_id": "task1",
                    "tool_call_id": "tool2",
                    "tool_name": "write_file",
                    "lane": "edit",
                    "action": "exact",
                    "reason": "exact_tool:write_file",
                },
            ]
            self._write_events(td, rows)
            line = module._headroom_turn_summary_line(
                {"session_id": "s1", "turn_id": "turn1", "task_id": "task1"}
            )
        self.assertIn("`1000→200`", line)
        self.assertIn("saved `800` (`80.0%`, est.)", line)
        self.assertIn("compressed `1`", line)
        self.assertIn("exact/skipped `1`", line)
        self.assertIn("dupes ignored `1`", line)
        self.assertNotIn("internal saved", line)

    def test_request_attribution_counts_retained_pressure_not_new_savings(self):
        with tempfile.TemporaryDirectory() as td:
            module = self._load(td)
            self._write_events(
                td,
                [self._compressed_event(), self._compressed_event(event_id="event-b")],
            )
            kwargs = {
                "session_id": "s1",
                "request": {
                    "body": {
                        "messages": [
                            {"role": "user", "content": "quoted marker=shouldnotmatch999"},
                            {
                                "role": "tool",
                                "tool_call_id": "tool1",
                                "content": "compressed result marker=abc123def456",
                            },
                        ]
                    }
                },
            }
            first = module._headroom_request_attribution(kwargs)
            second = module._headroom_request_attribution(kwargs)
        self.assertEqual(first["coverage"], "correlated")
        self.assertEqual(first["marker_count"], 1)
        self.assertEqual(first["correlated_event_count"], 1)
        self.assertEqual(first["metric_event_count"], 1)
        self.assertEqual(first["legacy_metric_event_count"], 0)
        self.assertEqual(first["marker_correlation_completeness_pct"], 100.0)
        self.assertEqual(first["model_facing_metric_coverage"], "full")
        self.assertEqual(first["model_facing_metric_completeness_pct"], 100.0)
        self.assertEqual(first["duplicate_events_ignored"], 1)
        self.assertEqual(first["retained_transform_est_tokens_before"], 1000)
        self.assertEqual(first["retained_transform_est_tokens_after"], 200)
        self.assertEqual(first["retained_transform_est_tokens_saved"], 800)
        self.assertFalse(first["counts_as_new_savings"])
        self.assertFalse(first["full_request_counterfactual_available"])
        self.assertEqual(first["first_observed_in_process_count"], 1)
        self.assertEqual(second["first_observed_in_process_count"], 0)

    def test_request_attribution_exposes_partial_v2_metric_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            module = self._load(td)
            legacy = self._compressed_event(
                event_id="legacy-event",
                dedupe_key="legacy-transform",
                marker="legacy123456",
                telemetry_schema_version="headroom.telemetry.v1",
            )
            for key in (
                "model_facing_est_tokens_before",
                "model_facing_est_tokens_after",
                "model_facing_est_tokens_saved",
            ):
                legacy.pop(key, None)
            self._write_events(td, [self._compressed_event(), legacy])
            attrs = module._headroom_request_attribution(
                {
                    "session_id": "s1",
                    "request_messages": [
                        {
                            "role": "tool",
                            "content": "marker=abc123def456 and marker=legacy123456",
                        }
                    ],
                }
            )
        self.assertEqual(attrs["coverage"], "correlated")
        self.assertEqual(attrs["correlated_event_count"], 2)
        self.assertEqual(attrs["metric_event_count"], 1)
        self.assertEqual(attrs["legacy_metric_event_count"], 1)
        self.assertEqual(attrs["model_facing_metric_coverage"], "partial")
        self.assertEqual(attrs["model_facing_metric_completeness_pct"], 50.0)
        self.assertEqual(attrs["retained_transform_est_tokens_before"], 1000)

    def test_pre_api_request_persists_attribution_without_raw_marker_list(self):
        with tempfile.TemporaryDirectory() as td:
            module = self._load(td)
            self._write_events(td, [self._compressed_event()])
            setattr(module, "_notify_visible_pre_call", lambda **_kwargs: None)
            module.on_pre_api_request(
                session_id="s1",
                turn_id="turn1",
                task_id="task1",
                platform="telegram",
                api_request_id="api1",
                provider="openai-codex",
                model="gpt-test",
                api_mode="responses",
                api_call_count=2,
                request={
                    "body": {
                        "messages": [
                            {"role": "tool", "content": "marker=abc123def456"},
                        ]
                    }
                },
                request_messages=[{"role": "tool", "content": "marker=abc123def456"}],
                request_char_count=1000,
                message_count=1,
                tool_count=0,
            )
            trace_files = list((Path(td) / "control-plane" / "llm-monitor" / "traces").glob("*.jsonl"))
            self.assertEqual(len(trace_files), 1)
            rows = [json.loads(line) for line in trace_files[0].read_text(encoding="utf-8").splitlines()]
            request_row = next(row for row in rows if row.get("type") == "llm_request")
        attribution = request_row["headroom_attribution"]
        self.assertEqual(attribution["coverage"], "correlated")
        self.assertEqual(attribution["metric_scope"], "retained_tool_transforms_in_request")
        self.assertFalse(attribution["counts_as_new_savings"])
        self.assertNotIn("markers", attribution)


if __name__ == "__main__":
    unittest.main()
