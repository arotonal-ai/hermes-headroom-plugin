#!/usr/bin/env python3
"""Install and verify a production Headroom runtime for the Hermes plugin.

This script is intentionally separate from `hermes plugins install`: the Hermes
plugin can load without the upstream Headroom proxy, but `RUNTIME_FULL` requires
an isolated runtime venv, a loopback proxy, /readyz, and compress -> retrieve
smoke verification.

Default behavior:
- create/update a persistent venv at ~/.cache/hermes-headroom-venv
- install the latest available `headroom-ai[proxy]` unless --spec overrides it
- start `headroom proxy --host 127.0.0.1 --port 28787` when not already ready
- run the plugin smoke against that endpoint
- install the bundled owner-local llm-monitor companion plugin unless skipped
- exit 0 only for RUNTIME_FULL unless --no-smoke/--no-start/--companion-only is requested

Linux durable mode:
- add `--systemd-user` to write, enable, and start a durable user service
- exit state becomes RUNTIME_FULL_DURABLE when systemd + smoke both pass
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import venv
from pathlib import Path
from typing import Any

HEADROOM_RUNTIME_VERSION = "0.31.0"
DEFAULT_SPEC = f"headroom-ai[proxy]=={HEADROOM_RUNTIME_VERSION}"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 28787
DEFAULT_SERVICE_NAME = "hermes-context-reduction.service"
DEFAULT_CCR_BACKEND = "memory"
DEFAULT_CCR_TTL_SECONDS = 1800


def default_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def default_venv() -> Path:
    default_path = Path.home() / ".cache" / f"hermes-headroom-venv-{HEADROOM_RUNTIME_VERSION}"
    return Path(os.environ.get("HEADROOM_RUNTIME_VENV") or default_path).expanduser()


def bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def exe_name(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def run(cmd: list[str], *, timeout: int, log: Path) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n$ {' '.join(cmd)}\n")
        fh.write(proc.stdout)
    return proc


def http_get_json(url: str, timeout: int = 5) -> tuple[int | None, dict[str, Any] | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 loopback/default endpoint
            body = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except Exception:
                data = None
            return int(resp.status), data, body[:500]
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def readyz(proxy_url: str) -> tuple[bool, str]:
    status, data, body = http_get_json(f"{proxy_url.rstrip('/')}/readyz", timeout=5)
    ok = status == 200 and (not isinstance(data, dict) or bool(data.get("ready", True)))
    return ok, f"status={status} body={data if data is not None else body}"


def wait_readyz(proxy_url: str, *, timeout: int, log: Path) -> tuple[bool, str]:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        ok, detail = readyz(proxy_url)
        last = detail
        if ok:
            return True, detail
        time.sleep(1)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\nreadyz timeout for {proxy_url}: {last}\n")
    return False, last


def runtime_store_posture(proxy_url: str) -> dict[str, Any]:
    """Read the actual Headroom 0.31 CCR backend/TTL for deployment evidence."""
    status, data, body = http_get_json(f"{proxy_url.rstrip('/')}/v1/retrieve/stats", timeout=5)
    if status != 200 or not isinstance(data, dict):
        return {"ok": False, "status": status, "error": body}
    raw_store = data.get("store")
    store: dict[str, Any] = raw_store if isinstance(raw_store, dict) else {}
    backend_data = store.get("backend")
    if isinstance(backend_data, dict):
        backend = backend_data.get("backend_type")
        entry_count = backend_data.get("entry_count")
    else:
        backend = backend_data
        entry_count = store.get("entry_count")
    ttl = store.get("default_ttl_seconds")
    return {
        "ok": bool(backend) and isinstance(ttl, int),
        "status": status,
        "backend": backend,
        "ttl_seconds": ttl,
        "entry_count": entry_count,
    }


def start_proxy(
    headroom: Path,
    host: str,
    port: int,
    log: Path,
    pid_file: Path,
    *,
    ccr_backend: str,
    ccr_ttl_seconds: int,
) -> int:
    proxy_log = log.parent / "headroom-proxy.log"
    out = proxy_log.open("a", encoding="utf-8")
    cmd = [str(headroom), "proxy", "--host", host, "--port", str(port)]
    runtime_env = os.environ.copy()
    runtime_env["HEADROOM_CCR_BACKEND"] = ccr_backend
    runtime_env["HEADROOM_CCR_TTL_SECONDS"] = str(ccr_ttl_seconds)
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": out,
        "stderr": subprocess.STDOUT,
        "text": True,
        "env": runtime_env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n$ {' '.join(cmd)}\nstarted pid={proc.pid} proxy_log={proxy_log}\n")
    return int(proc.pid)


def run_systemctl(args: list[str], *, log: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["systemctl", "--user", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n$ systemctl --user {' '.join(args)}\n")
        fh.write(proc.stdout)
    return proc


def systemd_user_available() -> bool:
    """Return whether this host can manage a systemd user service."""
    return os.name != "nt" and shutil.which("systemctl") is not None


def write_systemd_user_unit(
    headroom: Path,
    host: str,
    port: int,
    service_name: str,
    log: Path,
    *,
    ccr_backend: str,
    ccr_ttl_seconds: int,
) -> Path:
    if not systemd_user_available():
        raise RuntimeError("systemd --user is available only on Linux hosts with systemctl")
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / service_name
    unit_path.write_text(
        f"""[Unit]
