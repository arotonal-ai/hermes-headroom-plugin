import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_headroom_plugin import middleware


class LlmRequestCompressionConfigTest(unittest.TestCase):
    def test_request_middleware_is_opt_in(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(middleware.llm_request_compression_enabled({}))
            self.assertTrue(
                middleware.llm_request_compression_enabled(
                    {"llm_request_middleware": {"enabled": True, "mode": "tool_results"}}
                )
            )
            self.assertFalse(
                middleware.llm_request_compression_enabled(
                    {"llm_request_middleware": {"enabled": True, "mode": "observe"}}
                )
            )


class LlmRequestMiddlewareTest(unittest.TestCase):
    def setUp(self):
        middleware._LLM_REQUEST_TRANSFORM_CACHE.clear()
        self.large = "".join(
            f"diagnostic line {i} WARNING status=PASS path=/tmp/run/{i}\n"
            for i in range(500)
        )
        self.calls = []

        def fake_compress(**kwargs):
            self.calls.append(kwargs)
            return f"[COMPRESSED:{kwargs['tool_name']}:{kwargs['tool_call_id']}]"

        self.enabled = patch(
            "hermes_headroom_plugin.middleware.llm_request_compression_enabled",
            return_value=True,
        )
        self.compressor = patch(
            "hermes_headroom_plugin.middleware.compress_tool_result_for_context",
            side_effect=fake_compress,
        )
        self.enabled.start()
        self.compressor.start()

    def tearDown(self):
        self.compressor.stop()
        self.enabled.stop()
        middleware._LLM_REQUEST_TRANSFORM_CACHE.clear()

    def invoke(self, request, api_mode):
        original = copy.deepcopy(request)
        result = middleware.on_llm_request(
            request=request,
            api_mode=api_mode,
            task_id="task-1",
            session_id="session-1",
            turn_id="turn-1",
            api_request_id="api-1",
            platform="telegram",
            provider="provider-irrelevant",
            model="model-irrelevant",
        )
        self.assertEqual(request, original, "middleware must be copy-on-write")
        self.assertIsInstance(result, dict)
        if not isinstance(result, dict):
            self.fail("middleware did not return a request replacement")
        return result["request"], result

    def assert_call_contract(self, mode):
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["event_surface"], "llm_request")
        self.assertEqual(call["measurement_scope_override"], f"llm_request_tool_result:{mode}")
        self.assertFalse(call["allow_below_min_aggregate"])
        self.assertEqual(call["api_request_id"], "api-1")
        self.assertEqual(call["session_id"], "session-1")

    def test_chat_completions_preserves_transport_controls_and_tool_contract(self):
        request = {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "exact system"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-chat",
                            "type": "function",
                            "function": {
                                "name": "terminal",
                                "arguments": json.dumps({"command": "pytest -q"}),
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-chat", "content": self.large},
            ],
            "tools": [{"type": "function", "function": {"name": "terminal"}}],
            "stream": True,
            "extra_headers": {"x-test": "keep"},
        }
        effective, result = self.invoke(request, "chat_completions")
        self.assertEqual(effective["messages"][2]["content"], "[COMPRESSED:terminal:call-chat]")
        self.assertEqual(effective["messages"][0]["content"], "exact system")
        self.assertEqual(effective["tools"], request["tools"])
        self.assertTrue(effective["stream"])
        self.assertEqual(effective["extra_headers"], {"x-test": "keep"})
        self.assertEqual(result["reason"], "compressed_tool_results:chat_completions:1")
        self.assert_call_contract("chat_completions")

    def test_codex_responses_preserves_reasoning_tools_headers_and_multimodal_items(self):
        request = {
            "model": "test-model",
            "instructions": "exact instructions",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call-resp",
                    "name": "session_search",
                    "arguments": json.dumps({"query": "history"}),
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-resp",
                    "output": [
                        {"type": "input_text", "text": self.large},
                        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                    ],
                },
            ],
            "tools": [{"type": "function", "name": "session_search"}],
            "stream": True,
            "reasoning": {"effort": "medium"},
            "include": ["reasoning.encrypted_content"],
            "extra_headers": {"conversation_id": "keep"},
        }
        effective, result = self.invoke(request, "responses")
        output = effective["input"][1]["output"]
        self.assertEqual(output[0]["text"], "[COMPRESSED:session_search:call-resp]")
        self.assertEqual(output[1], request["input"][1]["output"][1])
        for key in ("tools", "stream", "reasoning", "include", "extra_headers", "instructions"):
            self.assertEqual(effective[key], request[key])
        self.assertEqual(result["reason"], "compressed_tool_results:codex_responses:1")
        self.assert_call_contract("codex_responses")

    def test_anthropic_preserves_signed_thinking_cache_control_and_images(self):
        request = {
            "model": "claude-test",
            "system": [{"type": "text", "text": "exact system", "cache_control": {"type": "ephemeral"}}],
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "exact", "signature": "signed"},
                        {"type": "tool_use", "id": "toolu-1", "name": "web_extract", "input": {"urls": ["https://example.com"]}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu-1",
                            "cache_control": {"type": "ephemeral"},
                            "content": [
                                {"type": "text", "text": self.large},
                                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                            ],
                        }
                    ],
                },
            ],
            "tools": [{"name": "web_extract"}],
            "stream": True,
        }
        effective, result = self.invoke(request, "anthropic_messages")
        tool_result = effective["messages"][1]["content"][0]
        self.assertEqual(tool_result["content"][0]["text"], "[COMPRESSED:web_extract:toolu-1]")
        self.assertEqual(tool_result["content"][1], request["messages"][1]["content"][0]["content"][1])
        self.assertEqual(effective["messages"][0], request["messages"][0])
        self.assertEqual(tool_result["cache_control"], {"type": "ephemeral"})
        self.assertEqual(effective["system"], request["system"])
        self.assertEqual(effective["tools"], request["tools"])
        self.assertTrue(effective["stream"])
        self.assertEqual(result["reason"], "compressed_tool_results:anthropic_messages:1")
        self.assert_call_contract("anthropic_messages")

    def test_bedrock_preserves_sentinels_inference_guardrails_and_images(self):
        request = {
            "modelId": "bedrock-test",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"toolUse": {"toolUseId": "bed-1", "name": "read_file", "input": {"path": "/tmp/a"}}}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": "bed-1",
                                "content": [
                                    {"text": self.large},
                                    {"image": {"format": "png", "source": {"bytes": "AAAA"}}},
                                ],
                            }
                        }
                    ],
                },
            ],
            "system": [{"text": "exact system"}],
            "inferenceConfig": {"maxTokens": 4096, "temperature": 0.1},
            "toolConfig": {"tools": [{"toolSpec": {"name": "read_file"}}]},
            "guardrailConfig": {"guardrailIdentifier": "keep"},
            "__bedrock_converse__": True,
            "__bedrock_region__": "us-east-1",
        }
        effective, result = self.invoke(request, "bedrock_converse")
        content = effective["messages"][1]["content"][0]["toolResult"]["content"]
        self.assertEqual(content[0]["text"], "[COMPRESSED:read_file:bed-1]")
        self.assertEqual(content[1], request["messages"][1]["content"][0]["toolResult"]["content"][1])
        for key in ("system", "inferenceConfig", "toolConfig", "guardrailConfig", "__bedrock_converse__", "__bedrock_region__"):
            self.assertEqual(effective[key], request[key])
        self.assertEqual(result["reason"], "compressed_tool_results:bedrock_converse:1")
        self.assert_call_contract("bedrock_converse")

    def test_repeated_request_reuses_bounded_transform_cache_without_new_compression(self):
        request = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "cache-call", "function": {"name": "search_files", "arguments": "{}"}}
                    ],
                },
                {"role": "tool", "tool_call_id": "cache-call", "content": self.large},
            ],
            "stream": True,
        }
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.middleware.hermes_home", return_value=Path(td)
        ):
            first, _ = self.invoke(request, "chat_completions")
            second, _ = self.invoke(request, "chat_completions")
            event_path = Path(td) / "control-plane" / "headroom" / "events" / "headroom-events.jsonl"
            events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.calls[0]["logical_source_id"]), 64)
        self.assertEqual(events[-1]["action"], "retained")
        self.assertEqual(events[-1]["reason"], "request_cache_reuse")
        self.assertFalse(events[-1]["new_savings_event"])
        self.assertEqual(events[-1]["logical_source_id"], self.calls[0]["logical_source_id"])

    def test_logical_source_dedupe_is_stable_across_api_requests(self):
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.middleware.hermes_home", return_value=Path(td)
        ):
            for api_request_id in ("api-1", "api-2"):
                middleware._emit_headroom_event(
                    action="compressed",
                    tool_name="search_files",
                    args={},
                    reason="eligible_tool:search_files",
                    session_id="session-stable",
                    tool_call_id="call-stable",
                    api_request_id=api_request_id,
                    surface="llm_request",
                    original_chars=10000,
                    tokens_saved=2250,
                    model_facing_chars_before=10000,
                    model_facing_chars_after=1000,
                    measurement_scope="llm_request_tool_result:codex_responses",
                    marker="stablemarker123",
                    logical_source_id="a" * 64,
                )
            event_path = Path(td) / "control-plane" / "headroom" / "events" / "headroom-events.jsonl"
            events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[0]["dedupe_key"], events[1]["dedupe_key"])
        self.assertTrue(events[0]["new_savings_event"])
        self.assertFalse(events[1]["new_savings_event"])
        self.assertTrue(events[1]["attribution_duplicate"])

    def test_already_compressed_unsupported_and_disabled_are_noops(self):
        compressed_request = {
            "messages": [
                {"role": "tool", "tool_call_id": "x", "name": "terminal", "content": "[Headroom auto-compressed]" + self.large}
            ]
        }
        self.assertIsNone(middleware.on_llm_request(request=compressed_request, api_mode="chat_completions"))
        self.assertEqual(self.calls, [])
        self.assertIsNone(middleware.on_llm_request(request={"messages": []}, api_mode="future_protocol"))
        with patch(
            "hermes_headroom_plugin.middleware.llm_request_compression_enabled",
            return_value=False,
        ):
            self.assertIsNone(middleware.on_llm_request(request=compressed_request, api_mode="chat_completions"))

    def test_compressor_exception_fails_open_without_mutating_original(self):
        request = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [{"id": "call-1", "function": {"name": "terminal", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": self.large},
            ],
            "stream": True,
        }
        original = copy.deepcopy(request)
        with patch(
            "hermes_headroom_plugin.middleware.compress_tool_result_for_context",
            side_effect=RuntimeError("synthetic failure"),
        ):
            self.assertIsNone(middleware.on_llm_request(request=request, api_mode="chat_completions"))
        self.assertEqual(request, original)


if __name__ == "__main__":
    unittest.main()
