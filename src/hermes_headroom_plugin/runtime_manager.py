"""Portable, explicit Headroom runtime lifecycle manager.

This module is packaged with the Hermes plugin. It installs the official
Headroom distribution into an isolated venv, delegates persistent lifecycle to
Headroom's native ``install`` subsystem, and verifies the Hermes
compress -> retrieve contract. Plugin registration never invokes this module.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from .proxy import readyz, smoke

RUNTIME_VERSION = "0.32.1"
LITELLM_VERSION = "1.94.0rc3"
DEFAULT_HEADROOM_SPEC = f"headroom-ai[proxy]=={RUNTIME_VERSION}"
DEFAULT_LITELLM_SPEC = f"litellm=={LITELLM_VERSION}"
PYPI_INDEX_URL = "https://pypi.org/simple"
_OPERATOR_RE = r"(?:===|==|~=|!=|<=|>=|<|>)"
_VERSION_TOKEN_RE = r"[A-Za-z0-9][A-Za-z0-9.*+!_-]*"
_SPECIFIER_RE = rf"{_OPERATOR_RE}{_VERSION_TOKEN_RE}(?:,{_OPERATOR_RE}{_VERSION_TOKEN_RE})*"
_HEADROOM_SPEC_RE = re.compile(rf"headroom-ai\[proxy\]{_SPECIFIER_RE}")
_LITELLM_SPEC_RE = re.compile(rf"litellm{_SPECIFIER_RE}")
DEFAULT_PROFILE = "hermes-plugin"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_CCR_BACKEND = "memory"
DEFAULT_CCR_TTL_SECONDS = 1800
STATE_SCHEMA = 1
STATE_FILE = "manager-state.json"
MARKER_FILE = ".hermes-headroom-runtime-manager"
LOCK_FILE = ".setup.lock"
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_UPSTREAM_STATUS_FIELDS = frozenset(
    {"profile", "preset", "runtime", "supervisor", "scope", "port", "status", "healthy"}
)
_WINDOWS_TASK_CONTRACT = "hermes-windows-task-v1"
_WINDOWS_LAUNCHER_NAME = "ensure-headroom-hidden.vbs"


_WINDOWS_TASK_OVERLAY_SCRIPT = r"""
import hashlib
import copy
import os
import subprocess
import sys
import tempfile

from headroom.install.models import ArtifactRecord
from headroom.install.paths import profile_root, windows_ensure_cmd_path

_CONTRACT = "hermes-windows-task-v1"
_LAUNCHER_NAME = "ensure-headroom-hidden.vbs"


def _launcher_content(command):
    escaped = str(command).replace('"', '""')
    command_line = f'command = "cmd.exe /d /c ""{escaped}""' + '"\r\n'
    return (
        "Option Explicit\r\n"
        "Dim shell, command, exitCode\r\n"
        'Set shell = CreateObject("WScript.Shell")\r\n'
        + command_line
        + "exitCode = shell.Run(command, 0, True)\r\n"
        + "WScript.Quit exitCode\r\n"
    )


def _task_specs(manifest, launcher):
    action = f'wscript.exe //B //NoLogo "{launcher}"'
    service = manifest.service_name
    return [
        (f"{service}-startup", ["/SC", "ONSTART"], {"kind": "boot"}, action),
        (f"{service}-health", ["/SC", "MINUTE", "/MO", "5"], {"kind": "interval", "minutes": 5}, action),
    ]


