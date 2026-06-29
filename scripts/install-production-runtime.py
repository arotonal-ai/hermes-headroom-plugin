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
- exit 0 only for RUNTIME_FULL unless --no-smoke/--no-start is requested
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

DEFAULT_SPEC = "headroom-ai[proxy]"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 28787


def default_venv() -> Path:
    return Path(os.environ.get("HEADROOM_RUNTIME_VENV") or Path.home() / ".cache" / "hermes-headroom-venv").expanduser()


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


def start_proxy(headroom: Path, host: str, port: int, log: Path, pid_file: Path) -> int:
    proxy_log = log.parent / "headroom-proxy.log"
    out = proxy_log.open("a", encoding="utf-8")
    cmd = [str(headroom), "proxy", "--host", host, "--port", str(port)]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": out,
        "stderr": subprocess.STDOUT,
        "text": True,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install/start/verify Headroom runtime for Hermes plugin production use.")
    parser.add_argument("--venv", default=str(default_venv()), help="persistent runtime venv path")
    parser.add_argument("--spec", default=os.environ.get("HEADROOM_AI_SPEC", DEFAULT_SPEC), help=f"pip package spec; default installs latest ({DEFAULT_SPEC})")
    parser.add_argument("--host", default=os.environ.get("HEADROOM_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HEADROOM_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--install-timeout", type=int, default=int(os.environ.get("HEADROOM_DEP_INSTALL_TIMEOUT", "900")))
    parser.add_argument("--ready-timeout", type=int, default=int(os.environ.get("HEADROOM_PROXY_READY_TIMEOUT", "90")))
    parser.add_argument("--recreate", action="store_true", help="delete and recreate the venv before installing")
    parser.add_argument("--no-start", action="store_true", help="install/check dependency only; do not start proxy")
    parser.add_argument("--no-smoke", action="store_true", help="skip plugin compress/retrieve smoke after readyz")
    parser.add_argument("--stop-existing", action="store_true", help="stop PID recorded in the venv pid file before starting")
    parser.add_argument("--json", action="store_true", help="emit machine-readable result")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    venv_dir = Path(args.venv).expanduser().resolve()
    log_dir = venv_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "install-production-runtime.log"
    pid_file = venv_dir / "headroom-proxy.pid"
    proxy_url = f"http://{args.host}:{args.port}"
    result: dict[str, Any] = {"state": "FAIL", "proxy_url": proxy_url, "venv": str(venv_dir), "spec": args.spec, "log": str(log)}

    if args.recreate and venv_dir.exists():
        shutil.rmtree(venv_dir)
        log_dir.mkdir(parents=True, exist_ok=True)

    try:
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
                    already_ready, detail = readyz(proxy_url)
                    started_pid = None
                    if not already_ready:
                        started_pid = start_proxy(headroom, args.host, args.port, log, pid_file)
                    ready, detail = wait_readyz(proxy_url, timeout=args.ready_timeout, log=log)
                    result.update({"readyz": detail, "started_pid": started_pid})
                    if not ready:
                        result.update({"state": "RUNTIME_PARTIAL", "phase": "readyz", "ok": False})
                    elif args.no_smoke:
                        result.update({"state": "RUNTIME_PARTIAL", "phase": "ready_no_smoke", "ok": True})
                    else:
                        ok, smoke_result, output_tail = smoke(repo_root, python, proxy_url, log)
                        result.update({"smoke": smoke_result, "output_tail": output_tail if not ok else ""})
                        if ok:
                            result.update({"state": "RUNTIME_FULL", "phase": "smoke", "ok": True})
                        else:
                            result.update({"state": "RUNTIME_PARTIAL", "phase": "smoke", "ok": False})
    except Exception as exc:
        result.update({"state": "FAIL", "phase": "exception", "error": f"{type(exc).__name__}: {exc}"})

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{result.get('state')}: proxy={result.get('proxy_url')} venv={result.get('venv')} log={result.get('log')}")
        if result.get("state") == "RUNTIME_FULL":
            smoke_result = result.get("smoke") or {}
            if isinstance(smoke_result, dict):
                print(f"RUNTIME_FULL: sentinel_found={smoke_result.get('sentinel_found')} tokens_saved={smoke_result.get('tokens_saved')}")
        elif result.get("output_tail"):
            print(str(result.get("output_tail"))[-2000:], file=sys.stderr)
    return 0 if result.get("state") == "RUNTIME_FULL" or (args.no_start and result.get("ok")) or (args.no_smoke and result.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
