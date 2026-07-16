import json
import unittest
from unittest.mock import patch

from hermes_headroom_plugin import benchmark


class AdoptionBenchmarkTest(unittest.TestCase):
    def _runtime_mocks(self, *, tokens_before, tokens_after, tokens_saved):
        retained = {}

        def fake_compress(messages, model="gpt-5.5", proxy_url=None):
            del model, proxy_url
            retained["content"] = json.dumps(messages, ensure_ascii=False)
            return {
                "ok": True,
                "messages": [{"role": "tool", "content": "<<ccr:abc123,base64,1KB>>"}],
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "tokens_saved": tokens_saved,
            }

        def fake_retrieve(marker, proxy_url=None):
            del marker, proxy_url
            return {"success": True, "result": {"original_content": retained["content"]}}

        return fake_compress, fake_retrieve

    def test_adopt_loop_when_savings_exceed_overhead_and_quality_passes(self):
        fake_compress, fake_retrieve = self._runtime_mocks(tokens_before=8000, tokens_after=800, tokens_saved=7200)
        with patch("hermes_headroom_plugin.benchmark.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.benchmark.compress_messages", fake_compress
        ), patch("hermes_headroom_plugin.benchmark.retrieve", fake_retrieve), patch(
            "hermes_headroom_plugin.benchmark.resolve_proxy_url", return_value="http://127.0.0.1:28787"
        ):
            report = benchmark.run_benchmark(benchmark.BenchmarkConfig(samples=1, min_net_saved_chars=1000))
        self.assertEqual(report["decision"], "ADOPT_LOOP")
        self.assertEqual(report["status"], "RUNTIME_FULL")
        self.assertEqual(report["quality"], "pass")
        self.assertGreater(report["metrics"]["net_saved_chars"], 1000)

    def test_compression_only_when_loop_overhead_is_too_high(self):
        fake_compress, fake_retrieve = self._runtime_mocks(tokens_before=1000, tokens_after=900, tokens_saved=100)
        with patch("hermes_headroom_plugin.benchmark.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.benchmark.compress_messages", fake_compress
        ), patch("hermes_headroom_plugin.benchmark.retrieve", fake_retrieve), patch(
            "hermes_headroom_plugin.benchmark.resolve_proxy_url", return_value="http://127.0.0.1:28787"
        ):
            report = benchmark.run_benchmark(benchmark.BenchmarkConfig(samples=1, min_net_saved_chars=999999))
        self.assertEqual(report["decision"], "COMPRESSION_ONLY")
        self.assertEqual(report["quality"], "pass")

    def test_disable_loop_reporting_when_runtime_not_ready(self):
        with patch("hermes_headroom_plugin.benchmark.readyz", return_value={"ok": False}), patch(
            "hermes_headroom_plugin.benchmark.resolve_proxy_url", return_value="http://127.0.0.1:28787"
        ):
            report = benchmark.run_benchmark(benchmark.BenchmarkConfig(samples=1))
        self.assertEqual(report["decision"], "DISABLE_LOOP_REPORTING")
        self.assertEqual(report["status"], "RUNTIME_PARTIAL")
        self.assertEqual(report["quality"], "fail")

    def test_text_output_contains_decision_and_next(self):
        text = benchmark._format_text(
            {
                "decision": "COMPRESSION_ONLY",
                "status": "RUNTIME_FULL",
                "auto_compression": "on",
                "quality": "pass",
                "metrics": {"net_saved_chars": 1, "chars_saved": 2, "overhead_chars": 1, "overhead_ratio": 0.5},
                "next": "keep compression",
            }
        )
        self.assertIn("COMPRESSION_ONLY", text)
        self.assertIn("next=keep compression", text)


if __name__ == "__main__":
    unittest.main()
