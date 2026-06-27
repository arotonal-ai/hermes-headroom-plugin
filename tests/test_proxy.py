import os
import unittest

from hermes_headroom_plugin.proxy import resolve_proxy_url


class ProxyResolutionTest(unittest.TestCase):
    def test_env_host_port_preferred(self):
        old = {k: os.environ.get(k) for k in ["HEADROOM_HOST", "HEADROOM_PORT", "HEADROOM_PROXY_URL"]}
        try:
            os.environ["HEADROOM_HOST"] = "127.0.0.1"
            os.environ["HEADROOM_PORT"] = "29999"
            os.environ["HEADROOM_PROXY_URL"] = "http://127.0.0.1:11111"
            self.assertEqual(resolve_proxy_url({}), "http://127.0.0.1:29999")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
