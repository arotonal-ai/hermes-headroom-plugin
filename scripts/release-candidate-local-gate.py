#!/usr/bin/env python3
"""Local release-candidate gate for the Hermes Headroom plugin.

This is intentionally repo-portable: it derives paths from this checkout, uses
venvs/temp homes under the requested run directory, starts only loopback
Headroom proxy processes, and does not push, tag, publish, or mutate the real
Hermes profile.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.request
import venv
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PROJECT_VERSION = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
EXPECTED_PLUGIN_SPEC = f"hermes-headroom-plugin=={PROJECT_VERSION}"
HEADROOM_RUNTIME_VERSION = "0.32.1"
DEFAULT_HEADROOM_SPEC = f"headroom-ai[proxy]=={HEADROOM_RUNTIME_VERSION}"
LITELLM_RUNTIME_VERSION = "1.91.3"
DEFAULT_LITELLM_SPEC = f"litellm=={LITELLM_RUNTIME_VERSION}"
COMPAT_HEADROOM_RUNTIME_VERSION = "0.31.0"
DEFAULT_COMPAT_HEADROOM_SPEC = f"headroom-ai[proxy]=={COMPAT_HEADROOM_RUNTIME_VERSION}"
PREVIOUS_PUBLIC_VERSION = "0.5.2"
PREVIOUS_PUBLIC_REF = f"v{PREVIOUS_PUBLIC_VERSION}"
EPHEMERAL_ENV_DIRS = (
    "build-venv",
    "pytest-venv",
    "wheel-install-venv",
    "upgrade-rollback-venv",
    "headroom-runtime-venv",
    "temp-headroom-home",
    "workload-venv",
)
PUBLIC_SCAN_PATHS = [
    "README.md",
    "INSTALL.md",
    "AGENTS.md",
    "SECURITY.md",
    "PRIVACY.md",
    "ACKNOWLEDGEMENTS.md",
    "docs",
    "scripts",
    "src",
    "plugin.yaml",
    "pyproject.toml",
    ".github/workflows",
]
OWNER_LOCAL_PATTERNS = [
    re.compile(r"/home/[A-Za-z0-9._-]+"),
    re.compile(r"/Users/[A-Za-z0-9._-]+"),
]
SECRET_PATTERNS = [
    re.compile(r"gho_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile("-----BEGIN " + r"(?:RSA |OPENSSH |EC |DSA |)PRIVATE KEY-----"),
]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _tree_size_bytes(path: Path) -> int:
    total = 0
    for root, dirs, names in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
        for name in names:
            candidate = Path(root) / name
            if candidate.is_symlink():
                continue
            with contextlib.suppress(OSError):
                total += candidate.stat().st_size
    return total


def cleanup_ephemeral_envs(run_dir: Path, *, keep: bool = False) -> dict[str, Any]:
    """Remove only allowlisted per-run virtualenvs after evidence is written.

    Reports, logs, package artifacts, command receipts, temporary Hermes-home
    payloads and workload matrices remain.  The strict name/parent/symlink
    guards prevent this retention step from becoming a general deletion API.
    """
    root = run_dir.resolve()
    entries: list[dict[str, Any]] = []
    removed_bytes = 0
    for name in EPHEMERAL_ENV_DIRS:
        candidate = run_dir / name
        entry: dict[str, Any] = {"name": name, "existed": candidate.exists()}
        if not candidate.exists():
            entry["status"] = "absent"
            entries.append(entry)
            continue
        resolved = candidate.resolve()
        if candidate.is_symlink() or resolved.parent != root or resolved.name != name:
            entry.update({"status": "blocked", "error": "path_guard_failed"})
            entries.append(entry)
            continue
        size = _tree_size_bytes(candidate)
        entry["bytes"] = size
        if keep:
            entry["status"] = "retained_by_request"
        else:
            try:
                shutil.rmtree(candidate)
            except OSError as exc:
                entry.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
            else:
                entry["status"] = "removed"
                removed_bytes += size
        entries.append(entry)
    errors = [entry for entry in entries if entry["status"] in {"blocked", "error"}]
    return {
        "pass": not errors,
        "keep_ephemeral_envs": keep,
        "allowlist": list(EPHEMERAL_ENV_DIRS),
        "removed_bytes": removed_bytes,
        "entries": entries,
        "errors": errors,
    }


def bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def run(cmd: list[str], *, cwd: Path = REPO, timeout: int = 600, env: dict[str, str] | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=env,
            check=False,
        )
        return {"cmd": cmd, "returncode": proc.returncode, "stdout": proc.stdout, "duration_s": round(time.perf_counter() - started, 3)}
    except Exception as exc:  # noqa: BLE001
        return {"cmd": cmd, "returncode": 127, "stdout": f"{type(exc).__name__}: {exc}", "duration_s": round(time.perf_counter() - started, 3)}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def release_gate_lock(lock_dir: Path) -> None:
    """Release only the exact atomic lock directory created by this gate."""
    with contextlib.suppress(FileNotFoundError):
        (lock_dir / "owner.json").unlink()
    with contextlib.suppress(FileNotFoundError):
        lock_dir.rmdir()


def acquire_gate_lock(run_root: Path, *, register_atexit: bool = True) -> Path:
    """Fail closed when another RC gate is using the same evidence root."""
    lock_dir = run_root / ".release-candidate-local-gate.lock"
    lock_dir.mkdir()
    write_json(
        lock_dir / "owner.json",
        {"pid": os.getpid(), "repo": str(REPO), "started_at": utc_iso(), "head": git_head()},
    )
    if register_atexit:
        atexit.register(release_gate_lock, lock_dir)
    return lock_dir


def checkout_snapshot() -> dict[str, Any]:
    """Capture the committed identity and all tracked/untracked checkout drift."""
    head = run(["git", "rev-parse", "HEAD"], timeout=30)
    tree = run(["git", "rev-parse", "HEAD^{tree}"], timeout=30)
    status = run(["git", "status", "--short", "--untracked-files=all"], timeout=30)
    return {
        "commands_ok": all(item["returncode"] == 0 for item in (head, tree, status)),
        "head": head["stdout"].strip(),
        "tree": tree["stdout"].strip(),
        "status_short": status["stdout"],
        "status_returncode": status["returncode"],
    }


def free_loopback_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_readyz(proxy_url: str, *, timeout: int = 90) -> dict[str, Any]:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{proxy_url}/readyz", timeout=3) as resp:  # noqa: S310 loopback gate
                body = resp.read().decode("utf-8", "replace")
                data = json.loads(body)
                if resp.status == 200 and isinstance(data, dict) and data.get("ready", True):
                    return {"ok": True, "status": resp.status, "body": data}
                last = body[:500]
        except Exception as exc:  # pragma: no cover - timing/platform dependent
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
    return {"ok": False, "last": last}


def create_venv(venv_dir: Path) -> Path:
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    return bin_dir(venv_dir) / exe("python")


def git_head() -> str:
    result = run(["git", "rev-parse", "HEAD"], timeout=30)
    return result["stdout"].strip() if result["returncode"] == 0 else "unknown"


def tracked_files() -> list[Path]:
    result = run(["git", "ls-files"], timeout=60)
    if result["returncode"] == 0:
        return [REPO / line for line in result["stdout"].splitlines() if line.strip()]
    files: list[Path] = []
    for root, dirs, names in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "__pycache__", ".pytest_cache", "build", "dist", "release-candidate-runs"}]
        files.extend(Path(root) / n for n in names)
    return files


def public_path_scan() -> dict[str, Any]:
    allowed_roots = [(REPO / p).resolve() for p in PUBLIC_SCAN_PATHS]
    hits: list[dict[str, Any]] = []
    scanned = 0
    for path in tracked_files():
        resolved = path.resolve()
        if not any(resolved == root or root in resolved.parents for root in allowed_roots):
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        scanned += 1
        rel = str(path.relative_to(REPO))
        for i, line in enumerate(text.splitlines(), 1):
            for pattern in OWNER_LOCAL_PATTERNS:
                if pattern.search(line):
                    hits.append({"file": rel, "line": i, "kind": "owner_local_path", "pattern": pattern.pattern})
            for pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    hits.append({"file": rel, "line": i, "kind": "secret_pattern", "pattern": pattern.pattern})
    return {"pass": not hits, "scanned_files": scanned, "hits": hits}


def archive_members(path: Path) -> list[str]:
    if path.suffix == ".whl" or path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            return zf.namelist()
    if path.suffixes[-2:] == [".tar", ".gz"] or path.suffix in {".tgz", ".gz"}:
        with tarfile.open(path, "r:*") as tf:
            return tf.getnames()
    return []


def read_archive_texts(path: Path, limit_bytes: int = 80_000) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if path.suffix == ".whl" or path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith((".py", ".md", ".toml", ".yaml", ".yml", ".txt", ".sh")):
                    out.append((name, zf.read(name)[:limit_bytes].decode("utf-8", "ignore")))
    else:
        with tarfile.open(path, "r:*") as tf:
            for member in tf.getmembers():
                if member.isfile() and member.name.endswith((".py", ".md", ".toml", ".yaml", ".yml", ".txt", ".sh")):
                    fh = tf.extractfile(member)
                    if fh:
                        out.append((member.name, fh.read(limit_bytes).decode("utf-8", "ignore")))
    return out


def portable_core_version_issues(artifact: Path, archive_texts: list[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    texts = read_archive_texts(artifact) if archive_texts is None else archive_texts
    portable_core_docs = [(member, text) for member, text in texts if member.endswith("portable-core.md")]
    if not portable_core_docs:
        return [{"artifact": artifact.name, "kind": "missing_portable_core_doc"}]

    expected_row = f"| Plugin | `{EXPECTED_PLUGIN_SPEC}` |"
    issues: list[dict[str, Any]] = []
    for member, text in portable_core_docs:
        found_rows = [line.strip() for line in text.splitlines() if "hermes-headroom-plugin==" in line]
        if found_rows != [expected_row]:
            issues.append(
                {
                    "artifact": artifact.name,
                    "member": member,
                    "kind": "portable_core_plugin_version_mismatch",
                    "expected": expected_row,
                    "found": found_rows,
                }
            )
    return issues


def build_and_inspect(run_dir: Path) -> dict[str, Any]:
    venv_dir = run_dir / "build-venv"
    python = create_venv(venv_dir)
    dist_dir = run_dir / "dist"
    steps = [
        run([str(python), "-m", "pip", "install", "--upgrade", "pip", "build"], timeout=300),
        run([str(python), "-m", "build", "--sdist", "--wheel", "--outdir", str(dist_dir)], timeout=300),
    ]
    artifacts = sorted(dist_dir.glob("*")) if dist_dir.exists() else []
    issues: list[dict[str, Any]] = []
    for artifact in artifacts:
        members = archive_members(artifact)
        required_members = {
            "missing_migration_doc": "/docs/MIGRATION-v0.4.md",
            "missing_install_doc": "/INSTALL.md",
            "missing_git_runtime_launcher": "/scripts/headroom-runtime.py",
            "missing_lifecycle_canary": "/scripts/test-runtime-manager-lifecycle.py",
        }
        if artifact.suffixes[-2:] == [".tar", ".gz"]:
            for kind, suffix in required_members.items():
                if not any(member.endswith(suffix) for member in members):
                    issues.append({"artifact": artifact.name, "kind": kind})
        for member in members:
            lowered = member.lower()
            if any(bad in lowered for bad in (".git/", ".venv/", "__pycache__", ".pytest_cache", "release-candidate-runs")):
                issues.append({"artifact": artifact.name, "member": member, "kind": "forbidden_member"})
        archive_texts = read_archive_texts(artifact)
        issues.extend(portable_core_version_issues(artifact, archive_texts))
        for member, text in archive_texts:
            for pattern in OWNER_LOCAL_PATTERNS:
                if pattern.search(text):
                    issues.append({"artifact": artifact.name, "member": member, "kind": "owner_local_path", "pattern": pattern.pattern})
            for pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    issues.append({"artifact": artifact.name, "member": member, "kind": "secret_pattern", "pattern": pattern.pattern})
    return {
        "pass": all(s["returncode"] == 0 for s in steps) and len(artifacts) >= 2 and not issues,
        "steps": [{"cmd": s["cmd"], "returncode": s["returncode"], "duration_s": s["duration_s"], "stdout_tail": s["stdout"][-3000:]} for s in steps],
        "artifacts": [str(p) for p in artifacts],
        "issues": issues,
    }


def pytest_gate(run_dir: Path) -> dict[str, Any]:
    venv_dir = run_dir / "pytest-venv"
    python = create_venv(venv_dir)
    steps = [
        run([str(python), "-m", "pip", "install", "--upgrade", "pip"], timeout=240),
        run([str(python), "-m", "pip", "install", "-e", f"{REPO}[test]"], timeout=360),
        run([str(python), "-m", "pytest", "-q"], timeout=480),
    ]
    return {"pass": all(s["returncode"] == 0 for s in steps), "venv": str(venv_dir), "steps": [{"cmd": s["cmd"], "returncode": s["returncode"], "duration_s": s["duration_s"], "stdout_tail": s["stdout"][-3000:]} for s in steps]}


def wheel_install_gate(run_dir: Path, build_gate: dict[str, Any]) -> dict[str, Any]:
    wheels = [Path(p) for p in build_gate.get("artifacts", []) if str(p).endswith(".whl")]
    if not wheels:
        return {"pass": False, "error": "no wheel artifact"}
    venv_dir = run_dir / "wheel-install-venv"
    python = create_venv(venv_dir)
    wheel = wheels[0]
    steps = [
        run([str(python), "-m", "pip", "install", "--upgrade", "pip"], timeout=240),
        run([str(python), "-m", "pip", "install", str(wheel)], timeout=300),
        run([str(python), "-m", "pip", "check"], timeout=60),
    ]
    checks = []
    for name in ("headroom-worker-lane", "headroom-background-lane", "headroom-command-preflight", "headroom-health-audit", "headroom-proxy-start", "headroom-runtime"):
        checks.append(run([str(bin_dir(venv_dir) / exe(name)), "--help"], timeout=60))
    import_check = run([str(python), "-c", "import hermes_headroom_plugin, importlib.metadata as m; print(m.version('hermes-headroom-plugin'))"], timeout=60)
    return {
        "pass": all(s["returncode"] == 0 for s in steps) and all(c["returncode"] == 0 for c in checks) and import_check["returncode"] == 0,
        "wheel": str(wheel),
        "venv": str(venv_dir),
        "steps": [{"cmd": s["cmd"], "returncode": s["returncode"], "duration_s": s["duration_s"], "stdout_tail": s["stdout"][-1500:]} for s in steps],
        "checks": [{"cmd": c["cmd"], "returncode": c["returncode"], "duration_s": c["duration_s"], "stdout_head": c["stdout"][:800]} for c in checks],
        "import_check": {"returncode": import_check["returncode"], "stdout": import_check["stdout"].strip()},
    }


def package_upgrade_rollback_gate(run_dir: Path, build_gate: dict[str, Any]) -> dict[str, Any]:
    """Prove v0.5.2 -> RC -> v0.5.2 package transitions in an isolated venv."""
    rc_wheels = [Path(p) for p in build_gate.get("artifacts", []) if str(p).endswith(".whl")]
    build_python = bin_dir(run_dir / "build-venv") / exe("python")
    if not rc_wheels or not build_python.exists():
        return {"pass": False, "error": "missing RC wheel or build environment"}

    previous_archive = run_dir / f"{PREVIOUS_PUBLIC_REF}-source.tar"
    previous_source = run_dir / f"{PREVIOUS_PUBLIC_REF}-source"
    previous_dist = run_dir / f"{PREVIOUS_PUBLIC_REF}-dist"
    previous_source.mkdir(parents=True, exist_ok=True)
    previous_dist.mkdir(parents=True, exist_ok=True)
    steps: list[dict[str, Any]] = []
    archive = run(
        ["git", "archive", "--format=tar", "--output", str(previous_archive), PREVIOUS_PUBLIC_REF],
        timeout=60,
    )
    steps.append(archive)
    if archive["returncode"] == 0:
        try:
            root = previous_source.resolve()
            with tarfile.open(previous_archive, "r") as tf:
                for member in tf.getmembers():
                    destination = (previous_source / member.name).resolve()
                    if root not in destination.parents and destination != root:
                        raise ValueError(f"unsafe archive member: {member.name}")
                tf.extractall(previous_source)
        except Exception as exc:  # noqa: BLE001
            steps.append({"cmd": ["safe_extract", str(previous_archive)], "returncode": 1, "stdout": f"{type(exc).__name__}: {exc}", "duration_s": 0})

    if all(step["returncode"] == 0 for step in steps):
        steps.append(
            run(
                [str(build_python), "-m", "build", "--wheel", "--outdir", str(previous_dist)],
                cwd=previous_source,
                timeout=300,
            )
        )
    previous_wheels = sorted(previous_dist.glob("*.whl"))
    if not previous_wheels:
        return {
            "pass": False,
            "previous_ref": PREVIOUS_PUBLIC_REF,
            "rc_wheel": str(rc_wheels[0]),
            "steps": [{"cmd": step.get("cmd"), "returncode": step.get("returncode"), "stdout_tail": str(step.get("stdout", ""))[-2000:]} for step in steps],
            "error": "previous wheel build failed",
        }

    venv_dir = run_dir / "upgrade-rollback-venv"
    python = create_venv(venv_dir)
    previous_wheel = previous_wheels[0]
    rc_wheel = rc_wheels[0]
    transitions = [
        ("install_previous", [str(python), "-m", "pip", "install", str(previous_wheel)]),
        ("upgrade_to_rc", [str(python), "-m", "pip", "install", "--upgrade", str(rc_wheel)]),
        ("rollback_to_previous", [str(python), "-m", "pip", "install", "--force-reinstall", str(previous_wheel)]),
    ]
    expected = [PREVIOUS_PUBLIC_VERSION, PROJECT_VERSION, PREVIOUS_PUBLIC_VERSION]
    observed: list[dict[str, Any]] = []
    steps.append(run([str(python), "-m", "pip", "install", "--upgrade", "pip"], timeout=240))
    for (name, cmd), expected_version in zip(transitions, expected, strict=True):
        install = run(cmd, timeout=300)
        steps.append(install)
        probe = run(
            [str(python), "-c", "import hermes_headroom_plugin, importlib.metadata as m; print(m.version('hermes-headroom-plugin'))"],
            timeout=60,
        )
        steps.append(probe)
        observed.append(
            {
                "transition": name,
                "expected": expected_version,
                "observed": probe["stdout"].strip(),
                "pass": install["returncode"] == 0 and probe["returncode"] == 0 and probe["stdout"].strip() == expected_version,
            }
        )
    entrypoint = run([str(bin_dir(venv_dir) / exe("headroom-runtime")), "--help"], timeout=60)
    steps.append(entrypoint)
    return {
        "pass": all(step["returncode"] == 0 for step in steps) and all(item["pass"] for item in observed),
        "previous_ref": PREVIOUS_PUBLIC_REF,
        "previous_wheel": str(previous_wheel),
        "rc_wheel": str(rc_wheel),
        "transitions": observed,
        "steps": [
            {"cmd": step.get("cmd"), "returncode": step.get("returncode"), "duration_s": step.get("duration_s"), "stdout_tail": str(step.get("stdout", ""))[-1500:]}
            for step in steps
        ],
    }


def isolated_runtime_env(run_dir: Path) -> dict[str, str]:
    """Return a memory-only Headroom environment contained inside one RC run."""
    runtime_home = run_dir / "temp-headroom-home"
    workspace = runtime_home / ".headroom"
    workspace.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(runtime_home),
            "USERPROFILE": str(runtime_home),
            "HEADROOM_WORKSPACE_DIR": str(workspace),
            "HEADROOM_CONFIG_DIR": str(workspace / "config"),
            "HEADROOM_CCR_BACKEND": "memory",
            "HEADROOM_CCR_TTL_SECONDS": "1800",
            "HEADROOM_TELEMETRY": "off",
            "HEADROOM_MEMORY_ENABLED": "0",
            "HEADROOM_MEMORY_LEARN": "0",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def start_proxy_with_fresh_runtime(run_dir: Path, spec: str, litellm_spec: str, install_timeout: int) -> tuple[subprocess.Popen[str] | None, str, Path, dict[str, Any]]:
    runtime_dir = run_dir / "headroom-runtime-venv"
    python = create_venv(runtime_dir)
    log = run_dir / "logs" / "headroom-runtime.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = isolated_runtime_env(run_dir)
    for cmd in ([str(python), "-m", "pip", "install", "--upgrade", "pip"], [str(python), "-m", "pip", "install", spec, litellm_spec]):
        result = run(cmd, timeout=install_timeout, env=env)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"\n$ {' '.join(cmd)}\n{result['stdout']}\n")
        if result["returncode"] != 0:
            return None, "", log, {"ok": False, "phase": "install", "cmd": cmd, "stdout_tail": result["stdout"][-3000:]}
    port = free_loopback_port()
    proxy_url = f"http://127.0.0.1:{port}"
    headroom = bin_dir(runtime_dir) / exe("headroom")
    fh = log.open("a", encoding="utf-8")
    proc = subprocess.Popen([str(headroom), "proxy", "--host", "127.0.0.1", "--port", str(port), "--no-telemetry"], cwd=str(REPO), env=env, stdout=fh, stderr=subprocess.STDOUT, text=True)
    setattr(proc, "_headroom_log_fh", fh)
    ready = wait_readyz(proxy_url)
    return proc, proxy_url, log, ready


def stop_proxy(proc: subprocess.Popen[str] | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    fh = getattr(proc, "_headroom_log_fh", None)
    if fh:
        fh.close()


def bulky_log(label: str, lines: int) -> str:
    return "\n".join(
        f"level={'ERROR' if i % 97 == 0 else 'WARNING' if i % 53 == 0 else 'INFO'} run_id={label}-run task_id={label}-task line={i} status={'failed' if i % 97 == 0 else 'ok'} path=/tmp/{label}/artifact-{i % 19}.log sentinel={label.upper()}_RC_SENTINEL diagnostic='bulky intermediate trace for release candidate gate'"
        for i in range(lines)
    ) + "\n"


def browser_trace(lines: int) -> str:
    return "\n".join(
        f"browser event_index={i} session_id=rc-sess-{i%5} frame_id=frame-{i%7} target_id=target-{i%3} selector=#node-{i%31} bounds=10,20,300,40 source_url=https://example.test/page/{i%11} status={'warning' if i%89==0 else 'ok'} sentinel=BROWSER_RC_SENTINEL message='DOM diagnostic event {i}'"
        for i in range(lines)
    ) + "\n"


def research_corpus(lines: int) -> str:
    return "\n".join(
        f"source_url=https://example.org/paper/{i%23} document_id=doc-{i%23} citation=[{i}] title='Portable Headroom Evidence {i}' sentinel=RESEARCH_RC_SENTINEL excerpt='semantic retrieval and context optimization repeated bulky corpus line {i}'"
        for i in range(lines)
    ) + "\n"


def workload_matrix(run_dir: Path, proxy_url: str, wheel_gate: dict[str, Any]) -> dict[str, Any]:
    wheels = [Path(wheel_gate["wheel"])] if wheel_gate.get("wheel") else []
    venv_dir = run_dir / "workload-venv"
    python = create_venv(venv_dir)
    install = [
        run([str(python), "-m", "pip", "install", "--upgrade", "pip"], timeout=240),
        run([str(python), "-m", "pip", "install", str(wheels[0])], timeout=300) if wheels else {"returncode": 1, "stdout": "missing wheel", "cmd": [], "duration_s": 0},
    ]
    hermes_home = run_dir / "temp-hermes-home-workload"
    hermes_home.mkdir(parents=True, exist_ok=True)
    payload_dir = run_dir / "workload-payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)
    cases = [
        {"name": "terminal_qa_build_log", "tool": "terminal", "args": {"command": "pytest tests --maxfail=1", "lane": "qa", "data_class": "qa_trace"}, "body": bulky_log("qa", 5200), "expect": "compress", "sentinel": "QA_RC_SENTINEL"},
        {"name": "delegate_subagent_trace", "tool": "delegate_task", "args": {"goal": "delegate bulky trace", "lane": "delegate", "data_class": "worker_trace_raw"}, "body": bulky_log("delegate", 5000), "expect": "compress", "sentinel": "DELEGATE_RC_SENTINEL"},
        {"name": "browser_debug_trace", "tool": "browser_snapshot", "args": {"lane": "browser debug", "data_class": "browser_debug_trace"}, "body": browser_trace(4200), "expect": "compress", "sentinel": "BROWSER_RC_SENTINEL"},
        {"name": "research_corpus_web_extract", "tool": "web_extract", "args": {"lane": "research", "data_class": "research_corpus"}, "body": research_corpus(4200), "expect": "compress", "sentinel": "RESEARCH_RC_SENTINEL"},
        {"name": "exact_git_diff_negative", "tool": "terminal", "args": {"command": "git diff -- README.md", "lane": "dev"}, "body": "*** Begin Patch\n" + bulky_log("diff", 2500) + "*** End Patch\n", "expect": "exact", "sentinel": "DIFF_RC_SENTINEL"},
        {"name": "secret_material_negative", "tool": "terminal", "args": {"command": "diagnostic", "lane": "diagnostic"}, "body": ("-----BEGIN " + "PRIVATE KEY-----\n" + ("[REDACTED KEY MATERIAL]\n" * 500) + "-----END " + "PRIVATE KEY-----\n"), "expect": "exact", "sentinel": "SECRET_RC_SENTINEL"},
        {"name": "worker_final_packet_negative", "tool": "delegate_task", "args": {"goal": "return final packet", "lane": "delegate"}, "body": "# Worker Final Packet\n\nstatus: PASS\nclaim_ledger: exact\n" + bulky_log("final", 2200), "expect": "exact", "sentinel": "FINAL_RC_SENTINEL"},
    ]
    results: list[dict[str, Any]] = []
    for case in cases:
        payload_path = payload_dir / f"{case['name']}.txt"
        payload_path.write_text(case["body"], encoding="utf-8")
        code = f"""
