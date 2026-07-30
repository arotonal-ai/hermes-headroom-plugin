from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


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


def _create_directory_symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        if sys.platform.startswith("win") and getattr(exc, "winerror", None) == 1314:
            raise unittest.SkipTest(
                "Windows directory symlink requires Developer Mode or elevation"
            ) from exc
        raise


class SymlinkPrivilegeHandlingTest(unittest.TestCase):
    def test_windows_privilege_error_skips(self):
        error = OSError("privilege not held")
        setattr(error, "winerror", 1314)
        with patch.object(sys, "platform", "win32"), patch.object(
            Path, "symlink_to", side_effect=error
        ), self.assertRaises(unittest.SkipTest):
            _create_directory_symlink_or_skip(Path("link"), Path("target"))

    def test_other_symlink_errors_remain_failures(self):
        error = OSError("access denied")
        setattr(error, "winerror", 5)
        with patch.object(sys, "platform", "win32"), patch.object(
            Path, "symlink_to", side_effect=error
        ), self.assertRaises(OSError):
            _create_directory_symlink_or_skip(Path("link"), Path("target"))


def test_release_candidate_default_runtime_is_pinned() -> None:
    assert MODULE.HEADROOM_RUNTIME_VERSION == "0.33.0"
    assert MODULE.DEFAULT_HEADROOM_SPEC == "headroom-ai[proxy]==0.33.0"
    assert MODULE.LITELLM_RUNTIME_VERSION == "1.94.0rc3"
    assert MODULE.DEFAULT_LITELLM_SPEC == "litellm==1.94.0rc3"
    assert RUNTIME_SMOKE_MODULE.DEFAULT_LITELLM_SPEC == "litellm==1.94.0rc3"


def test_package_proxy_extra_matches_certified_runtime() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["version"] == "0.6.4"
    assert project["requires-python"] == ">=3.11,<3.15"
    assert project["optional-dependencies"]["proxy"] == [
        MODULE.DEFAULT_HEADROOM_SPEC,
        MODULE.DEFAULT_LITELLM_SPEC,
    ]

    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    portable_core = (REPO / "docs" / "portable-core.md").read_text(encoding="utf-8")
    packaged_docs = config["tool"]["setuptools"]["data-files"]["share/doc/hermes-headroom-plugin"]
    assert "docs/portable-core.md" in packaged_docs
    assert f"hermes-headroom-plugin=={project['version']}" in portable_core


def test_headroom_033_wrapped_api_evidence_matches_release_docs() -> None:
    evidence = json.loads(
        (REPO / "docs" / "evidence" / "headroom-033-api-compatibility.json").read_text(encoding="utf-8")
    )
    assert evidence["candidate"] == {
        "package": "headroom-ai",
        "version": "0.33.0",
        "sdist_sha256": "97d817e5923903d72bed24f75e0424e9cb7f86b3ddde0fc1acec4f3f85deeb5a",
    }
    assert evidence["baseline"]["version"] == "0.32.1"
    assert evidence["decision"] == "STATIC_COMPATIBILITY_PASS_REQUIRES_RUNTIME_LIFECYCLE_GATE"
    unchanged = set(evidence["wrapped_api_review"]["ast_and_signature_identical"])
    assert unchanged == {
        "headroom.cli.install._build_deployment_manifest",
        "headroom.cli.install._remove_deployment",
        "headroom.cli.install._start_deployment",
        "headroom.install.state.save_manifest",
        "headroom.install.supervisors.install_supervisor",
    }
    reviewed = evidence["wrapped_api_review"]["compatible_reviewed_change"]
    assert reviewed["symbol"] == "headroom.install.state.load_manifest"
    assert reviewed["signature_unchanged"] is True
    runtime_doc = (REPO / "docs" / "runtime-manager.md").read_text(encoding="utf-8")
    release_doc = (REPO / "docs" / "releases" / "v0.6.4.md").read_text(encoding="utf-8")
    for document in (runtime_doc, release_doc):
        assert evidence["candidate"]["sdist_sha256"] in document
        assert "load_manifest" in document


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
        "local-suffix": "| Plugin | `hermes-headroom-plugin==0.6.0+0.5.2` |",
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
        assert 'default: "headroom-ai[proxy]==0.33.0"' in workflow
        assert 'default: "litellm==1.94.0rc3"' in workflow
        assert "HEADROOM_AI_SPEC:" in workflow
        assert "HEADROOM_LITELLM_SPEC:" in workflow

    assert "latest-litellm-monitor:" in future_monitor
    assert 'default: "litellm>=1.86.2,<2.0"' in future_monitor
    assert 'HEADROOM_SPEC: "headroom-ai[proxy]==0.33.0"' in future_monitor
    assert 'python-version: "3.12"' in future_monitor
    assert "continue-on-error: true" in future_monitor
    assert '--litellm-spec "$LITELLM_SPEC"' in future_monitor
    assert "The certified LiteLLM pin is unchanged" in future_monitor
    assert "INFO: resolved runtime versions" in runtime_script
    assert "test-runtime-manager-lifecycle.py" in runtime_smoke
    assert "--manager-command headroom-runtime" in runtime_smoke
    assert "test-headroom-runtime-smoke.py" not in runtime_smoke
    assert "test-headroom-runtime-smoke.py" in future_monitor


