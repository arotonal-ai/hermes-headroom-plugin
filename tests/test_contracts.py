import unittest

from hermes_headroom_plugin.contracts import (
    CompressionResult,
    ProviderHealth,
    ReductionContext,
    ReductionProvider,
    RetrievalResult,
    normalize_ccr_hash,
)


class FakeReductionProvider:
    name = "fake"

    def __init__(self):
        self.values = {"abc123": "exact retained content"}

    def ready(self):
        return ProviderHealth(ready=True, provider=self.name, status=200)

    def compress(self, payload, context=None):
        del context
        return CompressionResult(ok=True, value=f"reduced:{payload}", marker="abc123", provider=self.name)

    def retrieve(self, hash_key):
        content = self.values.get(hash_key)
        return RetrievalResult(
            success=content is not None,
            hash=hash_key,
            content=content,
            error="" if content is not None else "missing or expired",
            provider=self.name,
            expired_or_missing=content is None,
        )

    def stats(self):
        return {"entries": len(self.values)}


class ProviderContractTest(unittest.TestCase):
    def test_fake_provider_satisfies_runtime_contract(self):
        provider = FakeReductionProvider()
        self.assertIsInstance(provider, ReductionProvider)
        self.assertTrue(provider.ready().ready)
        result = provider.compress("payload", ReductionContext(tool_name="terminal"))
        self.assertEqual(result.value_or("original"), "reduced:payload")
        retrieved = provider.retrieve(result.marker)
        self.assertTrue(retrieved.success)
        self.assertTrue(retrieved.exact)
        self.assertEqual(retrieved.content, "exact retained content")

    def test_compression_failure_is_copy_on_write_fail_open(self):
        original = {"unchanged": [1, 2, 3]}
        failed = CompressionResult(ok=False, error="provider unavailable")
        self.assertIs(failed.value_or(original), original)

    def test_missing_retrieval_is_explicit_not_fabricated(self):
        result = FakeReductionProvider().retrieve("expired1")
        self.assertFalse(result.success)
        self.assertTrue(result.expired_or_missing)
        self.assertIsNone(result.content)


class MarkerContractTest(unittest.TestCase):
    def test_normalizes_only_known_hash_and_marker_forms(self):
        cases = {
            "abc123": "abc123",
            "ccr:abc123": "abc123",
            "<<ccr:abc123,base64,1KB>>": "abc123",
            "[Headroom compressed hash=abc123]": "abc123",
            "marker='abc123'": "abc123",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_ccr_hash(raw), expected)

    def test_rejects_missing_short_or_arbitrary_prose(self):
        for raw in (None, "", "abc", "please retrieve abc123", "<<ccr:bad hash>>"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_ccr_hash(raw), "")


if __name__ == "__main__":
    unittest.main()
