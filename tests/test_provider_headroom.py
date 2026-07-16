import unittest
from unittest.mock import patch

from hermes_headroom_plugin.contracts import ReductionContext, ReductionProvider
from hermes_headroom_plugin.provider_headroom import HeadroomReductionProvider


class HeadroomProviderAdapterTest(unittest.TestCase):
    def test_adapter_satisfies_provider_protocol(self):
        self.assertIsInstance(HeadroomReductionProvider(), ReductionProvider)

    @patch("hermes_headroom_plugin.provider_headroom.readyz")
    def test_ready_maps_health_without_exposing_payload(self, ready):
        ready.return_value = {"ready": True, "status": 200, "detail": "ok"}
        result = HeadroomReductionProvider(proxy_url="http://127.0.0.1:28787").ready()
        self.assertTrue(result.ready)
        self.assertEqual(result.status, 200)
        ready.assert_called_once_with(proxy_url="http://127.0.0.1:28787")

    @patch("hermes_headroom_plugin.provider_headroom.compress_messages")
    def test_compress_maps_exact_provider_outcome(self, compress):
        compress.return_value = {
            "success": True,
            "messages": [{"role": "assistant", "content": "<<ccr:abc123>>"}],
            "markers": ["abc123"],
            "tokens_before": 100,
            "tokens_after": 20,
            "tokens_saved": 80,
            "private": "must-not-enter-metrics",
        }
        provider = HeadroomReductionProvider(proxy_url="http://127.0.0.1:28787")
        result = provider.compress([{"role": "user", "content": "x"}], ReductionContext(model="gpt-test"))
        self.assertTrue(result.ok)
        self.assertEqual(result.marker, "abc123")
        self.assertEqual(result.metrics, {"tokens_before": 100, "tokens_after": 20, "tokens_saved": 80})
        compress.assert_called_once_with(
            [{"role": "user", "content": "x"}], model="gpt-test", proxy_url="http://127.0.0.1:28787"
        )

    @patch("hermes_headroom_plugin.provider_headroom.compress_messages")
    def test_compress_failure_is_typed_for_caller_fail_open(self, compress):
        compress.return_value = {"success": False, "status": 503, "error": "unavailable"}
        original = [{"role": "user", "content": "exact"}]
        result = HeadroomReductionProvider().compress(original)
        self.assertFalse(result.ok)
        self.assertIs(result.value_or(original), original)

    def test_compress_rejects_non_message_payload_without_network(self):
        result = HeadroomReductionProvider().compress({"content": "wrong shape"})
        self.assertFalse(result.ok)
        self.assertIn("list", result.error)

    @patch("hermes_headroom_plugin.provider_headroom.retrieve")
    def test_retrieve_is_hash_only_and_exact(self, retrieve):
        retrieve.return_value = {"success": True, "hash": "abc123", "content": "complete exact source"}
        result = HeadroomReductionProvider(proxy_url="http://127.0.0.1:28787").retrieve("<<ccr:abc123>>")
        self.assertTrue(result.success)
        self.assertTrue(result.exact)
        self.assertEqual(result.content, "complete exact source")
        retrieve.assert_called_once_with("abc123", proxy_url="http://127.0.0.1:28787")

    @patch("hermes_headroom_plugin.provider_headroom.retrieve")
    def test_expired_or_missing_hash_is_explicit(self, retrieve):
        retrieve.return_value = {"success": False, "status": 404, "error": "not found or expired"}
        result = HeadroomReductionProvider().retrieve("abc123")
        self.assertFalse(result.success)
        self.assertTrue(result.expired_or_missing)
        self.assertIsNone(result.content)

    @patch("hermes_headroom_plugin.provider_headroom.retrieve_stats")
    def test_stats_are_read_only_mapping(self, stats):
        stats.return_value = {"success": True, "entries": 2}
        self.assertEqual(HeadroomReductionProvider().stats()["entries"], 2)


if __name__ == "__main__":
    unittest.main()