def test_release_candidate_workflow_fetches_tag_history_for_rollback() -> None:
    release_candidate = (REPO / ".github" / "workflows" / "release-candidate.yml").read_text(encoding="utf-8")

    assert "actions/checkout@v7" in release_candidate
    assert "fetch-depth: 0" in release_candidate


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


def test_release_gate_does_not_false_green_missing_hermes_cli() -> None:
    gate_script = SCRIPT.read_text(encoding="utf-8")
    release_workflow = (REPO / ".github" / "workflows" / "release-candidate.yml").read_text(encoding="utf-8")
    release_doc = (REPO / "docs" / "release-candidate.md").read_text(encoding="utf-8")

    assert '"--allow-hermes-install-deferred"' in gate_script
    assert "--allow-hermes-install-deferred" in release_workflow
    assert "target-host Hermes install deferred" in release_workflow
    assert "`verified: true`" in release_doc
    assert "`deferred: false`" in release_doc

    with patch.object(MODULE.shutil, "which", return_value=None):
        command, gate = MODULE.clean_temp_hermes_install_gate(allow_deferred=False)
        assert command["returncode"] == 127
        assert gate == {"pass": False, "verified": False, "deferred": True}

        deferred_command, deferred_gate = MODULE.clean_temp_hermes_install_gate(
            allow_deferred=True
        )
        assert deferred_command["returncode"] == 0
        assert deferred_gate == {"pass": True, "verified": False, "deferred": True}


def test_release_gate_verifies_clean_install_when_hermes_cli_is_present() -> None:
    result = {"returncode": 0, "stdout": "PASS", "duration_s": 1}
    with patch.object(MODULE.shutil, "which", return_value="/usr/bin/hermes"), patch.object(
        MODULE, "run", return_value=result
    ) as run_mock:
        command, gate = MODULE.clean_temp_hermes_install_gate(allow_deferred=False)

    assert command == result
    assert gate == {"pass": True, "verified": True, "deferred": False}
    run_mock.assert_called_once_with(
        ["bash", "scripts/test-clean-hermes-install.sh", "--local"], timeout=300
    )


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
    _create_directory_symlink_or_skip(run_dir / MODULE.EPHEMERAL_ENV_DIRS[0], outside)

    report = MODULE.cleanup_ephemeral_envs(run_dir)

    assert report["pass"] is False
    assert report["entries"][0]["status"] == "blocked"
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_release_gate_lock_blocks_concurrent_run_root(tmp_path: Path) -> None:
    lock_dir = MODULE.acquire_gate_lock(tmp_path, register_atexit=False)
    try:
        assert lock_dir.is_dir()
        assert (lock_dir / "owner.json").is_file()
        try:
            MODULE.acquire_gate_lock(tmp_path, register_atexit=False)
        except FileExistsError:
            pass
        else:
            raise AssertionError("concurrent gate lock was not blocked")
    finally:
        MODULE.release_gate_lock(lock_dir)
    assert not lock_dir.exists()


def test_checkout_snapshot_reports_exact_checkout_identity() -> None:
    snapshot = MODULE.checkout_snapshot()
    assert snapshot["commands_ok"] is True
    assert snapshot["head"]
    assert snapshot["tree"]
    assert isinstance(snapshot["status_short"], str)


def test_release_gate_requires_checkout_stability() -> None:
    gate_script = SCRIPT.read_text(encoding="utf-8")
    assert 'gates["checkout_stability"]' in gate_script
    assert '"RC_GATE_CONCURRENT_RUN_BLOCKED"' in gate_script
    assert 'initial_checkout.get("head") == final_checkout.get("head")' in gate_script
    assert 'initial_checkout.get("tree") == final_checkout.get("tree")' in gate_script


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
