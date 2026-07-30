import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_headroom_plugin.retention import maybe_prune_reports, prune_report_artifacts, retention_settings


class ReportRetentionTest(unittest.TestCase):
    def _write_group(self, root: Path, name: str, *, size: int, mtime: float, pinned: bool = False) -> list[Path]:
        paths = [
            root / f"{name}.json",
            root / f"{name}.compressed.json",
            root / f"{name}.redacted.log",
        ]
        for path in paths:
            path.write_bytes(b"x" * size)
            os.utime(path, (mtime, mtime))
        if pinned:
            marker = root / f"{name}.keep"
            marker.write_text("retain\n", encoding="utf-8")
            os.utime(marker, (mtime, mtime))
            paths.append(marker)
        return paths

    def test_age_pruning_deletes_complete_old_group_and_preserves_pinned(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = self._write_group(root, "auto-tool-old-terminal", size=10, mtime=now - 20 * 86400)
            pinned = self._write_group(root, "auto-tool-pinned-terminal", size=10, mtime=now - 20 * 86400, pinned=True)
            recent = self._write_group(root, "auto-tool-recent-terminal", size=10, mtime=now - 60)
            result = prune_report_artifacts(root, retention_days=14, max_bytes=1024 * 1024, now=now)
            self.assertEqual(result["selected_groups"], 1)
            self.assertEqual(result["deleted_files"], 3)
            self.assertTrue(all(not path.exists() for path in old))
            self.assertTrue(all(path.exists() for path in pinned + recent))

    def test_size_pruning_deletes_oldest_groups_atomically(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            oldest = self._write_group(root, "auto-tool-oldest-terminal", size=100, mtime=now - 300)
            newer = self._write_group(root, "auto-tool-newer-terminal", size=100, mtime=now - 200)
            newest = self._write_group(root, "auto-tool-newest-terminal", size=100, mtime=now - 100)
            result = prune_report_artifacts(root, retention_days=0, max_bytes=600, now=now)
            self.assertEqual(result["selected_groups"], 1)
            self.assertEqual(result["after_bytes"], 600)
            self.assertTrue(all(not path.exists() for path in oldest))
            self.assertTrue(all(path.exists() for path in newer + newest))

    def test_exact_sidecar_is_grouped_with_its_report(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report = root / "auto-tool-old-worker.json"
            exact = root / "auto-tool-old-worker.exact.log"
            for path in (report, exact):
                path.write_text("exact evidence\n", encoding="utf-8")
                os.utime(path, (now - 20 * 86400, now - 20 * 86400))
            result = prune_report_artifacts(root, retention_days=14, max_bytes=1024, now=now)
            self.assertEqual(result["selected_groups"], 1)
            self.assertEqual(result["deleted_files"], 2)
            self.assertFalse(report.exists())
            self.assertFalse(exact.exists())

    def test_dry_run_reports_without_deleting(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self._write_group(root, "auto-tool-old-terminal", size=10, mtime=now - 20 * 86400)
            result = prune_report_artifacts(root, retention_days=14, max_bytes=1024, now=now, dry_run=True)
            self.assertEqual(result["selected_groups"], 1)
            self.assertEqual(result["deleted_files"], 3)
            self.assertTrue(all(path.exists() for path in paths))

    def test_environment_overrides_config_and_interval_is_bounded(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {
                "HEADROOM_REPORT_RETENTION_DAYS": "7",
                "HEADROOM_REPORT_MAX_BYTES": "12345",
                "HEADROOM_REPORT_PRUNE_INTERVAL_SECONDS": "999",
            },
        ):
            settings = retention_settings({"report_retention_days": 30, "report_max_bytes": 99})
            self.assertEqual(settings["retention_days"], 7)
            self.assertEqual(settings["max_bytes"], 12345)
            first = maybe_prune_reports(Path(td), force=True)
            second = maybe_prune_reports(Path(td))
            self.assertFalse(first.get("skipped", False))
            self.assertEqual(second.get("reason"), "interval")


if __name__ == "__main__":
    unittest.main()
