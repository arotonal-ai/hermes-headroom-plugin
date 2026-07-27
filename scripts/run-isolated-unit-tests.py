#!/usr/bin/env python3
"""Run the unit suite without inheriting a live Headroom/Hermes deployment."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def isolated_environment(home: Path) -> dict[str, str]:
    """Return a subprocess environment isolated from operator runtime state."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("HEADROOM_")}
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "HERMES_HOME": str(home / ".hermes"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    source = str(REPO / "src")
    inherited_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = source + (os.pathsep + inherited_pythonpath if inherited_pythonpath else "")
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
    with tempfile.TemporaryDirectory(prefix="hermes-headroom-unit-home-") as td:
        home = Path(td)
        (home / ".hermes").mkdir()
        completed = subprocess.run(
            test_command(), cwd=REPO, env=isolated_environment(home), check=False
        )
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