Description=Hermes Context Reduction Layer (Headroom proxy)
After=network-online.target

[Service]
Type=simple
ExecStart={headroom} proxy --host {host} --port {port} --no-telemetry
Restart=on-failure
RestartSec=5
Environment=HEADROOM_TELEMETRY=off
Environment=HEADROOM_DISABLE_UPDATE_CHECK=1
Environment=HEADROOM_HOST={host}
Environment=HEADROOM_PORT={port}
Environment=HEADROOM_CCR_BACKEND={ccr_backend}
Environment=HEADROOM_CCR_TTL_SECONDS={ccr_ttl_seconds}

[Install]
WantedBy=default.target
""",
        encoding="utf-8",
    )
    try:
        unit_path.chmod(0o600)
    except OSError:
        pass
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\nwrote systemd user unit: {unit_path}\n")
    return unit_path


def enable_systemd_user_service(
    headroom: Path,
    host: str,
    port: int,
    service_name: str,
    log: Path,
    *,
    ccr_backend: str,
    ccr_ttl_seconds: int,
) -> dict[str, Any]:
    unit_path = write_systemd_user_unit(
        headroom,
        host,
        port,
        service_name,
        log,
        ccr_backend=ccr_backend,
        ccr_ttl_seconds=ccr_ttl_seconds,
    )
    out: dict[str, Any] = {"service": service_name, "unit_path": str(unit_path), "ok": False}
    for args in (["daemon-reload"], ["enable", service_name], ["restart", service_name]):
        proc = run_systemctl(list(args), log=log)
        if proc.returncode != 0:
            out.update({"phase": "systemctl", "command": " ".join(args), "returncode": proc.returncode, "output_tail": proc.stdout[-2000:]})
            return out
    active = run_systemctl(["is-active", service_name], log=log)
    enabled = run_systemctl(["is-enabled", service_name], log=log)
    out.update({"active": active.stdout.strip(), "enabled": enabled.stdout.strip()})
    out["ok"] = active.returncode == 0 and enabled.returncode == 0 and out["active"] == "active" and out["enabled"] == "enabled"
    return out


def smoke(repo_root: Path, python: Path, proxy_url: str, log: Path) -> tuple[bool, dict[str, Any] | None, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    env["HEADROOM_PROXY_URL"] = proxy_url
    env.pop("HEADROOM_ALLOW_REMOTE_PROXY", None)
    code = """