def _snapshot_supervisor(specs, launcher):
    tasks = {}
    for name, _schedule, _trigger, _action in specs:
        proc = subprocess.run(
            ["schtasks", "/Query", "/TN", name, "/XML"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError(
                f"cannot safely snapshot scheduled task before mutation: {name}"
            )
        tasks[name] = proc.stdout
    return {
        "tasks": tasks,
        "launcher": launcher.read_bytes() if launcher.is_file() else None,
    }


def _restore_supervisor_snapshot(snapshot, launcher):
    errors = []
    for name, xml_payload in snapshot["tasks"].items():
        if not xml_payload:
            errors.append(f"empty-snapshot:{name}")
            continue
        xml_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as handle:
                handle.write(xml_payload)
                xml_path = handle.name
            subprocess.run(
                ["schtasks", "/Create", "/TN", name, "/XML", xml_path, "/F"],
                check=True,
            )
        except Exception:
            errors.append(f"restore:{name}")
        finally:
            if xml_path is not None:
                try:
                    os.unlink(xml_path)
                except OSError:
                    errors.append(f"cleanup:{name}")
    old_launcher = snapshot["launcher"]
    if old_launcher is None:
        launcher.unlink(missing_ok=True)
    else:
        launcher.write_bytes(old_launcher)
    if errors:
        raise RuntimeError("Windows task rollback failed: " + ",".join(errors))


def _install_silent_windows_tasks(manifest, records, return_snapshot=False):
    if not sys.platform.startswith("win") or manifest.supervisor_kind != "task":
        return records
    launcher = profile_root(manifest.profile) / _LAUNCHER_NAME
    launcher_bytes = _launcher_content(windows_ensure_cmd_path(manifest.profile)).encode("utf-16")
    launcher_hash = hashlib.sha256(launcher_bytes).hexdigest()
    user_args = ["/RU", "SYSTEM"] if manifest.scope == "system" else []
    specs = _task_specs(manifest, launcher)
    snapshot = _snapshot_supervisor(specs, launcher)
    launcher.write_bytes(launcher_bytes)
    try:
        for name, schedule, _trigger, action in specs:
            subprocess.run(
                ["schtasks", "/Create", "/TN", name, "/TR", action, *schedule, "/F", *user_args],
                check=True,
            )
    except Exception:
        _restore_supervisor_snapshot(snapshot, launcher)
        raise

    task_metadata = {
        name: {
            "contract": _CONTRACT,
            "action": {"command": "wscript.exe", "arguments": f'//B //NoLogo "{launcher}"'},
            "trigger": trigger,
            "enabled": True,
        }
        for name, _schedule, trigger, _action in specs
    }
    found = set()
    filtered = []
    for record in records:
        if record.kind == "script" and record.path == str(launcher):
            continue
        if record.kind == "windows-task" and record.path in task_metadata:
            record.metadata = task_metadata[record.path]
            found.add(record.path)
        filtered.append(record)
    if found != set(task_metadata):
        _restore_supervisor_snapshot(snapshot, launcher)
        raise RuntimeError("upstream task artifacts did not match the managed Windows task names")
    filtered.append(
        ArtifactRecord(
            kind="script",
            path=str(launcher),
            metadata={"contract": _CONTRACT, "sha256": launcher_hash},
        )
    )
    if return_snapshot:
        return filtered, snapshot
    return filtered
"""

# Headroom 0.32's public ``install apply`` always writes persistent shell
# environment blocks for user/system scope, even with manual providers and no
# targets. This pinned helper keeps upstream manifest/supervisor/runtime code
# but deliberately omits only provider/shell mutation activation.
_SAFE_APPLY_SCRIPT = _WINDOWS_TASK_OVERLAY_SCRIPT + r"""
import json
import sys

from headroom.cli.install import (
    _build_deployment_manifest,
    _remove_deployment,
    _start_deployment,
)
from headroom.install.state import ManifestError, load_manifest, save_manifest
from headroom.install.supervisors import install_supervisor

cfg = json.load(sys.stdin)
manifest = _build_deployment_manifest(
    profile=cfg["profile"],
    preset=cfg["preset"],
    runtime="python",
    scope="user",
    provider_mode="manual",
    targets=(),
    port=int(cfg["port"]),
    backend="anthropic",
    anyllm_provider=None,
    region=None,
    proxy_mode="token",
    memory=False,
    telemetry=False,
    no_telemetry=True,
    image="ghcr.io/headroomlabs-ai/headroom:latest",
    no_http2=False,
    code_aware=False,
    intercept_tool_results=False,
    protect_tool_results=None,
    bedrock_profile=None,
    extra_env=cfg["extra_env"],
)
if manifest.provider_mode != "manual" or manifest.targets:
    raise RuntimeError("unsafe provider-selection plan")
manifest.mutations = []
try:
    existing = load_manifest(manifest.profile)
except ManifestError as exc:
    raise RuntimeError(f"existing manifest is corrupt: {exc}") from exc
if existing is not None:
    raise RuntimeError(
        "a deployment manifest already exists; run explicit uninstall before setup"
    )
try:
    manifest.artifacts = install_supervisor(manifest)
    manifest.artifacts = _install_silent_windows_tasks(manifest, manifest.artifacts)
    save_manifest(manifest)
    _start_deployment(manifest)
except Exception as exc:
    try:
        _remove_deployment(manifest)
    except Exception as cleanup_exc:
        raise RuntimeError(
            f"lifecycle apply failed and rollback was incomplete: remove-new: {cleanup_exc}"
        ) from exc
    raise
saved = load_manifest(manifest.profile)
if saved is None or saved.mutations or saved.targets or saved.provider_mode != "manual":
    _remove_deployment(manifest)
    raise RuntimeError("saved manifest failed no-mutation invariants")
print(json.dumps({
    "profile": saved.profile,
    "provider_mode": saved.provider_mode,
    "targets": saved.targets,
    "mutations": saved.mutations,
    "artifacts": [item.path for item in saved.artifacts],
}, sort_keys=True))
"""


_SAFE_RECONCILE_SCRIPT = _WINDOWS_TASK_OVERLAY_SCRIPT + r"""
import json
import sys

from headroom.install.state import ManifestError, load_manifest, save_manifest

cfg = json.load(sys.stdin)
try:
    manifest = load_manifest(cfg["profile"])
except ManifestError as exc:
    raise RuntimeError(f"existing manifest is corrupt: {exc}") from exc
if manifest is None:
    raise RuntimeError("managed deployment manifest is missing")
if (
    not sys.platform.startswith("win")
    or manifest.supervisor_kind != "task"
    or manifest.scope != "user"
    or manifest.provider_mode != "manual"
    or manifest.targets
    or manifest.mutations
):
    raise RuntimeError("manifest is not an eligible manager-owned Windows task deployment")
original_artifacts = copy.deepcopy(manifest.artifacts)
launcher = profile_root(manifest.profile) / _LAUNCHER_NAME
try:
    manifest.artifacts, supervisor_snapshot = _install_silent_windows_tasks(
        manifest, manifest.artifacts, return_snapshot=True
    )
    save_manifest(manifest)
    saved = load_manifest(manifest.profile)
    owned = [
        item for item in (saved.artifacts if saved is not None else [])
        if item.metadata.get("contract") == _CONTRACT
    ]
    if saved is None or len(owned) != 3:
        raise RuntimeError("reconciled manifest did not persist the complete Windows task contract")
except Exception:
    if "supervisor_snapshot" in locals():
        _restore_supervisor_snapshot(supervisor_snapshot, launcher)
    manifest.artifacts = original_artifacts
    save_manifest(manifest)
    raise
print(json.dumps({"profile": saved.profile, "contract": _CONTRACT, "owned_artifacts": len(owned)}, sort_keys=True))
"""


@dataclass
class RuntimeState:
    schema: int
    status: str
    runtime_version: str
    litellm_version: str
    headroom_spec: str
    litellm_spec: str
    profile: str
    host: str
    port: int
    preset: str
    runtime_root: str
    venv_dir: str
    workspace_dir: str
    proxy_url: str
    updated_at: str
    last_error: str | None = None


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def default_runtime_root() -> Path:
    configured = os.environ.get("HEADROOM_RUNTIME_ROOT")
    return Path(configured).expanduser() if configured else _hermes_home() / "runtimes" / "headroom"


def _venv_dir(runtime_root: Path) -> Path:
    return runtime_root / f"venv-{RUNTIME_VERSION}"


def _workspace_dir(runtime_root: Path) -> Path:
    return runtime_root / "workspace"


def _bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def _exe(venv_dir: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return _bin_dir(venv_dir) / f"{name}{suffix}"


def _validate_profile(profile: str) -> str:
    if not _PROFILE_RE.fullmatch(profile):
        raise ValueError("profile must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
    return profile


def _validate_port(port: int) -> int:
    if not 1 <= int(port) <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return int(port)


def _validate_package_spec(spec: str, *, package: str) -> str:
    pattern = _HEADROOM_SPEC_RE if package == "headroom-ai" else _LITELLM_SPEC_RE
    if not isinstance(spec, str) or not pattern.fullmatch(spec):
        raise ValueError(
            f"{package} must use a package-name plus version specifier; URLs, paths, markers, and credentials are unsupported"
        )
    return spec


def _resolve_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    forbidden = {
        Path(root.anchor),
        Path.home().resolve(),
        _hermes_home().resolve(),
        Path(tempfile.gettempdir()).resolve(),
    }
    if root in forbidden or len(root.parts) < 3:
        raise ValueError(
            "runtime root must be a dedicated leaf directory, not a filesystem, home, Hermes, or shared-temp root"
        )
    return root


def _default_preset() -> str:
    return "persistent-task" if sys.platform.startswith("win") else "persistent-service"


def _proxy_url(port: int) -> str:
    return f"http://{DEFAULT_HOST}:{port}"


def _state_path(root: Path) -> Path:
    return root / STATE_FILE


def _path_identity_equal(left: str | Path, right: str | Path) -> bool:
    a = Path(left)
    b = Path(right)
    try:
        return a.samefile(b)
    except (FileNotFoundError, OSError):
        return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(
            os.path.normpath(str(b))
        )


def _load_state(root: Path) -> RuntimeState | None:
    path = _state_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        state = RuntimeState(**data)
        _validate_profile(state.profile)
        _validate_port(state.port)
        expected = {
            "schema": STATE_SCHEMA,
            "host": DEFAULT_HOST,
            "runtime_root": str(root),
            "venv_dir": str(Path(state.runtime_root) / _venv_dir(root).name),
            "workspace_dir": str(Path(state.runtime_root) / _workspace_dir(root).name),
            "proxy_url": _proxy_url(state.port),
        }
        actual = {
            "schema": state.schema,
            "host": state.host,
            "runtime_root": state.runtime_root,
            "venv_dir": state.venv_dir,
            "workspace_dir": state.workspace_dir,
            "proxy_url": state.proxy_url,
        }
        scalar_match = all(
            actual[key] == expected[key] for key in ("schema", "host", "proxy_url")
        )
        path_match = all(
            _path_identity_equal(actual[key], expected[key])
            for key in ("runtime_root", "venv_dir", "workspace_dir")
        )
        if not (scalar_match and path_match):
            raise ValueError(f"state identity mismatch: expected {expected}, got {actual}")
        if state.preset not in {"persistent-service", "persistent-task"}:
            raise ValueError(f"unsupported state preset: {state.preset}")
        return state
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"manager state is invalid: {path}: {exc}") from exc


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(path)


def _write_state(root: Path, state: RuntimeState) -> None:
    _write_private(_state_path(root), json.dumps(asdict(state), indent=2, sort_keys=True) + "\n")


def _ensure_marker(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    marker = root / MARKER_FILE
    if marker.exists():
        if marker.read_text(encoding="utf-8").strip() != str(STATE_SCHEMA):
            raise RuntimeError(f"runtime root marker mismatch: {marker}")
        return
    _write_private(marker, f"{STATE_SCHEMA}\n")


def _safe_to_purge(root: Path) -> bool:
    marker = root / MARKER_FILE
    try:
        return marker.is_file() and marker.read_text(encoding="utf-8").strip() == str(STATE_SCHEMA)
    except OSError:
        return False


def _root_claim_contract(root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True, "runtime_root": str(root)}
    if not root.exists():
        return result
    if not root.is_dir() or root.is_symlink():
        return {
            **result,
            "ok": False,
            "detail": "runtime root exists but is not a dedicated real directory",
        }
    marker = root / MARKER_FILE
    state = _state_path(root)
    if marker.exists() or state.exists():
        return result
    try:
        entries = sorted(item.name for item in root.iterdir())
    except OSError as exc:
        return {**result, "ok": False, "detail": f"runtime root cannot be inspected: {exc}"}
    if entries:
        return {
            **result,
            "ok": False,
            "detail": "existing non-empty runtime root is not manager-owned",
            "unexpected_entries": entries[:20],
        }
    return result


def _managed_root_contract(root: Path, state: RuntimeState) -> dict[str, Any]:
    expected_dirs = {
        Path(state.venv_dir).name: Path(state.venv_dir),
        Path(state.workspace_dir).name: Path(state.workspace_dir),
    }
    expected_files = {STATE_FILE, MARKER_FILE, "manager.log", "install.log"}
    allowed = set(expected_dirs) | expected_files
    try:
        entries = {item.name: item for item in root.iterdir()}
    except OSError as exc:
        return {"ok": False, "detail": f"runtime root cannot be inspected: {exc}"}
    unexpected = sorted(set(entries) - allowed)
    invalid: list[str] = []
    for name, expected in expected_dirs.items():
        item = entries.get(name)
        if item is None:
            continue
        if item.is_symlink() or not item.is_dir() or not _path_identity_equal(item, expected):
            invalid.append(name)
    for name in expected_files:
        item = entries.get(name)
        if item is not None and (item.is_symlink() or not item.is_file()):
            invalid.append(name)
    ok = not unexpected and not invalid
    return {
        "ok": ok,
        "unexpected_entries": unexpected[:20],
        "invalid_entries": sorted(invalid),
        "allowed_entries": sorted(allowed),
        "detail": None if ok else "runtime root contains entries outside the manager-owned deletion contract",
    }


def _rmtree_managed_directory(root: Path, directory: Path) -> None:
    if os.name != "nt" and shutil.rmtree.avoids_symlink_attacks:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        root_fd = os.open(root, flags)
        try:
            shutil.rmtree(directory.name, dir_fd=root_fd)
        finally:
            os.close(root_fd)
        return
    shutil.rmtree(directory)


def _purge_managed_root(root: Path, state: RuntimeState) -> dict[str, Any]:
    contract = _managed_root_contract(root, state)
    if not contract.get("ok"):
        return contract
    try:
        for directory in (Path(state.venv_dir), Path(state.workspace_dir)):
            contract = _managed_root_contract(root, state)
            if not contract.get("ok"):
                return contract
            if directory.exists():
                _rmtree_managed_directory(root, directory)
        contract = _managed_root_contract(root, state)
        if not contract.get("ok"):
            return contract
        for name in ("manager.log", "install.log", STATE_FILE, MARKER_FILE):
            path = root / name
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        root.rmdir()
    except (NotImplementedError, OSError) as exc:
        return {
            **contract,
            "ok": False,
            "fail_closed": True,
            "detail": f"managed-root deletion stopped safely: {type(exc).__name__}: {exc}",
        }
    return {**contract, "ok": True, "runtime_root_removed": True}


def _lock_path(root: Path) -> Path:
    return root.parent / f".{root.name}{LOCK_FILE}"


def _acquire_lock(root: Path) -> int:
    path = _lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"another runtime-manager operation is active: {path}") from exc


def _release_lock(root: Path, fd: int) -> None:
    try:
        os.close(fd)
    finally:
        try:
            _lock_path(root).unlink()
        except FileNotFoundError:
            pass


def _append_log(log: Path, command: Sequence[str], output: str) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    display = list(command)
    if "-c" in display:
        index = display.index("-c")
        if index + 1 < len(display):
            display[index + 1] = "<embedded-python>"
    with log.open("a", encoding="utf-8") as handle:
        handle.write("\n$ " + " ".join(display) + "\n")
        handle.write(output)
        if output and not output.endswith("\n"):
            handle.write("\n")
    try:
        log.chmod(0o600)
    except OSError:
        pass


def _run(
    command: Sequence[str],
    *,
    timeout: int,
    log: Path | None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=timeout,
        env=env,
        input=input_text,
    )
    if log is not None:
        _append_log(log, command, proc.stdout)
    return proc


def _isolated_python_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in {"PYTHONHOME", "PYTHONPATH"}
    }


def _runtime_env(root: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in _isolated_python_env().items()
        if not key.upper().startswith("HEADROOM_")
    }
    env.update(
        {
            "HEADROOM_WORKSPACE_DIR": str(_workspace_dir(root)),
            "HEADROOM_TELEMETRY": "off",
            "HEADROOM_DISABLE_UPDATE_CHECK": "1",
            "HEADROOM_CCR_BACKEND": DEFAULT_CCR_BACKEND,
            "HEADROOM_CCR_TTL_SECONDS": str(DEFAULT_CCR_TTL_SECONDS),
        }
    )
    return env


def _is_windows() -> bool:
    return os.name == "nt"


def _windows_launcher_path(root: Path, profile: str) -> Path:
    return _manifest_path(root, profile).parent / _WINDOWS_LAUNCHER_NAME


def _windows_ensure_command_path(root: Path, profile: str) -> Path:
    return _manifest_path(root, profile).parent / "ensure-headroom.cmd"


def _windows_launcher_content(command: Path) -> str:
    escaped = str(command).replace('"', '""')
    command_line = f'command = "cmd.exe /d /c ""{escaped}""' + '"\r\n'
    return (
        "Option Explicit\r\n"
        "Dim shell, command, exitCode\r\n"
        'Set shell = CreateObject("WScript.Shell")\r\n'
        + command_line
        + "exitCode = shell.Run(command, 0, True)\r\n"
        + "WScript.Quit exitCode\r\n"
    )


def _windows_launcher_bytes(root: Path, profile: str) -> bytes:
    return _windows_launcher_content(_windows_ensure_command_path(root, profile)).encode("utf-16")


def _windows_task_arguments(launcher: Path) -> str:
    return f'//B //NoLogo "{launcher}"'


def _windows_task_metadata(root: Path, profile: str) -> dict[str, dict[str, Any]]:
    launcher = _windows_launcher_path(root, profile)
    service = f"headroom-{profile}"
    common = {
        "contract": _WINDOWS_TASK_CONTRACT,
        "action": {"command": "wscript.exe", "arguments": _windows_task_arguments(launcher)},
        "enabled": True,
    }
    return {
        f"{service}-startup": {**common, "trigger": {"kind": "boot"}},
        f"{service}-health": {
            **common,
            "trigger": {"kind": "interval", "minutes": 5},
        },
    }


def _windows_manifest_task_contract(
    root: Path, *, profile: str, data: dict[str, Any]
) -> dict[str, Any]:
    reasons: list[str] = []
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list):
        artifacts = []
        reasons.append("artifacts_missing")
    launcher = _windows_launcher_path(root, profile)
    expected_hash = hashlib.sha256(_windows_launcher_bytes(root, profile)).hexdigest()
    launcher_records = [
        item
        for item in artifacts
        if isinstance(item, dict)
        and item.get("kind") == "script"
        and _path_identity_equal(item.get("path", ""), launcher)
    ]
    if len(launcher_records) != 1:
        reasons.append("launcher_artifact")
    else:
        metadata = launcher_records[0].get("metadata")
        if not isinstance(metadata, dict) or metadata != {
            "contract": _WINDOWS_TASK_CONTRACT,
            "sha256": expected_hash,
        }:
            reasons.append("launcher_metadata")
    if not launcher.is_file():
        reasons.append("launcher_missing")
    else:
        try:
            actual_hash = hashlib.sha256(launcher.read_bytes()).hexdigest()
        except OSError:
            actual_hash = ""
        if actual_hash != expected_hash:
            reasons.append("launcher_hash")

    expected_tasks = _windows_task_metadata(root, profile)
    for task_name, expected_metadata in expected_tasks.items():
        records = [
            item
            for item in artifacts
            if isinstance(item, dict)
            and item.get("kind") == "windows-task"
            and str(item.get("path", "")).casefold() == task_name.casefold()
        ]
        if len(records) != 1:
            reasons.append(f"task_artifact:{task_name.rsplit('-', 1)[-1]}")
            continue
        if records[0].get("metadata") != expected_metadata:
            reasons.append(f"task_metadata:{task_name.rsplit('-', 1)[-1]}")
    return {
        "ok": not reasons,
        "contract": _WINDOWS_TASK_CONTRACT,
        "migration_required": bool(reasons),
        "launcher_present": launcher.is_file(),
        "reasons": reasons,
    }


def _windows_migration_required(manifest_contract: dict[str, Any]) -> bool:
    windows_contract = manifest_contract.get("windows_task_contract")
    return bool(
        isinstance(windows_contract, dict)
        and windows_contract.get("migration_required") is True
    )


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_direct_children(parent: ET.Element | None, name: str) -> list[ET.Element]:
    if parent is None:
        return []
    return [item for item in parent if _xml_local_name(item.tag) == name]


def _xml_direct_text(parent: ET.Element, name: str) -> str:
    child = next((item for item in parent if _xml_local_name(item.tag) == name), None)
    return (child.text or "").strip() if child is not None else ""


def _windows_arguments_match(value: str, launcher: Path) -> bool:
    match = re.fullmatch(
        r'\s*//B\s+//NoLogo\s+(?:"([^"]+)"|(\S+))\s*',
        value,
        flags=re.IGNORECASE,
    )
    if match is None:
        return False
    return _path_identity_equal(match.group(1) or match.group(2), launcher)


def _parse_windows_task_xml(
    xml_text: str | bytes,
    *,
    launcher: Path,
    trigger_kind: str,
    legacy_launcher: Path | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    candidates: list[str] = []
    if isinstance(xml_text, str):
        candidates.append(xml_text)
    else:
        for encoding in (
            "utf-16",
            "utf-16-le",
            "utf-16-be",
            "utf-8-sig",
            "mbcs",
            "cp1252",
        ):
            try:
                decoded = xml_text.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
            if decoded not in candidates:
                candidates.append(decoded)
    root: ET.Element | None = None
    for candidate in candidates:
        normalized = candidate.lstrip("\ufeff \t\r\n")
        try:
            root = ET.fromstring(normalized)
            break
        except ET.ParseError:
            continue
    if root is None:
        return {"ok": False, "exists": True, "reasons": ["invalid_xml"]}
    if _xml_local_name(root.tag) != "Task":
        reasons.append("document_root")

    settings_nodes = _xml_direct_children(root, "Settings")
    settings = settings_nodes[0] if len(settings_nodes) == 1 else None
    if settings is None:
        reasons.append("settings_structure")
    enabled_text = _xml_direct_text(settings, "Enabled") if settings is not None else "false"
    enabled = settings is not None and enabled_text.casefold() != "false"
    if not enabled:
        reasons.append("disabled")

    actions_nodes = _xml_direct_children(root, "Actions")
    actions = actions_nodes[0] if len(actions_nodes) == 1 else None
    action_children = list(actions) if actions is not None else []
    exec_nodes = _xml_direct_children(actions, "Exec")
    action_structure_exact = bool(
        actions is not None
        and len(action_children) == 1
        and len(exec_nodes) == 1
    )
    exec_node = exec_nodes[0] if action_structure_exact else None
    command = _xml_direct_text(exec_node, "Command") if exec_node is not None else ""
    arguments = _xml_direct_text(exec_node, "Arguments") if exec_node is not None else ""
    command_name = re.split(r"[\\/]", command.strip().strip('"'))[-1].casefold()
    action_command_exact = action_structure_exact and command_name == "wscript.exe"
    action_arguments_exact = action_structure_exact and _windows_arguments_match(arguments, launcher)
    legacy_action_command_exact = bool(
        legacy_launcher is not None
        and action_structure_exact
        and _path_identity_equal(command.strip().strip('"'), legacy_launcher)
    )
    legacy_action_arguments_exact = bool(
        legacy_action_command_exact and not arguments.strip()
    )
    if not action_structure_exact:
        reasons.append("action_structure")
    if not action_command_exact:
        reasons.append("action_command")
    if not action_arguments_exact:
        reasons.append("action_arguments")

    triggers_nodes = _xml_direct_children(root, "Triggers")
    triggers = triggers_nodes[0] if len(triggers_nodes) == 1 else None
    trigger_children = list(triggers) if triggers is not None else []
    trigger_exact = False
    if trigger_kind == "startup":
        trigger_nodes = _xml_direct_children(triggers, "BootTrigger")
        trigger = trigger_nodes[0] if len(trigger_nodes) == 1 else None
        trigger_exact = bool(
            triggers is not None
            and len(trigger_children) == 1
            and trigger is not None
            and _xml_direct_text(trigger, "Enabled").casefold() != "false"
        )
    elif trigger_kind == "health":
        trigger_nodes = _xml_direct_children(triggers, "TimeTrigger")
        trigger = trigger_nodes[0] if len(trigger_nodes) == 1 else None
        repetition_nodes = _xml_direct_children(trigger, "Repetition")
        repetition = repetition_nodes[0] if len(repetition_nodes) == 1 else None
        interval_nodes = _xml_direct_children(repetition, "Interval")
        interval = interval_nodes[0] if len(interval_nodes) == 1 else None
        trigger_exact = bool(
            triggers is not None
            and len(trigger_children) == 1
            and trigger is not None
            and _xml_direct_text(trigger, "Enabled").casefold() != "false"
            and interval is not None
            and (interval.text or "").strip().upper() == "PT5M"
        )
    if not trigger_exact:
        reasons.append("trigger")
    return {
        "ok": not reasons,
        "exists": True,
        "enabled": enabled,
        "action_command_exact": action_command_exact,
        "action_arguments_exact": action_arguments_exact,
        "legacy_action_command_exact": legacy_action_command_exact,
        "legacy_action_arguments_exact": legacy_action_arguments_exact,
        "managed_action_identity": bool(
            (action_command_exact and action_arguments_exact)
            or (legacy_action_command_exact and legacy_action_arguments_exact)
        ),
        "trigger_exact": trigger_exact,
        "reasons": reasons,
    }


def _query_windows_task_xml(task_name: str) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name, "/XML"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"exists": False, "xml": "", "reason": "query_failed"}
    if proc.returncode != 0:
        return {"exists": False, "xml": "", "reason": "not_found"}
    output = proc.stdout if isinstance(proc.stdout, bytes) else str(proc.stdout).encode()
    return {"exists": True, "xml": output}


def _probe_exists(command: Sequence[str]) -> bool:
    try:
        proc = subprocess.run(
            list(command), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _supervisor_presence(profile: str) -> dict[str, Any]:
    service_name = f"headroom-{profile}"
    evidence: list[str] = []
    if os.name == "nt":
        if _probe_exists(["sc.exe", "query", service_name]):
            evidence.append(f"windows-service:{service_name}")
        for task_name in (f"{service_name}-startup", f"{service_name}-health"):
            if _probe_exists(["schtasks", "/Query", "/TN", task_name]):
                evidence.append(f"scheduled-task:{task_name}")
    elif sys.platform == "darwin":
        label = f"com.headroom.{profile}"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        if plist.exists():
            evidence.append(f"plist:{plist}")
        if _probe_exists(["launchctl", "print", f"gui/{os.getuid()}/{label}"]):
            evidence.append(f"launchd:{label}")
    else:
        unit = f"{service_name}.service"
        unit_path = Path.home() / ".config" / "systemd" / "user" / unit
        if unit_path.exists():
            evidence.append(f"systemd-unit:{unit_path}")
        if _probe_exists(["systemctl", "--user", "is-enabled", unit]):
            evidence.append(f"systemd-enabled:{unit}")
        try:
            cron = subprocess.run(
                ["crontab", "-l"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, check=False, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            cron = None
        if cron is not None and f"# >>> headroom {profile} >>>" in cron.stdout:
            evidence.append(f"crontab:{profile}")
    return {"present": bool(evidence), "service_name": service_name, "evidence": evidence}


def _supervisor_contract(root: Path, *, profile: str, preset: str) -> dict[str, Any]:
    presence = _supervisor_presence(profile)
    if not (_is_windows() and preset == "persistent-task"):
        return {**presence, "ok": presence.get("present") is True, "reasons": []}
    path = _manifest_path(root, profile)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    manifest_contract = _windows_manifest_task_contract(root, profile=profile, data=data)
    launcher = _windows_launcher_path(root, profile)
    legacy_launcher = _windows_ensure_command_path(root, profile)
    service = f"headroom-{profile}"
    tasks: dict[str, Any] = {}
    evidence: list[str] = []
    reasons: list[str] = []
    if any(str(item).startswith("windows-service:") for item in presence.get("evidence", [])):
        reasons.append("unexpected_windows_service")
    for kind, task_name in (
        ("startup", f"{service}-startup"),
        ("health", f"{service}-health"),
    ):
        queried = _query_windows_task_xml(task_name)
        if not queried.get("exists"):
            parsed = {"ok": False, "exists": False, "reasons": [queried.get("reason", "not_found")]}
        else:
            evidence.append(f"scheduled-task:{task_name}")
            xml_payload = queried.get("xml", b"")
            if not isinstance(xml_payload, (str, bytes)):
                xml_payload = b""
            parsed = _parse_windows_task_xml(
                xml_payload,
                launcher=launcher,
                trigger_kind=kind,
                legacy_launcher=legacy_launcher,
            )
        tasks[kind] = parsed
        reasons.extend(f"{kind}:{reason}" for reason in parsed.get("reasons", []))
    if not manifest_contract.get("ok"):
        reasons.extend(
            f"manifest:{reason}" for reason in manifest_contract.get("reasons", [])
        )
    present = all(task.get("exists") is True for task in tasks.values())
    combined_evidence = sorted(set(presence.get("evidence", [])) | set(evidence))
    return {
        "ok": not reasons,
        "present": present,
        "service_name": service,
        "evidence": combined_evidence,
        "tasks": tasks,
        "manifest": manifest_contract,
        "migration_required": manifest_contract.get("migration_required") is True,
        "reasons": reasons,
    }


def _parse_upstream_status(
    output: str, *, profile: str, preset: str, port: int
) -> dict[str, Any]:
    """Parse Headroom 0.32.1 human status output into fail-closed evidence."""

    fields: dict[str, str] = {}
    duplicates: list[str] = []
    for raw_line in output.splitlines():
        line = _ANSI_ESCAPE_RE.sub("", raw_line)
        match = re.match(r"^\s*([A-Za-z]+)\s*:\s*(.*?)\s*$", line)
        if match is None:
            continue
        key = match.group(1).casefold()
        if key not in _UPSTREAM_STATUS_FIELDS:
            continue
        if key in fields:
            duplicates.append(key)
            continue
        fields[key] = match.group(2).strip()

    expected = {
        "profile": profile,
        "preset": preset,
        "runtime": "python",
        "supervisor": "service" if preset == "persistent-service" else "task",
        "scope": "user",
        "port": str(port),
        "status": "running",
        "healthy": "yes",
    }
    reasons: list[str] = []
    missing = sorted(_UPSTREAM_STATUS_FIELDS.difference(fields))
    if missing:
        reasons.append("missing_fields:" + ",".join(missing))
    if duplicates:
        reasons.append("duplicate_fields:" + ",".join(sorted(set(duplicates))))
    for key, expected_value in expected.items():
        actual = fields.get(key)
        if actual is None:
            continue
        matches = actual == expected_value if key == "profile" else actual.casefold() == expected_value
        if not matches:
            reasons.append(f"mismatch:{key}")
    return {
        "ok": not reasons,
        "fields": fields,
        "expected": expected,
        "reasons": reasons,
    }


def _upstream_status_evidence(
    *,
    headroom: Path,
    root: Path,
    profile: str,
    preset: str,
    port: int,
    timeout: int,
    write_log: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": headroom.is_file(),
        "exit_code": None,
        "output": "",
        "semantic": {
            "ok": False,
            "fields": {},
            "expected": {},
            "reasons": ["managed_cli_missing"],
        },
        "ok": False,
    }
    if not headroom.is_file():
        return result
    runtime_env = _runtime_env(root)
    if not write_log:
        runtime_env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = _run(
        [str(headroom), "install", "status", "--profile", profile],
        timeout=timeout,
        log=root / "manager.log" if write_log else None,
        env=runtime_env,
    )
    semantic = _parse_upstream_status(
        proc.stdout, profile=profile, preset=preset, port=port
    )
    result.update(
        {
            "exit_code": proc.returncode,
            "output": proc.stdout[-2000:],
            "semantic": semantic,
            "ok": proc.returncode == 0 and semantic.get("ok") is True,
        }
    )
    return result


def _ensure_runtime(
    root: Path,
    *,
    python: str,
    headroom_spec: str,
    litellm_spec: str,
    timeout: int,
) -> tuple[Path, dict[str, str]]:
    venv_dir = _venv_dir(root)
    python_exe = _exe(venv_dir, "python")
    if not python_exe.is_file():
        created = _run(
            [python, "-m", "venv", str(venv_dir)],
            timeout=timeout,
            log=root / "install.log",
            env=_isolated_python_env(),
        )
        if created.returncode != 0:
            raise RuntimeError(f"runtime venv creation failed; see {root / 'install.log'}")
    pip = _exe(venv_dir, "pip")
    if not pip.is_file():
        raise RuntimeError(f"pip was not created in runtime venv: {pip}")
    log = root / "install.log"
    install = _run(
        [
            str(pip),
            "--isolated",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--index-url",
            PYPI_INDEX_URL,
            headroom_spec,
            litellm_spec,
        ],
        timeout=timeout,
        log=log,
        env=_isolated_python_env(),
    )
    if install.returncode != 0:
        raise RuntimeError(f"official runtime installation failed; see {log}")
    probe = _run(
        [
            str(python_exe),
            "-c",
            (
                "from importlib.metadata import version; "
                "print(version('headroom-ai')); print(version('litellm'))"
            ),
        ],
        timeout=30,
        log=log,
        env=_isolated_python_env(),
    )
    versions = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
    if probe.returncode != 0 or len(versions) < 2:
        raise RuntimeError(f"could not verify installed runtime versions; see {log}")
    resolved = {"headroom": versions[-2], "litellm": versions[-1]}
    if headroom_spec == DEFAULT_HEADROOM_SPEC and resolved["headroom"] != RUNTIME_VERSION:
        raise RuntimeError(f"resolved headroom-ai {resolved['headroom']}, expected {RUNTIME_VERSION}")
    if litellm_spec == DEFAULT_LITELLM_SPEC and resolved["litellm"] != LITELLM_VERSION:
        raise RuntimeError(f"resolved litellm {resolved['litellm']}, expected {LITELLM_VERSION}")
    return _exe(venv_dir, "headroom"), resolved


def _safe_apply_payload(*, root: Path, profile: str, port: int, preset: str) -> dict[str, Any]:
    return {
        "profile": profile,
        "port": port,
        "preset": preset,
        "extra_env": {
            "HEADROOM_WORKSPACE_DIR": str(_workspace_dir(root)),
            "HEADROOM_CCR_BACKEND": DEFAULT_CCR_BACKEND,
            "HEADROOM_CCR_TTL_SECONDS": str(DEFAULT_CCR_TTL_SECONDS),
            "HEADROOM_DISABLE_UPDATE_CHECK": "1",
        },
    }


def _safe_apply(
    *, root: Path, profile: str, port: int, preset: str, timeout: int
) -> subprocess.CompletedProcess[str]:
    python_exe = _exe(_venv_dir(root), "python")
    payload = _safe_apply_payload(root=root, profile=profile, port=port, preset=preset)
    return _run(
        [str(python_exe), "-c", _SAFE_APPLY_SCRIPT],
        timeout=timeout,
        log=root / "manager.log",
        env=_runtime_env(root),
        input_text=json.dumps(payload, sort_keys=True),
    )


def _safe_reconcile(*, root: Path, profile: str, timeout: int) -> subprocess.CompletedProcess[str]:
    python_exe = _exe(_venv_dir(root), "python")
    return _run(
        [str(python_exe), "-c", _SAFE_RECONCILE_SCRIPT],
        timeout=timeout,
        log=root / "manager.log",
        env=_runtime_env(root),
        input_text=json.dumps({"profile": profile}, sort_keys=True),
    )


def _tcp_port_open(port: int) -> bool:
    try:
        with socket.create_connection((DEFAULT_HOST, port), timeout=0.25):
            return True
    except OSError:
        return False


_WINDOWS_LISTENER_INVENTORY_PS = "\n".join(  # noqa: FLY002
    (
        "$ErrorActionPreference = 'Stop'",
        "$items = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |",
        "    Where-Object { $_.LocalPort -eq __PORT__ } |",
        "    Select-Object LocalAddress, LocalPort, OwningProcess)",
        "$rows = @()",
        "foreach ($item in $items) {",
        '    $proc = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $item.OwningProcess) -ErrorAction Stop',
        "    $rows += [PSCustomObject]@{",
        "        local_address = $item.LocalAddress",
        "        local_port = [int]$item.LocalPort",
        "        pid = [int]$item.OwningProcess",
        "        executable_path = [string]$proc.ExecutablePath",
        "        command_line = [string]$proc.CommandLine",
        "    }",
        "}",
        "@($rows) | ConvertTo-Json -Compress -Depth 4",
    )
)


def _windows_command_image(command_line: str) -> str:
    value = command_line.lstrip()
    if not value:
        return ""
    if value.startswith('"'):
        end = value.find('"', 1)
        if end < 0:
            return ""
        value = value[1:end]
    else:
        value = value.split(maxsplit=1)[0]
    return value.replace("/", "\\").casefold()


def _windows_venv_base_executables(venv_dir: Path) -> tuple[Path, ...]:
    try:
        lines = (venv_dir / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    for line in lines:
        key, separator, value = line.partition("=")
        candidate = value.strip()
        if (
            separator
            and key.strip().casefold() == "executable"
            and ntpath.isabs(candidate)
            and ntpath.basename(candidate).casefold() == "python.exe"
        ):
            return (Path(candidate),)
    return ()


def _windows_listener_inventory(
    *,
    port: int,
    expected_executables: Sequence[Path] | None,
    expected_venv_base_executables: Sequence[Path] = (),
    timeout: int,
) -> dict[str, Any]:
    """Inventory a Windows listener without connecting to the application."""

    selected_port = _validate_port(port)
    result: dict[str, Any] = {
        "method": "windows_os_socket_process_inventory",
        "probe_port": selected_port,
        "http_probe": "not_probed_read_only",
        "inventory_ok": False,
        "present": None,
        "records": [],
        "identity": {
            "proven": False,
            "expected_executables": (
                [str(path) for path in expected_executables]
                if expected_executables is not None
                else []
            ),
            "expected_venv_base_executables": [
                str(path) for path in expected_venv_base_executables
            ],
        },
    }
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        result["reason"] = "powershell_unavailable"
        return result
    script = _WINDOWS_LISTENER_INVENTORY_PS.replace("__PORT__", str(selected_port))
    proc = _run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=min(max(timeout, 1), 30),
        log=None,
        env=os.environ.copy(),
    )
    if proc.returncode != 0:
        result.update({"reason": "windows_socket_inventory_failed", "exit_code": proc.returncode})
        return result
    try:
        decoded = json.loads(proc.stdout.strip() or "[]")
    except json.JSONDecodeError:
        result["reason"] = "windows_socket_inventory_invalid_json"
        return result
    records = decoded if isinstance(decoded, list) else [decoded]
    raw_records = [record for record in records if isinstance(record, dict)]
    normalized: list[dict[str, Any]] = []
    command_lines: list[str] = []
    for record in raw_records:
        sanitized = dict(record)
        command_lines.append(str(sanitized.pop("command_line", "") or ""))
        normalized.append(sanitized)
    result.update({"inventory_ok": True, "present": bool(normalized), "records": normalized})
    if expected_executables is None:
        return result
    expected = {
        str(path).replace("/", "\\").casefold() for path in expected_executables
    }
    expected_venv_bases = {
        str(path).replace("/", "\\").casefold()
        for path in expected_venv_base_executables
    }
    matching: list[dict[str, Any]] = []
    match_basis: list[str] = []
    for index, record in enumerate(normalized):
        executable = str(record.get("executable_path") or "").replace("/", "\\").casefold()
        command_image = _windows_command_image(command_lines[index])
        basis = ""
        if executable in expected:
            basis = "managed_os_image"
        elif executable in expected_venv_bases and command_image in expected:
            basis = "venv_redirector_chain"
        if (
            str(record.get("local_address") or "") == DEFAULT_HOST
            and basis
        ):
            matching.append(record)
            match_basis.append(basis)
    pids: set[int] = {
        int(record["pid"]) for record in normalized if isinstance(record.get("pid"), int)
    }
    result["identity"] = {
        "proven": len(normalized) == 1 and len(pids) == 1 and len(matching) == 1,
        "expected_executables": [str(path) for path in expected_executables],
        "expected_venv_base_executables": [
            str(path) for path in expected_venv_base_executables
        ],
        "loopback_only": all(
            str(record.get("local_address") or "") == DEFAULT_HOST for record in normalized
        ),
        "matching_pids": sorted(
            int(record["pid"]) for record in matching if isinstance(record.get("pid"), int)
        ),
        "match_basis": match_basis,
        "observed_pids": sorted(pids),
    }
    return result


def _manifest_path(root: Path, profile: str) -> Path:
    return _workspace_dir(root) / "deploy" / profile / "manifest.json"


def _manifest_contract(
    root: Path,
    *,
    profile: str,
    port: int,
    preset: str,
    require_windows_task_contract: bool = True,
) -> dict[str, Any]:
    path = _manifest_path(root, profile)
    result: dict[str, Any] = {"ok": False, "path": str(path), "available": path.is_file()}
    if not path.is_file():
        result["detail"] = "upstream manifest missing"
        return result
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result["detail"] = f"upstream manifest invalid: {exc}"
        return result
    supervisor_kind = "service" if preset == "persistent-service" else "task"
    service_name = f"headroom-{profile}"
    expected_identity = {
        "profile": profile,
        "preset": preset,
        "runtime_kind": "python",
        "supervisor_kind": supervisor_kind,
        "scope": "user",
        "provider_mode": "manual",
        "port": port,
        "host": DEFAULT_HOST,
        "backend": "anthropic",
        "anyllm_provider": None,
        "region": None,
        "proxy_mode": "token",
        "memory_enabled": False,
        "telemetry_enabled": False,
        "service_name": service_name,
        "container_name": service_name,
        "health_url": f"{_proxy_url(port)}/readyz",
    }
    expected_env = {
        "HEADROOM_PORT": str(port),
        "HEADROOM_HOST": DEFAULT_HOST,
        "HEADROOM_MODE": "token",
        "HEADROOM_BACKEND": "anthropic",
        "HEADROOM_TELEMETRY": "off",
        "HEADROOM_CCR_BACKEND": DEFAULT_CCR_BACKEND,
        "HEADROOM_CCR_TTL_SECONDS": str(DEFAULT_CCR_TTL_SECONDS),
        "HEADROOM_DISABLE_UPDATE_CHECK": "1",
    }
    expected_proxy_args = [
        "--host", DEFAULT_HOST,
        "--port", str(port),
        "--mode", "token",
        "--backend", "anthropic",
        "--no-telemetry",
        "--no-code-aware",
    ]
    mismatches = [
        key for key, expected in expected_identity.items() if data.get(key) != expected
    ]
    if data.get("targets") != []:
        mismatches.append("targets")
    if data.get("mutations") != []:
        mismatches.append("mutations")
    if data.get("tool_envs") != {}:
        mismatches.append("tool_envs")
    actual_env = data.get("base_env")
    environment_exact = False
    if isinstance(actual_env, dict):
        actual_non_path_env = {
            key: value
            for key, value in actual_env.items()
            if key != "HEADROOM_WORKSPACE_DIR"
        }
        environment_exact = actual_non_path_env == expected_env and _path_identity_equal(
            actual_env.get("HEADROOM_WORKSPACE_DIR", ""), _workspace_dir(root)
        )
    if not environment_exact:
        mismatches.append("base_env")
    if data.get("proxy_args") != expected_proxy_args:
        mismatches.append("proxy_args")
    windows_task_contract: dict[str, Any] | None = None
    if _is_windows() and preset == "persistent-task":
        windows_task_contract = _windows_manifest_task_contract(
            root, profile=profile, data=data
        )
        if require_windows_task_contract and not windows_task_contract.get("ok"):
            mismatches.append("windows_task_contract")
    result.update(
        {
            "profile": data.get("profile"),
            "preset": data.get("preset"),
            "runtime_kind": data.get("runtime_kind"),
            "supervisor_kind": data.get("supervisor_kind"),
            "scope": data.get("scope"),
            "provider_mode": data.get("provider_mode"),
            "port": data.get("port"),
            "host": data.get("host"),
            "service_name": data.get("service_name"),
            "targets_empty": data.get("targets") == [],
            "mutations_empty": data.get("mutations") == [],
            "environment_exact": environment_exact,
            "proxy_args_exact": data.get("proxy_args") == expected_proxy_args,
            "windows_task_contract": windows_task_contract,
            "mismatches": mismatches,
        }
    )
    result["ok"] = not mismatches
    if mismatches:
        result["detail"] = "manifest failed complete manager-owned identity contract"
    return result

def _wait_supervisor_absent(profile: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = _supervisor_presence(profile)
    while last.get("present") and time.monotonic() < deadline:
        time.sleep(0.5)
        last = _supervisor_presence(profile)
    return last


def _wait_ready(proxy_url: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = readyz(proxy_url)
        if last.get("ok"):
            return last
        time.sleep(1)
    return last or readyz(proxy_url)


def _wait_stopped(proxy_url: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = readyz(proxy_url)
        if not last.get("ok"):
            return last
        time.sleep(1)
    return last or readyz(proxy_url)


def _state_for(
    root: Path,
    *,
    status: str,
    headroom_spec: str,
    litellm_spec: str,
    profile: str,
    port: int,
    preset: str,
    versions: dict[str, str] | None = None,
    error: str | None = None,
) -> RuntimeState:
    versions = versions or {}
    return RuntimeState(
        schema=STATE_SCHEMA,
        status=status,
        runtime_version=versions.get("headroom", RUNTIME_VERSION),
        litellm_version=versions.get("litellm", LITELLM_VERSION),
        headroom_spec=headroom_spec,
        litellm_spec=litellm_spec,
        profile=profile,
        host=DEFAULT_HOST,
        port=port,
        preset=preset,
        runtime_root=str(root),
        venv_dir=str(_venv_dir(root)),
        workspace_dir=str(_workspace_dir(root)),
        proxy_url=_proxy_url(port),
        updated_at=_utc_now(),
        last_error=error,
    )


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    decision = payload.get("decision") or payload.get("status") or "UNKNOWN"
    print(f"Headroom runtime manager: {decision}")
    for key in ("profile", "proxy_url", "preset", "runtime_version", "litellm_version", "detail", "next"):
        value = payload.get(key)
        if value not in (None, ""):
            print(f"{key}: {value}")


def setup(args: argparse.Namespace) -> int:
    root = _resolve_root(args.runtime_root)
    profile = _validate_profile(args.profile)
    port = _validate_port(args.port)
    args.headroom_spec = _validate_package_spec(args.headroom_spec, package="headroom-ai")
    args.litellm_spec = _validate_package_spec(args.litellm_spec, package="litellm")
    preset = args.preset or _default_preset()
    proxy_url = _proxy_url(port)
    plan = {
        "decision": "DRY_RUN" if args.dry_run else "SETUP",
        "profile": profile,
        "proxy_url": proxy_url,
        "preset": preset,
        "runtime_root": str(root),
        "headroom_spec": args.headroom_spec,
        "litellm_spec": args.litellm_spec,
        "provider_mode": "manual",
        "provider_targets": [],
        "provider_mutations": False,
        "telemetry": "off",
    }
    if args.dry_run:
        plan["upstream_api"] = [
            "_build_deployment_manifest",
            "install_supervisor",
            "save_manifest",
            "_start_deployment",
        ]
        _emit(plan, as_json=args.json)
        return 0

    lock_fd = _acquire_lock(root)
    try:
        existing = _load_state(root)
        if existing is not None:
            requested_identity = (profile, port, preset, args.headroom_spec, args.litellm_spec)
            existing_identity = (
                existing.profile,
                existing.port,
                existing.preset,
                existing.headroom_spec,
                existing.litellm_spec,
            )
            if requested_identity != existing_identity:
                _emit(
                    {
                        **plan,
                        "decision": "MANAGED_CONFIGURATION_CONFLICT",
                        "detail": "requested profile/port/preset/specs differ from existing manager state",
                        "next": "run headroom-runtime uninstall before changing managed identity",
                    },
                    as_json=args.json,
                )
                return 2
        manifest_exists = _manifest_path(root, profile).is_file()
        supervisor = _supervisor_presence(profile)
        if existing is None and (manifest_exists or supervisor["present"]):
            _emit(
                {
                    **plan,
                    "decision": "FOREIGN_DEPLOYMENT_CONFLICT",
                    "detail": "an upstream manifest or global supervisor exists without manager-owned state",
                    "manifest_exists": manifest_exists,
                    "supervisor": supervisor,
                    "next": "use a different --profile or remove/adopt the existing deployment explicitly",
                },
                as_json=args.json,
            )
            return 2
        claim = _root_claim_contract(root)
        if not claim.get("ok"):
            _emit(
                {
                    **plan,
                    "decision": "RUNTIME_ROOT_CONFLICT",
                    "detail": claim.get("detail"),
                    "unexpected_entries": claim.get("unexpected_entries", []),
                },
                as_json=args.json,
            )
            return 2
        health = readyz(proxy_url)
        tcp_open = _tcp_port_open(port)
        if health.get("ok") and existing is None:
            _emit(
                {
                    **plan,
                    "decision": "PORT_CONFLICT_UNMANAGED",
                    "detail": "a ready service already owns the port but no manager state exists",
                    "next": "choose another --port or manage the existing runtime explicitly",
                },
                as_json=args.json,
            )
            return 2
        if not health.get("ok") and tcp_open:
            _emit(
                {
                    **plan,
                    "decision": "PORT_CONFLICT_UNKNOWN_SERVICE",
                    "detail": f"a TCP listener that did not pass Headroom readiness owns {proxy_url}",
                    "next": "stop the listener or choose another --port",
                },
                as_json=args.json,
            )
            return 2
        managed_cli = _exe(_venv_dir(root), "headroom")
        if health.get("ok") and existing is not None:
            manifest_contract = _manifest_contract(root, profile=profile, port=port, preset=preset)
            migration_required = _windows_migration_required(manifest_contract)
            if migration_required:
                base_contract = _manifest_contract(
                    root,
                    profile=profile,
                    port=port,
                    preset=preset,
                    require_windows_task_contract=False,
                )
                if base_contract.get("ok"):
                    _emit(
                        {
                            "decision": "MIGRATION_REQUIRED",
                            "profile": profile,
                            "migration_required": True,
                            "manifest_contract": manifest_contract,
                            "next": "run headroom-runtime reconcile --dry-run --json",
                        },
                        as_json=args.json,
                    )
                    return 1
            supervisor_contract = _supervisor_contract(
                root, profile=profile, preset=preset
            )
            upstream = _upstream_status_evidence(
                headroom=managed_cli,
                root=root,
                profile=profile,
                preset=preset,
                port=port,
                timeout=30,
            )
            verification = smoke(proxy_url)
            if (
                upstream.get("ok") is True
                and supervisor_contract.get("ok") is True
                and manifest_contract.get("ok")
                and verification.get("ok")
                and verification.get("sentinel_found") is True
            ):
                existing.status = "RUNTIME_FULL_DURABLE"
                existing.updated_at = _utc_now()
                existing.last_error = None
                _write_state(root, existing)
                _emit(
                    {
                        **asdict(existing),
                        "decision": existing.status,
                        "manifest_contract": manifest_contract,
                        "smoke": verification,
                        "upstream_status": upstream,
                        "supervisor": supervisor_contract,
                    },
                    as_json=args.json,
                )
                return 0
            _emit(
                {
                    **asdict(existing),
                    "decision": "RUNTIME_PARTIAL",
                    "detail": "ready listener did not match complete manager/upstream/manifest evidence",
                    "manifest_contract": manifest_contract,
                    "upstream_status": upstream,
                    "supervisor": supervisor_contract,
                    "next": "run headroom-runtime doctor, then reconcile or uninstall before repair",
                },
                as_json=args.json,
            )
            return 2

        if existing is not None and (manifest_exists or supervisor["present"]):
            _emit(
                {
                    **asdict(existing),
                    "decision": "MANAGED_REPAIR_REQUIRES_UNINSTALL",
                    "detail": "an unhealthy managed deployment still has manifest/supervisor state",
                    "manifest_exists": manifest_exists,
                    "supervisor": supervisor,
                    "next": "run headroom-runtime doctor, then explicit uninstall before setup",
                },
                as_json=args.json,
            )
            return 2

        _ensure_marker(root)
        versions: dict[str, str] | None = None
        try:
            headroom, versions = _ensure_runtime(
                root,
                python=args.python,
                headroom_spec=args.headroom_spec,
                litellm_spec=args.litellm_spec,
                timeout=args.install_timeout,
            )
            partial = _state_for(
                root,
                status="RUNTIME_PARTIAL",
                headroom_spec=args.headroom_spec,
                litellm_spec=args.litellm_spec,
                profile=profile,
                port=port,
                preset=preset,
                versions=versions,
            )
            _write_state(root, partial)
            if not headroom.is_file():
                raise RuntimeError(f"headroom CLI missing after install: {headroom}")
            applied = _safe_apply(
                root=root,
                profile=profile,
                port=port,
                preset=preset,
                timeout=args.install_timeout,
            )
            if applied.returncode != 0:
                raise RuntimeError(f"safe upstream lifecycle apply failed; see {root / 'manager.log'}")
            try:
                apply_result = json.loads(applied.stdout.splitlines()[-1])
            except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("safe apply returned no verifiable manifest result") from exc
            if (
                apply_result.get("provider_mode") != "manual"
                or apply_result.get("targets") != []
                or apply_result.get("mutations") != []
            ):
                raise RuntimeError("safe apply manifest invariants failed")
            manifest_contract = _manifest_contract(root, profile=profile, port=port, preset=preset)
            if not manifest_contract.get("ok"):
                raise RuntimeError(f"saved manifest contract failed: {manifest_contract}")
            health = _wait_ready(proxy_url, args.ready_timeout)
            if not health.get("ok"):
                raise RuntimeError(f"runtime readiness failed: {health}")
            verification = smoke(proxy_url)
            if not verification.get("ok") or verification.get("sentinel_found") is not True:
                raise RuntimeError(f"compress/retrieve sentinel smoke failed: {verification}")
            upstream = _upstream_status_evidence(
                headroom=headroom,
                root=root,
                profile=profile,
                preset=preset,
                port=port,
                timeout=30,
            )
            supervisor = _supervisor_contract(root, profile=profile, preset=preset)
            if upstream.get("ok") is not True or supervisor.get("ok") is not True:
                reasons = upstream.get("semantic", {}).get("reasons", [])
                raise RuntimeError(
                    "durable lifecycle semantic verification failed: "
                    f"upstream_reasons={reasons}, "
                    f"supervisor_ok={supervisor.get('ok') is True}, "
                    f"supervisor_reasons={supervisor.get('reasons', [])}"
                )
            full = _state_for(
                root,
                status="RUNTIME_FULL_DURABLE",
                headroom_spec=args.headroom_spec,
                litellm_spec=args.litellm_spec,
                profile=profile,
                port=port,
                preset=preset,
                versions=versions,
            )
            _write_state(root, full)
            _emit(
                {
                    **asdict(full),
                    "decision": full.status,
                    "smoke": verification,
                    "upstream_status": upstream,
                    "supervisor": supervisor,
                },
                as_json=args.json,
            )
            return 0
        except Exception as exc:
            failed = _state_for(
                root,
                status="RUNTIME_PARTIAL",
                headroom_spec=args.headroom_spec,
                litellm_spec=args.litellm_spec,
                profile=profile,
                port=port,
                preset=preset,
                versions=versions,
                error=str(exc),
            )
            _write_state(root, failed)
            _emit(
                {
                    **asdict(failed),
                    "decision": "RUNTIME_PARTIAL",
                    "detail": str(exc),
                    "next": "run headroom-runtime doctor, then headroom-runtime uninstall for rollback",
                },
                as_json=args.json,
            )
            return 2
    finally:
        _release_lock(root, lock_fd)


def status(args: argparse.Namespace) -> int:
    root = _resolve_root(args.runtime_root)
    state = _load_state(root)
    if state is None:
        _emit(
            {"decision": "RUNTIME_ABSENT", "runtime_root": str(root), "next": "run headroom-runtime setup"},
            as_json=args.json,
        )
        return 1
    health = readyz(state.proxy_url)
    manifest_contract = _manifest_contract(
        root, profile=state.profile, port=state.port, preset=state.preset
    )
    payload = {
        **asdict(state),
        "readyz": health,
        "manifest_contract": manifest_contract,
        "migration_required": _windows_migration_required(manifest_contract),
    }
    headroom = _exe(Path(state.venv_dir), "headroom")
    upstream = _upstream_status_evidence(
        headroom=headroom,
        root=root,
        profile=state.profile,
        preset=state.preset,
        port=state.port,
        timeout=args.timeout,
    )
    supervisor = _supervisor_contract(root, profile=state.profile, preset=state.preset)
    payload["upstream_status_exit"] = upstream.get("exit_code")
    payload["upstream_status"] = upstream.get("output", "")
    payload["upstream_status_semantic"] = upstream.get("semantic")
    payload["supervisor"] = supervisor
    decision = (
        "RUNTIME_FULL_DURABLE"
        if health.get("ok")
        and upstream.get("ok") is True
        and supervisor.get("ok") is True
        and manifest_contract.get("ok")
        else "RUNTIME_PARTIAL"
    )
    payload["decision"] = decision
    _emit(payload, as_json=args.json)
    return 0 if decision == "RUNTIME_FULL_DURABLE" else 1


def doctor(args: argparse.Namespace) -> int:
    root = _resolve_root(args.runtime_root)
    state = _load_state(root)
    if state is None:
        _emit(
            {"decision": "RUNTIME_ABSENT", "runtime_root": str(root), "next": "run headroom-runtime setup"},
            as_json=args.json,
        )
        return 2
    health = readyz(state.proxy_url)
    verification = smoke(state.proxy_url) if health.get("ok") else {"ok": False, "phase": "readyz"}
    manifest_contract = _manifest_contract(
        root, profile=state.profile, port=state.port, preset=state.preset
    )
    headroom = _exe(Path(state.venv_dir), "headroom")
    upstream = _upstream_status_evidence(
        headroom=headroom,
        root=root,
        profile=state.profile,
        preset=state.preset,
        port=state.port,
        timeout=args.timeout,
    )
    supervisor = _supervisor_contract(root, profile=state.profile, preset=state.preset)
    full = bool(
        health.get("ok")
        and verification.get("ok")
        and verification.get("sentinel_found") is True
        and upstream.get("ok") is True
        and supervisor.get("ok") is True
        and manifest_contract.get("ok")
    )
    decision = "RUNTIME_FULL_DURABLE" if full else "RUNTIME_PARTIAL"
    _emit(
        {
            **asdict(state),
            "decision": decision,
            "readyz": health,
            "smoke": verification,
            "manifest_contract": manifest_contract,
            "migration_required": _windows_migration_required(manifest_contract),
            "upstream_status": upstream,
            "supervisor": supervisor,
            "next": (
                None
                if full
                else "run headroom-runtime reconcile --dry-run --json"
                if _is_windows() and state.preset == "persistent-task"
                else "inspect manager.log/install.log or run headroom-runtime uninstall"
            ),
        },
        as_json=args.json,
    )
    return 0 if full else 2


def _read_only_reconcile_plan(
    root: Path, *, timeout: int, probe_port: int | None = None
) -> tuple[dict[str, Any], int]:
    """Build a zero-write ownership and migration plan for an existing runtime."""
    state = _load_state(root)
    if state is None:
        selected_probe_port = (
            DEFAULT_PORT if probe_port is None else _validate_port(probe_port)
        )
        listener = _windows_listener_inventory(
            port=selected_probe_port, expected_executables=None, timeout=timeout
        )
        if listener.get("present") is not False:
            listener_reason = (
                "os_listener_without_manager_identity"
                if listener.get("present") is True
                else "listener_inventory_unavailable"
            )
            return (
                {
                    "schema": "headroom-reconcile-plan-v1",
                    "decision": "OWNERSHIP_AMBIGUOUS",
                    "classification": "foreign_or_unmanaged",
                    "writes_performed": False,
                    "inventory": {
                        "manager_state": {"present": False, "valid": False},
                        "purge_marker": {"valid": False},
                        "runtime": {"ok": False, "reason": "manager_state_missing"},
                        "manifest": {"ok": False, "available": False},
                        "listener": listener,
                        "supervisor": {"ok": False, "present": None, "reason": "profile_unknown"},
                        "upstream_status": {"ok": False, "reason": "managed_cli_unknown"},
                    },
                    "ownership": {
                        "proven": False,
                        "evidence": (
                            ["os_listener_present"] if listener.get("present") is True else []
                        ),
                        "missing": [
                            "manager_state_identity",
                            "purge_marker",
                            "manifest_identity",
                            "runtime_identity",
                            "supervisor_identity",
                        ],
                    },
                    "adoption": {
                        "eligible": False,
                        "reasons": [listener_reason],
                    },
                    "next_steps": [
                        "preserve the listener and inspect its owning process, manifest, supervisor and runtime independently",
                        "use a different profile and loopback port unless exact manager ownership can be proven",
                    ],
                    "rollback": ["no mutation was attempted"],
                },
                2,
            )
        return (
            {
                "schema": "headroom-reconcile-plan-v1",
                "decision": "RUNTIME_ABSENT",
                "classification": "absent",
                "writes_performed": False,
                "inventory": {
                    "manager_state": {"present": False, "valid": False},
                    "listener": listener,
                },
                "ownership": {"proven": False, "evidence": [], "missing": ["manager_state_identity"]},
                "adoption": {"eligible": False, "reasons": ["runtime_absent"]},
                "next_steps": ["run headroom-runtime setup --dry-run --json"],
                "rollback": ["no mutation was attempted"],
            },
            2,
        )

    marker_valid = _safe_to_purge(root)
    runtime_python = _exe(Path(state.venv_dir), "python")
    headroom_cli = _exe(Path(state.venv_dir), "headroom")
    runtime_identity = {
        "ok": bool(
            runtime_python.is_file()
            and headroom_cli.is_file()
            and state.runtime_version == RUNTIME_VERSION
            and state.litellm_version == LITELLM_VERSION
            and state.headroom_spec == DEFAULT_HEADROOM_SPEC
            and state.litellm_spec == DEFAULT_LITELLM_SPEC
        ),
        "python_present": runtime_python.is_file(),
        "headroom_cli_present": headroom_cli.is_file(),
        "runtime_version": state.runtime_version,
        "litellm_version": state.litellm_version,
        "specs_exact": (
            state.headroom_spec == DEFAULT_HEADROOM_SPEC
            and state.litellm_spec == DEFAULT_LITELLM_SPEC
        ),
    }
    manifest_contract = _manifest_contract(
        root, profile=state.profile, port=state.port, preset=state.preset
    )
    listener = _windows_listener_inventory(
        port=state.port,
        expected_executables=(runtime_python, headroom_cli),
        expected_venv_base_executables=_windows_venv_base_executables(
            Path(state.venv_dir)
        ),
        timeout=timeout,
    )
    upstream = {
        "status": "not_probed_read_only",
        "ok": None,
        "reason": "application_level_status_can_write_runtime_logs",
    }
    supervisor = _supervisor_contract(root, profile=state.profile, preset=state.preset)
    expected_service = f"headroom-{state.profile}"
    supervisor_identity = bool(
        supervisor.get("present") is True
        and supervisor.get("service_name") == expected_service
    )
    supervisor_tasks = supervisor.get("tasks", {})
    managed_task_action_identity = bool(
        isinstance(supervisor_tasks, dict)
        and all(
            isinstance(supervisor_tasks.get(kind), dict)
            and supervisor_tasks[kind].get("exists") is True
            and supervisor_tasks[kind].get("enabled") is True
            and supervisor_tasks[kind].get("managed_action_identity") is True
            for kind in ("startup", "health")
        )
    )
    mismatches = list(manifest_contract.get("mismatches") or [])
    mismatch_set = set(mismatches)
    migration_only = bool(mismatch_set) and mismatch_set <= {
        "mutations",
        "windows_task_contract",
    }
    manifest_base_identity = bool(
        manifest_contract.get("available")
        and (manifest_contract.get("ok") or migration_only)
    )
    deployment_evidence_checks = {
        "manager_state_identity": True,
        "purge_marker": marker_valid,
        "manifest_base_identity": manifest_base_identity,
        "runtime_identity": runtime_identity["ok"],
        "supervisor_name_identity": supervisor_identity,
    }
    listener_binding_proven = listener.get("identity", {}).get("proven") is True
    evidence_checks = {
        **deployment_evidence_checks,
        "listener_process_identity": listener_binding_proven,
    }
    deployment_identity_proven = all(deployment_evidence_checks.values())
    ownership_proven = all(evidence_checks.values())
    inventory = {
        "manager_state": {
            "present": True,
            "valid": True,
            "profile": state.profile,
            "preset": state.preset,
            "port": state.port,
        },
        "purge_marker": {"valid": marker_valid},
        "runtime": runtime_identity,
        "manifest": manifest_contract,
        "listener": listener,
        "supervisor": supervisor,
        "upstream_status": upstream,
    }
    ownership = {
        "proven": ownership_proven,
        "evidence": [key for key, value in evidence_checks.items() if value],
        "missing": [key for key, value in evidence_checks.items() if not value],
        "deployment": {
            "proven": deployment_identity_proven,
            "evidence": [
                key for key, value in deployment_evidence_checks.items() if value
            ],
            "missing": [
                key for key, value in deployment_evidence_checks.items() if not value
            ],
        },
        "listener_binding": {
            "proven": listener_binding_proven,
            "evidence": (
                ["listener_process_identity"] if listener_binding_proven else []
            ),
            "missing": (
                [] if listener_binding_proven else ["listener_process_identity"]
            ),
        },
    }

    if state.preset != "persistent-task":
        return (
            {
                "schema": "headroom-reconcile-plan-v1",
                "decision": "RECONCILIATION_NOT_APPLICABLE",
                "classification": "non_windows_task_preset",
                "writes_performed": False,
                "inventory": inventory,
                "ownership": ownership,
                "adoption": {"eligible": False, "reasons": ["persistent_task_preset_required"]},
                "next_steps": ["use status and doctor for this native lifecycle"],
                "rollback": ["no mutation was attempted"],
            },
            0,
        )

    if (
        manifest_contract.get("ok")
        and supervisor.get("ok")
        and deployment_identity_proven
    ):
        return (
            {
                "schema": "headroom-reconcile-plan-v1",
                "decision": "RECONCILIATION_NOT_REQUIRED",
                "classification": "current_manager_contract",
                "writes_performed": False,
                "inventory": inventory,
                "ownership": ownership,
                "adoption": {"eligible": False, "reasons": ["already_manager_owned"]},
                "next_steps": ["run headroom-runtime doctor --json"],
                "rollback": ["no mutation was attempted"],
            },
            0,
        )

    if "mutations" in mismatch_set:
        decision = (
            "REINSTALL_REQUIRED"
            if deployment_identity_proven
            else "OWNERSHIP_AMBIGUOUS"
        )
        classification = (
            "manager_owned_legacy_mutations"
            if deployment_identity_proven
            else "legacy_mutations_ownership_unproven"
        )
        if deployment_identity_proven:
            next_steps = [
                "preserve manager state, manifest, supervisor task exports and mutation-target backups",
                "obtain an explicit target-host mutation gate before changing the existing deployment",
                "before any operation that may stop or replace the listener, establish listener binding independently",
                "if listener binding cannot be established, preserve this deployment and use a separate clean runtime root, profile and loopback port",
                "only after listener binding and the target-host gate, use the pinned upstream manifest removal path to replay recorded mutation rollback; verify listener and supervisor absence; then run setup",
            ]
            rollback = [
                "do not delete or edit mutation records in the manifest",
                "stop and restore target backups if any recorded mutation cannot be reversed exactly",
                "retain the current runtime until the removal and clean setup gates are both available",
            ]
        else:
            next_steps = [
                "do not run reconcile --apply, uninstall or upstream removal while deployment identity is unproven",
                "preserve the deployment and resolve every missing deployment identity check and manifest mismatch independently",
                "use a separate clean runtime root, profile and loopback port if exact deployment identity cannot be established",
            ]
            rollback = ["no mutation was attempted; preserve the existing deployment unchanged"]
        return (
            {
                "schema": "headroom-reconcile-plan-v1",
                "decision": decision,
                "classification": classification,
                "writes_performed": False,
                "inventory": inventory,
                "ownership": ownership,
                "mutation_authority": {
                    "eligible": False,
                    "scope": None,
                    "resources": [],
                    "reasons": [
                        "mutation_history_requires_symmetric_rollback",
                        "explicit_target_host_mutation_gate_required",
                        *(
                            []
                            if listener_binding_proven
                            else ["listener_binding_unproven"]
                        ),
                    ],
                },
                "adoption": {
                    "eligible": False,
                    "reasons": [
                        "mutation_history_requires_symmetric_rollback",
                        "listener_process_identity_does_not_replace_manager_identity",
                    ],
                },
                "next_steps": next_steps,
                "rollback": rollback,
            },
            1 if deployment_identity_proven else 2,
        )

    if mismatch_set == {"windows_task_contract"} and deployment_identity_proven:
        if not managed_task_action_identity:
            return (
                {
                    "schema": "headroom-reconcile-plan-v1",
                    "decision": "RECONCILE_BLOCKED",
                    "classification": "manager_deployment_unproven_task_actions",
                    "writes_performed": False,
                    "apply_required": False,
                    "inventory": inventory,
                    "ownership": ownership,
                    "mutation_authority": {
                        "eligible": False,
                        "scope": None,
                        "resources": [],
                        "evidence": [],
                        "reasons": ["managed_task_action_identity_missing"],
                    },
                    "adoption": {
                        "eligible": False,
                        "reasons": ["manager_owned_reconciliation_not_adoption"],
                    },
                    "next_steps": [
                        "preserve both task XML exports and inspect their actions before any mutation"
                    ],
                    "rollback": ["no mutation was attempted"],
                },
                2,
            )
        return (
            {
                "schema": "headroom-reconcile-plan-v1",
                "decision": "MIGRATION_REQUIRED",
                "classification": "manager_owned_windows_task_contract",
                "writes_performed": False,
                "apply_required": True,
                "inventory": inventory,
                "ownership": ownership,
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
                "adoption": {
                    "eligible": False,
                    "reasons": ["manager_owned_reconciliation_not_adoption"],
                },
                "next": "run headroom-runtime reconcile --apply --json",
                "next_steps": [
                    "review the inventory, then run headroom-runtime reconcile --apply --json"
                ],
                "rollback": [
                    "apply snapshots both managed tasks and launcher before the first mutation"
                ],
            },
            1,
        )

    return (
        {
            "schema": "headroom-reconcile-plan-v1",
            "decision": "RECONCILE_BLOCKED",
            "classification": "ownership_or_identity_mismatch",
            "writes_performed": False,
            "inventory": inventory,
            "ownership": ownership,
            "mutation_authority": {
                "eligible": False,
                "scope": None,
                "resources": [],
                "reasons": ["complete_positive_deployment_identity_missing"],
            },
            "adoption": {"eligible": False, "reasons": ["complete_positive_identity_missing"]},
            "next_steps": ["preserve the runtime and inspect every reported mismatch before any mutation"],
            "rollback": ["no mutation was attempted"],
        },
        2,
    )


def _task_reconcile_apply_authorized(plan: dict[str, Any]) -> bool:
    authority = plan.get("mutation_authority") or {}
    ownership = plan.get("ownership") or {}
    deployment = ownership.get("deployment") or {}
    return bool(
        plan.get("decision") == "MIGRATION_REQUIRED"
        and deployment.get("proven") is True
        and authority.get("eligible") is True
        and authority.get("scope") == "windows_task_contract"
        and authority.get("evidence") == ["managed_task_action_identity"]
        and authority.get("resources")
        == [
            "managed_windows_launcher",
            "managed_windows_scheduled_tasks",
            "manifest_artifacts",
        ]
    )


def reconcile(args: argparse.Namespace) -> int:
    root = _resolve_root(args.runtime_root)
    if not _is_windows():
        _emit(
            {
                "decision": "RECONCILIATION_NOT_APPLICABLE",
                "detail": "managed silent-task reconciliation is native-Windows only",
            },
            as_json=args.json,
        )
        return 0
    if not args.apply:
        plan, code = _read_only_reconcile_plan(
            root, timeout=args.timeout, probe_port=args.probe_port
        )
        _emit(plan, as_json=args.json)
        return code
    if args.probe_port is not None:
        _emit(
            {
                "decision": "RECONCILE_BLOCKED",
                "detail": "--probe-port is a read-only discovery option and cannot be combined with --apply",
            },
            as_json=args.json,
        )
        return 2
    preflight, _ = _read_only_reconcile_plan(
        root, timeout=args.timeout, probe_port=None
    )
    if not _task_reconcile_apply_authorized(preflight):
        _emit(
            {
                "decision": "RECONCILE_BLOCKED",
                "detail": "read-only preflight did not grant windows_task_contract mutation authority",
                "writes_performed": False,
                "preflight": preflight,
            },
            as_json=args.json,
        )
        return 2
    lock_fd = _acquire_lock(root)
    try:
        locked_preflight, _ = _read_only_reconcile_plan(
            root, timeout=args.timeout, probe_port=None
        )
        if not _task_reconcile_apply_authorized(locked_preflight):
            _emit(
                {
                    "decision": "RECONCILE_BLOCKED",
                    "detail": "windows_task_contract mutation authority changed after lock acquisition",
                    "writes_performed": True,
                    "write_scope": ["transaction_lock_only"],
                    "preflight": locked_preflight,
                },
                as_json=args.json,
            )
            return 2
        state = _load_state(root)
        if state is None:
            _emit(
                {
                    "decision": "RUNTIME_ABSENT",
                    "runtime_root": str(root),
                    "next": "run headroom-runtime setup",
                },
                as_json=args.json,
            )
            return 2
        if not _safe_to_purge(root):
            _emit(
                {
                    "decision": "RECONCILE_BLOCKED",
                    "detail": "manager marker is missing or invalid",
                },
                as_json=args.json,
            )
            return 2
        if state.preset != "persistent-task":
            _emit(
                {
                    "decision": "RECONCILIATION_NOT_APPLICABLE",
                    "profile": state.profile,
                    "detail": "managed deployment does not use the Windows persistent-task preset",
                },
                as_json=args.json,
            )
            return 0
        base_contract = _manifest_contract(
            root,
            profile=state.profile,
            port=state.port,
            preset=state.preset,
            require_windows_task_contract=False,
        )
        if not base_contract.get("ok"):
            _emit(
                {
                    "decision": "RECONCILE_BLOCKED",
                    "profile": state.profile,
                    "detail": "manifest failed the manager-owned base identity contract",
                    "manifest_contract": base_contract,
                    "next": "preserve the runtime and inspect or uninstall explicitly",
                },
                as_json=args.json,
            )
            return 2
        manifest_contract = _manifest_contract(
            root, profile=state.profile, port=state.port, preset=state.preset
        )
        supervisor = None
        if manifest_contract.get("ok"):
            supervisor = _supervisor_contract(
                root, profile=state.profile, preset=state.preset
            )
            if supervisor.get("ok"):
                _emit(
                    {
                        "decision": "RECONCILIATION_NOT_REQUIRED",
                        "profile": state.profile,
                        "manifest_contract": manifest_contract,
                        "supervisor": supervisor,
                    },
                    as_json=args.json,
                )
                return 0
        plan = {
            "decision": "MIGRATION_REQUIRED",
            "profile": state.profile,
            "apply_required": True,
            "manifest_contract": manifest_contract,
            "supervisor": supervisor,
            "next": "run headroom-runtime reconcile --apply --json",
        }
        if not args.apply:
            _emit(plan, as_json=args.json)
            return 1
        python_exe = _exe(_venv_dir(root), "python")
        if not python_exe.is_file():
            _emit(
                {
                    **plan,
                    "decision": "RECONCILE_BLOCKED",
                    "detail": "managed runtime Python is missing",
                },
                as_json=args.json,
            )
            return 2
        applied = _safe_reconcile(root=root, profile=state.profile, timeout=args.timeout)
        if applied.returncode != 0:
            _emit(
                {
                    **plan,
                    "decision": "RECONCILE_PARTIAL",
                    "detail": "managed Windows task reconciliation failed; evidence was preserved",
                    "next": "inspect manager.log, then retry or run explicit uninstall",
                },
                as_json=args.json,
            )
            return 2
        manifest_contract = _manifest_contract(
            root, profile=state.profile, port=state.port, preset=state.preset
        )
        supervisor = _supervisor_contract(root, profile=state.profile, preset=state.preset)
        headroom = _exe(Path(state.venv_dir), "headroom")
        upstream = _upstream_status_evidence(
            headroom=headroom,
            root=root,
            profile=state.profile,
            preset=state.preset,
            port=state.port,
            timeout=args.timeout,
        )
        health = readyz(state.proxy_url)
        verification = smoke(state.proxy_url) if health.get("ok") else {"ok": False, "phase": "readyz"}
        full = bool(
            manifest_contract.get("ok")
            and supervisor.get("ok")
            and upstream.get("ok") is True
            and health.get("ok")
            and verification.get("ok")
            and verification.get("sentinel_found") is True
        )
        if not full:
            _emit(
                {
                    "decision": "RECONCILE_PARTIAL",
                    "profile": state.profile,
                    "manifest_contract": manifest_contract,
                    "supervisor": supervisor,
                    "upstream_status": upstream,
                    "readyz": health,
                    "smoke": verification,
                    "next": "inspect manager.log, then retry or run explicit uninstall",
                },
                as_json=args.json,
            )
            return 2
        state.status = "RUNTIME_FULL_DURABLE"
        state.updated_at = _utc_now()
        state.last_error = None
        _write_state(root, state)
        _emit(
            {
                "decision": "RECONCILED",
                "durability": state.status,
                "profile": state.profile,
                "manifest_contract": manifest_contract,
                "supervisor": supervisor,
                "upstream_status": upstream,
                "readyz": health,
                "smoke": verification,
            },
            as_json=args.json,
        )
        return 0
    finally:
        _release_lock(root, lock_fd)


def uninstall(args: argparse.Namespace) -> int:
    root = _resolve_root(args.runtime_root)
    lock_fd = _acquire_lock(root)
    try:
        state = _load_state(root)
        if state is None:
            _emit({"decision": "RUNTIME_ABSENT", "runtime_root": str(root)}, as_json=args.json)
            return 0
        if not _safe_to_purge(root):
            _emit(
                {
                    "decision": "UNINSTALL_BLOCKED",
                    "detail": f"manager marker missing or invalid: {root / MARKER_FILE}",
                },
                as_json=args.json,
            )
            return 2
        manifest_contract = _manifest_contract(
            root,
            profile=state.profile,
            port=state.port,
            preset=state.preset,
            require_windows_task_contract=False,
        )
        if not manifest_contract.get("ok"):
            partial_without_manifest = (
                state.status == "RUNTIME_PARTIAL"
                and manifest_contract.get("available") is False
            )
            if partial_without_manifest:
                health = readyz(state.proxy_url)
                supervisor = _supervisor_presence(state.profile)
                if not health.get("ok") and not supervisor.get("present"):
                    purge = {"ok": True}
                    if not args.keep_runtime:
                        purge = _purge_managed_root(root, state)
                    else:
                        try:
                            _state_path(root).unlink()
                        except FileNotFoundError:
                            pass
                    if not purge.get("ok"):
                        _emit(
                            {
                                "decision": "UNINSTALL_BLOCKED",
                                "detail": purge.get("detail"),
                                "root_contract": purge,
                                "upstream_mutation_invoked": False,
                            },
                            as_json=args.json,
                        )
                        return 2
                    _emit(
                        {
                            "decision": "UNINSTALLED_PARTIAL_STATE",
                            "profile": state.profile,
                            "proxy_url": state.proxy_url,
                            "runtime_files": "preserved" if args.keep_runtime else "removed",
                            "upstream_mutation_invoked": False,
                        },
                        as_json=args.json,
                    )
                    return 0
            _emit(
                {
                    "decision": "UNINSTALL_BLOCKED",
                    "detail": "saved manifest failed the complete manager-owned contract; no upstream mutation was invoked",
                    "manifest_contract": manifest_contract,
                    "next": "preserve runtime state and inspect the manifest/supervisor manually",
                },
                as_json=args.json,
            )
            return 2
        headroom = _exe(Path(state.venv_dir), "headroom")
        if not headroom.is_file():
            _emit(
                {
                    "decision": "UNINSTALL_BLOCKED",
                    "detail": f"managed Headroom CLI missing: {headroom}",
                    "next": "restore the runtime venv or remove supervisor artifacts manually",
                },
                as_json=args.json,
            )
            return 2
        proc = _run(
            [str(headroom), "install", "remove", "--profile", state.profile],
            timeout=args.timeout,
            log=root / "manager.log",
            env=_runtime_env(root),
        )
        if proc.returncode != 0:
            _emit(
                {
                    "decision": "UNINSTALL_PARTIAL",
                    "detail": proc.stdout[-2000:],
                    "next": f"inspect {root / 'manager.log'}; runtime files were preserved",
                },
                as_json=args.json,
            )
            return 2
        health = _wait_stopped(state.proxy_url, args.stop_timeout)
        supervisor = _wait_supervisor_absent(state.profile, args.stop_timeout)
        if health.get("ok") or supervisor.get("present"):
            reasons = []
            if health.get("ok"):
                reasons.append("managed listener is still ready")
            if supervisor.get("present"):
                reasons.append("native supervisor is still present")
            _emit(
                {
                    "decision": "UNINSTALL_PARTIAL",
                    "profile": state.profile,
                    "proxy_url": state.proxy_url,
                    "detail": "upstream remove returned success but " + " and ".join(reasons),
                    "supervisor": supervisor,
                    "next": f"runtime files were preserved at {root}",
                },
                as_json=args.json,
            )
            return 2
        if not args.keep_runtime:
            purge = _purge_managed_root(root, state)
            if not purge.get("ok"):
                _emit(
                    {
                        "decision": "UNINSTALL_PARTIAL",
                        "profile": state.profile,
                        "proxy_url": state.proxy_url,
                        "detail": purge.get("detail"),
                        "root_contract": purge,
                        "next": f"unexpected files were preserved at {root}",
                    },
                    as_json=args.json,
                )
                return 2
        else:
            try:
                _state_path(root).unlink()
            except FileNotFoundError:
                pass
        decision = "UNINSTALLED"
        _emit(
            {
                "decision": decision,
                "profile": state.profile,
                "proxy_url": state.proxy_url,
                "runtime_files": "preserved" if args.keep_runtime else "removed",
            },
            as_json=args.json,
        )
        return 0 if decision == "UNINSTALLED" else 2

    finally:
        _release_lock(root, lock_fd)

def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runtime-root", default=str(default_runtime_root()))
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="headroom-runtime", description="Manage the explicit official Headroom runtime used by the Hermes plugin.")
    sub = parser.add_subparsers(dest="command", required=True)

    setup_parser = sub.add_parser("setup", help="install, supervise, and verify the official runtime")
    _add_common(setup_parser)
    setup_parser.add_argument("--profile", default=DEFAULT_PROFILE)
    setup_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    setup_parser.add_argument("--preset", choices=("persistent-service", "persistent-task"), default=None)
    setup_parser.add_argument("--python", default=sys.executable)
    setup_parser.add_argument("--headroom-spec", default=os.environ.get("HEADROOM_AI_SPEC", DEFAULT_HEADROOM_SPEC))
    setup_parser.add_argument("--litellm-spec", default=os.environ.get("HEADROOM_LITELLM_SPEC", DEFAULT_LITELLM_SPEC))
    setup_parser.add_argument("--install-timeout", type=int, default=600)
    setup_parser.add_argument("--ready-timeout", type=int, default=45)
    setup_parser.add_argument("--dry-run", action="store_true")
    setup_parser.set_defaults(func=setup)

    status_parser = sub.add_parser("status", help="show manager, upstream lifecycle, and readiness state")
    _add_common(status_parser)
    status_parser.add_argument("--timeout", type=int, default=30)
    status_parser.set_defaults(func=status)

    doctor_parser = sub.add_parser("doctor", help="verify readiness, upstream lifecycle, and compress/retrieve")
    _add_common(doctor_parser)
    doctor_parser.add_argument("--timeout", type=int, default=30)
    doctor_parser.set_defaults(func=doctor)

    reconcile_parser = sub.add_parser(
        "reconcile", help="plan or explicitly apply the managed Windows silent-task contract"
    )
    _add_common(reconcile_parser)
    reconcile_parser.add_argument("--timeout", type=int, default=60)
    reconcile_parser.add_argument(
        "--probe-port",
        type=int,
        default=None,
        help="loopback listener port to inspect when manager state is absent (default: 8787)",
    )
    reconcile_mode = reconcile_parser.add_mutually_exclusive_group()
    reconcile_mode.add_argument(
        "--dry-run", action="store_true", help="print the no-write plan (default)"
    )
    reconcile_mode.add_argument(
        "--apply", action="store_true", help="apply the displayed manager-owned reconciliation"
    )
    reconcile_parser.set_defaults(func=reconcile)

    uninstall_parser = sub.add_parser("uninstall", help="remove supervisor, manifest, managed mutations, and runtime files")
    _add_common(uninstall_parser)
    uninstall_parser.add_argument("--timeout", type=int, default=60)
    uninstall_parser.add_argument("--stop-timeout", type=int, default=20)
    uninstall_parser.add_argument("--keep-runtime", action="store_true")
    uninstall_parser.set_defaults(func=uninstall)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        _emit({"decision": "ERROR", "detail": str(exc)}, as_json=getattr(args, "json", False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
