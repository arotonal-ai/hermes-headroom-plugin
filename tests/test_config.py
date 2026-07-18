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
        self.assertEqual(DEFAULT_PROXY_URL, "http://127.0.0.1:8787")
        self.assertEqual(cfg.proxy_url, DEFAULT_PROXY_URL)
        self.assertTrue(cfg.auto_compression)
        self.assertFalse(cfg.llm_request_enabled)
        self.assertEqual(cfg.llm_request_mode, "tool_results")
        self.assertEqual(cfg.min_tool_result_chars, DEFAULT_MIN_TOOL_RESULT_CHARS)
        self.assertEqual(cfg.event_log_max_bytes, DEFAULT_EVENT_LOG_MAX_BYTES)
        self.assertEqual(cfg.llm_request_cache_max, 2048)
        self.assertFalse(cfg.visible_status_marker)
        self.assertFalse(cfg.first_turn_hint)
        self.assertFalse(cfg.experimental_below_min_terminal_aggregate)
        self.assertEqual(cfg.report_retention_days, 14)
        self.assertEqual(cfg.report_max_bytes, 256 * 1024 * 1024)
        self.assertEqual(cfg.report_prune_interval_seconds, 3600)
        self.assertEqual(cfg.compatibility_warnings, ())

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

    def test_secondary_runtime_settings_share_effective_precedence(self):
        cfg = resolve_effective_config(
            raw_config={
                "llm_request_cache_max": 100,
                "visible_status_marker": False,
                "first_turn_hint": False,
                "experimental_below_min_terminal_aggregate": False,
                "report_retention_days": 30,
                "report_max_bytes": 1000,
                "report_prune_interval_seconds": 100,
            },
            env={
                "HEADROOM_LLM_REQUEST_CACHE_MAX": "300",
                "HEADROOM_VISIBLE_STATUS_MARKER": "1",
                "HEADROOM_FIRST_TURN_HINT": "true",
                "HEADROOM_EXPERIMENTAL_BELOW_MIN_AGGREGATE": "yes",
                "HEADROOM_REPORT_RETENTION_DAYS": "7",
                "HEADROOM_REPORT_MAX_BYTES": "12345",
                "HEADROOM_REPORT_PRUNE_INTERVAL_SECONDS": "999",
            },
            overrides={"llm_request_cache_max": 400, "report_retention_days": 9},
        )
        self.assertEqual(cfg.llm_request_cache_max, 400)
        self.assertTrue(cfg.visible_status_marker)
        self.assertTrue(cfg.first_turn_hint)
        self.assertTrue(cfg.experimental_below_min_terminal_aggregate)
        self.assertEqual(cfg.report_retention_days, 9)
        self.assertEqual(cfg.report_max_bytes, 12345)
        self.assertEqual(cfg.report_prune_interval_seconds, 999)

    def test_legacy_endpoint_shims_are_accepted_with_warnings(self):
        cfg = resolve_effective_config(
            raw_config={"host": "127.0.0.1", "port": 29999},
            env={"HEADROOM_HOST": "127.0.0.1", "HEADROOM_PORT": "28888"},
        )
        self.assertEqual(cfg.proxy_url, "http://127.0.0.1:28888")
        self.assertEqual(
            cfg.compatibility_warnings,
            (
                "legacy context_reduction.host; use context_reduction.proxy_url",
                "legacy context_reduction.port; use context_reduction.proxy_url",
                "legacy HEADROOM_HOST; use HEADROOM_PROXY_URL",
                "legacy HEADROOM_PORT; use HEADROOM_PROXY_URL",
            ),
        )

    def test_legacy_yaml_aliases_are_accepted_with_warnings(self):
        cfg = resolve_effective_config(
            raw_config={
                "auto_terminal": False,
                "compression_mode": "manual",
                "events_max_bytes": 100000,
            },
            env={},
        )
        self.assertFalse(cfg.auto_compression)
        self.assertEqual(cfg.event_log_max_bytes, 100000)
        self.assertEqual(
            cfg.compatibility_warnings,
            (
                "legacy context_reduction.auto_terminal; use context_reduction.auto_compression",
                "legacy context_reduction.compression_mode; use context_reduction.mode",
                "legacy context_reduction.events_max_bytes; use context_reduction.event_log_max_bytes",
            ),
        )


if __name__ == "__main__":
    unittest.main()
