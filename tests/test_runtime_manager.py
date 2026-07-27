import contextlib
import io
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_headroom_plugin import runtime_manager as manager


class RuntimeManagerTest(unittest.TestCase):
    def setUp(self):
        self._isolated_home = tempfile.TemporaryDirectory(prefix="headroom-runtime-manager-test-")
        home = Path(self._isolated_home.name)
        hermes_home = home / ".hermes"
        hermes_home.mkdir()
        clean_env = {
            key: value for key, value in os.environ.items() if not key.startswith("HEADROOM_")
        }
        clean_env.update(
            {"HOME": str(home), "USERPROFILE": str(home), "HERMES_HOME": str(hermes_home)}
        )
        self._isolated_env = patch.dict(os.environ, clean_env, clear=True)
        self._isolated_env.start()
        self._absent_supervisor = patch.object(
            manager,
            "_supervisor_presence",
            return_value={"present": False, "evidence": []},
        )
        self._absent_supervisor.start()

    def tearDown(self):
        self._absent_supervisor.stop()
        self._isolated_env.stop()
        self._isolated_home.cleanup()

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

    def _upstream_status_output(
        self,
        *,
        profile: str = "hermes-test",
        port: int = 57881,
        preset: str = "persistent-service",
        status: str = "running",
        healthy: str = "yes",
    ) -> str:
        supervisor = "service" if preset == "persistent-service" else "task"
        return "\n".join(
            [
                f"Profile:    {profile}",
                f"Preset:     {preset}",
                "Runtime:    python",
                f"Supervisor: {supervisor}",
                "Scope:      user",
                f"Port:       {port}",
                f"Status:     {status}",
                f"Healthy:    {healthy}",
            ]
        ) + "\n"

    def _present_supervisor(self, profile: str = "hermes-test") -> dict[str, object]:
        service_name = f"headroom-{profile}"
        task = {
            "exists": True,
            "enabled": True,
            "managed_action_identity": True,
        }
        return {
            "present": True,
            "service_name": service_name,
            "evidence": [f"test-supervisor:{service_name}"],
            "tasks": {"startup": dict(task), "health": dict(task)},
        }

    def _listener_inventory(
        self,
        state: manager.RuntimeState | None = None,
        *,
        port: int | None = None,
        present: bool | None = True,
        proven: bool = True,
    ) -> dict[str, object]:
        selected_port = state.port if state is not None else int(port or manager.DEFAULT_PORT)
        expected_executables = (
            [
                str(manager._exe(Path(state.venv_dir), "python")),
                str(manager._exe(Path(state.venv_dir), "headroom")),
            ]
            if state is not None
            else []
        )
        return {
            "method": "windows_os_socket_process_inventory",
            "probe_port": selected_port,
            "http_probe": "not_probed_read_only",
            "inventory_ok": present is not None,
            "present": present,
            "records": (
                [{"pid": 4242, "executable_path": expected_executables[0]}]
                if present and expected_executables
                else []
            ),
            "identity": {
                "proven": proven,
                "expected_executables": expected_executables,
            },
        }

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
        artifacts=None,
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
                    "artifacts": [] if artifacts is None else artifacts,
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
        self.assertEqual(payload["headroom_spec"], "headroom-ai[proxy]==0.32.1")
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
            preset = "persistent-service"
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
                    preset=preset,
                )
                return applied

            upstream_status = subprocess.CompletedProcess(
                ["headroom"],
                0,
                self._upstream_status_output(
                    profile=manager.DEFAULT_PROFILE,
                    port=57884,
                    preset=preset,
                ),
            )

            with (
                patch.object(manager, "_is_windows", return_value=False),
                patch.object(manager, "readyz", return_value={"ok": False, "status": None}),
                patch.object(
                    manager,
                    "_ensure_runtime",
                    return_value=(cli, {"headroom": "0.32.1", "litellm": "1.91.3"}),
                ),
                patch.object(manager, "_safe_apply", side_effect=apply_and_write_manifest) as safe_apply,
                patch.object(manager, "_wait_ready", return_value={"ok": True, "status": 200}),
                patch.object(manager, "smoke", return_value={"ok": True, "sentinel_found": True}),
                patch.object(manager, "_run", return_value=upstream_status),
                patch.object(
                    manager,
                    "_supervisor_presence",
                    side_effect=[
                        {"present": False, "service_name": "", "evidence": []},
                        self._present_supervisor(manager.DEFAULT_PROFILE),
                    ],
                ),
            ):
                code, output = self._run_main(
                    [
                        "setup",
                        "--runtime-root",
                        str(root),
                        "--port",
                        "57884",
                        "--preset",
                        preset,
                        "--json",
                    ]
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
                    return_value=(cli, {"headroom": "0.32.1", "litellm": "1.91.3"}),
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
                    return_value=(cli, {"headroom": "0.32.1", "litellm": "1.91.3"}),
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
            completed = subprocess.CompletedProcess(
                ["headroom"], 0, self._upstream_status_output()
            )
            with (
                patch.object(manager, "readyz", return_value={"ok": True, "status": 200}),
                patch.object(manager, "_run", return_value=completed),
                patch.object(
                    manager,
                    "_supervisor_presence",
                    return_value=self._present_supervisor(),
                ),
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
            completed = subprocess.CompletedProcess(
                ["headroom"], 0, self._upstream_status_output()
            )
            with (
                patch.object(manager, "readyz", return_value={"ok": True, "status": 200}),
                patch.object(manager, "smoke", return_value={"ok": True, "sentinel_found": True}),
                patch.object(manager, "_run", return_value=completed),
                patch.object(
                    manager,
                    "_supervisor_presence",
                    return_value=self._present_supervisor(),
                ),
            ):
                code, output = self._run_main(["doctor", "--runtime-root", str(root), "--json"])
        payload = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(payload["decision"], "RUNTIME_FULL_DURABLE")
        self.assertTrue(payload["smoke"]["sentinel_found"])

    def test_doctor_routes_legacy_manifest_mismatch_to_read_only_reconcile(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            state.preset = "persistent-task"
            manager._write_state(root, state)
            self._fake_cli(state)
            self._write_manifest(
                root,
                profile=state.profile,
                preset=state.preset,
                mutations=[{"target": "legacy-environment"}],
            )
            with (
                patch.object(manager, "_is_windows", return_value=True),
                patch.object(manager, "readyz", return_value={"ok": True, "status": 200}),
                patch.object(manager, "smoke", return_value={"ok": True, "sentinel_found": True}),
                patch.object(manager, "_upstream_status_evidence", return_value={"ok": True}),
                patch.object(manager, "_supervisor_contract", return_value={"ok": False, "present": True}),
            ):
                code, output = self._run_main(
                    ["doctor", "--runtime-root", str(root), "--json"]
                )
        payload = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(payload["decision"], "RUNTIME_PARTIAL")
        self.assertEqual(payload["next"], "run headroom-runtime reconcile --dry-run --json")

    def test_parse_upstream_status_requires_complete_semantic_identity(self):
        good = manager._parse_upstream_status(
            self._upstream_status_output(),
            profile="hermes-test",
            preset="persistent-service",
            port=57881,
        )
        self.assertIs(good["ok"], True)

        cases = {
            "stopped": self._upstream_status_output(status="stopped"),
            "unhealthy": self._upstream_status_output(healthy="no"),
            "wrong_profile": self._upstream_status_output(profile="foreign"),
            "missing_status": self._upstream_status_output().replace(
                "Status:     running\n", ""
            ),
            "duplicate_status": self._upstream_status_output() + "Status: stopped\n",
        }
        for name, output in cases.items():
            with self.subTest(name=name):
                evidence = manager._parse_upstream_status(
                    output,
                    profile="hermes-test",
                    preset="persistent-service",
                    port=57881,
                )
                self.assertIs(evidence["ok"], False)
                self.assertTrue(evidence["reasons"])

    def test_read_only_upstream_status_probe_disables_manager_log_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            state = self._state(root)
            headroom = self._fake_cli(state)
            completed = subprocess.CompletedProcess(
                [str(headroom)], 0, self._upstream_status_output()
            )
            with patch.object(manager, "_run", return_value=completed) as run:
                evidence = manager._upstream_status_evidence(
                    headroom=headroom,
                    root=root,
                    profile=state.profile,
                    preset=state.preset,
                    port=state.port,
                    timeout=30,
                    write_log=False,
                )
        self.assertIs(evidence["ok"], True)
        self.assertIsNone(run.call_args.kwargs["log"])
        self.assertEqual(run.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")

    def test_status_rejects_stopped_but_healthy_upstream(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            manager._write_state(root, state)
            self._write_manifest(root)
            self._fake_cli(state)
            completed = subprocess.CompletedProcess(
                ["headroom"], 0, self._upstream_status_output(status="stopped")
            )
            with (
                patch.object(manager, "readyz", return_value={"ok": True, "status": 200}),
                patch.object(manager, "_run", return_value=completed),
                patch.object(
                    manager,
                    "_supervisor_presence",
                    return_value=self._present_supervisor(),
                ),
            ):
                code, output = self._run_main(
                    ["status", "--runtime-root", str(root), "--json"]
                )
        payload = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(payload["decision"], "RUNTIME_PARTIAL")
        self.assertIn("mismatch:status", payload["upstream_status_semantic"]["reasons"])

    def test_status_rejects_running_upstream_without_supervisor_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            manager._write_state(root, state)
            self._write_manifest(root)
            self._fake_cli(state)
            completed = subprocess.CompletedProcess(
                ["headroom"], 0, self._upstream_status_output()
            )
            with (
                patch.object(manager, "readyz", return_value={"ok": True, "status": 200}),
                patch.object(manager, "_run", return_value=completed),
            ):
                code, output = self._run_main(
                    ["status", "--runtime-root", str(root), "--json"]
                )
        payload = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(payload["decision"], "RUNTIME_PARTIAL")
        self.assertIs(payload["upstream_status_semantic"]["ok"], True)
        self.assertIs(payload["supervisor"]["present"], False)

    def test_doctor_rejects_stopped_upstream_even_when_smoke_passes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            manager._write_state(root, state)
            self._write_manifest(root)
            self._fake_cli(state)
            completed = subprocess.CompletedProcess(
                ["headroom"], 0, self._upstream_status_output(status="stopped")
            )
            with (
                patch.object(manager, "readyz", return_value={"ok": True, "status": 200}),
                patch.object(manager, "smoke", return_value={"ok": True, "sentinel_found": True}),
                patch.object(manager, "_run", return_value=completed),
                patch.object(
                    manager,
                    "_supervisor_presence",
                    return_value=self._present_supervisor(),
                ),
            ):
                code, output = self._run_main(
                    ["doctor", "--runtime-root", str(root), "--json"]
                )
        payload = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(payload["decision"], "RUNTIME_PARTIAL")
        self.assertIn(
            "mismatch:status",
            payload["upstream_status"]["semantic"]["reasons"],
        )

    def test_setup_keeps_partial_state_when_upstream_reports_stopped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            preset = "persistent-service"
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
                    port=57887,
                    preset=preset,
                )
                return applied

            stopped = subprocess.CompletedProcess(
                ["headroom"],
                0,
                self._upstream_status_output(
                    profile=manager.DEFAULT_PROFILE,
                    port=57887,
                    preset=preset,
                    status="stopped",
                ),
            )
            with (
                patch.object(manager, "_is_windows", return_value=False),
                patch.object(manager, "readyz", return_value={"ok": False, "status": None}),
                patch.object(
                    manager,
                    "_ensure_runtime",
                    return_value=(cli, {"headroom": "0.32.1", "litellm": "1.91.3"}),
                ),
                patch.object(manager, "_safe_apply", side_effect=apply_and_write_manifest),
                patch.object(manager, "_wait_ready", return_value={"ok": True, "status": 200}),
                patch.object(manager, "smoke", return_value={"ok": True, "sentinel_found": True}),
                patch.object(manager, "_run", return_value=stopped),
                patch.object(
                    manager,
                    "_supervisor_presence",
                    side_effect=[
                        {"present": False, "service_name": "", "evidence": []},
                        self._present_supervisor(manager.DEFAULT_PROFILE),
                    ],
                ),
            ):
                code, output = self._run_main(
                    [
                        "setup",
                        "--runtime-root",
                        str(root),
                        "--port",
                        "57887",
                        "--preset",
                        preset,
                        "--json",
                    ]
                )
            state = manager._load_state(root)
        payload = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(payload["decision"], "RUNTIME_PARTIAL")
        self.assertIn("durable lifecycle semantic verification failed", payload["detail"])
        self.assertIsNotNone(state)
        self.assertEqual(state.status if state else None, "RUNTIME_PARTIAL")

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
            manager._validate_package_spec("headroom-ai[proxy]=>0.32.1", package="headroom-ai")
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
                subprocess.CompletedProcess([str(python_exe)], 0, "0.32.1\n1.91.3\n"),
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

    def test_manager_marker_makes_runtime_root_private_on_posix(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX mode bits are not authoritative on Windows")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            mode = root.stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)

    def test_purge_symlink_swap_after_validation_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "runtime"
            outside = base / "outside"
            outside.mkdir()
            sentinel = outside / "DO_NOT_DELETE"
            sentinel.write_text("safe", encoding="utf-8")
            manager._ensure_marker(root)
            state = self._state(root)
            manager._write_state(root, state)
            workspace = Path(state.workspace_dir)
            workspace.mkdir()
            (workspace / "owned.txt").write_text("owned", encoding="utf-8")
            original_rmtree = manager.shutil.rmtree
            swapped = False

            def swap_then_delete(path, *args, **kwargs):
                nonlocal swapped
                if Path(path).name == workspace.name and not swapped:
                    moved = root / "workspace-before-swap"
                    workspace.rename(moved)
                    try:
                        workspace.symlink_to(outside, target_is_directory=True)
                    except OSError as exc:
                        self.skipTest(f"directory symlinks unavailable: {exc}")
                    swapped = True
                return original_rmtree(path, *args, **kwargs)

            with patch.object(manager.shutil, "rmtree", side_effect=swap_then_delete):
                result = manager._purge_managed_root(root, state)
            sentinel_exists = sentinel.exists()
            root_exists = root.exists()
        self.assertFalse(result["ok"], result)
        self.assertTrue(result.get("fail_closed"), result)
        self.assertTrue(sentinel_exists)
        self.assertTrue(root_exists)

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

    def test_windows_launcher_content_is_hidden_waiting_and_deterministic(self):
        command = Path(r"C:\Managed Runtime\ensure-headroom.cmd")
        first = manager._windows_launcher_content(command)
        second = manager._windows_launcher_content(command)
        self.assertEqual(first, second)
        self.assertIn("shell.Run(command, 0, True)", first)
        self.assertIn('cmd.exe /d /c ""C:\\Managed Runtime\\ensure-headroom.cmd""', first)
        self.assertIn("WScript.Quit exitCode", first)

    def test_parse_windows_task_xml_requires_enabled_action_and_trigger(self):
        launcher = Path(r"C:\Managed Runtime\ensure-headroom-hidden.vbs")
        startup_xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>
  <Settings><Enabled>true</Enabled></Settings>
  <Actions><Exec><Command>wscript.exe</Command><Arguments>//B //NoLogo "{launcher}"</Arguments></Exec></Actions>
</Task>"""
        health_xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><TimeTrigger><Repetition><Interval>PT5M</Interval></Repetition><Enabled>true</Enabled></TimeTrigger></Triggers>
  <Settings><Enabled>true</Enabled></Settings>
  <Actions><Exec><Command>wscript.exe</Command><Arguments>//B //NoLogo "{launcher}"</Arguments></Exec></Actions>
</Task>"""
        startup = manager._parse_windows_task_xml(
            startup_xml, launcher=launcher, trigger_kind="startup"
        )
        health = manager._parse_windows_task_xml(
            health_xml, launcher=launcher, trigger_kind="health"
        )
        health_utf16 = manager._parse_windows_task_xml(
            health_xml.encode("utf-16"), launcher=launcher, trigger_kind="health"
        )
        health_prefixed_utf16 = manager._parse_windows_task_xml(
            ("\r\n\ufeff" + health_xml).encode("utf-16-le"),
            launcher=launcher,
            trigger_kind="health",
        )
        self.assertTrue(startup["ok"])
        self.assertTrue(startup["managed_action_identity"])
        self.assertTrue(health["ok"])
        self.assertTrue(health_utf16["ok"])
        self.assertTrue(health_prefixed_utf16["ok"])
        legacy_launcher = Path(r"C:\Managed Runtime\ensure-headroom.cmd")
        legacy_xml = startup_xml.replace(
            "wscript.exe", str(legacy_launcher)
        ).replace(f'//B //NoLogo "{launcher}"', "")
        legacy = manager._parse_windows_task_xml(
            legacy_xml,
            launcher=launcher,
            legacy_launcher=legacy_launcher,
            trigger_kind="startup",
        )
        self.assertFalse(legacy["ok"])
        self.assertTrue(legacy["legacy_action_command_exact"])
        self.assertTrue(legacy["legacy_action_arguments_exact"])
        self.assertTrue(legacy["managed_action_identity"])
        legacy_with_extra_argument = manager._parse_windows_task_xml(
            legacy_xml.replace("<Arguments></Arguments>", "<Arguments>--foreign</Arguments>"),
            launcher=launcher,
            legacy_launcher=legacy_launcher,
            trigger_kind="startup",
        )
        self.assertFalse(legacy_with_extra_argument["legacy_action_arguments_exact"])
        self.assertFalse(legacy_with_extra_argument["managed_action_identity"])
        plain_launcher = Path(r"C:\Managed\ensure-headroom-hidden.vbs")
        plain_xml = health_xml.replace(str(launcher), str(plain_launcher)).replace(
            f'"{plain_launcher}"', str(plain_launcher)
        )
        unquoted = manager._parse_windows_task_xml(
            plain_xml,
            launcher=plain_launcher,
            trigger_kind="health",
        )
        self.assertTrue(unquoted["ok"])
        drifted = manager._parse_windows_task_xml(
            startup_xml.replace("wscript.exe", "cmd.exe"),
            launcher=launcher,
            trigger_kind="startup",
        )
        self.assertFalse(drifted["ok"])
        self.assertIn("action_command", drifted["reasons"])

    def test_manifest_contract_marks_legacy_windows_tasks_for_migration(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            profile = "hermes-test"
            service = f"headroom-{profile}"
            self._write_manifest(
                root,
                profile=profile,
                preset="persistent-task",
                artifacts=[
                    {"kind": "windows-task", "path": f"{service}-startup", "metadata": {}},
                    {"kind": "windows-task", "path": f"{service}-health", "metadata": {}},
                ],
            )
            with patch.object(manager, "_is_windows", return_value=True, create=True):
                contract = manager._manifest_contract(
                    root, profile=profile, port=57881, preset="persistent-task"
                )
        self.assertFalse(contract["ok"])
        self.assertTrue(contract["windows_task_contract"]["migration_required"])
        self.assertIn("windows_task_contract", contract["mismatches"])

    def test_windows_listener_inventory_uses_os_tables_without_http(self):
        expected_python = Path("C:/Hermes/headroom/venv/Scripts/python.exe")
        expected_headroom = Path("C:/Hermes/headroom/venv/Scripts/headroom.exe")
        expected_base = Path("C:/hostedtoolcache/windows/Python/3.11/x64/python.exe")
        completed = subprocess.CompletedProcess(
            ["powershell.exe"],
            0,
            json.dumps(
                [
                    {
                        "local_address": "127.0.0.1",
                        "local_port": 57881,
                        "pid": 4242,
                        "executable_path": str(expected_base),
                        "command_line": f'"{expected_headroom}" proxy --port 57881',
                    }
                ]
            ),
        )
        with (
            patch.object(manager.shutil, "which", return_value="powershell.exe"),
            patch.object(manager, "_run", return_value=completed) as run,
        ):
            inventory = manager._windows_listener_inventory(
                port=57881,
                expected_executables=(expected_python, expected_headroom),
                expected_venv_base_executables=(expected_base,),
                timeout=60,
            )
        self.assertIs(inventory["inventory_ok"], True)
        self.assertIs(inventory["present"], True)
        self.assertIs(inventory["identity"]["proven"], True)
        self.assertEqual(inventory["identity"]["match_basis"], ["venv_redirector_chain"])
        self.assertNotIn("command_line", inventory["records"][0])
        self.assertEqual(inventory["http_probe"], "not_probed_read_only")
        command = run.call_args.args[0]
        self.assertIn("Get-NetTCPConnection", command[-1])
        self.assertNotIn("readyz", command[-1].lower())
        self.assertNotIn("http", command[-1].lower())
        self.assertIsNone(run.call_args.kwargs["log"])

    def test_windows_listener_inventory_fails_closed_for_non_loopback(self):
        expected_python = Path("C:/Hermes/headroom/venv/Scripts/python.exe")
        completed = subprocess.CompletedProcess(
            ["powershell.exe"],
            0,
            json.dumps(
                {
                    "local_address": "0.0.0.0",
                    "local_port": 57881,
                    "pid": 4242,
                    "executable_path": str(expected_python),
                    "command_line": f'"{expected_python}" -m headroom',
                }
            ),
        )
        with (
            patch.object(manager.shutil, "which", return_value="powershell.exe"),
            patch.object(manager, "_run", return_value=completed),
        ):
            inventory = manager._windows_listener_inventory(
                port=57881, expected_executables=(expected_python,), timeout=60
            )
        self.assertIs(inventory["present"], True)
        self.assertIs(inventory["identity"]["loopback_only"], False)
        self.assertIs(inventory["identity"]["proven"], False)

    def test_windows_listener_inventory_rejects_managed_path_as_later_argument(self):
        expected_python = Path("C:/Hermes/headroom/venv/Scripts/python.exe")
        completed = subprocess.CompletedProcess(
            ["powershell.exe"],
            0,
            json.dumps(
                {
                    "local_address": "127.0.0.1",
                    "local_port": 57881,
                    "pid": 4242,
                    "executable_path": "C:/hostedtoolcache/windows/Python/3.11/x64/python.exe",
                    "command_line": (
                        '"C:/hostedtoolcache/windows/Python/3.11/x64/python.exe" '
                        f'--untrusted-argument "{expected_python}"'
                    ),
                }
            ),
        )
        with (
            patch.object(manager.shutil, "which", return_value="powershell.exe"),
            patch.object(manager, "_run", return_value=completed),
        ):
            inventory = manager._windows_listener_inventory(
                port=57881, expected_executables=(expected_python,), timeout=60
            )
        self.assertIs(inventory["present"], True)
        self.assertIs(inventory["identity"]["proven"], False)
        self.assertEqual(inventory["identity"]["matching_pids"], [])
        self.assertNotIn("command_line", inventory["records"][0])

    def test_windows_listener_inventory_rejects_spoofed_command_image(self):
        expected_python = Path("C:/Hermes/headroom/venv/Scripts/python.exe")
        expected_base = Path("C:/hostedtoolcache/windows/Python/3.11/x64/python.exe")
        completed = subprocess.CompletedProcess(
            ["powershell.exe"],
            0,
            json.dumps(
                {
                    "local_address": "127.0.0.1",
                    "local_port": 57881,
                    "pid": 4242,
                    "executable_path": "C:/untrusted/arbitrary.exe",
                    "command_line": f'"{expected_python}" proxy --port 57881',
                }
            ),
        )
        with (
            patch.object(manager.shutil, "which", return_value="powershell.exe"),
            patch.object(manager, "_run", return_value=completed),
        ):
            inventory = manager._windows_listener_inventory(
                port=57881,
                expected_executables=(expected_python,),
                expected_venv_base_executables=(expected_base,),
                timeout=60,
            )
        self.assertIs(inventory["identity"]["proven"], False)
        self.assertEqual(inventory["identity"]["matching_pids"], [])
        self.assertEqual(inventory["identity"]["match_basis"], [])

    def test_windows_venv_base_executable_is_read_from_managed_config(self):
        with tempfile.TemporaryDirectory() as td:
            venv = Path(td)
            (venv / "pyvenv.cfg").write_text(
                "home = C:\\Python311\n"
                "executable = C:\\Python311\\python.exe\n",
                encoding="utf-8",
            )
            executables = manager._windows_venv_base_executables(venv)
        self.assertEqual(executables, (Path("C:\\Python311\\python.exe"),))

    def test_reconcile_is_read_only_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            state.preset = "persistent-task"
            manager._write_state(root, state)
            self._fake_cli(state)
            manager._exe(Path(state.venv_dir), "python").write_text("fake", encoding="utf-8")
            service = f"headroom-{state.profile}"
            manifest = self._write_manifest(
                root,
                profile=state.profile,
                preset=state.preset,
                artifacts=[
                    {"kind": "windows-task", "path": f"{service}-startup", "metadata": {}},
                    {"kind": "windows-task", "path": f"{service}-health", "metadata": {}},
                ],
            )
            before = manifest.read_bytes()
            with (
                patch.object(manager, "_is_windows", return_value=True, create=True),
                patch.object(
                    manager,
                    "_windows_listener_inventory",
                    return_value=self._listener_inventory(state, proven=False),
                ),
                patch.object(manager, "readyz") as ready,
                patch.object(
                    manager,
                    "_supervisor_contract",
                    return_value={**self._present_supervisor(state.profile), "ok": False},
                ),
                patch.object(manager, "_upstream_status_evidence") as upstream,
                patch.object(manager, "_acquire_lock") as acquire,
                patch.object(manager, "_safe_reconcile", create=True) as apply,
            ):
                code, output = self._run_main(
                    ["reconcile", "--runtime-root", str(root), "--dry-run", "--json"]
                )
            after = manifest.read_bytes()
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertEqual(payload["decision"], "MIGRATION_REQUIRED")
        self.assertIs(payload["ownership"]["proven"], False)
        self.assertIs(payload["ownership"]["deployment"]["proven"], True)
        self.assertIs(payload["ownership"]["listener_binding"]["proven"], False)
        self.assertIs(payload["mutation_authority"]["eligible"], True)
        self.assertEqual(payload["mutation_authority"]["scope"], "windows_task_contract")
        self.assertTrue(manager._task_reconcile_apply_authorized(payload))
        self.assertEqual(before, after)
        ready.assert_not_called()
        upstream.assert_not_called()
        acquire.assert_not_called()
        apply.assert_not_called()

    def test_reconcile_blocks_task_migration_when_task_actions_are_unproven(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            state.preset = "persistent-task"
            manager._write_state(root, state)
            self._fake_cli(state)
            manager._exe(Path(state.venv_dir), "python").write_text(
                "fake", encoding="utf-8"
            )
            service = f"headroom-{state.profile}"
            self._write_manifest(
                root,
                profile=state.profile,
                preset=state.preset,
                artifacts=[
                    {"kind": "windows-task", "path": f"{service}-startup", "metadata": {}},
                    {"kind": "windows-task", "path": f"{service}-health", "metadata": {}},
                ],
            )
            foreign_task = {
                "exists": True,
                "enabled": True,
                "managed_action_identity": False,
            }
            supervisor = {
                "ok": False,
                "present": True,
                "service_name": service,
                "evidence": [
                    f"scheduled-task:{service}-startup",
                    f"scheduled-task:{service}-health",
                ],
                "tasks": {
                    "startup": dict(foreign_task),
                    "health": dict(foreign_task),
                },
            }
            with (
                patch.object(manager, "_is_windows", return_value=True, create=True),
                patch.object(
                    manager,
                    "_windows_listener_inventory",
                    return_value=self._listener_inventory(state, proven=False),
                ),
                patch.object(manager, "_supervisor_contract", return_value=supervisor),
                patch.object(manager, "_acquire_lock") as acquire,
                patch.object(manager, "_safe_reconcile") as apply,
            ):
                code, output = self._run_main(
                    ["reconcile", "--runtime-root", str(root), "--dry-run", "--json"]
                )
        payload = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(payload["decision"], "RECONCILE_BLOCKED")
        self.assertEqual(
            payload["classification"], "manager_deployment_unproven_task_actions"
        )
        self.assertIs(payload["mutation_authority"]["eligible"], False)
        self.assertEqual(
            payload["mutation_authority"]["reasons"],
            ["managed_task_action_identity_missing"],
        )
        self.assertFalse(manager._task_reconcile_apply_authorized(payload))
        acquire.assert_not_called()
        apply.assert_not_called()

    def test_reconcile_dry_run_classifies_legacy_mutations_without_any_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            state.preset = "persistent-task"
            manager._write_state(root, state)
            self._fake_cli(state)
            manager._exe(Path(state.venv_dir), "python").write_text("fake", encoding="utf-8")
            service = f"headroom-{state.profile}"
            self._write_manifest(
                root,
                profile=state.profile,
                preset=state.preset,
                mutations=[{"target": "legacy-environment"}],
                artifacts=[
                    {"kind": "windows-task", "path": f"{service}-startup", "metadata": {}},
                    {"kind": "windows-task", "path": f"{service}-health", "metadata": {}},
                ],
            )
            proxy_log = manager._workspace_dir(root) / "logs" / "proxy.log"
            proxy_log.parent.mkdir(parents=True, exist_ok=True)
            proxy_log.write_bytes(b"preexisting-log\n")

            def application_probe_would_write(*args, **kwargs):
                del args, kwargs
                with proxy_log.open("ab") as stream:
                    stream.write(b"forbidden-dry-run-write\n")
                return {"ok": True}

            before = {
                str(path.relative_to(root)): (path.stat().st_mtime_ns, path.read_bytes())
                for path in root.rglob("*") if path.is_file()
            }
            supervisor = {
                "ok": False,
                "present": True,
                "service_name": service,
                "evidence": [f"scheduled-task:{service}-startup", f"scheduled-task:{service}-health"],
                "reasons": ["manifest:legacy-task-contract"],
            }
            with (
                patch.object(manager, "_is_windows", return_value=True),
                patch.object(
                    manager,
                    "_windows_listener_inventory",
                    return_value=self._listener_inventory(state, proven=False),
                ),
                patch.object(manager, "readyz", side_effect=application_probe_would_write) as ready,
                patch.object(manager, "_supervisor_contract", return_value=supervisor),
                patch.object(
                    manager,
                    "_upstream_status_evidence",
                    side_effect=application_probe_would_write,
                ) as upstream,
                patch.object(manager, "_acquire_lock") as acquire,
                patch.object(manager, "_safe_reconcile") as apply,
            ):
                code, output = self._run_main(
                    ["reconcile", "--runtime-root", str(root), "--dry-run", "--json"]
                )
            after = {
                str(path.relative_to(root)): (path.stat().st_mtime_ns, path.read_bytes())
                for path in root.rglob("*") if path.is_file()
            }
            lock_exists = manager._lock_path(root).exists()
        payload = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(payload["decision"], "REINSTALL_REQUIRED")
        self.assertEqual(payload["classification"], "manager_owned_legacy_mutations")
        self.assertIs(payload["writes_performed"], False)
        self.assertEqual(payload["inventory"]["manifest"]["mismatches"], ["mutations", "windows_task_contract"])
        self.assertIs(payload["ownership"]["proven"], False)
        self.assertIs(payload["ownership"]["deployment"]["proven"], True)
        self.assertIs(payload["ownership"]["listener_binding"]["proven"], False)
        self.assertIs(payload["mutation_authority"]["eligible"], False)
        self.assertIn(
            "listener_binding_unproven", payload["mutation_authority"]["reasons"]
        )
        self.assertFalse(manager._task_reconcile_apply_authorized(payload))
        self.assertIs(payload["adoption"]["eligible"], False)
        self.assertIn("mutation_history_requires_symmetric_rollback", payload["adoption"]["reasons"])
        self.assertTrue(payload["next_steps"])
        self.assertTrue(payload["rollback"])
        self.assertEqual(
            payload["inventory"]["listener"]["http_probe"], "not_probed_read_only"
        )
        self.assertEqual(
            payload["inventory"]["upstream_status"]["status"], "not_probed_read_only"
        )
        self.assertEqual(before, after)
        self.assertFalse(lock_exists)
        ready.assert_not_called()
        upstream.assert_not_called()
        acquire.assert_not_called()
        apply.assert_not_called()

    def test_reconcile_with_unproven_legacy_mutations_never_recommends_removal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            state.preset = "persistent-task"
            manager._write_state(root, state)
            self._fake_cli(state)
            manager._exe(Path(state.venv_dir), "python").write_text("fake", encoding="utf-8")
            manifest = {
                "ok": False,
                "available": True,
                "mismatches": ["mutations", "port"],
            }
            supervisor = {
                "ok": False,
                "present": True,
                "service_name": f"headroom-{state.profile}",
            }
            with (
                patch.object(manager, "_is_windows", return_value=True),
                patch.object(manager, "_manifest_contract", return_value=manifest),
                patch.object(manager, "_supervisor_contract", return_value=supervisor),
                patch.object(manager, "readyz", return_value={"ok": True}),
                patch.object(manager, "_upstream_status_evidence", return_value={"ok": True}),
                patch.object(manager, "_acquire_lock") as acquire,
            ):
                code, output = self._run_main(
                    ["reconcile", "--runtime-root", str(root), "--dry-run", "--json"]
                )
        payload = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(payload["decision"], "OWNERSHIP_AMBIGUOUS")
        self.assertIs(payload["ownership"]["proven"], False)
        guidance = " ".join(payload["next_steps"])
        self.assertIn("do not run", guidance)
        self.assertNotIn("use the pinned upstream manifest removal path", guidance)
        acquire.assert_not_called()

    def test_reconcile_never_adopts_an_os_listener_without_manager_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "absent-runtime"
            with (
                patch.object(manager, "_is_windows", return_value=True),
                patch.object(
                    manager,
                    "_windows_listener_inventory",
                    return_value=self._listener_inventory(port=manager.DEFAULT_PORT),
                ),
                patch.object(manager, "readyz") as ready,
                patch.object(manager, "_acquire_lock") as acquire,
            ):
                code, output = self._run_main(
                    ["reconcile", "--runtime-root", str(root), "--dry-run", "--json"]
                )
        payload = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(payload["decision"], "OWNERSHIP_AMBIGUOUS")
        self.assertEqual(payload["classification"], "foreign_or_unmanaged")
        self.assertIs(payload["writes_performed"], False)
        self.assertIs(payload["inventory"]["listener"]["present"], True)
        self.assertEqual(
            payload["inventory"]["listener"]["http_probe"], "not_probed_read_only"
        )
        self.assertIs(payload["inventory"]["manager_state"]["present"], False)
        self.assertIs(payload["adoption"]["eligible"], False)
        self.assertFalse(root.exists())
        ready.assert_not_called()
        acquire.assert_not_called()

    def test_reconcile_can_probe_a_nondefault_listener_when_manager_state_is_absent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "absent-runtime"
            with (
                patch.object(manager, "_is_windows", return_value=True),
                patch.object(
                    manager,
                    "_windows_listener_inventory",
                    return_value=self._listener_inventory(port=18787),
                ) as listener_inventory,
                patch.object(manager, "readyz") as ready,
                patch.object(manager, "_acquire_lock") as acquire,
            ):
                code, output = self._run_main(
                    [
                        "reconcile",
                        "--runtime-root",
                        str(root),
                        "--probe-port",
                        "18787",
                        "--dry-run",
                        "--json",
                    ]
                )
        payload = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(payload["decision"], "OWNERSHIP_AMBIGUOUS")
        self.assertEqual(payload["inventory"]["listener"]["probe_port"], 18787)
        listener_inventory.assert_called_once_with(
            port=18787, expected_executables=None, timeout=60
        )
        ready.assert_not_called()
        self.assertFalse(root.exists())
        acquire.assert_not_called()

    def test_reconcile_rejects_probe_port_with_apply_before_lock_or_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            with (
                patch.object(manager, "_is_windows", return_value=True),
                patch.object(manager, "_acquire_lock") as acquire,
            ):
                code, output = self._run_main(
                    [
                        "reconcile",
                        "--runtime-root",
                        str(root),
                        "--probe-port",
                        "18787",
                        "--apply",
                        "--json",
                    ]
                )
        payload = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(payload["decision"], "RECONCILE_BLOCKED")
        self.assertIn("read-only discovery", payload["detail"])
        self.assertFalse(root.exists())
        acquire.assert_not_called()

    def test_reconcile_apply_requires_scoped_preflight_before_lock_or_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            preflight = {
                "decision": "REINSTALL_REQUIRED",
                "writes_performed": False,
                "mutation_authority": {
                    "eligible": False,
                    "scope": None,
                    "resources": [],
                    "reasons": ["listener_binding_unproven"],
                },
            }
            with (
                patch.object(manager, "_is_windows", return_value=True),
                patch.object(
                    manager, "_read_only_reconcile_plan", return_value=(preflight, 1)
                ),
                patch.object(manager, "_acquire_lock") as acquire,
                patch.object(manager, "_safe_reconcile") as apply,
            ):
                code, output = self._run_main(
                    ["reconcile", "--runtime-root", str(root), "--apply", "--json"]
                )
        payload = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(payload["decision"], "RECONCILE_BLOCKED")
        self.assertIs(payload["writes_performed"], False)
        self.assertEqual(payload["preflight"], preflight)
        self.assertFalse(root.exists())
        acquire.assert_not_called()
        apply.assert_not_called()

    def test_reconcile_apply_rechecks_authority_after_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            authorized = {
                "decision": "MIGRATION_REQUIRED",
                "ownership": {"deployment": {"proven": True}},
                "mutation_authority": {
                    "eligible": True,
                    "scope": "windows_task_contract",
                    "resources": [
                        "managed_windows_launcher",
                        "managed_windows_scheduled_tasks",
                        "manifest_artifacts",
                    ],
                    "evidence": ["managed_task_action_identity"],
                },
            }
            changed = {
                "decision": "RECONCILE_BLOCKED",
                "ownership": {"deployment": {"proven": False}},
                "mutation_authority": {
                    "eligible": False,
                    "scope": None,
                    "resources": [],
                },
            }
            with (
                patch.object(manager, "_is_windows", return_value=True),
                patch.object(
                    manager,
                    "_read_only_reconcile_plan",
                    side_effect=[(authorized, 1), (changed, 2)],
                ),
                patch.object(manager, "_acquire_lock", return_value=42) as acquire,
                patch.object(manager, "_release_lock") as release,
                patch.object(manager, "_safe_reconcile") as apply,
            ):
                code, output = self._run_main(
                    ["reconcile", "--runtime-root", str(root), "--apply", "--json"]
                )
        payload = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(payload["decision"], "RECONCILE_BLOCKED")
        self.assertIs(payload["writes_performed"], True)
        self.assertEqual(payload["write_scope"], ["transaction_lock_only"])
        self.assertEqual(payload["preflight"], changed)
        acquire.assert_called_once_with(root)
        release.assert_called_once_with(root, 42)
        apply.assert_not_called()

    def test_reconcile_does_not_claim_current_contract_when_runtime_identity_is_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            state = self._state(root)
            state.preset = "persistent-task"
            manager._ensure_marker(root)
            manager._write_state(root, state)
            manifest = {"ok": True, "available": True, "mismatches": []}
            supervisor = {
                "ok": True,
                "present": True,
                "service_name": f"headroom-{state.profile}",
            }
            with (
                patch.object(manager, "_is_windows", return_value=True),
                patch.object(manager, "_manifest_contract", return_value=manifest),
                patch.object(manager, "_supervisor_contract", return_value=supervisor),
                patch.object(manager, "readyz", return_value={"ok": True}),
                patch.object(manager, "_acquire_lock") as acquire,
            ):
                code, output = self._run_main(
                    ["reconcile", "--runtime-root", str(root), "--dry-run", "--json"]
                )
        payload = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(payload["decision"], "RECONCILE_BLOCKED")
        self.assertIs(payload["ownership"]["proven"], False)
        self.assertIn("runtime_identity", payload["ownership"]["missing"])
        acquire.assert_not_called()

    def test_reconcile_reports_current_contract_only_with_complete_positive_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            state = self._state(root)
            state.preset = "persistent-task"
            manager._ensure_marker(root)
            manager._write_state(root, state)
            self._fake_cli(state)
            python_exe = manager._exe(Path(state.venv_dir), "python")
            python_exe.parent.mkdir(parents=True, exist_ok=True)
            python_exe.write_text("", encoding="utf-8")
            manifest = {"ok": True, "available": True, "mismatches": []}
            supervisor = {
                "ok": True,
                "present": True,
                "service_name": f"headroom-{state.profile}",
            }
            with (
                patch.object(manager, "_is_windows", return_value=True),
                patch.object(manager, "_manifest_contract", return_value=manifest),
                patch.object(manager, "_supervisor_contract", return_value=supervisor),
                patch.object(
                    manager,
                    "_windows_listener_inventory",
                    return_value=self._listener_inventory(state),
                ),
                patch.object(manager, "readyz") as ready,
                patch.object(manager, "_upstream_status_evidence") as upstream,
                patch.object(manager, "_acquire_lock") as acquire,
            ):
                code, output = self._run_main(
                    ["reconcile", "--runtime-root", str(root), "--dry-run", "--json"]
                )
        payload = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(payload["decision"], "RECONCILIATION_NOT_REQUIRED")
        self.assertIs(payload["ownership"]["proven"], True)
        ready.assert_not_called()
        upstream.assert_not_called()
        acquire.assert_not_called()

    def test_embedded_windows_lifecycle_scripts_compile(self):
        compile(manager._SAFE_APPLY_SCRIPT, "<safe-apply>", "exec")
        compile(manager._SAFE_RECONCILE_SCRIPT, "<safe-reconcile>", "exec")
        self.assertIn("_install_silent_windows_tasks", manager._SAFE_APPLY_SCRIPT)
        self.assertIn("_snapshot_supervisor", manager._SAFE_RECONCILE_SCRIPT)
        self.assertIn("_restore_supervisor_snapshot", manager._SAFE_RECONCILE_SCRIPT)
        install_offset = manager._WINDOWS_TASK_OVERLAY_SCRIPT.index(
            "def _install_silent_windows_tasks"
        )
        self.assertLess(
            manager._WINDOWS_TASK_OVERLAY_SCRIPT.index(
                "snapshot = _snapshot_supervisor", install_offset
            ),
            manager._WINDOWS_TASK_OVERLAY_SCRIPT.index(
                "launcher.write_bytes", install_offset
            ),
        )
        self.assertIn(
            "cannot safely snapshot scheduled task before mutation",
            manager._WINDOWS_TASK_OVERLAY_SCRIPT,
        )
        self.assertNotIn(
            '["schtasks", "/Delete", "/TN", name, "/F"]',
            manager._WINDOWS_TASK_OVERLAY_SCRIPT,
        )

    def test_windows_manifest_contract_detects_launcher_hash_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            profile = "hermes-test"
            launcher = manager._windows_launcher_path(root, profile)
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.write_bytes(manager._windows_launcher_bytes(root, profile))
            digest = manager.hashlib.sha256(launcher.read_bytes()).hexdigest()
            artifacts = [
                {
                    "kind": "script",
                    "path": str(launcher),
                    "metadata": {"contract": manager._WINDOWS_TASK_CONTRACT, "sha256": digest},
                },
                *[
                    {"kind": "windows-task", "path": name, "metadata": metadata}
                    for name, metadata in manager._windows_task_metadata(root, profile).items()
                ],
            ]
            manifest = self._write_manifest(
                root,
                profile=profile,
                preset="persistent-task",
                artifacts=artifacts,
            )
            data = json.loads(manifest.read_text(encoding="utf-8"))
            good = manager._windows_manifest_task_contract(root, profile=profile, data=data)
            launcher.write_bytes(b"drift")
            drifted = manager._windows_manifest_task_contract(root, profile=profile, data=data)
        self.assertTrue(good["ok"])
        self.assertFalse(drifted["ok"])
        self.assertIn("launcher_hash", drifted["reasons"])

    def test_parse_windows_task_xml_rejects_disabled_and_trigger_drift(self):
        launcher = Path(r"C:\Managed\ensure-headroom-hidden.vbs")
        xml = f"""<Task xmlns="urn:task">
  <Triggers><TimeTrigger><Repetition><Interval>PT10M</Interval></Repetition></TimeTrigger></Triggers>
  <Settings><Enabled>false</Enabled></Settings>
  <Actions><Exec><Command>wscript.exe</Command><Arguments>//B //NoLogo "{launcher}"</Arguments></Exec></Actions>
</Task>"""
        evidence = manager._parse_windows_task_xml(
            xml, launcher=launcher, trigger_kind="health"
        )
        self.assertFalse(evidence["ok"])
        self.assertIn("disabled", evidence["reasons"])
        self.assertIn("trigger", evidence["reasons"])

    def test_parse_windows_task_xml_rejects_wrappers_and_nested_contract_nodes(self):
        launcher = Path(r"C:\Managed\ensure-headroom-hidden.vbs")
        valid = f"""<Task>
  <Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>
  <Settings><Enabled>true</Enabled></Settings>
  <Actions><Exec><Command>wscript.exe</Command><Arguments>//B //NoLogo "{launcher}"</Arguments></Exec></Actions>
</Task>"""
        wrapped = manager._parse_windows_task_xml(
            f"<NotTask>{valid}</NotTask>", launcher=launcher, trigger_kind="startup"
        )
        nested = manager._parse_windows_task_xml(
            f"<Task><Container>{valid[6:-7]}</Container></Task>",
            launcher=launcher,
            trigger_kind="startup",
        )
        extra_action = manager._parse_windows_task_xml(
            valid.replace("</Actions>", "<ComHandler /></Actions>"),
            launcher=launcher,
            trigger_kind="startup",
        )
        self.assertFalse(wrapped["ok"])
        self.assertIn("document_root", wrapped["reasons"])
        self.assertFalse(nested["ok"])
        self.assertIn("settings_structure", nested["reasons"])
        self.assertFalse(extra_action["ok"])
        self.assertIn("action_structure", extra_action["reasons"])
        self.assertFalse(extra_action["managed_action_identity"])

    def test_windows_task_contract_rejects_unexpected_same_name_service(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            profile = "hermes-test"
            launcher = manager._windows_launcher_path(root, profile)
            startup_xml = f"""<Task><Triggers><BootTrigger /></Triggers><Settings><Enabled>true</Enabled></Settings><Actions><Exec><Command>wscript.exe</Command><Arguments>//B //NoLogo "{launcher}"</Arguments></Exec></Actions></Task>"""
            health_xml = f"""<Task><Triggers><TimeTrigger><Repetition><Interval>PT5M</Interval></Repetition></TimeTrigger></Triggers><Settings><Enabled>true</Enabled></Settings><Actions><Exec><Command>wscript.exe</Command><Arguments>//B //NoLogo "{launcher}"</Arguments></Exec></Actions></Task>"""
            with (
                patch.object(manager, "_is_windows", return_value=True),
                patch.object(
                    manager,
                    "_supervisor_presence",
                    return_value={
                        "present": True,
                        "service_name": f"headroom-{profile}",
                        "evidence": [f"windows-service:headroom-{profile}"],
                    },
                ),
                patch.object(
                    manager,
                    "_windows_manifest_task_contract",
                    return_value={"ok": True, "migration_required": False, "reasons": []},
                ),
                patch.object(
                    manager,
                    "_query_windows_task_xml",
                    side_effect=[
                        {"exists": True, "xml": startup_xml.encode("utf-16")},
                        {"exists": True, "xml": health_xml.encode("utf-16")},
                    ],
                ),
            ):
                contract = manager._supervisor_contract(
                    root, profile=profile, preset="persistent-task"
                )
        self.assertFalse(contract["ok"])
        self.assertIn("unexpected_windows_service", contract["reasons"])
        self.assertNotIn("startup:invalid_xml", contract["reasons"])
        self.assertNotIn("health:invalid_xml", contract["reasons"])

    def test_reconcile_apply_requires_post_apply_durability(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            state.preset = "persistent-task"
            manager._write_state(root, state)
            self._fake_cli(state)
            manager._exe(Path(state.venv_dir), "python").write_text("fake", encoding="utf-8")
            service = f"headroom-{state.profile}"
            self._write_manifest(
                root,
                profile=state.profile,
                preset=state.preset,
                artifacts=[
                    {"kind": "windows-task", "path": f"{service}-startup", "metadata": {}},
                    {"kind": "windows-task", "path": f"{service}-health", "metadata": {}},
                ],
            )
            applied = subprocess.CompletedProcess(["python"], 0, "{}\n")
            authorized_plan = {
                "decision": "MIGRATION_REQUIRED",
                "ownership": {"deployment": {"proven": True}},
                "mutation_authority": {
                    "eligible": True,
                    "scope": "windows_task_contract",
                    "resources": [
                        "managed_windows_launcher",
                        "managed_windows_scheduled_tasks",
                        "manifest_artifacts",
                    ],
                    "evidence": ["managed_task_action_identity"],
                    "reasons": [],
                },
            }
            with (
                patch.object(manager, "_is_windows", return_value=True),
                patch.object(
                    manager,
                    "_read_only_reconcile_plan",
                    return_value=(authorized_plan, 1),
                ),
                patch.object(manager, "_safe_reconcile", return_value=applied),
                patch.object(
                    manager,
                    "_manifest_contract",
                    side_effect=[
                        {"ok": True},
                        {"ok": False, "windows_task_contract": {"migration_required": True}},
                        {"ok": True},
                    ],
                ),
                patch.object(manager, "_supervisor_contract", return_value={"ok": False, "reasons": ["health:trigger"]}),
                patch.object(manager, "_upstream_status_evidence", return_value={"ok": True}),
                patch.object(manager, "readyz", return_value={"ok": True}),
                patch.object(manager, "smoke", return_value={"ok": True, "sentinel_found": True}),
            ):
                code, output = self._run_main(
                    ["reconcile", "--runtime-root", str(root), "--apply", "--json"]
                )
        self.assertEqual(code, 2)
        payload = json.loads(output)
        self.assertEqual(payload["decision"], "RECONCILE_PARTIAL")
        self.assertEqual(payload["supervisor"]["reasons"], ["health:trigger"])

    def test_status_surfaces_windows_migration_required(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "runtime"
            manager._ensure_marker(root)
            state = self._state(root)
            state.preset = "persistent-task"
            manager._write_state(root, state)
            self._fake_cli(state)
            contract = {
                "ok": False,
                "windows_task_contract": {
                    "ok": False,
                    "migration_required": True,
                    "reasons": ["launcher_artifact"],
                },
            }
            with (
                patch.object(manager, "_manifest_contract", return_value=contract),
                patch.object(manager, "readyz", return_value={"ok": True}),
                patch.object(manager, "_upstream_status_evidence", return_value={"ok": True}),
                patch.object(manager, "_supervisor_contract", return_value={"ok": False}),
            ):
                code, output = self._run_main(
                    ["status", "--runtime-root", str(root), "--json"]
                )
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertEqual(payload["decision"], "RUNTIME_PARTIAL")
        self.assertTrue(payload["migration_required"])


if __name__ == "__main__":
    unittest.main()