import json, os, re
from pathlib import Path
from hermes_headroom_plugin.middleware import compress_tool_result_for_context
from hermes_headroom_plugin.tools import handle_headroom_retrieve
body = Path({str(payload_path)!r}).read_text(encoding='utf-8', errors='replace')
report_dir = Path(os.environ['HERMES_HOME'])/'control-plane'/'headroom'/'reports'
before_reports = set(report_dir.glob('auto-tool-*.json')) if report_dir.exists() else set()
result = compress_tool_result_for_context(tool_name={case['tool']!r}, args={case['args']!r}, result=body, task_id={case['name']!r}, tool_call_id={case['name']!r})
text = result or ''
marker_match = re.search(r"marker=([^\\s\\]]+)", text)
marker = marker_match.group(1) if marker_match else ''
after_reports = sorted(p for p in report_dir.glob('auto-tool-*.json') if p not in before_reports) if report_dir.exists() else []
report_data = {{}}
if after_reports:
    report_data = json.loads(after_reports[-1].read_text(encoding='utf-8'))
source_path = Path(report_data.get('source_path', '')) if report_data.get('source_path') else None
source_has_sentinel = bool(source_path and source_path.exists() and {case['sentinel']!r} in source_path.read_text(encoding='utf-8', errors='replace'))
retrieve = {{}}
retrieve_has_sentinel = False
if marker:
    retrieve = json.loads(handle_headroom_retrieve({{'hash': marker}}))
    retrieve_has_sentinel = {case['sentinel']!r} in json.dumps(retrieve, ensure_ascii=False)
