import os
import unittest
from unittest.mock import patch

from hermes_headroom_plugin.proxy import (
    ProxyConfigurationError,
    is_loopback_proxy_url,
    readyz,
    resolve_proxy_url,
    retrieve,
    smoke,
)


class ProxyResolutionTest(unittest.TestCase):
    def _preserve_env(self, keys):
        return {k: os.environ.get(k) for k in keys}

    def _restore_env(self, old):
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_env_host_port_preferred(self):
        old = self._preserve_env(["HEADROOM_HOST", "HEADROOM_PORT", "HEADROOM_PROXY_URL", "HEADROOM_ALLOW_REMOTE_PROXY"])
        try:
            os.environ.pop("HEADROOM_ALLOW_REMOTE_PROXY", None)
            os.environ["HEADROOM_HOST"] = "127.0.0.1"
            os.environ["HEADROOM_PORT"] = "29999"
            os.environ["HEADROOM_PROXY_URL"] = "http://127.0.0.1:11111"
            self.assertEqual(resolve_proxy_url({}), "http://127.0.0.1:29999")
        finally:
            self._restore_env(old)

    def test_loopback_detection(self):
        for url in ["http://127.0.0.1:28787", "http://localhost:28787", "http://[::1]:28787"]:
            self.assertTrue(is_loopback_proxy_url(url), url)
        for url in ["http://example.com:28787", "http://192.168.1.5:28787"]:
            self.assertFalse(is_loopback_proxy_url(url), url)

    def test_remote_proxy_blocked_by_default(self):
        old = self._preserve_env(["HEADROOM_PROXY_URL", "HEADROOM_ALLOW_REMOTE_PROXY"])
        try:
            os.environ["HEADROOM_PROXY_URL"] = "http://192.168.1.5:28787"
            os.environ.pop("HEADROOM_ALLOW_REMOTE_PROXY", None)
            with self.assertRaises(ProxyConfigurationError):
                resolve_proxy_url({})
            status = readyz()
            self.assertFalse(status["ok"])
            self.assertEqual(status["error"], "proxy_configuration_blocked")
            self.assertIn("remote Headroom proxy URL blocked", str(status["body"]))
            result = smoke()
            self.assertFalse(result["ok"])
            self.assertEqual(result["phase"], "config")
        finally:
            self._restore_env(old)

    def test_remote_proxy_can_be_explicitly_allowed(self):
        old = self._preserve_env(["HEADROOM_PROXY_URL", "HEADROOM_ALLOW_REMOTE_PROXY"])
        try:
            os.environ["HEADROOM_PROXY_URL"] = "http://192.168.1.5:28787"
            os.environ["HEADROOM_ALLOW_REMOTE_PROXY"] = "1"
            self.assertEqual(resolve_proxy_url({}), "http://192.168.1.5:28787")
        finally:
            self._restore_env(old)
        self.assertEqual(resolve_proxy_url({"proxy_url": "http://192.168.1.5:28787", "allow_remote_proxy": True}), "http://192.168.1.5:28787")

    def test_retrieve_posts_hash_only_and_returns_complete_payload(self):
        retained = {"result": {"original_content": "complete exact retained payload"}}
        with patch("hermes_headroom_plugin.proxy.http_json", return_value=(200, retained, "")) as call:
            result = retrieve("<<ccr:abc123,base64,1KB>>", proxy_url="http://127.0.0.1:28787")
        self.assertTrue(result["success"])
        self.assertEqual(result["result"]["original_content"], "complete exact retained payload")
        self.assertEqual(call.call_args.args[1], {"hash": "abc123"})
        self.assertNotIn("query", call.call_args.args[1])

    def test_retrieve_rejects_invalid_hash_without_network(self):
        with patch("hermes_headroom_plugin.proxy.http_json") as call:
            result = retrieve("not a valid marker", proxy_url="http://127.0.0.1:28787")
        self.assertFalse(result["success"])
        self.assertIn("hash", result["error"])
        call.assert_not_called()

    def test_retrieve_reports_missing_or_expired_provider_response(self):
        with patch("hermes_headroom_plugin.proxy.http_json", return_value=(404, None, "marker missing or expired")):
            result = retrieve("abc123", proxy_url="http://127.0.0.1:28787")
        self.assertFalse(result["success"])
        self.assertIn("status=404", result["error"])


if __name__ == "__main__":
    unittest.main()
