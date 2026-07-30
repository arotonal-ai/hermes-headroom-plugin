from __future__ import annotations

import importlib.util
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "run-isolated-unit-tests.py"
SPEC = importlib.util.spec_from_file_location("run_isolated_unit_tests", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class IsolatedUnitRunnerTest(unittest.TestCase):
    def test_isolated_environment_drops_runtime_and_import_state(self):
        isolated_home = Path("/isolated/test-home")
        old = dict(os.environ)
        try:
            os.environ["HEADROOM_HOST"] = "127.0.0.1"
            os.environ["headroom_runtime_root"] = "/live/runtime"
            os.environ["PYTHONPATH"] = "/live/hermes/source"
            os.environ["pythonhome"] = "/live/hermes/venv"
            os.environ["VIRTUAL_ENV"] = "/live/hermes/venv"
            os.environ["KEEP_FOR_TEST"] = "yes"
            env = MODULE.isolated_environment(isolated_home)
        finally:
            os.environ.clear()
            os.environ.update(old)

        self.assertFalse(any(key.upper().startswith("HEADROOM_") for key in env))
        self.assertNotIn("PYTHONHOME", {key.upper() for key in env})
        self.assertNotIn("VIRTUAL_ENV", {key.upper() for key in env})
        self.assertEqual(env["HOME"], str(isolated_home))
        self.assertEqual(env["USERPROFILE"], str(isolated_home))
        self.assertEqual(env["HERMES_HOME"], str(isolated_home / ".hermes"))
        self.assertEqual(env["KEEP_FOR_TEST"], "yes")
        self.assertEqual(env["PYTHONPATH"], str(REPO / "src"))

    def test_runner_uses_full_pytest_collection_without_repo_cache(self):
        command = MODULE.test_command()
        self.assertEqual(command[1:3], ["-m", "pytest"])
        self.assertIn("no:cacheprovider", command)
        self.assertEqual(command[-1], "tests")

    def test_bootstrap_reason_covers_missing_pytest_and_live_hermes_host(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            MODULE, "_module_available", side_effect=lambda name: False
        ):
            self.assertEqual(
                MODULE.bootstrap_reason(), "pytest_missing_from_selected_interpreter"
            )

        with patch.dict(os.environ, {}, clear=True), patch.object(
            MODULE,
            "_module_available",
            side_effect=lambda name: name in {"pytest", "agent.context_engine"},
        ):
            self.assertEqual(
                MODULE.bootstrap_reason(),
                "selected_interpreter_can_import_live_hermes_host",
            )

        with patch.dict(
            os.environ, {MODULE.BOOTSTRAP_SENTINEL: "1"}, clear=True
        ), patch.object(MODULE, "_module_available", return_value=False):
            self.assertIsNone(MODULE.bootstrap_reason())

    def test_bootstrap_command_pins_dev_dependencies_without_project_lock(self):
        command = MODULE.bootstrap_command("/usr/bin/uv")
        self.assertEqual(command[:4], ["/usr/bin/uv", "run", "--isolated", "--no-project"])
        self.assertIn(MODULE.PYTEST_SPEC, command)
        self.assertIn(MODULE.PYYAML_SPEC, command)
        self.assertEqual(command[-2:], ["python", str(SCRIPT)])

    def test_main_bootstraps_with_uv_and_preserves_runtime_venv(self):
        completed = subprocess.CompletedProcess(args=["uv"], returncode=0)
        with patch.object(MODULE, "bootstrap_reason", return_value="pytest_missing"), patch.object(
            MODULE.shutil, "which", return_value="/usr/bin/uv"
        ), patch.object(MODULE.subprocess, "run", return_value=completed) as run:
            self.assertEqual(MODULE.main(), 0)

        args, kwargs = run.call_args
        self.assertEqual(args[0], MODULE.bootstrap_command("/usr/bin/uv"))
        self.assertEqual(kwargs["env"][MODULE.BOOTSTRAP_SENTINEL], "1")
        self.assertFalse(
            any(key.upper().startswith("HEADROOM_") for key in kwargs["env"])
        )

    def test_main_fails_actionably_when_uv_is_unavailable(self):
        with patch.object(MODULE, "bootstrap_reason", return_value="pytest_missing"), patch.object(
            MODULE.shutil, "which", return_value=None
        ):
            self.assertEqual(MODULE.main(), 2)


if __name__ == "__main__":
    unittest.main()
