from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_headroom_plugin.config import EffectiveConfig, resolve_effective_config
from hermes_headroom_plugin.contracts import ReductionContext
from hermes_headroom_plugin.local_exact_store import (
    ERROR,
    EXACT,
    EXPIRED,
    MISSING,
    REDACTED,
    _private_file,
    retain_local_source,
    retrieve_local_source_result,
)
from hermes_headroom_plugin.net_ledger import append_retrieval_event, build_net_ledger
from hermes_headroom_plugin.policy import semantic_admission
from hermes_headroom_plugin.provider_headroom import HeadroomReductionProvider
from hermes_headroom_plugin.reduction import compress_tool_result_for_context
from hermes_headroom_plugin.tools import handle_headroom_retrieve
from hermes_headroom_plugin.wrappers import compress_trace


class MultipartIntegrityTest(unittest.TestCase):
    def test_provider_preserves_all_markers_and_rejects_ambiguous_set(self):
        response = {
            "ok": True,
            "messages": [{"role": "tool", "content": "one <<ccr:marker-one>> two <<ccr:marker-two>>"}],
            "markers": ["marker-one", "marker-two"],
            "tokens_before": 5000,
            "tokens_after": 100,
            "tokens_saved": 4900,
        }
        provider = HeadroomReductionProvider()
        with patch("hermes_headroom_plugin.provider_headroom.compress_messages", return_value=response):
            result = provider.compress([{"role": "tool", "content": "large"}], ReductionContext())
        self.assertTrue(result.ok)
        self.assertEqual(result.marker, "")
        self.assertEqual(result.markers, ("marker-one", "marker-two"))
        self.assertFalse(result.metrics["marker_integrity_ok"])
        self.assertEqual(result.metrics["marker_count"], 2)

    def test_model_facing_reduction_fails_open_on_ambiguous_multipart_markers(self):
        source = "\n".join(f"worker trace line={index} status=ok" for index in range(900))
        response = {
            "ok": True,
            "messages": [
                {"role": "assistant", "content": "summary <<ccr:marker-one>>"},
                {"role": "tool", "content": "continuation <<ccr:marker-two>>"},
            ],
            "markers": ["marker-one", "marker-two"],
            "tokens_before": 7000,
            "tokens_after": 200,
            "tokens_saved": 6800,
        }
        with tempfile.TemporaryDirectory() as td, patch.dict("os.environ", {"HERMES_HOME": td}), patch(
            "hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}
        ), patch("hermes_headroom_plugin.provider_headroom.compress_messages", return_value=response):
            reduced = compress_tool_result_for_context(tool_name="delegate_task", args={}, result=source)
        self.assertIsNone(reduced)

    def test_worker_wrapper_never_selects_one_marker_from_ambiguous_set(self):
        response = {
            "ok": True,
            "messages": [{"role": "tool", "content": "one <<ccr:marker-one>> two <<ccr:marker-two>>"}],
            "tokens_before": 5000,
            "tokens_after": 100,
            "tokens_saved": 4900,
        }
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.wrappers.compress_messages", return_value=response
        ):
            result = compress_trace("http://127.0.0.1:8787", "worker", "focus", "trace\n" * 1000, Path(td), 100_000)
        self.assertEqual(result["status"], "ambiguous_marker_set")
        self.assertEqual(result["marker_count"], 2)
        self.assertIsNone(result["marker"])