import json
from hermes_headroom_plugin.proxy import smoke
result = smoke()
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
raise SystemExit(0 if result.get('ok') and result.get('sentinel_found') else 1)
""".strip()
    proc = subprocess.run([str(python), "-c", code], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180, env=env, check=False)
    with log.open("a", encoding="utf-8") as fh:
        fh.write("\n$ plugin compress/retrieve smoke\n")
        fh.write(proc.stdout)
    data = None
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        pass
    return proc.returncode == 0, data, proc.stdout[-4000:]


def stop_pid(pid_file: Path, log: Path) -> bool:
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        else:
            os.killpg(pid, signal.SIGTERM)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"\nstopped existing proxy pid={pid}\n")
        pid_file.unlink(missing_ok=True)
        return True
    except Exception as exc:
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"\nfailed to stop pid={pid}: {type(exc).__name__}: {exc}\n")
        return False




def _same_text_file(src: Path, dst: Path) -> bool:
    try:
        return src.read_bytes() == dst.read_bytes()
    except Exception:
        return False


def install_llm_monitor_companion(repo_root: Path, hermes_home: Path, *, force: bool, log: Path) -> dict[str, Any]:
    """Install bundled llm-monitor companion into a Hermes home without restarting Hermes.

    The companion is copied as normal Hermes plugin files under
    ``$HERMES_HOME/plugins/llm-monitor``. Existing local plugins are preserved by
    default: if files differ, the installer reports ``preserved_existing`` and
    requires ``--force-llm-monitor-companion`` to overwrite. This keeps owner-local
    state safe while still making clean/temp installs one-command complete.
    """
    source = repo_root / "src" / "hermes_headroom_plugin" / "companions" / "llm-monitor"
    target = hermes_home.expanduser().resolve() / "plugins" / "llm-monitor"
    required = ["__init__.py", "plugin.yaml"]
    out: dict[str, Any] = {
        "name": "llm-monitor",
        "source": str(source),
        "target": str(target),
        "ok": False,
    }
    if not source.is_dir() or any(not (source / name).exists() for name in required):
        out.update({"status": "missing_source", "missing": [name for name in required if not (source / name).exists()]})
        return out
    try:
        if target.exists():
            identical = all(_same_text_file(source / name, target / name) for name in required)
            if identical:
                out.update({"ok": True, "status": "up_to_date"})
                return out
            if not force:
                out.update({"ok": True, "status": "preserved_existing", "force_required_for_overwrite": True})
                return out
            shutil.rmtree(target)
            status = "overwritten"
        else:
            status = "installed"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
        for path in target.rglob("*"):
            if path.is_file():
                try:
                    os.chmod(path, 0o600)
                except Exception:
                    pass
        out.update({"ok": True, "status": status})
        return out
    except Exception as exc:
        out.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
        return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install/start/verify Headroom runtime for Hermes plugin production use.")
    parser.add_argument("--venv", default=str(default_venv()), help=f"persistent versioned runtime venv path; default {default_venv()}")
    parser.add_argument("--spec", default=os.environ.get("HEADROOM_AI_SPEC", DEFAULT_SPEC), help=f"pip package spec; reproducible default {DEFAULT_SPEC}")
    parser.add_argument("--host", default=os.environ.get("HEADROOM_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HEADROOM_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--ccr-backend", choices=("memory", "sqlite"), default=os.environ.get("HEADROOM_CCR_BACKEND", DEFAULT_CCR_BACKEND), help=f"CCR recovery backend; portable tool-core default {DEFAULT_CCR_BACKEND}")
    parser.add_argument("--ccr-ttl-seconds", type=int, default=int(os.environ.get("HEADROOM_CCR_TTL_SECONDS", str(DEFAULT_CCR_TTL_SECONDS))), help=f"CCR marker TTL; default {DEFAULT_CCR_TTL_SECONDS}")
    parser.add_argument("--install-timeout", type=int, default=int(os.environ.get("HEADROOM_DEP_INSTALL_TIMEOUT", "900")))
    parser.add_argument("--ready-timeout", type=int, default=int(os.environ.get("HEADROOM_PROXY_READY_TIMEOUT", "90")))
    parser.add_argument("--recreate", action="store_true", help="delete and recreate the venv before installing")
    parser.add_argument("--no-start", action="store_true", help="install/check dependency only; do not start proxy")
    parser.add_argument("--no-smoke", action="store_true", help="skip plugin compress/retrieve smoke after readyz")
    parser.add_argument("--stop-existing", action="store_true", help="stop PID recorded in the venv pid file before starting")
    parser.add_argument("--systemd-user", action="store_true", help="Linux only: write, enable, and restart a durable systemd --user service instead of a detached helper process")
    parser.add_argument("--service-name", default=os.environ.get("HEADROOM_SERVICE", DEFAULT_SERVICE_NAME), help=f"systemd --user service name for --systemd-user; default {DEFAULT_SERVICE_NAME}")
    parser.add_argument("--hermes-home", default=str(default_hermes_home()), help="Hermes home for optional companion plugin install; defaults to HERMES_HOME or ~/.hermes")
    companion_group = parser.add_mutually_exclusive_group()
    companion_group.add_argument("--with-llm-monitor-companion", action="store_true", help="opt in to installing the bundled llm-monitor companion plugin")
    companion_group.add_argument("--skip-llm-monitor-companion", action="store_true", help="deprecated compatibility flag; companion is skipped by default")
    parser.add_argument("--force-llm-monitor-companion", action="store_true", help="overwrite an existing llm-monitor companion plugin in --hermes-home; requires --with-llm-monitor-companion or --companion-only")
    parser.add_argument("--companion-only", action="store_true", help="install/verify bundled companion plugin only; do not install or start Headroom runtime")
    parser.add_argument("--json", action="store_true", help="emit machine-readable result")
    args = parser.parse_args(argv)
    if args.ccr_ttl_seconds <= 0:
        parser.error("--ccr-ttl-seconds must be greater than zero")
    if args.companion_only and args.skip_llm_monitor_companion:
        parser.error("--companion-only cannot be combined with --skip-llm-monitor-companion")
    if args.force_llm_monitor_companion and not (args.with_llm_monitor_companion or args.companion_only):
        parser.error("--force-llm-monitor-companion requires --with-llm-monitor-companion or --companion-only")

    repo_root = Path(__file__).resolve().parents[1]
    hermes_home = Path(args.hermes_home).expanduser().resolve()
    venv_dir = Path(args.venv).expanduser().resolve()
    log_dir = venv_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "install-production-runtime.log"
    pid_file = venv_dir / "headroom-proxy.pid"
    proxy_url = f"http://{args.host}:{args.port}"
    result: dict[str, Any] = {
        "state": "FAIL",
        "proxy_url": proxy_url,
        "venv": str(venv_dir),
        "spec": args.spec,
        "ccr_backend": args.ccr_backend,
        "ccr_ttl_seconds": args.ccr_ttl_seconds,
        "log": str(log),
        "hermes_home": str(hermes_home),
    }

    if args.recreate and venv_dir.exists():
        shutil.rmtree(venv_dir)
        log_dir.mkdir(parents=True, exist_ok=True)

    try:
        companion_ok = True
        install_companion = args.with_llm_monitor_companion or args.companion_only
        if install_companion:
            companion_result = install_llm_monitor_companion(repo_root, hermes_home, force=args.force_llm_monitor_companion, log=log)
            result["llm_monitor_companion"] = companion_result
            if not companion_result.get("ok"):
                result.update({"state": "FAIL", "phase": "llm_monitor_companion", "ok": False})
                companion_ok = False
        else:
            result["llm_monitor_companion"] = {"ok": True, "status": "skipped_default"}

        if companion_ok and args.companion_only:
            result.update({"state": "COMPANION_INSTALLED", "phase": "companion_only", "ok": True})
        elif companion_ok:
            if not (bin_dir(venv_dir) / exe_name("python")).exists():
                venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            python = bin_dir(venv_dir) / exe_name("python")
            headroom = bin_dir(venv_dir) / exe_name("headroom")

            for cmd in ([str(python), "-m", "pip", "install", "--upgrade", "pip"], [str(python), "-m", "pip", "install", "--upgrade", args.spec]):
                proc = run(cmd, timeout=args.install_timeout, log=log)
                if proc.returncode != 0:
                    result.update({"state": "FAIL", "phase": "install", "returncode": proc.returncode, "output_tail": proc.stdout[-2000:]})
                    break
            else:
                checks = [([str(headroom), "--help"], "proxy"), ([str(headroom), "proxy", "--help"], "--port")]
                for cmd, needle in checks:
                    proc = run(cmd, timeout=90, log=log)
                    if proc.returncode != 0 or needle not in proc.stdout:
                        result.update({"state": "FAIL", "phase": "cli", "returncode": proc.returncode, "missing": needle, "output_tail": proc.stdout[-2000:]})
                        break
                else:
                    if args.no_start:
                        result.update({"state": "RUNTIME_PARTIAL", "phase": "installed_no_start", "ok": True})
                    else:
                        if args.stop_existing:
                            stop_pid(pid_file, log)
                        started_pid = None
                        systemd_result = None
                        if args.systemd_user:
                            systemd_result = enable_systemd_user_service(
                                headroom,
                                args.host,
                                args.port,
                                args.service_name,
                                log,
                                ccr_backend=args.ccr_backend,
                                ccr_ttl_seconds=args.ccr_ttl_seconds,
                            )
                            result["systemd_user"] = systemd_result
                            if not systemd_result.get("ok"):
                                result.update({"state": "RUNTIME_PARTIAL", "phase": "systemd_user", "ok": False})
                        else:
                            already_ready, _detail = readyz(proxy_url)
                            if not already_ready:
                                started_pid = start_proxy(
                                    headroom,
                                    args.host,
                                    args.port,
                                    log,
                                    pid_file,
                                    ccr_backend=args.ccr_backend,
                                    ccr_ttl_seconds=args.ccr_ttl_seconds,
                                )
                        ready, detail = wait_readyz(proxy_url, timeout=args.ready_timeout, log=log)
                        result.update({"readyz": detail, "started_pid": started_pid})
                        if not ready:
                            result.update({"state": "RUNTIME_PARTIAL", "phase": "readyz", "ok": False})
                        else:
                            store_posture = runtime_store_posture(proxy_url)
                            result["runtime_store"] = store_posture
                            if not store_posture.get("ok"):
                                result.update({"state": "RUNTIME_PARTIAL", "phase": "runtime_store_posture", "ok": False})
                            elif store_posture.get("backend") != args.ccr_backend or store_posture.get("ttl_seconds") != args.ccr_ttl_seconds:
                                result.update(
                                    {
                                        "state": "RUNTIME_PARTIAL",
                                        "phase": "runtime_store_mismatch",
                                        "ok": False,
                                        "requested_store": {"backend": args.ccr_backend, "ttl_seconds": args.ccr_ttl_seconds},
                                    }
                                )
                            elif args.no_smoke:
                                result.update({"state": "RUNTIME_PARTIAL", "phase": "ready_no_smoke", "ok": True})
                            else:
                                ok, smoke_result, output_tail = smoke(repo_root, python, proxy_url, log)
                                result.update({"smoke": smoke_result, "output_tail": output_tail if not ok else ""})
                                if ok:
                                    state = "RUNTIME_FULL_DURABLE" if args.systemd_user and (systemd_result or {}).get("ok") else "RUNTIME_FULL"
                                    result.update({"state": state, "phase": "smoke", "ok": True})
                                else:
                                    result.update({"state": "RUNTIME_PARTIAL", "phase": "smoke", "ok": False})
    except Exception as exc:
        result.update({"state": "FAIL", "phase": "exception", "error": f"{type(exc).__name__}: {exc}"})

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{result.get('state')}: proxy={result.get('proxy_url')} venv={result.get('venv')} log={result.get('log')}")
        if result.get("state") in {"RUNTIME_FULL", "RUNTIME_FULL_DURABLE"}:
            smoke_result = result.get("smoke") or {}
            if isinstance(smoke_result, dict):
                print(f"{result.get('state')}: sentinel_found={smoke_result.get('sentinel_found')} tokens_saved={smoke_result.get('tokens_saved')}")
        elif result.get("output_tail"):
            print(str(result.get("output_tail"))[-2000:], file=sys.stderr)
    return 0 if result.get("state") in {"RUNTIME_FULL", "RUNTIME_FULL_DURABLE", "COMPANION_INSTALLED"} or (args.no_start and result.get("ok")) or (args.no_smoke and result.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
