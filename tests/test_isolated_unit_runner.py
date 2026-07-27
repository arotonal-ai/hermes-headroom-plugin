from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "run-isolated-unit-tests.py"
SPEC = importlib.util.spec_from_file_location("run_isolated_unit_tests", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class IsolatedUnitRunnerTest(unittest.TestCase):
    def test_isolated_environment_drops_headroom_state_and_preserves_unrelated_env(self):
        isolated_home = Path("/isolated/test-home")
        old = dict(os.environ)
        try:
            os.environ["HEADROOM_HOST"] = "127.0.0.1"
            os.environ["HEADROOM_RUNTIME_ROOT"] = "/live/runtime"
            os.environ["KEEP_FOR_TEST"] = "yes"
            env = MODULE.isolated_environment(isolated_home)
        finally:
            os.environ.clear()
            os.environ.update(old)

        self.assertFalse(any(key.startswith("HEADROOM_") for key in env))
        self.assertEqual(env["HOME"], str(isolated_home))
        self.assertEqual(env["USERPROFILE"], str(isolated_home))
        self.assertEqual(env["HERMES_HOME"], str(isolated_home / ".hermes"))
        self.assertEqual(env["KEEP_FOR_TEST"], "yes")
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep)[0], str(REPO / "src"))

    def test_runner_uses_full_pytest_collection_without_repo_cache(self):
        command = MODULE.test_command()
        self.assertEqual(command[1:3], ["-m", "pytest"])
        self.assertIn("no:cacheprovider", command)
        self.assertEqual(command[-1], "tests")


if __name__ == "__main__":
    unittest.main()