class SemanticAdmissionTest(unittest.TestCase):
    def test_hot_exact_classes_and_failure_traces_remain_exact(self):
        cases = [
            ("read_file", {"path": "/tmp/a"}, "source text", "source_readback"),
            ("skill_view", {"name": "promptkit"}, "skill instructions", "prompt_or_skill"),
            ("fact_store", {"action": "probe"}, "remembered facts", "memory_or_recall"),
            ("browser_cdp", {"method": "DOM.getDocument"}, "selector=#save target_id=t1", "interaction_state"),
            ("terminal", {}, "ERROR deploy failed; rollback restored prior service", "failure_trace"),
        ]
        for tool, args, result, expected_class in cases:
            with self.subTest(tool=tool):
                decision = semantic_admission(tool, args, result, age="hot")
                self.assertFalse(decision.compress)
                self.assertEqual(decision.data_class, expected_class)
                self.assertIn(decision.outcome, {"always_exact", "hot_exact_then_cold_compact"})

    def test_safe_raw_and_worker_traces_and_cold_history_are_eligible(self):
        worker = semantic_admission("delegate_task", {}, "worker trace status=ok\n" * 100, age="hot")
        self.assertTrue(worker.compress)
        self.assertEqual(worker.data_class, "worker_trace_raw")

        logs = semantic_admission("terminal", {}, "INFO request complete status=ok\n" * 100, age="hot")
        self.assertTrue(logs.compress)
        self.assertEqual(logs.data_class, "diagnostic_trace")

        cold = semantic_admission(
            "read_file",
            {"path": "/tmp/a"},
            "source text",
            surface="lifecycle",
            age="cold",
        )
        self.assertTrue(cold.compress)
        self.assertEqual(cold.reason, "aged_source_readback")

        for tool, data_class in (("web_extract", "research_corpus"), ("browser_snapshot", "browser_debug_trace")):
            with self.subTest(cold_class=data_class):
                aged = semantic_admission(
                    tool,
                    {"data_class": data_class},
                    "aged material",
                    surface="lifecycle",
                    age="cold",
                )
                self.assertTrue(aged.compress)
                self.assertEqual(aged.outcome, "hot_exact_then_cold_compact")

    def test_owner_exclusions_are_hard_policy(self):
        decision = semantic_admission(
            "delegate_task",
            {},
            "worker trace status=ok\n" * 100,
            excluded_tools=("delegate_task",),
        )
        self.assertFalse(decision.compress)
        self.assertEqual(decision.reason, "config_excluded_tool")


