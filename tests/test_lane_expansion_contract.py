"""Executable architecture contract tests for Headroom lane expansion.

These tests keep the lane-expansion architecture track concrete without
promoting new product defaults. They cover safety gates plus the first general
exact-header slice for header-sensitive bulky intermediates.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_headroom_plugin import middleware


class LaneExpansionContractTest(unittest.TestCase):
    def _large_text(self, seed: str, *, min_chars: int = 35_000) -> str:
        lines = []
        i = 0
        while sum(len(line) for line in lines) < min_chars:
            lines.append(f"{seed} line={i} status=WARNING path=/tmp/headroom-contract/{i}\n")
            i += 1
        return "".join(lines)

    def _huge_text(self, seed: str) -> str:
        return self._large_text(seed, min_chars=130_000)

    def _compressed_response(self) -> dict:
        return {
            "ok": True,
            "tokens_before": 32000,
            "tokens_after": 240,
            "tokens_saved": 31760,
            "compression_ratio": 0.0075,
            "messages": [
                {
                    "role": "tool",
                    "name": "worker_trace",
                    "content": "<<ccr:contractmarker001,base64,4KB>>",
                }
            ],
        }

    def test_multimodal_dict_shape_passes_through_without_proxy_call(self):
        payload = {
            "_multimodal": True,
            "text": "screenshot available",
            "image_path": "/tmp/screen.png",
            "png_b64": "synthetic-base64-not-real",
        }
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages"
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="computer_use",
                args={"action": "screenshot"},
                next_call=lambda current_args: payload,
            )
        self.assertIs(out, payload)
        compress.assert_not_called()

    def test_final_packet_in_delegate_lane_remains_exact(self):
        final_packet = "# Worker Final Packet\n\nstatus: PASS\n" + self._large_text("final packet")
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages"
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="delegate_task",
                args={"goal": "fan-in worker diagnostics"},
                next_call=lambda current_args: final_packet,
            )
        self.assertEqual(out, final_packet)
        compress.assert_not_called()

    def test_private_key_like_material_remains_exact_and_does_not_proxy(self):
        protected = "-----BEGIN " + "OPENSSH PRIVATE KEY-----\n" + self._large_text("protected material")
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages"
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="terminal",
                args={"command": "journalctl --user"},
                next_call=lambda current_args: protected,
            )
        self.assertEqual(out, protected)
        compress.assert_not_called()

    def test_proxy_down_returns_original_for_eligible_lane(self):
        result = self._large_text("delegate diagnostics")
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": False}), patch(
            "hermes_headroom_plugin.middleware.compress_messages"
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="delegate_task",
                args={"goal": "fan-in worker diagnostics"},
                next_call=lambda current_args: result,
            )
        self.assertEqual(out, result)
        compress.assert_not_called()

    def test_protected_args_must_not_enter_proxy_payload(self):
        captured = {}
        synthetic_secret = "SYNTHETIC_SECRET_VALUE_1234567890"

        def fake_compress(messages):
            captured["messages"] = messages
            return self._compressed_response()

        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.middleware.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", side_effect=fake_compress
        ):
            middleware.on_tool_execution(
                tool_name="terminal",
                args={"command": "pytest", "api_token": synthetic_secret},
                next_call=lambda current_args: self._large_text("qa trace"),
            )

        proxy_payload = json.dumps(captured.get("messages"), ensure_ascii=False)
        self.assertNotIn(synthetic_secret, proxy_payload)

    def test_late_private_key_like_material_remains_exact_and_creates_no_sidecar(self):
        protected_tail = (
            self._large_text("benign prefix", min_chars=25_000)
            + "\n-----BEGIN " + "OPENSSH PRIVATE KEY-----\nSYNTHETIC_PRIVATE_KEY_BODY_AFTER_SCAN_WINDOW\n"
            + self._large_text("benign suffix", min_chars=10_000)
        )
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.middleware.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=self._compressed_response()
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="terminal",
                args={"command": "journalctl --user"},
                next_call=lambda current_args: protected_tail,
            )
            reports_dir = Path(td) / "control-plane" / "headroom" / "reports"
            sidecars = list(reports_dir.glob("*.redacted.log")) if reports_dir.exists() else []

        self.assertEqual(out, protected_tail)
        compress.assert_not_called()
        self.assertEqual(sidecars, [])

    def test_late_cdp_cookie_dump_remains_exact_and_creates_no_sidecar(self):
        cookie_tail = (
            self._large_text("benign browser prefix", min_chars=25_000)
            + '\nCDP Network.getAllCookies cookie name=session value="SYNTHETIC_COOKIE_AFTER_PREFIX_123456" domain=example.test\n'
            + self._large_text("benign browser suffix", min_chars=10_000)
        )
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.middleware.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=self._compressed_response()
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="browser_cdp",
                args={"method": "DOM.getDocument"},
                next_call=lambda current_args: cookie_tail,
            )
            reports_dir = Path(td) / "control-plane" / "headroom" / "reports"
            sidecars = list(reports_dir.glob("*.redacted.log")) if reports_dir.exists() else []

        self.assertEqual(out, cookie_tail)
        compress.assert_not_called()
        self.assertEqual(sidecars, [])

    def test_nested_sensitive_args_fail_closed_without_proxy_or_sidecar(self):
        result = self._large_text("qa trace")
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.middleware.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=self._compressed_response()
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="terminal",
                args={"request": {"headers": [{"Authorization": "Bearer SYNTHETIC_TOKEN_1234567890"}]}},
                next_call=lambda current_args: result,
            )
            reports_dir = Path(td) / "control-plane" / "headroom" / "reports"
            sidecars = list(reports_dir.glob("*.redacted.log")) if reports_dir.exists() else []

        self.assertEqual(out, result)
        compress.assert_not_called()
        self.assertEqual(sidecars, [])

    def test_explicit_long_comments_history_without_anchor_fails_closed(self):
        result = self._huge_text("large comments blob with no stable comment thread message or timestamp fields")
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=self._compressed_response()
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="delegate_task",
                args={"data_class": "long_comments_history"},
                next_call=lambda current_args: result,
            )
        self.assertEqual(out, result)
        compress.assert_not_called()

    def test_explicit_raw_feed_snapshot_without_anchor_fails_closed(self):
        result = self._huge_text("large feed blob with no stable item feed source cursor or timestamp fields")
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=self._compressed_response()
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="delegate_task",
                args={"data_class": "raw_feed_snapshot"},
                next_call=lambda current_args: result,
            )
        self.assertEqual(out, result)
        compress.assert_not_called()

    def test_explicit_long_comments_history_with_comment_anchor_can_compress(self):
        result = self._huge_text(
            "comments comment_id=C-123 thread_id=T-9 status=open latest_actionable_comment=reply-with-source"
        )
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=self._compressed_response()
        ):
            out = middleware.on_tool_execution(
                tool_name="delegate_task",
                args={"data_class": "long_comments_history"},
                next_call=lambda current_args: result,
            )
        self.assertIn("classification: long_comments_history", out)
        self.assertIn("comment_id=C-123", out)
        self.assertIn("thread_id=T-9", out)
        self.assertIn("latest_actionable_comment=reply-with-source", out)

    def test_browser_cdp_cookie_dump_must_not_create_sidecar_or_proxy_call(self):
        cookie_value = "SYNTHETIC_COOKIE_VALUE_1234567890"
        result = self._large_text(
            f'CDP Network.getAllCookies cookie name=session value="{cookie_value}" domain=example.test'
        )
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.middleware.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=self._compressed_response()
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="browser_cdp",
                args={"method": "Network.getAllCookies"},
                next_call=lambda current_args: result,
            )
            reports_dir = Path(td) / "control-plane" / "headroom" / "reports"
            sidecars = list(reports_dir.glob("*.redacted.log")) if reports_dir.exists() else []

        self.assertEqual(out, result)
        compress.assert_not_called()
        self.assertEqual(sidecars, [])

    def test_browser_vision_final_like_result_must_remain_exact_unless_marked_intermediate(self):
        result = self._large_text("FINAL VISUAL ANSWER: the chart shows revenue rising")
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=self._compressed_response()
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="browser_vision",
                args={"question": "What does this page show?"},
                next_call=lambda current_args: result,
            )
        self.assertEqual(out, result)
        compress.assert_not_called()

    def test_research_query_hint_alone_must_not_authorize_precision_sensitive_compression(self):
        result = self._large_text(
            'x_search answer with citation url=https://source.example/a title="Source A" degraded=false'
        )
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=self._compressed_response()
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="x_search",
                args={"query": "research source.example citations"},
                next_call=lambda current_args: result,
            )
        self.assertEqual(out, result)
        compress.assert_not_called()

    def test_always_chars_unknown_orchestration_gets_exact_header_before_compression(self):
        result = self._huge_text(
            "kanban task_id=TASK-1 run_id=7 status=in_progress acceptance=preserve-exact-header"
        )
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=self._compressed_response()
        ):
            out = middleware.on_tool_execution(
                tool_name="kanban_show",
                args={"task_id": "TASK-1"},
                next_call=lambda current_args: result,
            )
        self.assertIn("exact_header", out)
        self.assertIn("classification: orchestration_fanin", out)
        self.assertIn("TASK-1", out)
        self.assertIn("status=in_progress", out)
        self.assertIn("acceptance=preserve-exact-header", out)

    def test_header_required_orchestration_without_critical_fields_fails_closed(self):
        result = self._huge_text("kanban history with no stable task identifier or status")
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=self._compressed_response()
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="kanban_show",
                args={},
                next_call=lambda current_args: result,
            )
        self.assertEqual(out, result)
        compress.assert_not_called()

    def test_always_chars_research_corpus_gets_citation_header_before_compression(self):
        result = self._huge_text(
            "x_search answer citation url=https://source.example/a title=Source-A document_id=doc-123 degraded=false line=41"
        )
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=self._compressed_response()
        ):
            out = middleware.on_tool_execution(
                tool_name="x_search",
                args={"query": "neutral synthesis"},
                next_call=lambda current_args: result,
            )
        self.assertIn("classification: research_corpus", out)
        self.assertIn("https://source.example/a", out)
        self.assertIn("document_id=doc-123", out)
        self.assertIn("degraded=false", out)

    def test_header_required_research_without_citation_or_quality_anchor_fails_closed(self):
        result = self._huge_text("large answer-like corpus without durable citation anchors")
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=self._compressed_response()
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="x_search",
                args={"query": "neutral synthesis"},
                next_call=lambda current_args: result,
            )
        self.assertEqual(out, result)
        compress.assert_not_called()

    def test_always_chars_interaction_state_gets_selector_header_before_compression(self):
        result = self._huge_text(
            "CDP method=DOM.getDocument url=https://example.test/app frame_id=FRAME-1 target_id=TARGET-1 selector=#submit bounds=10x20x100x32 status=ok"
        )
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=self._compressed_response()
        ):
            out = middleware.on_tool_execution(
                tool_name="browser_cdp",
                args={"method": "DOM.getDocument"},
                next_call=lambda current_args: result,
            )
        self.assertIn("classification: interaction_state", out)
        self.assertIn("https://example.test/app", out)
        self.assertIn("frame_id=FRAME-1", out)
        self.assertIn("selector=#submit", out)
        self.assertIn("bounds=10x20x100x32", out)

    def test_header_required_interaction_without_actionable_anchor_fails_closed(self):
        result = self._huge_text("large browser payload without url selector node id bounds or error")
        with patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=self._compressed_response()
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="browser_cdp",
                args={"method": "DOM.getDocument"},
                next_call=lambda current_args: result,
            )
        self.assertEqual(out, result)
        compress.assert_not_called()

    def test_compressed_replacement_must_not_label_redacted_sidecar_as_exact_source(self):
        result = self._large_text("delegate diagnostics")
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.middleware.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=self._compressed_response()
        ):
            out = middleware.on_tool_execution(
                tool_name="delegate_task",
                args={"goal": "fan-in worker diagnostics"},
                next_call=lambda current_args: result,
            )
        self.assertNotIn("Redacted exact source sidecar", out)
        self.assertIn("redacted_sidecar", out)


if __name__ == "__main__":
    unittest.main()
