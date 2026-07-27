"""Pytest host isolation; canonical full-suite isolation lives in the runner."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest  # type: ignore[import-not-found]


_SESSION_STATE = "_hermes_headroom_isolated_environment"


def pytest_configure(config) -> None:
    """Isolate environment before pytest imports test/plugin modules."""
    if hasattr(config, _SESSION_STATE):
        return
    original = dict(os.environ)
    temporary_home = tempfile.TemporaryDirectory(prefix="hermes-headroom-pytest-home-")
    home = Path(temporary_home.name)
    hermes_home = home / ".hermes"
    hermes_home.mkdir()
    clean_env = {
        key: value for key, value in original.items() if not key.startswith("HEADROOM_")
    }
    clean_env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "HERMES_HOME": str(hermes_home),
            "HERMES_HEADROOM_PYTEST_ISOLATED": "1",
        }
    )
    os.environ.clear()
    os.environ.update(clean_env)
    setattr(config, _SESSION_STATE, (original, temporary_home))


def pytest_unconfigure(config) -> None:
    """Restore the caller environment if pytest was embedded in-process."""
    state = getattr(config, _SESSION_STATE, None)
    if state is None:
        return
    original, temporary_home = state
    os.environ.clear()
    os.environ.update(original)
    temporary_home.cleanup()
    delattr(config, _SESSION_STATE)


@pytest.fixture(autouse=True)
def isolate_live_headroom_host(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Give each direct pytest test a clean environment under the session sandbox."""
    for key in tuple(os.environ):
        if key.startswith("HEADROOM_"):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "isolated-host-home"
    home.mkdir()
    hermes_home = home / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
