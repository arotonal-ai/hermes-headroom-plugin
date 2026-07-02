import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_headroom_plugin import proxy
from hermes_headroom_plugin.commands import events_summary_main, handle_headroom_command


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

    def test_command_status_reports_visible_marker_state(self):
        with patch('hermes_headroom_plugin.commands.readyz', return_value={"ok": True, "proxy_url": "http://127.0.0.1:28787", "status": 200, "body": {"ready": True}}):
            text = handle_headroom_command('status')
        self.assertIn('visible_marker=on:[HR✓]', text)

    def test_command_smoke_proxy_down(self):
        with patch('hermes_headroom_plugin.commands.smoke', return_value={"ok": False, "phase": "readyz", "proxy_url": "http://x", "error": "proxy not ready"}):
            text = handle_headroom_command('smoke')
        self.assertIn('Headroom smoke FAIL', text)
        self.assertIn('readyz', text)

    def test_command_on_reports_active_without_mutating_runtime(self):
        with patch('hermes_headroom_plugin.commands.readyz', return_value={"ok": True, "proxy_url": "http://127.0.0.1:28787", "status": 200, "body": {"ready": True}}):
            text = handle_headroom_command('on')
        self.assertIn('already active', text)
        self.assertIn('/headroom smoke', text)
        self.assertIn('visible_marker=on:[HR✓]', text)

    def test_command_on_reports_no_slash_toggle_when_proxy_down(self):
        with patch('hermes_headroom_plugin.commands.readyz', return_value={"ok": False, "proxy_url": "http://127.0.0.1:28787", "status": None, "body": "connection refused"}):
            text = handle_headroom_command('on')
        self.assertIn('no slash-side toggle', text)
        self.assertIn('not ready', text)
        self.assertIn('visible_marker=on:[HR!]', text)

    def test_unknown_command_usage_mentions_on_compatibility(self):
        text = handle_headroom_command('some')
        self.assertIn('status|smoke|audit|on', text)


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


if __name__ == '__main__':
    unittest.main()
