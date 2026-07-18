import contextlib
import io
import inspect
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_headroom_plugin import runtime_manager as manager


class RuntimeManagerTest(unittest.TestCase):
    def _state(self, root: Path, *, port: int = 57881) -> manager.RuntimeState:
        return manager._state_for(
            root,
            status="RUNTIME_FULL_DURABLE",
            headroom_spec=manager.DEFAULT_HEADROOM_SPEC,
            litellm_spec=manager.DEFAULT_LITELLM_SPEC,
            profile="hermes-test",
            port=port,
            preset="persistent-service",
            versions={"headroom": manager.RUNTIME_VERSION, "litellm": manager.LITELLM_VERSION},
        )

    def _fake_cli(self, state: manager.RuntimeState) -> Path:
        cli = manager._exe(Path(state.venv_dir), "headroom")
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.write_text("fake", encoding="utf-8")
        return cli

    def _write_manifest(
        self,
        root: Path,
        *,
        profile: str = "hermes-test",
        port: int = 57881,
        preset: str = "persistent-service",
        targets=None,
        mutations=None,
        service_name: str | None = None,
    ) -> Path:
        path = manager._workspace_dir(root) / "deploy" / profile / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        supervisor_kind = "service" if preset == "persistent-service" else "task"
        service_name = service_name or f"headroom-{profile}"
        path.write_text(
            json.dumps(
                {
                    "profile": profile,
                    "preset": preset,
                    "runtime_kind": "python",
                    "supervisor_kind": supervisor_kind,
                    "scope": "user",
                    "provider_mode": "manual",
                    "targets": [] if targets is None else targets,
                    "port": port,
                    "host": manager.DEFAULT_HOST,
                    "backend": "anthropic",
                    "anyllm_provider": None,
                    "region": None,
                    "proxy_mode": "token",
                    "memory_enabled": False,
                    "telemetry_enabled": False,
                    "service_name": service_name,
                    "container_name": f"headroom-{profile}",
                    "health_url": f"http://{manager.DEFAULT_HOST}:{port}/readyz",
                    "base_env": {
                        "HEADROOM_PORT": str(port),
                        "HEADROOM_HOST": manager.DEFAULT_HOST,
                        "HEADROOM_MODE": "token",
                        "HEADROOM_BACKEND": "anthropic",
                        "HEADROOM_TELEMETRY": "off",
                        "HEADROOM_WORKSPACE_DIR": str(manager._workspace_dir(root)),
                        "HEADROOM_CCR_BACKEND": manager.DEFAULT_CCR_BACKEND,
                        "HEADROOM_CCR_TTL_SECONDS": str(manager.DEFAULT_CCR_TTL_SECONDS),
                        "HEADROOM_DISABLE_UPDATE_CHECK": "1",
                    },
                    "tool_envs": {},
                    "proxy_args": [
                        "--host", manager.DEFAULT_HOST,
                        "--port", str(port),
                        "--mode", "token",
                        "--backend", "anthropic",
                        "--no-telemetry", "--no-code-aware",
                    ],
                    "mutations": [] if mutations is None else mutations,
                    "artifacts": [],
                }
            ),
            encoding="utf-8",
        )
        return path

    def _run_main(self, argv):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = manager.main(argv)
        return code, stream.getvalue()

    def test_setup_dry_run_is_no_write_and_disables_mutating_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "absent-runtime-root"
            code, output = self._run_main(
                [
                    "setup",
                    "--runtime-root",
                    str(root),
                    "--port",
                    "57882",
                    "--dry-run",
                    "--json",
                ]
            )
            payload = json.loads(output)
        self.assertEqual(code, 0)
        self.assertFalse(root.exists())
        self.assertEqual(payload["decision"], "DRY_RUN")
        self.assertEqual(payload["provider_mode"], "manual")
        self.assertEqual(payload["provider_targets"], [])
        self.assertIs(payload["provider_mutations"], False)
        self.assertNotIn("command", payload)
        self.assertIn("install_supervisor", payload["upstream_api"])
        self.assertEqual(payload["headroom_spec"], "headroom-ai[proxy]==0.32.0")
        self.assertEqual(payload["litellm_spec"], "litellm==1.91.3")

    def test_default_preset_is_user_service_except_windows_task(self):
        with patch.object(manager.sys, "platform", "win32"):
            self.assertEqual(manager._default_preset(), "persistent-task")
        with patch.object(manager.sys, "platform", "darwin"):
            self.assertEqual(manager._default_preset(), "persistent-service")
        with patch.object(manager.sys, "platform", "linux"):
            self.assertEqual(manager._default_preset(), "persistent-service")

    def test_safe_apply_contract_omits_provider_and_shell_mutation_activation(self):
        script = manager._SAFE_APPLY_SCRIPT
        self.assertIn('provider_mode="manual"', script)
        self.assertIn("targets=()", script)
        self.assertIn("manifest.mutations = []", script)
        self.assertIn("saved.mutations", script)
        self.assertNotIn("apply_mutations(", script)
        self.assertNotIn("_activate_deployment_mutations", script)
        self.assertNotIn("_restore_deployment", script)
        self.assertIn("if existing is not None:", script)
        self.assertIn("run explicit uninstall before setup", script)

    def test_safe_apply_payload_has_only_runtime_environment(self):
        payload = manager._safe_apply_payload(
            root=Path("/tmp/headroom-safe"),
            profile="hermes-test",
            port=57880,
            preset="persistent-service",
        )
        self.assertEqual(payload["profile"], "hermes-test")
        self.assertEqual(payload["port"], 57880)
        self.assertEqual(payload["preset"], "persistent-service")
        self.assertEqual(
            set(payload["extra_env"]),
            {
                "HEADROOM_WORKSPACE_DIR",
                "HEADROOM_CCR_BACKEND",
                "HEADROOM_CCR_TTL_SECONDS",
                "HEADROOM_DISABLE_UPDATE_CHECK",
            },
        )

    def test_setup_blocks_ready_unmanaged_port_before_writes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            with (
                patch.object(manager, "readyz", return_value={"ok": True, "status": 200}),
                patch.object(manager, "_ensure_runtime") as ensure_runtime,
            ):
                code, output = self._run_main(
                    ["setup", "--runtime-root", str(root), "--port", "57883", "--json"]
                )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["decision"], "PORT_CONFLICT_UNMANAGED")
        self.assertFalse(root.exists())
        ensure_runtime.assert_not_called()

    def test_setup_blocks_non_http_tcp_listener_before_writes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            with (
                patch.object(manager, "readyz", return_value={"ok": False, "status": None}),
                patch.object(manager, "_tcp_port_open", return_value=True),
                patch.object(manager, "_ensure_runtime") as ensure_runtime,
            ):
                code, output = self._run_main(
                    ["setup", "--runtime-root", str(root), "--port", "57887", "--json"]
                )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["decision"], "PORT_CONFLICT_UNKNOWN_SERVICE")
        self.assertFalse(root.exists())
        ensure_runtime.assert_not_called()

    def test_setup_writes_full_state_only_after_safe_apply_ready_and_smoke(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            cli = manager._exe(manager._venv_dir(root), "headroom")
            cli.parent.mkdir(parents=True, exist_ok=True)
            cli.write_text("fake", encoding="utf-8")
            manager._ensure_marker(root)
            applied = subprocess.CompletedProcess(
                ["safe-apply"],
                0,
                json.dumps({"provider_mode": "manual", "targets": [], "mutations": []}) + "\n",
            )
            def apply_and_write_manifest(**_kwargs):
                self._write_manifest(
                    root,
                    profile=manager.DEFAULT_PROFILE,
                    port=57884,
                    preset=manager._default_preset(),
                )
                return applied

            with (
                patch.object(manager, "readyz", return_value={"ok": False, "status": None}),
                patch.object(
                    manager,
                    "_ensure_runtime",
                    return_value=(cli, {"headroom": "0.32.0", "litellm": "1.91.3"}),
                ),
                patch.object(manager, "_safe_apply", side_effect=apply_and_write_manifest) as safe_apply,
                patch.object(manager, "_wait_ready", return_value={"ok": True, "status": 200}),
                patch.object(manager, "smoke", return_value={"ok": True, "sentinel_found": True}),
            ):
                code, output = self._run_main(
                    ["setup", "--runtime-root", str(root), "--port", "57884", "--json"]
                )
            state = manager._load_state(root)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["decision"], "RUNTIME_FULL_DURABLE")
        self.assertIsNotNone(state)
        self.assertEqual(state.status if state else None, "RUNTIME_FULL_DURABLE")
        safe_apply.assert_called_once()

    def test_setup_rejects_safe_apply_result_with_mutations(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            cli = manager._exe(manager._venv_dir(root), "headroom")
            cli.parent.mkdir(parents=True, exist_ok=True)
            cli.write_text("fake", encoding="utf-8")
            manager._ensure_marker(root)
            unsafe = subprocess.CompletedProcess(
                ["safe-apply"],
                0,
                json.dumps(
                    {
                        "provider_mode": "manual",
                        "targets": [],
                        "mutations": [{"target": "env"}],
                    }
                ) + "\n",
            )
            with (
                patch.object(manager, "readyz", return_value={"ok": False, "status": None}),
                patch.object(
                    manager,
                    "_ensure_runtime",
                    return_value=(cli, {"headroom": "0.32.0", "litellm": "1.91.3"}),
                ),
                patch.object(manager, "_safe_apply", return_value=unsafe),
            ):
                code, output = self._run_main(
                    ["setup", "--runtime-root", str(root), "--port", "57886", "--json"]
                )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["decision"], "RUNTIME_PARTIAL")
        self.assertIn("invariants failed", json.loads(output)["detail"])

    def test_safe_apply_failure_keeps_partial_state_for_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            cli = manager._exe(manager._venv_dir(root), "headroom")
            cli.parent.mkdir(parents=True, exist_ok=True)
            cli.write_text("fake", encoding="utf-8")
            manager._ensure_marker(root)
            completed = subprocess.CompletedProcess(["safe-apply"], 1, "apply failed\n")
            with (
                patch.object(manager, "readyz", return_value={"ok": False, "status": None}),
                patch.object(
                    manager,
                    "_ensure_runtime",
                    return_value=(cli, {"headroom": "0.32.0", "litellm": "1.91.3"}),
                ),
                patch.object(manager, "_safe_apply", return_value=completed),
            ):
                code, output = self._run_main(
                    ["setup", "--runtime-root", str(root), "--port", "57885", "--json"]
                )
            state = manager._load_state(root)
            marker_exists = (root / manager.MARKER_FILE).exists()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["decision"], "RUNTIME_PARTIAL")
        self.assertIsNotNone(state)
        self.assertEqual(state.status if state else None, "RUNTIME_PARTIAL")
        self.assertTrue(marker_exists)

    def test_status_requires_ready_proxy_and_upstream_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            manager._write_state(root, state)
            self._write_manifest(root)
            completed = subprocess.CompletedProcess(["headroom"], 0, "Healthy: yes\n")
            with (
                patch.object(manager, "readyz", return_value={"ok": True, "status": 200}),
                patch.object(manager, "_run", return_value=completed),
            ):
                code_without_cli, _ = self._run_main(["status", "--runtime-root", str(root), "--json"])
                self._fake_cli(state)
                code_full, output = self._run_main(["status", "--runtime-root", str(root), "--json"])
        self.assertEqual(code_without_cli, 1)
        self.assertEqual(code_full, 0)
        self.assertEqual(json.loads(output)["decision"], "RUNTIME_FULL_DURABLE")

    def test_doctor_requires_upstream_status_and_compress_retrieve(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            manager._write_state(root, state)
            self._write_manifest(root)
            self._fake_cli(state)
            completed = subprocess.CompletedProcess(["headroom"], 0, "Healthy: yes\n")
            with (
                patch.object(manager, "readyz", return_value={"ok": True, "status": 200}),
                patch.object(manager, "smoke", return_value={"ok": True, "sentinel_found": True}),
                patch.object(manager, "_run", return_value=completed),
            ):
                code, output = self._run_main(["doctor", "--runtime-root", str(root), "--json"])
        payload = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(payload["decision"], "RUNTIME_FULL_DURABLE")
        self.assertTrue(payload["smoke"]["sentinel_found"])

    def test_status_rejects_manifest_with_provider_mutations(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            manager._write_state(root, state)
            self._write_manifest(root, mutations=[{"target": "env"}])
            self._fake_cli(state)
            completed = subprocess.CompletedProcess(["headroom"], 0, "Healthy: yes\n")
            with (
                patch.object(manager, "readyz", return_value={"ok": True, "status": 200}),
                patch.object(manager, "_run", return_value=completed),
            ):
                code, output = self._run_main(["status", "--runtime-root", str(root), "--json"])
        payload = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(payload["decision"], "RUNTIME_PARTIAL")
        self.assertIs(payload["manifest_contract"]["ok"], False)

    def test_status_and_uninstall_reject_manifest_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            manager._write_state(root, state)
            self._write_manifest(root, port=state.port + 1, service_name="headroom-foreign")
            self._fake_cli(state)
            completed = subprocess.CompletedProcess(["headroom"], 0, "Healthy: yes\n")
            with (
                patch.object(manager, "readyz", return_value={"ok": True, "status": 200}),
                patch.object(manager, "_run", return_value=completed),
            ):
                status_code, status_output = self._run_main(
                    ["status", "--runtime-root", str(root), "--json"]
                )
            with patch.object(manager, "_run") as remove:
                uninstall_code, uninstall_output = self._run_main(
                    ["uninstall", "--runtime-root", str(root), "--json"]
                )
        status_payload = json.loads(status_output)
        self.assertEqual(status_code, 1)
        self.assertEqual(status_payload["decision"], "RUNTIME_PARTIAL")
        self.assertIn("port", status_payload["manifest_contract"]["mismatches"])
        self.assertIn("service_name", status_payload["manifest_contract"]["mismatches"])
        self.assertEqual(uninstall_code, 2)
        self.assertEqual(json.loads(uninstall_output)["decision"], "UNINSTALL_BLOCKED")
        remove.assert_not_called()

    def test_corrupt_manager_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            (root / manager.STATE_FILE).write_text("{not-json", encoding="utf-8")
            code, output = self._run_main(["uninstall", "--runtime-root", str(root), "--json"])
        payload = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(payload["decision"], "ERROR")
        self.assertIn("manager state is invalid", payload["detail"])

    def test_state_identity_cannot_point_cli_outside_runtime_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            state.venv_dir = str(Path(td) / "foreign-venv")
            manager._write_state(root, state)
            code, output = self._run_main(["uninstall", "--runtime-root", str(root), "--json"])
        payload = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(payload["decision"], "ERROR")
        self.assertIn("state identity mismatch", payload["detail"])

    def test_setup_rejects_url_specs_without_reflecting_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            secret_spec = "https://user:do-not-log@example.invalid/headroom.whl"
            code, output = self._run_main(
                ["setup", "--runtime-root", str(root), "--headroom-spec", secret_spec, "--json"]
            )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["decision"], "ERROR")
        self.assertNotIn("do-not-log", output)
        self.assertFalse(root.exists())

    def test_package_spec_operator_is_strict(self):
        with self.assertRaises(ValueError):
            manager._validate_package_spec("headroom-ai[proxy]=>0.32.0", package="headroom-ai")
        with self.assertRaises(ValueError):
            manager._validate_package_spec("litellm!!1.91.3", package="litellm")

    def test_lifecycle_script_rejects_secret_spec_before_writes_or_logs(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            secret = "https://user:canary-secret@example.invalid/headroom.whl"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(repo / "scripts" / "test-runtime-manager-lifecycle.py"),
                    "--runtime-root",
                    str(root),
                    "--headroom-spec",
                    secret,
                ],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=30,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("canary-secret", proc.stdout)
        self.assertFalse(root.exists())

    def test_runtime_install_uses_isolated_official_pypi(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            venv = manager._venv_dir(root)
            python_exe = manager._exe(venv, "python")
            pip = manager._exe(venv, "pip")
            python_exe.parent.mkdir(parents=True, exist_ok=True)
            python_exe.write_text("fake", encoding="utf-8")
            pip.write_text("fake", encoding="utf-8")
            completed = [
                subprocess.CompletedProcess([str(pip)], 0, "installed\n"),
                subprocess.CompletedProcess([str(python_exe)], 0, "0.32.0\n1.91.3\n"),
            ]
            with patch.object(manager, "_run", side_effect=completed) as run:
                manager._ensure_runtime(
                    root,
                    python=sys.executable,
                    headroom_spec=manager.DEFAULT_HEADROOM_SPEC,
                    litellm_spec=manager.DEFAULT_LITELLM_SPEC,
                    timeout=30,
                )
        install_command = run.call_args_list[0].args[0]
        self.assertEqual(install_command[:3], [str(pip), "--isolated", "install"])
        self.assertIn(manager.PYPI_INDEX_URL, install_command)
        self.assertIn("--no-input", install_command)

    def test_setup_blocks_foreign_manifest_before_install(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            self._write_manifest(root, profile=manager.DEFAULT_PROFILE)
            with (
                patch.object(manager, "_supervisor_presence", return_value={"present": False, "evidence": []}),
                patch.object(manager, "_ensure_runtime") as ensure_runtime,
            ):
                code, output = self._run_main(["setup", "--runtime-root", str(root), "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["decision"], "FOREIGN_DEPLOYMENT_CONFLICT")
        ensure_runtime.assert_not_called()

    def test_setup_blocks_foreign_global_supervisor_before_writes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            supervisor = {"present": True, "service_name": "headroom-hermes-plugin", "evidence": ["unit"]}
            with (
                patch.object(manager, "_supervisor_presence", return_value=supervisor),
                patch.object(manager, "_ensure_runtime") as ensure_runtime,
            ):
                code, output = self._run_main(["setup", "--runtime-root", str(root), "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["decision"], "FOREIGN_DEPLOYMENT_CONFLICT")
        self.assertFalse(root.exists())
        ensure_runtime.assert_not_called()

    def test_mutating_commands_lock_before_state_read(self):
        setup_source = inspect.getsource(manager.setup)
        uninstall_source = inspect.getsource(manager.uninstall)
        self.assertLess(setup_source.index("_acquire_lock"), setup_source.index("_load_state"))
        self.assertLess(uninstall_source.index("_acquire_lock"), uninstall_source.index("_load_state"))

    def test_state_and_manifest_accept_equivalent_symlink_paths(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            real_parent = base / "real"
            real_parent.mkdir()
            alias_parent = base / "alias"
            try:
                alias_parent.symlink_to(real_parent, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")
            real_root = real_parent / "runtime"
            alias_root = alias_parent / "runtime"
            manager._ensure_marker(real_root)
            state = self._state(real_root)
            manager._write_state(real_root, state)
            self._write_manifest(real_root)
            loaded = manager._load_state(alias_root)
            contract = manager._manifest_contract(
                alias_root,
                profile=state.profile,
                port=state.port,
                preset=state.preset,
            )
        self.assertIsNotNone(loaded)
        self.assertTrue(contract["ok"], contract)

    def test_rejects_shared_temp_root(self):
        code, output = self._run_main(
            ["setup", "--runtime-root", tempfile.gettempdir(), "--dry-run", "--json"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["decision"], "ERROR")

    def test_setup_blocks_nonempty_unmanaged_runtime_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            root.mkdir()
            (root / "unrelated.txt").write_text("keep", encoding="utf-8")
            with patch.object(manager, "_ensure_runtime") as ensure_runtime:
                code, output = self._run_main(
                    ["setup", "--runtime-root", str(root), "--port", "57881", "--json"]
                )
            marker_exists = (root / manager.MARKER_FILE).exists()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["decision"], "RUNTIME_ROOT_CONFLICT")
        self.assertFalse(marker_exists)
        ensure_runtime.assert_not_called()

    def test_uninstall_purges_inert_partial_state_without_upstream_remove(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            state.status = "RUNTIME_PARTIAL"
            manager._write_state(root, state)
            with (
                patch.object(manager, "readyz", return_value={"ok": False, "status": None}),
                patch.object(manager, "_supervisor_presence", return_value={"present": False, "evidence": []}),
                patch.object(manager, "_run") as run,
            ):
                code, output = self._run_main(["uninstall", "--runtime-root", str(root), "--json"])
            root_exists = root.exists()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["decision"], "UNINSTALLED_PARTIAL_STATE")
        self.assertFalse(root_exists)
        run.assert_not_called()

    def test_uninstall_blocks_root_with_unexpected_entry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            manager._write_state(root, state)
            self._write_manifest(root)
            cli = self._fake_cli(state)
            (root / "unrelated.txt").write_text("keep", encoding="utf-8")
            completed = subprocess.CompletedProcess([str(cli)], 0, "Removed deployment\n")
            with (
                patch.object(manager, "_run", return_value=completed),
                patch.object(manager, "_wait_stopped", return_value={"ok": False, "status": None}),
                patch.object(manager, "_wait_supervisor_absent", return_value={"present": False, "evidence": []}),
            ):
                code, output = self._run_main(["uninstall", "--runtime-root", str(root), "--json"])
            unrelated_exists = (root / "unrelated.txt").exists()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["decision"], "UNINSTALL_PARTIAL")
        self.assertTrue(unrelated_exists)

    def test_uninstall_preserves_root_if_supervisor_remains_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            manager._write_state(root, state)
            self._write_manifest(root)
            cli = self._fake_cli(state)
            completed = subprocess.CompletedProcess([str(cli)], 0, "Removed deployment\n")
            with (
                patch.object(manager, "_run", return_value=completed),
                patch.object(manager, "_wait_stopped", return_value={"ok": False, "status": None}),
                patch.object(manager, "_wait_supervisor_absent", return_value={"present": True, "evidence": ["native"]}),
            ):
                code, output = self._run_main(["uninstall", "--runtime-root", str(root), "--json"])
            root_exists = root.exists()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["decision"], "UNINSTALL_PARTIAL")
        self.assertTrue(root_exists)

    def test_uninstall_blocks_mutating_manifest_without_upstream_remove(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            manager._write_state(root, state)
            self._write_manifest(root, mutations=[{"target": "shell"}])
            self._fake_cli(state)
            with patch.object(manager, "_run") as run:
                code, output = self._run_main(["uninstall", "--runtime-root", str(root), "--json"])
            root_exists = root.exists()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["decision"], "UNINSTALL_BLOCKED")
        self.assertTrue(root_exists)
        run.assert_not_called()

    def test_runtime_env_drops_uncontrolled_headroom_variables(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(
            "os.environ",
            {"HEADROOM_PORT": "9999", "HEADROOM_CONFIG_DIR": "/unsafe", "KEEP_ME": "yes"},
        ):
            env = manager._runtime_env(Path(td) / "runtime")
        self.assertNotIn("HEADROOM_PORT", env)
        self.assertNotIn("HEADROOM_CONFIG_DIR", env)
        self.assertEqual(env["KEEP_ME"], "yes")
        self.assertEqual(env["HEADROOM_TELEMETRY"], "off")

    def test_setup_requires_uninstall_before_managed_identity_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            manager._write_state(root, self._state(root))
            with patch.object(manager, "_ensure_runtime") as ensure_runtime:
                code, output = self._run_main(
                    ["setup", "--runtime-root", str(root), "--profile", "other", "--port", "57881", "--json"]
                )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["decision"], "MANAGED_CONFIGURATION_CONFLICT")
        ensure_runtime.assert_not_called()

    def test_uninstall_delegates_remove_waits_for_stop_and_purges_marked_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            manager._write_state(root, state)
            self._write_manifest(root)
            cli = self._fake_cli(state)
            completed = subprocess.CompletedProcess([str(cli)], 0, "Removed deployment\n")
            with (
                patch.object(manager, "_run", return_value=completed) as run,
                patch.object(manager, "_wait_stopped", return_value={"ok": False, "status": None}),
            ):
                code, output = self._run_main(["uninstall", "--runtime-root", str(root), "--json"])
            root_exists = root.exists()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["decision"], "UNINSTALLED")
        self.assertFalse(root_exists)
        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ["install", "remove"])

    def test_uninstall_preserves_files_if_listener_remains_ready(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            manager._write_state(root, state)
            self._write_manifest(root)
            self._fake_cli(state)
            completed = subprocess.CompletedProcess(["headroom"], 0, "Removed deployment\n")
            with (
                patch.object(manager, "_run", return_value=completed),
                patch.object(manager, "_wait_stopped", return_value={"ok": True, "status": 200}),
            ):
                code, output = self._run_main(["uninstall", "--runtime-root", str(root), "--json"])
            root_exists = root.exists()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["decision"], "UNINSTALL_PARTIAL")
        self.assertTrue(root_exists)

    def test_uninstall_blocks_unmarked_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            state = self._state(root)
            manager._write_state(root, state)
            code, output = self._run_main(["uninstall", "--runtime-root", str(root), "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["decision"], "UNINSTALL_BLOCKED")

    def test_rejects_root_equal_to_hermes_home(self):
        with tempfile.TemporaryDirectory() as td, patch.dict("os.environ", {"HERMES_HOME": td}):
            code, output = self._run_main(
                ["setup", "--runtime-root", td, "--dry-run", "--json"]
            )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["decision"], "ERROR")


if __name__ == "__main__":
    unittest.main()