class LocalExactRecoveryTest(unittest.TestCase):
    def test_private_file_mode_uses_windows_acl_authority_instead_of_posix_bits(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "manifest.json"
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o666)
            with patch("hermes_headroom_plugin.local_exact_store._WINDOWS_MODE_BITS", True):
                self.assertTrue(_private_file(path))
            if os.name != "nt":
                with patch("hermes_headroom_plugin.local_exact_store._WINDOWS_MODE_BITS", False):
                    self.assertFalse(_private_file(path))

    @staticmethod
    def _config() -> EffectiveConfig:
        return EffectiveConfig(
            local_exact_enabled=True,
            local_exact_ttl_seconds=60,
            local_exact_max_bytes=1024 * 1024,
            local_exact_max_entries=8,
        )

    def test_content_addressed_exact_roundtrip_permissions_expiry_and_profile_isolation(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            home_one = Path(one)
            home_two = Path(two)
            retained = retain_local_source(
                "marker-123",
                "exact worker trace\nline two",
                data_class="worker_trace_raw",
                tool_name="delegate_task",
                home=home_one,
                config=self._config(),
                now=1000,
            )
            self.assertEqual(retained.state, EXACT)
            self.assertTrue(retained.sha256)
            blob = Path(retained.manifest_path).with_name(f"{retained.sha256}.payload")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(Path(retained.manifest_path).stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(blob.stat().st_mode), 0o600)

            recovered = retrieve_local_source_result("marker-123", home=home_one, now=1030)
            self.assertTrue(recovered.exact)
            self.assertEqual(recovered.content, "exact worker trace\nline two")
            self.assertEqual(retrieve_local_source_result("marker-123", home=home_two).state, MISSING)
            self.assertEqual(retrieve_local_source_result("marker-123", home=home_one, now=1061).state, EXPIRED)
            self.assertFalse(blob.exists())

    def test_sensitive_payload_records_redacted_tombstone_without_blob(self):
        with tempfile.TemporaryDirectory() as td:
            retained = retain_local_source(
                "marker-secret",
                "api_key=super-secret-value",
                data_class="raw_log",
                tool_name="terminal",
                home=Path(td),
                config=self._config(),
                now=1000,
            )
            self.assertEqual(retained.state, REDACTED)
            self.assertFalse(Path(retained.manifest_path).with_name(f"{retained.sha256}.payload").exists())
            recovered = retrieve_local_source_result("marker-secret", home=Path(td), now=1010)
            self.assertEqual(recovered.state, REDACTED)
            self.assertIsNone(recovered.content)
            expired = retrieve_local_source_result("marker-secret", home=Path(td), now=1061)
            self.assertEqual(expired.state, EXPIRED)

    def test_store_write_failure_is_fail_open_metadata(self):
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.local_exact_store._atomic_bytes", side_effect=OSError("read-only filesystem")
        ):
            retained = retain_local_source(
                "marker-write-failure",
                "safe trace",
                data_class="worker_trace_raw",
                tool_name="delegate_task",
                home=Path(td),
                config=self._config(),
            )
        self.assertEqual(retained.state, ERROR)
        self.assertIn("read-only filesystem", retained.error)

    def test_partial_manifest_failure_cleans_new_exact_blob(self):
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.local_exact_store._atomic_json",
            side_effect=[None, OSError("alias write failed")],
        ):
            retained = retain_local_source(
                "marker-partial-failure",
                "safe trace",
                data_class="worker_trace_raw",
                tool_name="delegate_task",
                home=Path(td),
                config=self._config(),
            )
            payloads = list((Path(td) / "control-plane" / "headroom" / "exact-sources").glob("*.payload"))
        self.assertEqual(retained.state, ERROR)
        self.assertEqual(payloads, [])

    def test_store_is_default_off_and_typed_config_is_bounded(self):
        self.assertFalse(resolve_effective_config(raw_config={}).local_exact_enabled)
        configured = resolve_effective_config(
            raw_config={
                "local_exact_store": {
                    "enabled": True,
                    "ttl_seconds": 1,
                    "max_entries": 0,
                    "max_bytes": 1,
                }
            }
        )
        self.assertTrue(configured.local_exact_enabled)
        self.assertEqual(configured.local_exact_ttl_seconds, 60)
        self.assertEqual(configured.local_exact_max_entries, 1)
        self.assertEqual(configured.local_exact_max_bytes, 64_000)

    def test_reduction_and_retrieve_tool_use_exact_local_manifest_end_to_end(self):
        source = "\n".join(f"worker trace line={index} status=ok" for index in range(1000))
        compressed = {
            "ok": True,
            "tokens_before": 12000,
            "tokens_after": 200,
            "tokens_saved": 11800,
            "markers": ["local-e2e-marker"],
            "messages": [{"role": "tool", "content": "summary <<ccr:local-e2e-marker>>"}],
        }
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config.yaml").write_text(
                "context_reduction:\n"
                "  local_exact_store:\n"
                "    enabled: true\n"
                "    ttl_seconds: 600\n"
                "    max_entries: 8\n"
                "    max_bytes: 1048576\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"HERMES_HOME": str(home)}), patch(
                "hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}
            ), patch(
                "hermes_headroom_plugin.provider_headroom.compress_messages", return_value=compressed
            ):
                reduced = compress_tool_result_for_context(tool_name="delegate_task", args={}, result=source)
                self.assertIsInstance(reduced, str)
                reduced_text = str(reduced)
                self.assertIn("authority=local_exact_manifest+ccr_temporal", reduced_text)
                self.assertIn("local_state=exact", reduced_text)
                with patch("hermes_headroom_plugin.tools.retrieve") as remote:
                    rendered = handle_headroom_retrieve(
                        {"hash": "local-e2e-marker"},
                        session_id="session-e2e",
                        tool_call_id="retrieve-e2e",
                    )
                remote.assert_not_called()
            payload = json.loads(rendered)
            self.assertTrue(payload["success"])
            self.assertTrue(payload["exact"])
            self.assertEqual(payload["state"], EXACT)
            self.assertEqual(payload["content"], source)
            self.assertNotIn("manifest_path", payload)


    def test_missing_provider_marker_synthesizes_retrievable_local_alias_when_enabled(self):
        source = "\n".join(
            f"diagnostic line={index} status=ok" + (" sentinel=LOCAL-ALIAS-EXACT" if index == 50 else "")
            for index in range(1200)
        )
        compressed = {
            "ok": True,
            "tokens_before": 12000,
            "tokens_after": 200,
            "tokens_saved": 11800,
            "messages": [{"role": "tool", "content": "bounded summary without provider marker"}],
        }
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config.yaml").write_text(
                "context_reduction:\n"
                "  local_exact_store:\n"
                "    enabled: true\n"
                "    ttl_seconds: 600\n"
                "    max_entries: 8\n"
                "    max_bytes: 1048576\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"HERMES_HOME": str(home)}), patch(
                "hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}
            ), patch(
                "hermes_headroom_plugin.provider_headroom.compress_messages", return_value=compressed
            ):
                reduced = compress_tool_result_for_context(tool_name="terminal", args={}, result=source)
                self.assertIsInstance(reduced, str)
                reduced_text = str(reduced)
                self.assertIn("headroom_retrieve(hash='local_", reduced_text)
                marker = reduced_text.split("headroom_retrieve(hash='", 1)[1].split("'", 1)[0]
                self.assertEqual(len(marker), len("local_") + 64)
                with patch("hermes_headroom_plugin.tools.retrieve") as remote:
                    rendered = handle_headroom_retrieve({"hash": marker})
                remote.assert_not_called()

            payload = json.loads(rendered)
            self.assertTrue(payload["success"])
            self.assertTrue(payload["exact"])
            self.assertEqual(payload["content"], source)
            events_path = home / "control-plane" / "headroom" / "events" / "headroom-events.jsonl"
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            compression = [item for item in events if item.get("type") == "headroom_tool_result"][-1]
            self.assertEqual(compression["action"], "compressed")
            self.assertEqual(compression["marker_origin"], "local_exact_synthesized")


    def test_missing_provider_marker_keeps_exact_when_local_alias_write_fails(self):
        source = "\n".join(f"diagnostic line={index} status=ok" for index in range(1200))
        compressed = {
            "ok": True,
            "tokens_before": 12000,
            "tokens_after": 200,
            "tokens_saved": 11800,
            "messages": [{"role": "tool", "content": "bounded summary without provider marker"}],
        }
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config.yaml").write_text(
                "context_reduction:\n"
                "  local_exact_store:\n"
                "    enabled: true\n"
                "    ttl_seconds: 600\n"
                "    max_entries: 8\n"
                "    max_bytes: 1048576\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"HERMES_HOME": str(home)}), patch(
                "hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}
            ), patch(
                "hermes_headroom_plugin.provider_headroom.compress_messages", return_value=compressed
            ), patch(
                "hermes_headroom_plugin.local_exact_store._atomic_bytes", side_effect=OSError("read-only store")
            ):
                reduced = compress_tool_result_for_context(tool_name="terminal", args={}, result=source)

            self.assertIsNone(reduced)
            events_path = home / "control-plane" / "headroom" / "events" / "headroom-events.jsonl"
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            compression = [item for item in events if item.get("type") == "headroom_tool_result"][-1]
            self.assertEqual(compression["action"], "skipped")
            self.assertEqual(compression["reason"], "missing_durable_marker")


    def test_missing_provider_marker_keeps_exact_when_local_alias_exceeds_quota(self):
        source = "\n".join(f"diagnostic line={index} status=ok payload={'x' * 100}" for index in range(1200))
        compressed = {
            "ok": True,
            "tokens_before": 30000,
            "tokens_after": 200,
            "tokens_saved": 29800,
            "messages": [{"role": "tool", "content": "bounded summary without provider marker"}],
        }
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config.yaml").write_text(
                "context_reduction:\n"
                "  local_exact_store:\n"
                "    enabled: true\n"
                "    ttl_seconds: 600\n"
                "    max_entries: 8\n"
                "    max_bytes: 1\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"HERMES_HOME": str(home)}), patch(
                "hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}
            ), patch(
                "hermes_headroom_plugin.provider_headroom.compress_messages", return_value=compressed
            ):
                reduced = compress_tool_result_for_context(tool_name="terminal", args={}, result=source)

            self.assertIsNone(reduced)
            events_path = home / "control-plane" / "headroom" / "events" / "headroom-events.jsonl"
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            compression = [item for item in events if item.get("type") == "headroom_tool_result"][-1]
            self.assertEqual(compression["action"], "skipped")
            self.assertEqual(compression["reason"], "missing_durable_marker")
            self.assertFalse(list((home / "control-plane" / "headroom" / "exact-sources").glob("*.payload")))

    def test_missing_provider_marker_keeps_exact_when_local_store_redacts_source(self):
        source = "\n".join(f"protected-fixture line={index} status=ok" for index in range(1200))
        compressed = {
            "ok": True,
            "tokens_before": 12000,
            "tokens_after": 200,
            "tokens_saved": 11800,
            "messages": [{"role": "tool", "content": "bounded summary without provider marker"}],
        }
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "config.yaml").write_text(
                "context_reduction:\n"
                "  local_exact_store:\n"
                "    enabled: true\n"
                "    ttl_seconds: 600\n"
                "    max_entries: 8\n"
                "    max_bytes: 1048576\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"HERMES_HOME": str(home)}), patch(
                "hermes_headroom_plugin.provider_headroom.readyz", return_value={"ok": True}
            ), patch(
                "hermes_headroom_plugin.provider_headroom.compress_messages", return_value=compressed
            ) as compress, patch(
                "hermes_headroom_plugin.local_exact_store._contains_protected_control", return_value=True
            ):
                reduced = compress_tool_result_for_context(tool_name="terminal", args={}, result=source)

            self.assertIsNone(reduced)
            compress.assert_called_once()
            self.assertFalse(list((home / "control-plane" / "headroom" / "exact-sources").glob("*.payload")))


