import unittest

from hermes_headroom_plugin.config import (
    DEFAULT_EVENT_LOG_MAX_BYTES,
    DEFAULT_MIN_TOOL_RESULT_CHARS,
    DEFAULT_PROXY_URL,
    resolve_effective_config,
)


class EffectiveConfigTest(unittest.TestCase):
    def test_clean_defaults_are_portable_and_request_middleware_is_off(self):
        cfg = resolve_effective_config(raw_config={}, env={})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.provider, "headroom")
        self.assertEqual(cfg.proxy_url, DEFAULT_PROXY_URL)
        self.assertTrue(cfg.auto_compression)
        self.assertFalse(cfg.llm_request_enabled)
        self.assertEqual(cfg.llm_request_mode, "tool_results")
        self.assertEqual(cfg.min_tool_result_chars, DEFAULT_MIN_TOOL_RESULT_CHARS)
        self.assertEqual(cfg.event_log_max_bytes, DEFAULT_EVENT_LOG_MAX_BYTES)

    def test_legacy_yaml_aliases_resolve_at_one_boundary(self):
        cfg = resolve_effective_config(
            raw_config={
                "host": "127.0.0.1",
                "port": 29999,
                "auto_compress": False,
                "events_max_bytes": 123456,
                "llm_request_middleware": {"enabled": True, "mode": "tool_results"},
            },
            env={},
        )
        self.assertEqual(cfg.proxy_url, "http://127.0.0.1:29999")
        self.assertFalse(cfg.auto_compression)
        self.assertTrue(cfg.llm_request_enabled)
        self.assertEqual(cfg.event_log_max_bytes, 123456)

    def test_environment_overrides_yaml_and_preserves_host_port_compatibility(self):
        cfg = resolve_effective_config(
            raw_config={"proxy_url": "http://127.0.0.1:11111", "auto_compression": False},
            env={
                "HEADROOM_HOST": "127.0.0.1",
                "HEADROOM_PORT": "29999",
                "HEADROOM_PROXY_URL": "http://127.0.0.1:22222",
                "HEADROOM_AUTO_COMPRESSION": "1",
                "HEADROOM_LLM_REQUEST_COMPRESSION": "1",
            },
        )
        self.assertEqual(cfg.proxy_url, "http://127.0.0.1:29999")
        self.assertTrue(cfg.auto_compression)
        self.assertTrue(cfg.llm_request_enabled)

    def test_explicit_overrides_beat_environment(self):
        cfg = resolve_effective_config(
            raw_config={"proxy_url": "http://127.0.0.1:11111"},
            env={"HEADROOM_PROXY_URL": "http://127.0.0.1:22222", "HEADROOM_ALLOW_REMOTE_PROXY": "0"},
            overrides={
                "proxy_url": "http://127.0.0.1:33333",
                "auto_compression": False,
                "allow_remote_proxy": True,
                "llm_request_middleware": {"enabled": False, "mode": "off"},
            },
        )
        self.assertEqual(cfg.proxy_url, "http://127.0.0.1:33333")
        self.assertFalse(cfg.auto_compression)
        self.assertTrue(cfg.allow_remote_proxy)
        self.assertFalse(cfg.llm_request_enabled)

    def test_numeric_limits_fail_safe(self):
        cfg = resolve_effective_config(
            raw_config={"min_tool_result_chars": "invalid", "event_log_max_bytes": 1},
            env={},
        )
        self.assertEqual(cfg.min_tool_result_chars, DEFAULT_MIN_TOOL_RESULT_CHARS)
        self.assertEqual(cfg.event_log_max_bytes, 64000)


if __name__ == "__main__":
    unittest.main()
