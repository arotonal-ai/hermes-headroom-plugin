#!/usr/bin/env python3
"""Cross-platform lifecycle canary for the packaged Headroom runtime manager."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any


_OPERATOR_RE = r"(?:===|==|~=|!=|<=|>=|<|>)"
_VERSION_TOKEN_RE = r"[A-Za-z0-9][A-Za-z0-9.*+!_-]*"
_SPECIFIER_RE = rf"{_OPERATOR_RE}{_VERSION_TOKEN_RE}(?:,{_OPERATOR_RE}{_VERSION_TOKEN_RE})*"
_HEADROOM_SPEC_RE = re.compile(rf"headroom-ai\[proxy\]{_SPECIFIER_RE}")
_LITELLM_SPEC_RE = re.compile(rf"litellm{_SPECIFIER_RE}")


def validate_package_spec(spec: str, *, package: str) -> str:
    pattern = _HEADROOM_SPEC_RE if package == "headroom-ai" else _LITELLM_SPEC_RE
    if not isinstance(spec, str) or not pattern.fullmatch(spec):
        raise ValueError(
            f"{package} must use a package-name plus version specifier; URLs, paths, markers, and credentials are unsupported"
        )
    return spec


def run(command: list[str], *, timeout: int, log: Path) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    log.parent.mkdir(parents=True, exist_ok=True)
    display = list(command)
    for flag in ("--headroom-spec", "--litellm-spec"):
        if flag in display:
            index = display.index(flag)
            if index + 1 < len(display):
                display[index + 1] = "<validated-package-spec>"
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n$ {' '.join(display)}\n{proc.stdout}\n")
    return proc


def parse_json(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        value = json.loads(proc.stdout)
    except (TypeError, json.JSONDecodeError):
        return {"parse_error": True, "stdout_tail": (proc.stdout or "")[-2000:]}
    return value if isinstance(value, dict) else {"parse_error": True, "value": value}


def free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/readyz", timeout=2) as response:  # noqa: S310 loopback only
            return response.status == 200
    except Exception:
        return False


def wait_stopped(url: str, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not ready(url):
            return True
        time.sleep(0.5)
    return not ready(url)


def command_absent(command: list[str]) -> bool:
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode != 0


def supervisor_absent(profile: str, supervisor_kind: str | None) -> bool:
    service_name = f"headroom-{profile}"
    if os.name == "nt":
        service_gone = command_absent(["sc.exe", "query", service_name])
        tasks_gone = all(
            command_absent(["schtasks", "/Query", "/TN", task])
            for task in (f"{service_name}-startup", f"{service_name}-health")
        )
        return service_gone and tasks_gone
    if sys.platform == "darwin":
        label = f"com.headroom.{profile}"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        return not plist.exists() and command_absent(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"]
        )
    unit = f"{service_name}.service"
    unit_path = Path.home() / ".config" / "systemd" / "user" / unit
    if unit_path.exists() or not command_absent(["systemctl", "--user", "is-enabled", unit]):
        return False
    if supervisor_kind == "task":
        try:
            cron = subprocess.run(
                ["crontab", "-l"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return f"# >>> headroom {profile} >>>" not in cron.stdout
    return True


def digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(131072), b""):
            value.update(chunk)
    return value.hexdigest()


def shell_surfaces() -> list[Path]:
    home = Path.home()
    candidates = [
        home / ".bashrc",
        home / ".zshrc",
        home / ".profile",
        home / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
        home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
    ]
    return candidates


def command_prefix(value: str) -> list[str]:
    path = Path(value).expanduser()
    if path.suffix.lower() == ".py":
        return [sys.executable, str(path.resolve())]
    return [value]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exercise setup/status/doctor/uninstall with a real native supervisor.")
    parser.add_argument("--manager-command", default="headroom-runtime")
    parser.add_argument("--runtime-root", default="")
    parser.add_argument("--headroom-spec", default=os.environ.get("HEADROOM_AI_SPEC", "headroom-ai[proxy]==0.32.0"))
    parser.add_argument("--litellm-spec", default=os.environ.get("HEADROOM_LITELLM_SPEC", "litellm==1.91.3"))
    parser.add_argument("--install-timeout", type=int, default=600)
    parser.add_argument("--ready-timeout", type=int, default=120)
    parser.add_argument("--report", default="")
    parser.add_argument("--log", default="")
    args = parser.parse_args(argv)
    try:
        args.headroom_spec = validate_package_spec(args.headroom_spec, package="headroom-ai")
        args.litellm_spec = validate_package_spec(args.litellm_spec, package="litellm")
    except ValueError as exc:
        print(f"lifecycle input rejected: {exc}", file=sys.stderr)
        return 2

    owned_temp = not args.runtime_root
    root = Path(args.runtime_root).expanduser().resolve() if args.runtime_root else Path(tempfile.mkdtemp(prefix="headroom-manager-lifecycle-")) / "runtime"
    log = Path(args.log).expanduser().resolve() if args.log else root.parent / "manager-lifecycle.log"
    profile = re.sub(r"[^A-Za-z0-9_.-]", "-", f"hermes-ci-{sys.platform}-{os.getpid()}")
    port = free_port()
    proxy_url = f"http://127.0.0.1:{port}"
    prefix = command_prefix(args.manager_command)
    before_shell = {str(path): digest(path) for path in shell_surfaces()}
    setup_proc: subprocess.CompletedProcess[str] | None = None
    status_proc: subprocess.CompletedProcess[str] | None = None
    doctor_proc: subprocess.CompletedProcess[str] | None = None
    uninstall_proc: subprocess.CompletedProcess[str] | None = None
    manifest_data: dict[str, Any] = {}
    artifacts: list[str] = []

    try:
        setup_proc = run(
            prefix
            + [
                "setup",
                "--runtime-root",
                str(root),
                "--profile",
                profile,
                "--port",
                str(port),
                "--headroom-spec",
                args.headroom_spec,
                "--litellm-spec",
                args.litellm_spec,
                "--install-timeout",
                str(args.install_timeout),
                "--ready-timeout",
                str(args.ready_timeout),
                "--json",
            ],
            timeout=args.install_timeout + args.ready_timeout + 60,
            log=log,
        )
        setup_json = parse_json(setup_proc)
        manifest = root / "workspace" / "deploy" / profile / "manifest.json"
        if manifest.is_file():
            manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
            artifacts = [str(item.get("path")) for item in manifest_data.get("artifacts", []) if isinstance(item, dict) and item.get("path")]
        if setup_proc.returncode == 0:
            status_proc = run(prefix + ["status", "--runtime-root", str(root), "--json"], timeout=90, log=log)
            doctor_proc = run(prefix + ["doctor", "--runtime-root", str(root), "--json"], timeout=180, log=log)
    except Exception as exc:  # noqa: BLE001
        setup_json = {"exception": f"{type(exc).__name__}: {exc}"}
    finally:
        if root.exists():
            try:
                uninstall_proc = run(
                    prefix + ["uninstall", "--runtime-root", str(root), "--stop-timeout", "45", "--json"],
                    timeout=120,
                    log=log,
                )
            except Exception as exc:  # noqa: BLE001
                uninstall_proc = subprocess.CompletedProcess(prefix, 127, f"{type(exc).__name__}: {exc}")

    after_shell = {str(path): digest(path) for path in shell_surfaces()}
    manifest_ok = bool(
        manifest_data.get("provider_mode") == "manual"
        and manifest_data.get("targets") == []
        and manifest_data.get("mutations") == []
    )
    smoke_json = setup_json.get("smoke")
    setup_ok = bool(
        setup_proc
        and setup_proc.returncode == 0
        and setup_json.get("decision") == "RUNTIME_FULL_DURABLE"
        and isinstance(smoke_json, dict)
        and smoke_json.get("sentinel_found") is True
    )
    status_json = parse_json(status_proc) if status_proc else {}
    doctor_json = parse_json(doctor_proc) if doctor_proc else {}
    uninstall_json = parse_json(uninstall_proc) if uninstall_proc else {}
    status_ok = bool(status_proc and status_proc.returncode == 0 and status_json.get("decision") == "RUNTIME_FULL_DURABLE")
    doctor_ok = bool(doctor_proc and doctor_proc.returncode == 0 and doctor_json.get("decision") == "RUNTIME_FULL_DURABLE")
    uninstall_ok = bool(uninstall_proc and uninstall_proc.returncode == 0 and uninstall_json.get("decision") == "UNINSTALLED")
    stopped = wait_stopped(proxy_url)
    artifacts_removed = all(not Path(path).exists() for path in artifacts)
    shell_unchanged = before_shell == after_shell
    root_removed = not root.exists()
    supervisor_removed = supervisor_absent(profile, manifest_data.get("supervisor_kind"))
    passed = all(
        (
            setup_ok,
            status_ok,
            doctor_ok,
            manifest_ok,
            uninstall_ok,
            stopped,
            artifacts_removed,
            supervisor_removed,
            shell_unchanged,
            root_removed,
        )
    )

    report = {
        "schema": "headroom-runtime-manager-lifecycle-v1",
        "pass": passed,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "profile": profile,
        "port": port,
        "headroom_spec": args.headroom_spec,
        "litellm_spec": args.litellm_spec,
        "setup": setup_json,
        "status": status_json,
        "doctor": doctor_json,
        "manifest": {
            "provider_mode": manifest_data.get("provider_mode"),
            "targets": manifest_data.get("targets"),
            "mutations": manifest_data.get("mutations"),
            "supervisor_kind": manifest_data.get("supervisor_kind"),
            "service_name": manifest_data.get("service_name"),
        },
        "rollback": {
            "uninstall": uninstall_json,
            "listener_stopped": stopped,
            "artifacts_removed": artifacts_removed,
            "supervisor_removed": supervisor_removed,
            "root_removed": root_removed,
        },
        "shell_unchanged": shell_unchanged,
        "checks": {
            "setup": setup_ok,
            "status": status_ok,
            "doctor": doctor_ok,
            "manifest": manifest_ok,
            "uninstall": uninstall_ok,
        },
        "log": str(log),
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report:
        report_path = Path(args.report).expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if owned_temp and passed:
        shutil.rmtree(root.parent, ignore_errors=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
