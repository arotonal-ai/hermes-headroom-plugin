"""Portable, explicit Headroom runtime lifecycle manager.

This module is packaged with the Hermes plugin. It installs the official
Headroom distribution into an isolated venv, delegates persistent lifecycle to
Headroom's native ``install`` subsystem, and verifies the Hermes
compress -> retrieve contract. Plugin registration never invokes this module.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from .proxy import readyz, smoke

RUNTIME_VERSION = "0.32.0"
LITELLM_VERSION = "1.91.3"
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

# Headroom 0.32's public ``install apply`` always writes persistent shell
# environment blocks for user/system scope, even with manual providers and no
# targets. This pinned helper keeps upstream manifest/supervisor/runtime code
# but deliberately omits only provider/shell mutation activation.
_SAFE_APPLY_SCRIPT = r"""
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
    if root == Path(root.anchor) or root == Path.home().resolve() or root == _hermes_home().resolve():
        raise ValueError("runtime root must be a dedicated child directory, not /, HOME, or HERMES_HOME")
    return root


def _default_preset() -> str:
    return "persistent-task" if sys.platform.startswith("win") else "persistent-service"


def _proxy_url(port: int) -> str:
    return f"http://{DEFAULT_HOST}:{port}"


def _state_path(root: Path) -> Path:
    return root / STATE_FILE


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
            "venv_dir": str(_venv_dir(root)),
            "workspace_dir": str(_workspace_dir(root)),
            "proxy_url": _proxy_url(state.port),
        }
        actual = {
            "schema": state.schema,
            "host": state.host,
            "runtime_root": str(Path(state.runtime_root).expanduser().resolve()),
            "venv_dir": str(Path(state.venv_dir).expanduser().resolve()),
            "workspace_dir": str(Path(state.workspace_dir).expanduser().resolve()),
            "proxy_url": state.proxy_url,
        }
        if actual != expected:
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
    log: Path,
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
    _append_log(log, command, proc.stdout)
    return proc


def _runtime_env(root: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("HEADROOM_")}
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
            env=os.environ.copy(),
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
        env=os.environ.copy(),
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


def _tcp_port_open(port: int) -> bool:
    try:
        with socket.create_connection((DEFAULT_HOST, port), timeout=0.25):
            return True
    except OSError:
        return False


def _manifest_path(root: Path, profile: str) -> Path:
    return _workspace_dir(root) / "deploy" / profile / "manifest.json"


def _manifest_contract(
    root: Path, *, profile: str, port: int, preset: str
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
        "HEADROOM_WORKSPACE_DIR": str(_workspace_dir(root)),
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
    if data.get("base_env") != expected_env:
        mismatches.append("base_env")
    if data.get("proxy_args") != expected_proxy_args:
        mismatches.append("proxy_args")
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
            "environment_exact": data.get("base_env") == expected_env,
            "proxy_args_exact": data.get("proxy_args") == expected_proxy_args,
            "mismatches": mismatches,
        }
    )
    result["ok"] = not mismatches
    if mismatches:
        result["detail"] = "manifest failed complete manager-owned identity contract"
    return result

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
            upstream = None
            if managed_cli.is_file():
                upstream = _run(
                    [str(managed_cli), "install", "status", "--profile", profile],
                    timeout=30,
                    log=root / "manager.log",
                    env=_runtime_env(root),
                )
            verification = smoke(proxy_url)
            if (
                upstream is not None
                and upstream.returncode == 0
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
                    "next": "run headroom-runtime doctor, then uninstall before repair",
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
            _emit({**asdict(full), "decision": full.status, "smoke": verification}, as_json=args.json)
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
    payload = {**asdict(state), "readyz": health, "manifest_contract": manifest_contract}
    headroom = _exe(Path(state.venv_dir), "headroom")
    upstream_exit: int | None = None
    if headroom.is_file():
        proc = _run(
            [str(headroom), "install", "status", "--profile", state.profile],
            timeout=args.timeout,
            log=root / "manager.log",
            env=_runtime_env(root),
        )
        upstream_exit = proc.returncode
        payload["upstream_status_exit"] = proc.returncode
        payload["upstream_status"] = proc.stdout[-2000:]
    decision = (
        "RUNTIME_FULL_DURABLE"
        if health.get("ok") and upstream_exit == 0 and manifest_contract.get("ok")
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
    upstream: dict[str, Any] = {"available": headroom.is_file()}
    if headroom.is_file():
        proc = _run(
            [str(headroom), "install", "status", "--profile", state.profile],
            timeout=args.timeout,
            log=root / "manager.log",
            env=_runtime_env(root),
        )
        upstream.update({"exit_code": proc.returncode, "output": proc.stdout[-2000:]})
    full = bool(
        health.get("ok")
        and verification.get("ok")
        and verification.get("sentinel_found") is True
        and upstream.get("exit_code") == 0
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
            "upstream_status": upstream,
            "next": None if full else "inspect manager.log/install.log or run headroom-runtime uninstall",
        },
        as_json=args.json,
    )
    return 0 if full else 2


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
            root, profile=state.profile, port=state.port, preset=state.preset
        )
        if not manifest_contract.get("ok"):
            _emit(
                {
                    "decision": "UNINSTALL_BLOCKED",
                    "detail": "saved manifest failed the no-target/no-mutation contract; no upstream mutation was invoked",
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
        if health.get("ok"):
            _emit(
                {
                    "decision": "UNINSTALL_PARTIAL",
                    "profile": state.profile,
                    "proxy_url": state.proxy_url,
                    "detail": "upstream remove returned success but the managed listener is still ready",
                    "next": f"runtime files were preserved at {root}",
                },
                as_json=args.json,
            )
            return 2
        if not args.keep_runtime:
            shutil.rmtree(root)
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
