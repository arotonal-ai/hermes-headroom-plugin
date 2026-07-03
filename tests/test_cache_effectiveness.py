import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_headroom_plugin import cache_effectiveness


class CacheEffectivenessTest(unittest.TestCase):
    def _home_with_events(self, events):
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        event_dir = home / "control-plane" / "headroom" / "events"
        event_dir.mkdir(parents=True)
        with (event_dir / "headroom-events.jsonl").open("w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event) + "\n")
        (home / "config.yaml").write_text(
            "model:\n  provider: openai-codex\n  api_mode: chat_completions\n  base_url: https://chatgpt.com/backend-api/codex\n",
            encoding="utf-8",
        )
        return tmp, home

    def test_keep_proxy_hot_path_when_compression_is_strong_and_provider_not_routed(self):
        events = [
            {"type": "headroom_tool_result", "action": "compressed", "tool_name": "terminal", "lane": "dev", "tokens_saved": 25000},
            {"type": "headroom_tool_result", "action": "compressed", "tool_name": "session_search", "lane": "research", "tokens_saved": 25000},
            {"type": "headroom_tool_result", "action": "compressed", "tool_name": "web_extract", "lane": "research", "tokens_saved": 25000},
        ]
        tmp, home = self._home_with_events(events)
        self.addCleanup(tmp.cleanup)
        stats = {
            "success": True,
            "store": {
                "entry_count": 4,
                "max_entries": 1000,
                "default_ttl_seconds": 1800,
                "total_original_tokens": 10000,
                "total_compressed_tokens": 2000,
                "total_retrievals": 3,
                "event_count": 9,
                "backend": {"backend_type": "sqlite", "bytes_used": 1000},
            },
            "recent_retrievals": [{"items_retrieved": 10}, {"items_retrieved": 0}],
        }
        with patch("hermes_headroom_plugin.cache_effectiveness.resolve_proxy_url", return_value="http://127.0.0.1:28787"), patch(
            "hermes_headroom_plugin.cache_effectiveness.readyz", return_value={"ok": True, "proxy_url": "http://127.0.0.1:28787"}
        ), patch("hermes_headroom_plugin.cache_effectiveness.retrieve_stats", return_value=stats), patch(
            "hermes_headroom_plugin.cache_effectiveness.auto_compression_enabled", return_value=True
        ):
            report = cache_effectiveness.run_report(cache_effectiveness.CacheEffectivenessConfig(event_limit=20, hermes_home_path=home))
        self.assertEqual(report["decision"], "KEEP_PROXY_HOT_PATH")
        self.assertEqual(report["provider_cache"]["active"], False)
        self.assertEqual(report["middleware"]["tokens_saved"], 75000)
        self.assertEqual(report["ccr_store"]["recent_success_rate"], 0.5)

    def test_do_not_use_provider_cache_when_runtime_unavailable(self):
        tmp, home = self._home_with_events([])
        self.addCleanup(tmp.cleanup)
        with patch("hermes_headroom_plugin.cache_effectiveness.resolve_proxy_url", return_value="http://127.0.0.1:28787"), patch(
            "hermes_headroom_plugin.cache_effectiveness.readyz", return_value={"ok": False, "proxy_url": "http://127.0.0.1:28787"}
        ):
            report = cache_effectiveness.run_report(cache_effectiveness.CacheEffectivenessConfig(hermes_home_path=home))
        self.assertEqual(report["decision"], "DO_NOT_USE_PROVIDER_CACHE")
        self.assertEqual(report["status"], "RUNTIME_PARTIAL")

    def test_text_output_contains_decision_and_cache_fields(self):
        text = cache_effectiveness._format_text(
            {
                "decision": "ADD_CACHE_UX",
                "status": "RUNTIME_FULL",
                "auto_compression": "on",
                "ccr_store": {"entry_count": 1, "max_entries": 10, "ttl_seconds": 1800, "ttl_risk": "low", "total_retrievals": 2, "recent_success_rate": 1.0},
                "middleware": {"tokens_saved": 42, "compressed_events": 2, "events": 3},
                "provider_cache": {"active": False},
                "model_path": {"provider": "openai-codex"},
                "next": "keep proxy",
            }
        )
        self.assertIn("ADD_CACHE_UX", text)
        self.assertIn("provider_cache_active=False", text)
        self.assertIn("next=keep proxy", text)


if __name__ == "__main__":
    unittest.main()
