import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
RESOLVER = SCRIPTS / "python-resolver.sh"
DEPENDENCY_HELPER = SCRIPTS / "test-headroom-dependency-install.py"


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
            self.assertNotRegex(text, r"(?m)^\s*python3\s", helper)

    def test_resolver_documents_windows_and_hermes_fallbacks(self):
        text = RESOLVER.read_text(encoding="utf-8")
        for needle in ["PYTHON_BIN", "python3", "python", "py -3", "hermes", "python.exe"]:
            self.assertIn(needle, text)

    @unittest.skipUnless(os.name == "posix", "uses POSIX shell wrappers")
    def test_resolver_can_use_python_colocated_with_hermes_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            fakebin = Path(tmp)
            python3 = fakebin / "python3"
            python3.write_text("#!/usr/bin/env bash\nexit 127\n", encoding="utf-8")
            python3.chmod(0o755)
            hermes = fakebin / "hermes"
            hermes.write_text("#!/usr/bin/env bash\necho fake hermes\n", encoding="utf-8")
            hermes.chmod(0o755)
            python = fakebin / "python"
            python.write_text(f"#!/usr/bin/env bash\nexec {sys.executable!r} \"$@\"\n", encoding="utf-8")
            python.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{fakebin}:/usr/bin:/bin"
            script = f"source {RESOLVER}; resolve_python_with_module json; printf '%s\n' \"${{PY_CMD[0]}}\""
            proc = subprocess.run(["bash", "-lc", script], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "python")

    def test_dependency_helper_checks_native_pydantic_core_runtime_import(self):
        text = DEPENDENCY_HELPER.read_text(encoding="utf-8")
        self.assertIn("pydantic_core._pydantic_core", text)
        self.assertIn("newer than the currently smoke-tested Windows runtime path", text)


if __name__ == "__main__":
    unittest.main()
