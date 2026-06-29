import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hermes_headroom_plugin import wrappers


class WrapperCliTest(unittest.TestCase):
    def test_preflight_recommends_wrap_for_large_pytest_like_command(self):
        with mock.patch("hermes_headroom_plugin.wrappers.resolve_proxy_url", return_value="http://127.0.0.1:28787"), \
             mock.patch("hermes_headroom_plugin.wrappers.compact_health", return_value={"ok": True, "proxy_url": "http://127.0.0.1:28787"}):
            info = wrappers.classify_command(["pytest", "tests"], expected_chars=80_000, threshold=40_000, always=120_000, force=False, no_health=False)
        self.assertEqual(info["decision"], "wrap")
        self.assertFalse(info["contract"]["runtime_config_mutated"])
        self.assertIn("127.0.0.1:28787", info["proxy_url"])

    def test_preflight_keeps_exact_commands_direct(self):
        info = wrappers.classify_command(["git", "diff"], expected_chars=None, threshold=40_000, always=120_000, force=False, no_health=True)
        self.assertEqual(info["decision"], "direct")
        self.assertTrue(info["exact_reasons"])

    def test_worker_wrapper_retains_exact_sidecars_without_compression(self):
        with tempfile.TemporaryDirectory() as td:
            out_root = Path(td) / "runs"
            rc = wrappers.worker_main([
                "--lane", "unit-test",
                "--out-root", str(out_root),
                "--no-compress",
                "--",
                sys.executable, "-c", "print('hello wrapper')",
            ])
            self.assertEqual(rc, 0)
            runs = list(out_root.glob("*-unit-test"))
            self.assertEqual(len(runs), 1)
            run = runs[0]
            report = json.loads((run / "worker-lane-wrapper-report.json").read_text())
            self.assertEqual(report["wrapper_status"], "PASS")
            self.assertTrue(report["worker_final_packet_exact"])
            self.assertFalse(report["contract"]["runtime_config_mutated"])
            self.assertTrue((run / "worker-final-packet.md").exists())
            self.assertIn("hello wrapper", (run / "worker-stdout.raw.txt").read_text())

    def test_worker_wrapper_redacts_common_secret_patterns(self):
        token = "abcdefghijklmnopqrstuvwxyz1234567890"
        prefix = "Authorization: " + chr(66) + "earer "
        text = wrappers.redact(prefix + token)
        self.assertIn("Bearer [REDACTED]", text)
        self.assertNotIn(token, text)

    def test_console_entrypoints_declared_without_provider_route_scripts(self):
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        text = pyproject.read_text()
        self.assertIn("headroom-worker-lane", text)
        self.assertIn("headroom-background-lane", text)
        self.assertIn("headroom-command-preflight", text)
        self.assertNotIn("headroom-smart-route", text)
        self.assertNotIn("headroom-cost-route", text)


if __name__ == "__main__":
    unittest.main()
