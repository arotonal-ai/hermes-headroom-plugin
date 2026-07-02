from pathlib import Path
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-production-runtime.py"


class InstallProductionRuntimeScriptTest(unittest.TestCase):
    def test_script_exists_and_has_safe_defaults(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('DEFAULT_SPEC = "headroom-ai[proxy]"', text)
        self.assertIn('DEFAULT_PORT = 28787', text)
        self.assertIn('RUNTIME_FULL', text)
        self.assertIn('RUNTIME_FULL_DURABLE', text)
        self.assertIn('headroom proxy', text)
        old_pin = '>=0.26,' + '<0.28'
        self.assertNotIn(old_pin, text)

    def test_help_documents_runtime_controls(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        for needle in ["--spec", "--port", "--no-start", "--no-smoke", "--stop-existing", "--systemd-user", "--service-name", "--hermes-home", "--skip-llm-monitor-companion", "--force-llm-monitor-companion", "--companion-only"]:
            self.assertIn(needle, proc.stdout)


    def test_llm_monitor_companion_bundle_is_present_and_packaged(self):
        root = ROOT / "src" / "hermes_headroom_plugin" / "companions" / "llm-monitor"
        self.assertTrue((root / "__init__.py").exists())
        self.assertTrue((root / "plugin.yaml").exists())
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("companions/llm-monitor/*", pyproject)

    def test_llm_monitor_companion_headroom_summary_prefers_turn_id_over_task_id(self):
        module_path = ROOT / "src" / "hermes_headroom_plugin" / "companions" / "llm-monitor" / "__init__.py"
        with tempfile.TemporaryDirectory() as td:
            old_home = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = td
            try:
                spec = importlib.util.spec_from_file_location("llm_monitor_companion_under_test", module_path)
                module = importlib.util.module_from_spec(spec)
                assert spec.loader is not None
                spec.loader.exec_module(module)
                event_dir = Path(td) / "control-plane" / "headroom" / "events"
                event_dir.mkdir(parents=True)
                event_log = event_dir / "headroom-events.jsonl"
                base = {"type": "headroom_tool_result", "session_id": "s1", "task_id": "task-shared", "lane": "terminal", "tool_name": "terminal"}
                rows = [
                    {**base, "turn_id": "turn-old", "action": "compressed", "tokens_saved": 1000},
                    {**base, "turn_id": "turn-new", "action": "exact", "tokens_saved": 0},
                ]
                event_log.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
                line = module._headroom_turn_summary_line({"session_id": "s1", "turn_id": "turn-new", "task_id": "task-shared"})
                self.assertIn("ready", line)
                self.assertIn("exact-safe `1`", line)
                self.assertIn("no eligible bulky output", line)
                self.assertNotIn("1,000", line)
            finally:
                if old_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = old_home

    def test_companion_only_installs_llm_monitor_into_temp_hermes_home(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            hermes_home = base / "hermes-home"
            venv = base / "venv"
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "--companion-only", "--hermes-home", str(hermes_home), "--venv", str(venv), "--json"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)
            data = json.loads(proc.stdout)
            self.assertEqual(data["state"], "COMPANION_INSTALLED")
            self.assertEqual(data["llm_monitor_companion"]["status"], "installed")
            target = hermes_home / "plugins" / "llm-monitor"
            self.assertTrue((target / "__init__.py").exists())
            self.assertTrue((target / "plugin.yaml").exists())
            self.assertFalse((venv / "bin" / "python").exists())

    def test_companion_only_preserves_existing_llm_monitor_without_force(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            hermes_home = base / "hermes-home"
            target = hermes_home / "plugins" / "llm-monitor"
            target.mkdir(parents=True)
            (target / "__init__.py").write_text("# owner local custom\n", encoding="utf-8")
            (target / "plugin.yaml").write_text("name: llm-monitor\nversion: custom\n", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "--companion-only", "--hermes-home", str(hermes_home), "--venv", str(base / "venv"), "--json"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)
            data = json.loads(proc.stdout)
            self.assertEqual(data["llm_monitor_companion"]["status"], "preserved_existing")
            self.assertIn("owner local custom", (target / "__init__.py").read_text(encoding="utf-8"))

    def test_linux_durable_runtime_state_is_documented_in_installer(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("systemctl", text)
        self.assertIn("hermes-context-reduction.service", text)
        self.assertIn("systemd --user", text)

    def test_docs_reference_production_installer(self):
        for rel in ["README.md", "INSTALL.md", "AGENTS.md", "docs/AGENT-INSTALL.md"]:
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("scripts/install-production-runtime.py", text, rel)
            self.assertIn("RUNTIME_FULL", text, rel)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertRegex(readme, re.compile(r"127\.0\.0\.1:28787", re.I))
        self.assertIn("RUNTIME_FULL_DURABLE", readme)

    def test_repo_no_longer_defaults_to_old_headroom_runtime_pin(self):
        offenders = []
        candidates = list(ROOT.rglob("*.py")) + list(ROOT.rglob("*.md")) + list(ROOT.rglob("*.yml")) + [ROOT / "pyproject.toml"]
        for path in candidates:
            if ".git" in path.parts or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            old_pin = ">=0.26," + "<0.28"
            if old_pin in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
