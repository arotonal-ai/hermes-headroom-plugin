import unittest
from unittest.mock import patch

from hermes_headroom_plugin import proxy
from hermes_headroom_plugin.commands import handle_headroom_command


class SmokeTest(unittest.TestCase):
    def test_smoke_compress_retrieve_pass(self):
        def fake_http_json(url, payload=None, timeout=15):
            if url.endswith('/readyz'):
                return 200, {"ready": True}, ""
            if url.endswith('/v1/compress'):
                return 200, {
                    "messages": [{"role": "tool", "content": "<<ccr:abc123,base64,1KB>>"}],
                    "tokens_before": 1000,
                    "tokens_after": 100,
                    "tokens_saved": 900,
                }, ""
            if url.endswith('/v1/retrieve'):
                return 200, {"result": {"count": 1, "original_content": payload.get("query", proxy.SMOKE_SENTINEL)}}, ""
            raise AssertionError(url)

        with patch('hermes_headroom_plugin.proxy.http_json', fake_http_json):
            result = proxy.smoke(proxy_url='http://127.0.0.1:28787')
        self.assertTrue(result['ok'])
        self.assertEqual(result['marker'], 'abc123')
        self.assertTrue(result['sentinel_found'])

    def test_command_smoke_proxy_down(self):
        with patch('hermes_headroom_plugin.commands.smoke', return_value={"ok": False, "phase": "readyz", "proxy_url": "http://x", "error": "proxy not ready"}):
            text = handle_headroom_command('smoke')
        self.assertIn('Headroom smoke FAIL', text)
        self.assertIn('readyz', text)


if __name__ == '__main__':
    unittest.main()
