import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_headroom_plugin import proxy
from hermes_headroom_plugin.commands import events_summary_main, handle_headroom_command


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self._auto_compression_env = patch.dict(
            os.environ,
            {"HEADROOM_AUTO_COMPRESSION": "1", "HEADROOM_VISIBLE_STATUS_MARKER": "0"},
        )
        self._auto_compression_env.start()

    def tearDown(self):
        self._auto_compression_env.stop()

    def test_smoke_compress_retrieve_pass(self):
        retained = {"content": ""}

        def fake_http_json(url, payload=None, timeout=15):
            del timeout
            if url.endswith('/readyz'):
                return 200, {"ready": True}, ""
            if url.endswith('/v1/compress'):
                retained["content"] = json.dumps((payload or {})["messages"], ensure_ascii=False)
                return 200, {
                    "messages": [{"role": "tool", "content": "<<ccr:abc123,base64,1KB>>"}],
                    "tokens_before": 1000,
                    "tokens_after": 100,
                    "tokens_saved": 900,
                }, ""
            if url.endswith('/v1/retrieve'):
                self.assertEqual(payload, {"hash": "abc123"})
                return 200, {"result": {"count": 1, "original_content": retained["content"]}}, ""
            raise AssertionError(url)

        with patch('hermes_headroom_plugin.proxy.http_json', fake_http_json):
            result = proxy.smoke(proxy_url='http://127.0.0.1:28787')
        self.assertTrue(result['ok'])
        self.assertEqual(result['marker'], 'abc123')
        self.assertTrue(result['sentinel_found'])

    def test_command_status_reports_visible_marker_state(self):
        with patch('hermes_headroom_plugin.commands.readyz', return_value={"ok": True, "proxy_url": "http://127.0.0.1:28787", "status": 200, "body": {"ready": True}}):
            text = handle_headroom_command('status')
        self.assertIn('visible_marker=off:disabled', text)
        self.assertIn('auto_compression=on', text)

    def test_command_status_reports_explicit_marker_opt_in(self):
        with patch.dict(os.environ, {"HEADROOM_VISIBLE_STATUS_MARKER": "1"}), patch(
            'hermes_headroom_plugin.commands.readyz',
            return_value={"ok": True, "proxy_url": "http://127.0.0.1:28787", "status": 200, "body": {"ready": True}},
        ):
            text = handle_headroom_command('status')
        self.assertIn('visible_marker=on:[HR✓]', text)

    def test_command_status_reports_manual_auto_compression_mode(self):
        with patch.dict(os.environ, {"HEADROOM_AUTO_COMPRESSION": "0"}), patch('hermes_headroom_plugin.commands.readyz', return_value={"ok": True, "proxy_url": "http://127.0.0.1:28787", "status": 200, "body": {"ready": True}}):
            text = handle_headroom_command('status')
        self.assertIn('auto_compression=manual', text)

    def test_command_smoke_proxy_down(self):
        with patch('hermes_headroom_plugin.commands.smoke', return_value={"ok": False, "phase": "readyz", "proxy_url": "http://x", "error": "proxy not ready"}):
            text = handle_headroom_command('smoke')
        self.assertIn('Headroom smoke FAIL', text)
        self.assertIn('readyz', text)


    def test_command_runtime_stats_uses_read_only_retrieve_stats(self):
        with patch('hermes_headroom_plugin.commands.readyz', return_value={"ok": True, "proxy_url": "http://127.0.0.1:28787", "status": 200, "body": {"ready": True}}), patch(
            'hermes_headroom_plugin.commands.retrieve_stats',
            return_value={
                "success": True,
                "store": {
                    "entry_count": 7,
                    "max_entries": 1000,
                    "default_ttl_seconds": 1800,
                    "total_original_tokens": 12000,
                    "total_compressed_tokens": 4000,
                    "total_retrievals": 3,
                    "event_count": 12,
                    "backend": {"backend_type": "sqlite", "db_path": "/sensitive/local/path.db"},
                },
                "recent_retrievals": [{"hash": "abc"}, {"hash": "def"}],
            },
        ):
            text = handle_headroom_command('runtime')
        self.assertIn('retrieve_stats=PASS', text)
        self.assertIn('entries=7', text)
        self.assertIn('orig_tokens=12000', text)
        self.assertIn('backend=sqlite', text)
        self.assertIn('recent=2', text)
        self.assertNotIn('/sensitive/local/path.db', text)

    def test_command_cache_reports_runtime_owned_store_without_local_path(self):
        with patch('hermes_headroom_plugin.commands.readyz', return_value={
            "ok": True,
            "proxy_url": "http://127.0.0.1:28787",
            "status": 200,
            "body": {"ready": True, "checks": {"cache": {"enabled": True, "status": "healthy"}}},
        }), patch(
            'hermes_headroom_plugin.commands.retrieve_stats',
            return_value={
                "success": True,
                "store": {
                    "entry_count": 25,
                    "max_entries": 100,
                    "default_ttl_seconds": 1800,
                    "total_retrievals": 9,
                    "event_count": 30,
                    "backend": {"backend_type": "sqlite", "bytes_used": 4096, "db_path": "/sensitive/local/ccr_store.db"},
                },
                "recent_retrievals": [{"hash": "abc"}],
            },
        ):
            text = handle_headroom_command('cache')
        self.assertIn('Headroom cache', text)
        self.assertIn('store=PASS', text)
        self.assertIn('entries=25', text)
        self.assertIn('usage_pct=25.0', text)
        self.assertIn('ttl_s=1800', text)
        self.assertIn('ttl=30m', text)
        self.assertIn('plugin_cache=none', text)
        self.assertIn('source_authority=backend_specific_unverified', text)
        self.assertIn('RUNTIME_FULL_DURABLE covers supervised runtime lifecycle', text)
        self.assertNotIn('/sensitive/local/ccr_store.db', text)

    def test_memory_ccr_contract_declares_restart_loss_and_marker_outliving_source(self):
        with patch('hermes_headroom_plugin.commands.readyz', return_value={
            "ok": True,
            "proxy_url": "http://127.0.0.1:28789",
            "status": 200,
            "body": {"ready": True, "checks": {"cache": {"enabled": True, "status": "healthy"}}},
        }), patch(
            'hermes_headroom_plugin.commands.retrieve_stats',
            return_value={
                "success": True,
                "store": {
                    "entry_count": 7,
                    "max_entries": 1000,
                    "default_ttl_seconds": 1800,
                    "backend": {"backend_type": "memory"},
                },
                "recent_retrievals": [],
            },
        ):
            text = handle_headroom_command('cache')
        self.assertIn('proxy=http://127.0.0.1:28789', text)
        self.assertIn('backend=memory', text)
        self.assertIn('ttl_s=1800', text)
        self.assertIn('source_authority=temporal', text)
        self.assertIn('restart_survival=no', text)
        self.assertIn('local_exact_fallback=none', text)
        self.assertIn('marker_outlives_source=possible', text)

    def test_command_on_reports_active_without_mutating_runtime(self):
        with patch('hermes_headroom_plugin.commands.readyz', return_value={"ok": True, "proxy_url": "http://127.0.0.1:28787", "status": 200, "body": {"ready": True}}):
            text = handle_headroom_command('on')
        self.assertIn('already active', text)
        self.assertIn('/headroom smoke', text)
        self.assertIn('visible_marker=off:disabled', text)

    def test_command_on_reports_no_slash_toggle_when_proxy_down(self):
        with patch('hermes_headroom_plugin.commands.readyz', return_value={"ok": False, "proxy_url": "http://127.0.0.1:28787", "status": None, "body": "connection refused"}):
            text = handle_headroom_command('on')
        self.assertIn('no slash-side toggle', text)
        self.assertIn('not ready', text)
        self.assertIn('visible_marker=off:disabled', text)
        self.assertIn('legacy read-only alias', text)

    def test_command_setup_reports_native_git_runtime_manager_without_mutating(self):
        script = Path('/tmp/headroom-plugin/scripts/headroom-runtime.py')
        with (
            patch('hermes_headroom_plugin.commands.readyz', return_value={"ok": False, "proxy_url": "http://127.0.0.1:8787", "status": None, "body": "connection refused"}),
            patch('hermes_headroom_plugin.commands._installed_runtime_script', return_value=script),
        ):
            text = handle_headroom_command('setup')
        self.assertIn('no state changed', text)
        self.assertIn(str(script), text)
        self.assertIn(' setup', text)
        self.assertIn('/headroom smoke', text)
        self.assertNotIn('--systemd-user', text)
        self.assertIn('py -3' if sys.platform == 'win32' else 'python3', text)

    def test_command_setup_uses_platform_appropriate_runtime_manager_command(self):
        cases = (("win32", "py -3"), ("darwin", "python3"), ("linux", "python3"))
        for platform, expected in cases:
            with self.subTest(platform=platform):
                with (
                    patch('hermes_headroom_plugin.commands.sys.platform', platform),
                    patch('hermes_headroom_plugin.commands.readyz', return_value={"ok": False, "proxy_url": "http://127.0.0.1:8787", "status": None, "body": "connection refused"}),
                    patch('hermes_headroom_plugin.commands._installed_runtime_script', return_value=Path('/tmp/headroom-plugin/scripts/headroom-runtime.py')),
                ):
                    text = handle_headroom_command('setup')
                self.assertIn(expected, text)
                self.assertIn(' setup', text)
                self.assertNotIn('--systemd-user', text)

    def test_command_setup_reports_wheel_runtime_manager_entrypoint(self):
        with (
            patch('hermes_headroom_plugin.commands.readyz', return_value={"ok": False, "proxy_url": "http://127.0.0.1:8787", "status": None, "body": "connection refused"}),
            patch('hermes_headroom_plugin.commands._installed_runtime_script', return_value=None),
        ):
            text = handle_headroom_command('setup')
        self.assertIn('headroom-runtime setup', text)
        self.assertIn('wheel environment', text)
        self.assertNotIn('not present', text)

    def test_unknown_command_usage_mentions_setup_and_on_compatibility(self):
        text = handle_headroom_command('some')
        self.assertIn('status|setup|smoke|audit|runtime|stats|cache', text)
        self.assertIn('legacy: on', text)


    def _write_events(self, root, events):
        path = Path(root) / "control-plane" / "headroom" / "events" / "headroom-events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n", encoding="utf-8")
        return path

    def test_usage_reports_no_events_without_creating_log(self):
        with tempfile.TemporaryDirectory() as td, patch('hermes_headroom_plugin.commands.hermes_home', return_value=Path(td)):
            text = handle_headroom_command('usage')
            event_path = Path(td) / "control-plane" / "headroom" / "events" / "headroom-events.jsonl"
        self.assertIn('Headroom usage · no events yet', text)
        self.assertFalse(event_path.exists())

    def test_usage_lanes_turn_and_tail_summarize_synthetic_events(self):
        events = [
            {
                "type": "headroom_tool_result",
                "ts": "2026-07-02T17:20:00Z",
                "action": "compressed",
                "tool_name": "delegate_task",
                "lane": "delegate",
                "turn_id": "turn-a",
                "session_id": "s",
                "platform": "telegram",
                "tokens_saved": 1200,
                "marker": "hash-a",
                "reason": "eligible_tool",
            },
            {
                "type": "headroom_tool_result",
                "ts": "2026-07-02T17:21:00Z",
                "action": "exact",
                "tool_name": "read_file",
                "lane": "unknown",
                "turn_id": "turn-a",
                "tokens_saved": None,
                "reason": "exact_tool:read_file",
            },
            {
                "type": "headroom_tool_result",
                "ts": "2026-07-02T17:22:00Z",
                "action": "blocked",
                "tool_name": "terminal",
                "lane": "terminal",
                "turn_id": "turn-b",
                "tokens_saved": 0,
                "reason": "protected_control_or_sensitive_material",
            },
            {"type": "not_headroom", "action": "compressed", "tokens_saved": 999999},
        ]
        with tempfile.TemporaryDirectory() as td, patch('hermes_headroom_plugin.commands.hermes_home', return_value=Path(td)):
            event_path = self._write_events(td, events)
            usage = handle_headroom_command('usage')
            turn = handle_headroom_command('usage turn turn-a')
            latest_turn = handle_headroom_command('usage turn')
            lanes = handle_headroom_command('lanes')
            decisions = handle_headroom_command('decisions turn turn-a')
            why = handle_headroom_command('why turn turn-a')
            opportunities = handle_headroom_command('opportunities')
            tail = handle_headroom_command('tail 2')
            self.assertTrue(event_path.exists())
        self.assertIn('events=3', usage)
        self.assertIn('compressed=1', usage)
        self.assertIn('exact=1', usage)
        self.assertIn('blocked=1', usage)
        self.assertIn('saved=1200', usage)
        self.assertIn('turn_id=turn-a', turn)
        self.assertIn('events=2', turn)
        self.assertIn('exact=1', turn)
        self.assertIn('turn_id=turn-b', latest_turn)
        self.assertIn('delegate: events=1 compressed=1 saved=1200', lanes)
        self.assertIn('terminal: events=1 compressed=0 saved=0', lanes)
        self.assertIn('file: events=1 compressed=0 saved=0', lanes)
        self.assertIn('Headroom decisions · turn_id=turn-a', decisions)
        self.assertIn('family=compressed', decisions)
        self.assertIn('read_file lane=file', decisions)
        self.assertIn('family=safety_exact', decisions)
        self.assertEqual(decisions, why)
        self.assertIn('Headroom opportunities · events=3', opportunities)
        self.assertIn('terminal/build logs', opportunities)
        self.assertIn('header-missing audit', opportunities)
        self.assertIn('Headroom tail · n=2', tail)
        self.assertIn('exact tool=read_file', tail)
        self.assertIn('blocked tool=terminal', tail)
        self.assertNotIn('999999', usage)

    def test_events_summary_cli_renderer_matches_slash_aggregator(self):
        events = [
            {
                "type": "headroom_tool_result",
                "ts": "2026-07-02T17:25:00Z",
                "action": "compressed",
                "tool_name": "terminal",
                "lane": "terminal",
                "turn_id": "turn-cli",
                "tokens_saved": 777,
                "marker": "hash-cli",
            }
        ]
        with tempfile.TemporaryDirectory() as td, patch('hermes_headroom_plugin.commands.hermes_home', return_value=Path(td)):
            self._write_events(td, events)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = events_summary_main(['usage', '--turn', 'turn-cli'])
            self.assertEqual(code, 0)
            text = buf.getvalue()
            self.assertIn('Headroom usage turn', text)
            self.assertIn('turn_id=turn-cli', text)
            self.assertIn('saved=777', text)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = events_summary_main(['tail', '-n', '1'])
            self.assertEqual(code, 0)
            self.assertIn('marker=hash-cli', buf.getvalue())

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = events_summary_main(['decisions', '--turn', 'turn-cli'])
            self.assertEqual(code, 0)
            decision_text = buf.getvalue()
            self.assertIn('Headroom decisions · turn_id=turn-cli', decision_text)
            self.assertIn('terminal lane=terminal', decision_text)
            self.assertIn('family=compressed', decision_text)

            with patch('hermes_headroom_plugin.commands.readyz', return_value={"ok": True, "proxy_url": "http://127.0.0.1:28787", "status": 200, "body": {"ready": True, "checks": {"cache": {"enabled": True, "status": "healthy"}}}}), patch(
                'hermes_headroom_plugin.commands.retrieve_stats',
                return_value={"success": True, "store": {"entry_count": 1, "backend": {"backend_type": "sqlite"}}, "recent_retrievals": []},
            ):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = events_summary_main(['runtime'])
            self.assertEqual(code, 0)
            self.assertIn('Headroom runtime', buf.getvalue())

            with patch('hermes_headroom_plugin.commands.readyz', return_value={"ok": True, "proxy_url": "http://127.0.0.1:28787", "status": 200, "body": {"ready": True, "checks": {"cache": {"enabled": True, "status": "healthy"}}}}), patch(
                'hermes_headroom_plugin.commands.retrieve_stats',
                return_value={"success": True, "store": {"entry_count": 1, "max_entries": 10, "default_ttl_seconds": 60, "backend": {"backend_type": "sqlite"}}, "recent_retrievals": []},
            ):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = events_summary_main(['cache'])
            self.assertEqual(code, 0)
            self.assertIn('Headroom cache', buf.getvalue())


if __name__ == '__main__':
    unittest.main()