out = {{
  'name': {case['name']!r},
  'expect': {case['expect']!r},
  'compressed': result is not None,
  'contains_auto_header': 'Headroom auto-compressed tool result' in text,
  'contains_private_key': 'PRIVATE KEY' in text,
  'marker': marker,
  'tokens_saved': report_data.get('tokens_saved'),
  'source_retained': bool(source_path and source_path.exists()),
  'source_has_sentinel': source_has_sentinel,
  'retrieve_success': retrieve.get('success'),
  'retrieve_has_sentinel': retrieve_has_sentinel,
  'report_data_class': report_data.get('data_class'),
}}
if out['expect'] == 'compress':
    out['pass'] = bool(out['compressed'] and out['contains_auto_header'] and not out['contains_private_key'] and out['source_retained'] and out['source_has_sentinel'] and isinstance(out.get('tokens_saved'), int) and out['tokens_saved'] > 1000)
else:
    out['pass'] = bool(
        not out['compressed']
        and not out['contains_auto_header']
        and not out['marker']
        and not out['source_retained']
        and out['tokens_saved'] is None
    )
print(json.dumps(out, sort_keys=True))
raise SystemExit(0 if out['pass'] else 1)
""".strip()
        env = os.environ.copy()
        env.update({"HEADROOM_PROXY_URL": proxy_url, "HERMES_HOME": str(hermes_home), "HEADROOM_TELEMETRY": "off"})
        result = run([str(python), "-c", code], timeout=180, env=env)
        try:
            parsed = json.loads(result["stdout"].strip().splitlines()[-1])
        except Exception:
            parsed = {"name": case["name"], "expect": case["expect"], "pass": False, "parse_error": True, "stdout_tail": result["stdout"][-2000:]}
        parsed["returncode"] = result["returncode"]
        parsed["duration_s"] = result["duration_s"]
        results.append(parsed)
    return {
        "pass": all(s["returncode"] == 0 for s in install) and all(r.get("pass") for r in results),
        "install": [{"cmd": s.get("cmd"), "returncode": s.get("returncode"), "duration_s": s.get("duration_s"), "stdout_tail": str(s.get("stdout", ""))[-1200:]} for s in install],
        "hermes_home": str(hermes_home),
        "results": results,
    }


def headroom_proxy_processes() -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    proc_root = Path("/proc")
    if proc_root.exists():
        for p in proc_root.iterdir():
            if not p.name.isdigit():
                continue
            try:
                raw = (p / "cmdline").read_bytes()
            except Exception:
                continue
            argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
            is_headroom_cli = any(Path(item).name.lower() in {"headroom", "headroom.exe"} for item in argv)
            if argv and is_headroom_cli and "proxy" in argv:
                matches.append({"pid": p.name, "argv": argv})
    return matches


def no_new_leftover_proxy(baseline: list[dict[str, Any]]) -> dict[str, Any]:
    """Allow pre-existing owner runtimes and reject only proxies leaked by this run."""
    current = headroom_proxy_processes()
    baseline_pids = {item["pid"] for item in baseline}
    new_matches = [item for item in current if item["pid"] not in baseline_pids]
    return {
        "pass": not new_matches,
        "baseline_headroom_proxy_processes": baseline,
        "current_headroom_proxy_processes": current,
        "new_headroom_proxy_processes": new_matches,
    }


def write_report(run_dir: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Hermes Headroom plugin release-candidate local gate",
        "",
        f"Generated UTC: {summary['generated_at']}",
        "",
        "## Decision",
        "",
        f"`{summary['decision']}`",
        "",
        "## Gates",
        "",
        "| Gate | Pass | Evidence |",
        "|---|---:|---|",
    ]
    for name, gate in summary["gates"].items():
        lines.append(f"| `{name}` | `{gate.get('pass')}` | `{gate.get('evidence', '')}` |")
    matrix = summary.get("workload_matrix", {})
    lines += ["", "## Bulky workload matrix", "", "| Case | Expect | Pass | Compressed | Source retained | Tokens saved | Retrieve sentinel |", "|---|---|---:|---:|---:|---:|---:|"]
    for row in matrix.get("results", []):
        lines.append(
            f"| `{row.get('name')}` | `{row.get('expect')}` | `{row.get('pass')}` | `{row.get('compressed')}` | `{row.get('source_retained')}` | `{row.get('tokens_saved')}` | `{row.get('retrieve_has_sentinel')}` |"
        )
    lines += [
        "",
        "## Scope",
        "",
        "- This gate is local-only. It does not push, tag, publish, or mutate the real Hermes profile.",
        "- PASS means the checkout is ready for owner review as a local release candidate, not that public release is authorized.",
        "- Public release still requires explicit owner approval, final diff review, remote CI readback, and release notes.",
    ]
    (run_dir / "RELEASE_CANDIDATE_LOCAL_GATE_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local release-candidate gate for Hermes Headroom plugin.")
    parser.add_argument("--run-root", default=str(REPO / "release-candidate-runs"), help="directory for gate evidence")
    parser.add_argument("--headroom-spec", default=os.environ.get("HEADROOM_AI_SPEC", DEFAULT_HEADROOM_SPEC))
    parser.add_argument("--litellm-spec", default=os.environ.get("HEADROOM_LITELLM_SPEC", DEFAULT_LITELLM_SPEC))
    parser.add_argument("--compat-headroom-spec", default=os.environ.get("HEADROOM_COMPAT_AI_SPEC", DEFAULT_COMPAT_HEADROOM_SPEC))
    parser.add_argument("--compat-litellm-spec", default=os.environ.get("HEADROOM_COMPAT_LITELLM_SPEC", DEFAULT_LITELLM_SPEC))
    parser.add_argument("--install-timeout", type=int, default=int(os.environ.get("HEADROOM_DEP_INSTALL_TIMEOUT", "600")))
    parser.add_argument(
        "--run-durable-lifecycle",
        action="store_true",
        help="exercise the real native user supervisor; omitted locally unless explicitly authorized",
    )
    parser.add_argument(
        "--keep-ephemeral-envs",
        action="store_true",
        help="retain allowlisted per-run virtualenvs for explicit debugging (default: remove after evidence capture)",
    )
    args = parser.parse_args(argv)

    run_root = Path(args.run_root).expanduser().resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    try:
        acquire_gate_lock(run_root)
    except FileExistsError:
        owner_path = run_root / ".release-candidate-local-gate.lock" / "owner.json"
        owner = owner_path.read_text(encoding="utf-8", errors="replace") if owner_path.exists() else "unknown"
        print(json.dumps({"decision": "RC_GATE_CONCURRENT_RUN_BLOCKED", "lock_owner": owner}, ensure_ascii=False))
        return 2

    run_dir = run_root / f"{utc_stamp()}-release-candidate-local-gate"
    run_dir.mkdir(parents=True, exist_ok=True)
    gates: dict[str, dict[str, Any]] = {}
    initial_checkout = checkout_snapshot()
    write_json(run_dir / "initial-checkout.json", initial_checkout)
    baseline_proxies = headroom_proxy_processes()
    write_json(run_dir / "preexisting-proxies.json", baseline_proxies)

    audit = run(["bash", "scripts/audit-repo-readiness.sh"], timeout=240)
    write_json(run_dir / "commands" / "audit-repo-readiness.json", audit)
    gates["repo_readiness_audit"] = {"pass": audit["returncode"] == 0, "evidence": str(run_dir / "commands" / "audit-repo-readiness.json")}

    context_loop = run([sys.executable, "scripts/context-economy-loop-gate.py"], timeout=args.install_timeout + 300)
    write_json(run_dir / "commands" / "context-economy-loop-gate.json", context_loop)
    gates["context_economy_loop_gate"] = {"pass": context_loop["returncode"] == 0, "evidence": str(run_dir / "commands" / "context-economy-loop-gate.json")}

    public_scan = public_path_scan()
    write_json(run_dir / "public-path-secret-scan.json", public_scan)
    gates["public_path_secret_scan"] = {"pass": public_scan.get("pass"), "evidence": str(run_dir / "public-path-secret-scan.json")}

    pytest_result = pytest_gate(run_dir)
    write_json(run_dir / "pytest-gate.json", pytest_result)
    gates["unit_contract_tests"] = {"pass": pytest_result.get("pass"), "evidence": str(run_dir / "pytest-gate.json")}

    build_result = build_and_inspect(run_dir)
    write_json(run_dir / "build-and-archive-inspection.json", build_result)
    gates["build_and_archive_inspection"] = {"pass": build_result.get("pass"), "evidence": str(run_dir / "build-and-archive-inspection.json")}

    wheel_result = wheel_install_gate(run_dir, build_result)
    write_json(run_dir / "wheel-install-entrypoints.json", wheel_result)
    gates["wheel_install_entrypoints"] = {"pass": wheel_result.get("pass"), "evidence": str(run_dir / "wheel-install-entrypoints.json")}

    upgrade_rollback = package_upgrade_rollback_gate(run_dir, build_result)
    write_json(run_dir / "package-upgrade-rollback.json", upgrade_rollback)
    gates["package_upgrade_rollback"] = {"pass": upgrade_rollback.get("pass"), "evidence": str(run_dir / "package-upgrade-rollback.json")}

    manager_exe = bin_dir(Path(wheel_result.get("venv", ""))) / exe("headroom-runtime")
    lifecycle_report = run_dir / "runtime-manager-lifecycle.json"
    lifecycle_log = run_dir / "logs" / "runtime-manager-lifecycle.log"
    if args.run_durable_lifecycle:
        lifecycle = run(
            [
                sys.executable,
                "scripts/test-runtime-manager-lifecycle.py",
                "--manager-command",
                str(manager_exe),
                "--runtime-root",
                str(run_dir / "managed-runtime"),
                "--headroom-spec",
                args.headroom_spec,
                "--litellm-spec",
                args.litellm_spec,
                "--install-timeout",
                str(args.install_timeout),
                "--report",
                str(lifecycle_report),
                "--log",
                str(lifecycle_log),
            ],
            timeout=args.install_timeout + 360,
        )
        lifecycle_pass = lifecycle["returncode"] == 0
        lifecycle_evidence = str(lifecycle_report)
    else:
        lifecycle = {
            "cmd": ["scripts/test-runtime-manager-lifecycle.py"],
            "returncode": 0,
            "stdout": "DEFERRED: native user-supervisor mutation requires the separate release/CI gate; v0.5.2 lifecycle implementation is preserved and unit-tested.",
            "duration_s": 0,
            "skipped": True,
            "skip_reason": "durable_lifecycle_requires_explicit_gate",
        }
        lifecycle_pass = True
        lifecycle_evidence = str(run_dir / "commands" / "runtime-manager-lifecycle-command.json")
    write_json(run_dir / "commands" / "runtime-manager-lifecycle-command.json", lifecycle)
    lifecycle_gate_name = (
        "wheel_runtime_manager_lifecycle"
        if args.run_durable_lifecycle
        else "durable_lifecycle_deferred_to_release_gate"
    )
    gates[lifecycle_gate_name] = {
        "pass": lifecycle_pass,
        "deferred": not args.run_durable_lifecycle,
        "evidence": lifecycle_evidence,
    }

    if shutil.which("hermes"):
        clean = run(["bash", "scripts/test-clean-hermes-install.sh", "--local"], timeout=300)
        clean_pass = clean["returncode"] == 0
    else:
        clean = {
            "cmd": ["bash", "scripts/test-clean-hermes-install.sh", "--local"],
            "returncode": 0,
            "stdout": "SKIP: hermes CLI not available in this runner; wheel install/entrypoint gate still validates package portability.",
            "duration_s": 0,
            "skipped": True,
            "skip_reason": "hermes_cli_not_available",
        }
        clean_pass = True
    write_json(run_dir / "commands" / "clean-temp-hermes-install.json", clean)
    gates["clean_temp_hermes_install"] = {"pass": clean_pass, "evidence": str(run_dir / "commands" / "clean-temp-hermes-install.json")}

    runtime = run([sys.executable, "scripts/test-headroom-runtime-smoke.py", "--spec", args.headroom_spec, "--litellm-spec", args.litellm_spec, "--install-timeout", str(args.install_timeout)], timeout=args.install_timeout + 240)
    write_json(run_dir / "commands" / "runtime-smoke.json", runtime)
    gates["runtime_compress_retrieve_smoke"] = {"pass": runtime["returncode"] == 0, "evidence": str(run_dir / "commands" / "runtime-smoke.json")}

    compat_runtime = run([sys.executable, "scripts/test-headroom-runtime-smoke.py", "--spec", args.compat_headroom_spec, "--litellm-spec", args.compat_litellm_spec, "--install-timeout", str(args.install_timeout)], timeout=args.install_timeout + 240)
    write_json(run_dir / "commands" / "compat-runtime-smoke.json", compat_runtime)
    gates["compat_runtime_compress_retrieve_smoke"] = {"pass": compat_runtime["returncode"] == 0, "evidence": str(run_dir / "commands" / "compat-runtime-smoke.json")}

    workload: dict[str, Any] = {"pass": False, "results": []}
    proxy_proc: subprocess.Popen[str] | None = None
    proxy_url = ""
    proxy_log = ""
    try:
        proxy_proc, proxy_url, proxy_log_path, ready = start_proxy_with_fresh_runtime(run_dir, args.headroom_spec, args.litellm_spec, args.install_timeout)
        proxy_log = str(proxy_log_path)
        if ready.get("ok"):
            workload = workload_matrix(run_dir, proxy_url, wheel_result)
            workload["proxy_url"] = proxy_url
            workload["proxy_log"] = proxy_log
        else:
            workload = {"pass": False, "ready": ready, "proxy_log": proxy_log}
    finally:
        stop_proxy(proxy_proc)
    write_json(run_dir / "bulky-workload-matrix.json", workload)
    gates["bulky_workload_matrix"] = {"pass": workload.get("pass"), "evidence": str(run_dir / "bulky-workload-matrix.json")}

    leftover = no_new_leftover_proxy(baseline_proxies)
    write_json(run_dir / "post-proxy-check.json", leftover)
    gates["no_new_leftover_proxy"] = {"pass": leftover.get("pass"), "evidence": str(run_dir / "post-proxy-check.json")}

    ephemeral_cleanup = cleanup_ephemeral_envs(run_dir, keep=args.keep_ephemeral_envs)
    write_json(run_dir / "ephemeral-env-cleanup.json", ephemeral_cleanup)
    gates["ephemeral_env_cleanup"] = {
        "pass": ephemeral_cleanup.get("pass"),
        "evidence": str(run_dir / "ephemeral-env-cleanup.json"),
    }

    final_checkout = checkout_snapshot()
    checkout_stability = {
        "pass": bool(
            initial_checkout.get("commands_ok")
            and final_checkout.get("commands_ok")
            and not initial_checkout.get("status_short")
            and not final_checkout.get("status_short")
            and initial_checkout.get("head") == final_checkout.get("head")
            and initial_checkout.get("tree") == final_checkout.get("tree")
        ),
        "initial": initial_checkout,
        "final": final_checkout,
    }
    write_json(run_dir / "checkout-stability.json", checkout_stability)
    gates["checkout_stability"] = {
        "pass": checkout_stability["pass"],
        "evidence": str(run_dir / "checkout-stability.json"),
    }
    status = {
        "cmd": ["git", "status", "--short", "--untracked-files=all"],
        "returncode": final_checkout.get("status_returncode", 1),
        "stdout": final_checkout.get("status_short", ""),
    }
    write_json(run_dir / "git-status.json", status)

    pass_count = sum(1 for g in gates.values() if g.get("pass"))
    total = len(gates)
    decision = "PLUGIN_RELEASE_CANDIDATE_LOCAL_PASS" if pass_count == total else "PLUGIN_RELEASE_CANDIDATE_LOCAL_GAPS_FOUND"
    summary = {
        "schema": "hermes-headroom-plugin-release-candidate-local-gate-v1",
        "generated_at": utc_iso(),
        "decision": decision,
        "pass_count": pass_count,
        "total_gates": total,
        "run_dir": str(run_dir),
        "repo": str(REPO),
        "plugin_head": final_checkout.get("head", "unknown"),
        "plugin_tree": final_checkout.get("tree", "unknown"),
        "gates": gates,
        "workload_matrix": workload,
        "proxy_url_used": proxy_url,
        "proxy_log": proxy_log,
        "default_runtime_spec": args.headroom_spec,
        "compat_runtime_spec": args.compat_headroom_spec,
        "durable_lifecycle_executed": args.run_durable_lifecycle,
        "remote_pushed": False,
        "public_release": False,
        "real_hermes_profile_mutated": False,
        "git_status_short": status.get("stdout", ""),
        "ephemeral_env_cleanup": ephemeral_cleanup,
        "next_gate": "OWNER_RELEASE_REVIEW_AND_REMOTE_CI_READBACK" if pass_count == total else "FIX_RC_GAPS_AND_RERUN",
    }
    write_json(run_dir / "summary.json", summary)
    write_report(run_dir, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if pass_count == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
