from __future__ import annotations

import importlib.util
import tempfile
import tomllib
import zipfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "release-candidate-local-gate.py"
SPEC = importlib.util.spec_from_file_location("release_candidate_local_gate", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

RUNTIME_SMOKE_SCRIPT = REPO / "scripts" / "test-headroom-runtime-smoke.py"
RUNTIME_SMOKE_SPEC = importlib.util.spec_from_file_location("headroom_runtime_smoke", RUNTIME_SMOKE_SCRIPT)
assert RUNTIME_SMOKE_SPEC and RUNTIME_SMOKE_SPEC.loader
RUNTIME_SMOKE_MODULE = importlib.util.module_from_spec(RUNTIME_SMOKE_SPEC)
RUNTIME_SMOKE_SPEC.loader.exec_module(RUNTIME_SMOKE_MODULE)


def test_release_candidate_default_runtime_is_pinned() -> None:
    assert MODULE.HEADROOM_RUNTIME_VERSION == "0.32.1"
    assert MODULE.DEFAULT_HEADROOM_SPEC == "headroom-ai[proxy]==0.32.1"
    assert MODULE.LITELLM_RUNTIME_VERSION == "1.91.3"
    assert MODULE.DEFAULT_LITELLM_SPEC == "litellm==1.91.3"
    assert RUNTIME_SMOKE_MODULE.DEFAULT_LITELLM_SPEC == "litellm==1.91.3"


def test_package_proxy_extra_matches_certified_runtime() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["version"] == "0.6.0rc1"
    assert project["optional-dependencies"]["proxy"] == [
        MODULE.DEFAULT_HEADROOM_SPEC,
        MODULE.DEFAULT_LITELLM_SPEC,
    ]

    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    portable_core = (REPO / "docs" / "portable-core.md").read_text(encoding="utf-8")
    packaged_docs = config["tool"]["setuptools"]["data-files"]["share/doc/hermes-headroom-plugin"]
    assert "docs/portable-core.md" in packaged_docs
    assert f"hermes-headroom-plugin=={project['version']}" in portable_core


def test_production_candidate_documents_real_config_keys_without_test_aliases() -> None:
    text = (REPO / "docs" / "production-candidate.md").read_text(encoding="utf-8")
    for key in (
        "materiality_chars:",
        "hot_tool_results:",
        "warm_tool_results:",
        "aggregate_budget_chars:",
        "compatibility_test_mode:",
        "disclosure_owner:",
    ):
        assert key in text
    assert "cold_after_turns:" not in text
    assert "min_reducible_tokens:" not in text
    assert "max_reductions_per_pass:" not in text
    assert "\n    compatibility_test:" not in text
    assert "\n    owner:" not in text


def test_archive_inspection_rejects_stale_packaged_portable_core() -> None:
    member = "package.data/data/share/doc/hermes-headroom-plugin/portable-core.md"
    expected_row = f"| Plugin | `{MODULE.EXPECTED_PLUGIN_SPEC}` |"
    invalid_documents = {
        "mixed": f"{expected_row}\n| Plugin | `hermes-headroom-plugin==0.5.1` |",
        "local-suffix": "| Plugin | `hermes-headroom-plugin==0.6.0rc1+0.5.2` |",
    }
    with tempfile.TemporaryDirectory() as temp_dir:
        for name, document in invalid_documents.items():
            stale_wheel = Path(temp_dir) / f"{name}.whl"
            with zipfile.ZipFile(stale_wheel, "w") as archive:
                archive.writestr(member, document)
            stale_issues = MODULE.portable_core_version_issues(stale_wheel)
            assert [issue["kind"] for issue in stale_issues] == ["portable_core_plugin_version_mismatch"]

        corrected_wheel = Path(temp_dir) / "corrected.whl"
        with zipfile.ZipFile(corrected_wheel, "w") as archive:
            archive.writestr(member, expected_row)
        assert MODULE.portable_core_version_issues(corrected_wheel) == []


def test_workflows_keep_certified_pin_separate_from_latest_litellm_canary() -> None:
    runtime_smoke = (REPO / ".github" / "workflows" / "runtime-smoke.yml").read_text(encoding="utf-8")
    release_candidate = (REPO / ".github" / "workflows" / "release-candidate.yml").read_text(encoding="utf-8")
    future_monitor = (REPO / ".github" / "workflows" / "future-runtime-monitor.yml").read_text(encoding="utf-8")
    runtime_script = RUNTIME_SMOKE_SCRIPT.read_text(encoding="utf-8")

    for workflow in (runtime_smoke, release_candidate):
        assert 'default: "headroom-ai[proxy]==0.32.1"' in workflow
        assert 'default: "litellm==1.91.3"' in workflow
        assert "HEADROOM_AI_SPEC:" in workflow
        assert "HEADROOM_LITELLM_SPEC:" in workflow

    assert "latest-litellm-monitor:" in future_monitor
    assert 'default: "litellm>=1.86.2,<2.0"' in future_monitor
    assert 'HEADROOM_SPEC: "headroom-ai[proxy]==0.32.1"' in future_monitor
    assert 'python-version: "3.12"' in future_monitor
    assert "continue-on-error: true" in future_monitor
    assert '--litellm-spec "$LITELLM_SPEC"' in future_monitor
    assert "The certified LiteLLM pin is unchanged" in future_monitor
    assert "INFO: resolved runtime versions" in runtime_script
    assert "test-runtime-manager-lifecycle.py" in runtime_smoke
    assert "--manager-command headroom-runtime" in runtime_smoke
    assert "test-headroom-runtime-smoke.py" not in runtime_smoke
    assert "test-headroom-runtime-smoke.py" in future_monitor


def test_release_gate_certifies_wheel_runtime_manager_lifecycle() -> None:
    gate_script = SCRIPT.read_text(encoding="utf-8")
    lifecycle_script = (REPO / "scripts" / "test-runtime-manager-lifecycle.py").read_text(encoding="utf-8")

    assert 'exe("headroom-runtime")' in gate_script
    assert '"wheel_runtime_manager_lifecycle"' in gate_script
    assert '"durable_lifecycle_deferred_to_release_gate"' in gate_script
    assert '"--run-durable-lifecycle"' in gate_script
    assert '"mutations": manifest_data.get("mutations")' in lifecycle_script
    assert 'manifest_data.get("mutations") == []' in lifecycle_script
    assert "shell_unchanged" in lifecycle_script
    assert "artifacts_removed" in lifecycle_script
    assert "supervisor_removed" in lifecycle_script
    assert '"uninstall"' in lifecycle_script


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


def test_isolated_runtime_env_keeps_ccr_state_inside_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    env = MODULE.isolated_runtime_env(run_dir)

    runtime_home = run_dir / "temp-headroom-home"
    workspace = runtime_home / ".headroom"
    assert env["HOME"] == str(runtime_home)
    assert env["USERPROFILE"] == str(runtime_home)
    assert env["HEADROOM_WORKSPACE_DIR"] == str(workspace)
    assert env["HEADROOM_CONFIG_DIR"] == str(workspace / "config")
    assert env["HEADROOM_CCR_BACKEND"] == "memory"
    assert env["HEADROOM_CCR_TTL_SECONDS"] == "1800"
    assert workspace.is_dir()
    assert "temp-headroom-home" in MODULE.EPHEMERAL_ENV_DIRS


def test_standalone_runtime_smoke_isolates_headroom_state(tmp_path: Path) -> None:
    env = RUNTIME_SMOKE_MODULE.isolated_runtime_env(tmp_path)

    runtime_home = tmp_path / "home"
    workspace = runtime_home / ".headroom"
    assert env["HOME"] == str(runtime_home)
    assert env["USERPROFILE"] == str(runtime_home)
    assert env["HEADROOM_WORKSPACE_DIR"] == str(workspace)
    assert env["HEADROOM_CONFIG_DIR"] == str(workspace / "config")
    assert env["HEADROOM_CCR_BACKEND"] == "memory"
    assert env["HEADROOM_CCR_TTL_SECONDS"] == "1800"
    assert workspace.is_dir()


def test_leftover_proxy_check_allows_baseline_and_rejects_new_process(monkeypatch) -> None:
    baseline = [{"pid": "100", "argv": ["headroom", "proxy"]}]
    monkeypatch.setattr(MODULE, "headroom_proxy_processes", lambda: list(baseline))
    assert MODULE.no_new_leftover_proxy(baseline)["pass"] is True

    monkeypatch.setattr(
        MODULE,
        "headroom_proxy_processes",
        lambda: [*baseline, {"pid": "101", "argv": ["headroom", "proxy"]}],
    )
    result = MODULE.no_new_leftover_proxy(baseline)
    assert result["pass"] is False
    assert [item["pid"] for item in result["new_headroom_proxy_processes"]] == ["101"]


def test_proxy_scanner_contract_accepts_python_wrapped_console_script() -> None:
    argv = ["/venv/bin/python", "/venv/bin/headroom", "proxy", "--port", "28787"]
    is_headroom_cli = any(Path(item).name.lower() in {"headroom", "headroom.exe"} for item in argv)
    assert is_headroom_cli is True
    assert "proxy" in argv
