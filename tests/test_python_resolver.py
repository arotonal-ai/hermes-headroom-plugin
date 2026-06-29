import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
RESOLVER = SCRIPTS / "python-resolver.sh"


class PythonResolverTest(unittest.TestCase):
    def test_bash_helpers_use_python_resolver_for_python_execution(self):
        helpers = [
            SCRIPTS / "audit-repo-readiness.sh",
            SCRIPTS / "install-hermes-plugin.sh",
            SCRIPTS / "verify-hermes-install.sh",
            SCRIPTS / "test-clean-hermes-install.sh",
            SCRIPTS / "test-headroom-dependency-install.sh",
        ]
        for helper in helpers:
            text = helper.read_text(encoding="utf-8")
            self.assertIn("python-resolver.sh", text, helper)
            self.assertIn("resolve_python", text, helper)
            executable_python3 = re.findall(r"(?m)^\s*python3\s", text)
            self.assertEqual(executable_python3, [], helper)

    def test_resolver_documents_windows_fallbacks(self):
        text = RESOLVER.read_text(encoding="utf-8")
        self.assertIn("PYTHON_BIN", text)
        self.assertIn("python3", text)
        self.assertIn("python", text)
        self.assertIn("py -3", text)


if __name__ == "__main__":
    unittest.main()
