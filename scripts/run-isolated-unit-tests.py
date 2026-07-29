#!/usr/bin/env python3
"""Run the full unit suite without inheriting a live Headroom/Hermes deployment.

For operator/developer shells, this entrypoint bootstraps an ephemeral ``uv``
environment when pytest is missing or the selected interpreter can import a live
Hermes host. It never installs pytest into the Hermes production/runtime venv.
CI and already-isolated callers with pytest available run directly.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PYTEST_SPEC = "pytest==9.1.1"
PYYAML_SPEC = "PyYAML>=6,<7"
BOOTSTRAP_SENTINEL = "HERMES_HEADROOM_UNIT_BOOTSTRAPPED"
_BLOCKED_IMPORT_ENV = {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}


def _clean_base_environment(*, keep_bootstrap_sentinel: bool = False) -> dict[str, str]:
    """Drop live Headroom and Python import-path state case-insensitively."""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper.startswith("HEADROOM_") or upper in _BLOCKED_IMPORT_ENV:
            continue
        if upper == BOOTSTRAP_SENTINEL and not keep_bootstrap_sentinel:
            continue
        env[key] = value
    return env


def isolated_environment(home: Path) -> dict[str, str]:
    """Return a pytest subprocess environment isolated from operator state."""
    env = _clean_base_environment()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "HERMES_HOME": str(home / ".hermes"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(REPO / "src"),
        }
    )
    return env


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, AttributeError, ValueError):
        return False


def bootstrap_reason() -> str | None:
    """Explain why an ephemeral developer environment is required."""
    if os.environ.get(BOOTSTRAP_SENTINEL) == "1":
        return None
    if not _module_available("pytest"):
        return "pytest_missing_from_selected_interpreter"
    if _module_available("agent.context_engine"):
        return "selected_interpreter_can_import_live_hermes_host"
    return None


def bootstrap_command(uv: str) -> list[str]:
    """Return the repository-independent, lock-free developer test command."""
    return [
        uv,
        "run",
        "--isolated",
        "--no-project",
        "--with",
        PYTEST_SPEC,
        "--with",
        PYYAML_SPEC,
        "--",
        "python",
        str(Path(__file__).resolve()),
    ]


def bootstrap_environment() -> dict[str, str]:
    env = _clean_base_environment()
    env[BOOTSTRAP_SENTINEL] = "1"
    return env


def test_command() -> list[str]:
    """Return the cache-free full-suite command used by CI and operators."""
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "tests",
    ]


def main() -> int:
    reason = bootstrap_reason()
    if reason:
        uv = shutil.which("uv")
        if not uv:
            print(
                "ERROR: isolated unit tests need uv because "
                f"{reason}. Install/use uv as a developer tool, then rerun "
                "`python scripts/run-isolated-unit-tests.py`. Do not install "
                "pytest into the Hermes production/runtime venv.",
                file=sys.stderr,
            )
            return 2
        print(
            f"INFO: bootstrapping ephemeral test environment with uv ({reason}); "
            "the Hermes production/runtime venv is unchanged.",
            file=sys.stderr,
        )
        completed = subprocess.run(
            bootstrap_command(uv),
            cwd=REPO,
            env=bootstrap_environment(),
            check=False,
        )
        return completed.returncode

    with tempfile.TemporaryDirectory(prefix="hermes-headroom-unit-home-") as td:
        home = Path(td)
        (home / ".hermes").mkdir()
        completed = subprocess.run(
            test_command(), cwd=REPO, env=isolated_environment(home), check=False
        )
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
