from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_headroom_plugin.config import EffectiveConfig
from hermes_headroom_plugin.context_engine import HeadroomCompositeEngine
from hermes_headroom_plugin.net_ledger import build_net_ledger


class _BuiltinObserver:
    def __init__(self):
        self.calls = []
        self.session_id = ""

    def on_session_start(self, session_id, **kwargs):
        self.session_id = session_id

    def on_turn_complete(self, messages, usage=None, **kwargs):
        self.calls.append((messages, usage, kwargs))


class NativeTurnUsageTest(unittest.TestCase):
    def test_completed_turn_emits_content_free_aggregate_usage_and_turn_result(self):
        with tempfile.TemporaryDirectory() as td:
            event_path = Path(td) / "events" / "headroom-events.jsonl"
            event_path.parent.mkdir(parents=True, exist_ok=True)
            builtin = _BuiltinObserver()
            engine = HeadroomCompositeEngine(
                model="openai-codex/gpt-test",
                builtin=builtin,
                effective_config=EffectiveConfig(),
            )
            engine.on_session_start("session-a")
            sentinel = "PROTECTED_CONTENT_MUST_NOT_ENTER_EVENT_LOG"
            messages = [{"role": "user", "content": sentinel}]
            usage = {
                "input_tokens": 1200,
                "output_tokens": 80,
                "total_tokens": 1280,
                "cache_read_tokens": 400,
                "cache_write_tokens": 20,
                "reasoning_tokens": 30,
            }
            with patch("hermes_headroom_plugin.observability._event_log_path", return_value=event_path):
                engine.on_turn_complete(
                    messages,
                    usage,
                    session_id="session-a",
                    turn_id="turn-a",
                    task_id="task-a",
                    api_call_count=3,
                    provider="openai-codex\ninjected",
                    model="gpt-test",
                    failed=False,
                    interrupted=False,
                    turn_exit_reason="completed",
                )

            raw = event_path.read_text(encoding="utf-8")
            events = [json.loads(line) for line in raw.splitlines()]
            self.assertNotIn(sentinel, raw)
            self.assertEqual(event_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual([event["type"] for event in events], ["headroom_turn_usage", "headroom_turn_result"])
            usage_event, turn_event = events
            self.assertEqual(usage_event["usage_scope"], "completed_turn_aggregate")
            self.assertEqual(usage_event["usage_authority"], "hermes_context_engine_completed_turn")
            self.assertFalse(usage_event["request_level_attribution"])
            self.assertFalse(usage_event["retry_tokens_inferred"])
            self.assertEqual(usage_event["api_call_count"], 3)
            self.assertEqual(usage_event["provider"], "openai-codex injected")
            self.assertEqual(usage_event["cache_read_tokens"], 400)
            self.assertEqual(usage_event["billing_authority"], "unavailable")
            self.assertTrue(turn_event["turn_success"])
            self.assertEqual(turn_event["result_scope"], "completed_turn_transport")
            self.assertEqual(len(builtin.calls), 1)
            self.assertIs(builtin.calls[0][0], messages)

            ledger = build_net_ledger(events)
            self.assertEqual(ledger["summary"]["turn_usage_count"], 1)
            self.assertEqual(ledger["summary"]["turn_prompt_or_input_tokens"], 1200)
            self.assertEqual(ledger["summary"]["turn_cache_read_tokens"], 400)
            self.assertEqual(ledger["summary"]["provider_request_count"], 0)
            self.assertEqual(ledger["turn_usage"][0]["request_level_attribution"], False)
            self.assertEqual(ledger["turn_usage"][0]["billing_authority"], "unavailable")
            self.assertEqual(ledger["task_results"], [])
            self.assertTrue(ledger["turn_results"][0]["turn_success"])

    def test_interrupted_turn_has_unknown_success_and_no_usage_when_absent(self):
        events = []
        engine = HeadroomCompositeEngine(effective_config=EffectiveConfig())
        with patch("hermes_headroom_plugin.context_engine.append_metadata_event", side_effect=events.append):
            engine.on_turn_complete(
                [],
                None,
                turn_id="turn-interrupted",
                task_id="task-interrupted",
                interrupted=True,
                failed=False,
                turn_exit_reason="interrupt",
            )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "headroom_turn_result")
        self.assertIsNone(events[0]["turn_success"])
        self.assertTrue(events[0]["interrupted"])

    def test_uncorrelated_completion_does_not_emit_ambiguous_events(self):
        events = []
        engine = HeadroomCompositeEngine(effective_config=EffectiveConfig())
        with patch("hermes_headroom_plugin.context_engine.append_metadata_event", side_effect=events.append):
            engine.on_turn_complete([], {"input_tokens": 10})
        self.assertEqual(events, [])

    def test_turn_aggregate_dedupe_is_session_scoped(self):
        events = [
            {"type": "headroom_turn_usage", "event_id": "a", "session_id": "session-a", "turn_id": "shared", "input_tokens": 10},
            {"type": "headroom_turn_usage", "event_id": "b", "session_id": "session-b", "turn_id": "shared", "input_tokens": 20},
            {"type": "headroom_turn_result", "event_id": "c", "session_id": "session-a", "turn_id": "shared", "turn_success": True},
            {"type": "headroom_turn_result", "event_id": "d", "session_id": "session-b", "turn_id": "shared", "turn_success": False},
        ]

        ledger = build_net_ledger(events)

        self.assertEqual(ledger["summary"]["turn_usage_count"], 2)
        self.assertEqual(ledger["summary"]["turn_prompt_or_input_tokens"], 30)
        self.assertEqual(ledger["summary"]["turn_result_count"], 2)


if __name__ == "__main__":
    unittest.main()