class NetLedgerTest(unittest.TestCase):
    def test_retrieval_event_dedupe_identity_ignores_render_size(self):
        captured: list[dict] = []
        with patch("hermes_headroom_plugin.net_ledger.append_metadata_event", side_effect=captured.append):
            append_retrieval_event(
                marker="marker-a",
                model_facing_chars=100,
                success=True,
                source="local",
                state="exact",
                session_id="session-a",
                tool_call_id="retrieve-a",
            )
            append_retrieval_event(
                marker="marker-a",
                model_facing_chars=200,
                success=True,
                source="local",
                state="exact",
                session_id="session-a",
                tool_call_id="retrieve-a",
            )
        self.assertEqual(captured[0]["dedupe_key"], captured[1]["dedupe_key"])

    def test_deduplicates_sources_retrievals_and_provider_requests(self):
        compression = {
            "type": "headroom_tool_result",
            "action": "compressed",
            "event_id": "compression-1",
            "dedupe_key": "dedupe-source-a",
            "logical_source_id": "source-a",
            "marker": "marker-a",
            "model_facing_chars_before": 10000,
            "model_facing_chars_after": 1000,
            "task_id": "task-a",
        }
        events = [
            compression,
            {**compression, "event_id": "compression-duplicate"},
            {
                "type": "headroom_retrieval",
                "event_id": "retrieval-1",
                "dedupe_key": "retrieval-a",
                "marker": "marker-a",
                "model_facing_chars": 1200,
            },
            {
                "type": "headroom_retrieval",
                "event_id": "retrieval-duplicate",
                "dedupe_key": "retrieval-a",
                "marker": "marker-a",
                "model_facing_chars": 1200,
            },
            {
                "type": "headroom_retry",
                "event_id": "retry-1",
                "logical_source_id": "source-a",
                "retry_input_tokens": 100,
                "extra_call_input_tokens": 50,
            },
            {
                "type": "provider_usage",
                "event_id": "provider-1",
                "api_request_id": "request-a",
                "logical_source_id": "source-a",
                "input_tokens": 2000,
                "cache_read_tokens": 500,
                "output_tokens": 100,
            },
            {
                "type": "provider_usage",
                "event_id": "provider-duplicate",
                "api_request_id": "request-a",
                "input_tokens": 9999,
            },
            {"type": "headroom_task_result", "event_id": "task-result", "task_id": "task-a", "success": True},
        ]
        ledger = build_net_ledger(events)
        self.assertEqual(ledger["summary"]["logical_sources"], 1)
        self.assertEqual(ledger["summary"]["provider_request_count"], 1)
        self.assertEqual(ledger["rows"][0]["gross_est_tokens_saved"], 2250)
        self.assertEqual(ledger["rows"][0]["retrieval_reintroduced_est_tokens"], 300)
        self.assertEqual(ledger["rows"][0]["net_est_tokens_saved"], 1800)
        self.assertEqual(ledger["provider_requests"][0]["prompt_or_input_tokens"], 2000)
        self.assertEqual(ledger["summary"]["provider_cache_read_tokens"], 500)
        self.assertEqual(ledger["rows"][0]["provider_request_ids"], ["request-a"])
        self.assertTrue(ledger["rows"][0]["task_success"]["success"])
        self.assertEqual(ledger["authorities"]["provider_usage_cache"], "provider_reported_non_additive")

    def test_event_join_is_order_independent(self):
        ledger = build_net_ledger(
            [
                {
                    "type": "headroom_retry",
                    "logical_source_id": "source-b",
                    "retry_input_tokens": 25,
                },
                {
                    "type": "provider_usage",
                    "api_request_id": "request-b",
                    "logical_source_id": "source-b",
                    "input_tokens": 100,
                    "cache_read_tokens": 40,
                },
                {
                    "type": "headroom_tool_result",
                    "action": "compressed",
                    "logical_source_id": "source-b",
                    "marker": "marker-b",
                    "model_facing_chars_before": 4000,
                    "model_facing_chars_after": 400,
                },
            ]
        )
        self.assertEqual(ledger["rows"][0]["retry_input_tokens"], 25)
        self.assertEqual(ledger["rows"][0]["provider_request_ids"], ["request-b"])
        self.assertEqual(ledger["provider_requests"][0]["prompt_or_input_tokens"], 100)


if __name__ == "__main__":
    unittest.main()
