from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release-candidate-local-gate.py"
SPEC = importlib.util.spec_from_file_location("release_candidate_local_gate", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_cleanup_ephemeral_envs_removes_only_allowlisted_dirs(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    for name in MODULE.EPHEMERAL_ENV_DIRS:
        env_dir = run_dir / name
        env_dir.mkdir()
        (env_dir / "payload.bin").write_bytes(b"x" * 64)
    evidence = run_dir / "summary.json"
    evidence.write_text("{}", encoding="utf-8")

    report = MODULE.cleanup_ephemeral_envs(run_dir)

    assert report["pass"] is True
    assert report["removed_bytes"] == 64 * len(MODULE.EPHEMERAL_ENV_DIRS)
    assert all(not (run_dir / name).exists() for name in MODULE.EPHEMERAL_ENV_DIRS)
    assert evidence.exists()


def test_cleanup_ephemeral_envs_keep_override(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    env_dir = run_dir / MODULE.EPHEMERAL_ENV_DIRS[0]
    env_dir.mkdir()
    (env_dir / "payload.bin").write_bytes(b"debug")

    report = MODULE.cleanup_ephemeral_envs(run_dir, keep=True)

    assert report["pass"] is True
    assert report["removed_bytes"] == 0
    assert env_dir.exists()
    assert report["entries"][0]["status"] == "retained_by_request"


def test_cleanup_ephemeral_envs_blocks_symlink_escape(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (run_dir / MODULE.EPHEMERAL_ENV_DIRS[0]).symlink_to(outside, target_is_directory=True)

    report = MODULE.cleanup_ephemeral_envs(run_dir)

    assert report["pass"] is False
    assert report["entries"][0]["status"] == "blocked"
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
