from pathlib import Path
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-production-runtime.py"


class InstallProductionRuntimeScriptTest(unittest.TestCase):
    def test_script_exists_and_has_safe_defaults(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('DEFAULT_SPEC = "headroom-ai[proxy]"', text)
        self.assertIn('DEFAULT_PORT = 28787', text)
        self.assertIn('RUNTIME_FULL', text)
        self.assertIn('headroom proxy', text)
        self.assertNotIn('>=0.26,<0.28', text)

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
        for needle in ["--spec", "--port", "--no-start", "--no-smoke", "--stop-existing"]:
            self.assertIn(needle, proc.stdout)

    def test_docs_reference_production_installer(self):
        for rel in ["README.md", "INSTALL.md", "AGENTS.md", "docs/AGENT-INSTALL.md"]:
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("scripts/install-production-runtime.py", text, rel)
            self.assertIn("RUNTIME_FULL", text, rel)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertRegex(readme, re.compile(r"127\.0\.0\.1:28787", re.I))


if __name__ == "__main__":
    unittest.main()
