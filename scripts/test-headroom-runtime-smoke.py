#!/usr/bin/env python3
"""Cross-platform full Headroom proxy/runtime smoke for the Hermes plugin.

Creates a temporary venv, installs headroom-ai[proxy], starts the upstream
Headroom proxy on loopback, waits for /readyz, and exercises the plugin's
compress -> retrieve smoke path against that proxy. It does not touch Hermes
config, HERMES_HOME, or global Python packages.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import venv
from pathlib import Path

DEFAULT_SPEC = "headroom-ai[proxy]>=0.26,<0.28"


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


def free_loopback_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_readyz(url: str, *, timeout: int, log: Path) -> tuple[bool, str]:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/readyz", timeout=3) as resp:  # noqa: S310 loopback test endpoint
                body = resp.read().decode("utf-8", errors="replace")
                last = f"status={resp.status} body={body[:500]}"
                if resp.status == 200:
                    try:
                        data = json.loads(body)
                    except Exception:
                        data = {}
                    if not isinstance(data, dict) or data.get("ready", True):
                        return True, last
        except Exception as exc:  # pragma: no cover - platform timing varies.
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\nreadyz timeout for {url}: {last}\n")
    return False, last


def terminate(proc: subprocess.Popen[str], log: Path) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    if proc.stdout is not None:
        with log.open("a", encoding="utf-8") as fh:
            fh.write("\n$ proxy output after termination\n")
            fh.write(proc.stdout.read() or "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start a real Headroom proxy and run plugin smoke against it.")
    parser.add_argument("--spec", default=os.environ.get("HEADROOM_AI_SPEC", DEFAULT_SPEC), help=f"package spec (default: {DEFAULT_SPEC})")
    parser.add_argument("--install-timeout", type=int, default=int(os.environ.get("HEADROOM_DEP_INSTALL_TIMEOUT", "600")))
    parser.add_argument("--ready-timeout", type=int, default=int(os.environ.get("HEADROOM_PROXY_READY_TIMEOUT", "90")))
    parser.add_argument("--keep", action="store_true", help="keep the temp venv/log directory")
    parser.add_argument("--port", type=int, default=0, help="loopback port to use; default chooses a free port")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    tmp_root = Path(tempfile.mkdtemp(prefix="headroom-runtime-smoke-"))
    venv_dir = tmp_root / "venv"
    log = tmp_root / "runtime-smoke.log"
    proxy_proc: subprocess.Popen[str] | None = None
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
        python = bin_dir(venv_dir) / exe_name("python")
        headroom = bin_dir(venv_dir) / exe_name("headroom")

        for cmd in ([str(python), "-m", "pip", "install", "--upgrade", "pip"], [str(python), "-m", "pip", "install", args.spec]):
            proc = run(cmd, timeout=args.install_timeout, log=log)
            if proc.returncode != 0:
                print(f"FAIL: install command failed rc={proc.returncode}; log={log}", file=sys.stderr)
                print(proc.stdout[-4000:], file=sys.stderr)
                return proc.returncode or 1

        port = args.port or free_loopback_port()
        proxy_url = f"http://127.0.0.1:{port}"
        proxy_cmd = [str(headroom), "proxy", "--host", "127.0.0.1", "--port", str(port)]
        proxy_proc = subprocess.Popen(proxy_cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        ready, detail = wait_readyz(proxy_url, timeout=args.ready_timeout, log=log)
        if not ready:
            print(f"FAIL: proxy did not become ready at {proxy_url}; last={detail}; log={log}", file=sys.stderr)
            return 1

        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo_root / "src")
        env["HEADROOM_PROXY_URL"] = proxy_url
        env.pop("HEADROOM_ALLOW_REMOTE_PROXY", None)
        smoke_code = """
import json
from hermes_headroom_plugin.proxy import smoke
result = smoke()
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
raise SystemExit(0 if result.get('ok') and result.get('sentinel_found') else 1)
""".strip()
        proc = subprocess.run([str(python), "-c", smoke_code], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120, env=env, check=False)
        with log.open("a", encoding="utf-8") as fh:
            fh.write("\n$ plugin smoke\n")
            fh.write(proc.stdout)
        if proc.returncode != 0:
            print(f"FAIL: plugin runtime smoke failed rc={proc.returncode}; log={log}", file=sys.stderr)
            print(proc.stdout[-4000:], file=sys.stderr)
            return proc.returncode or 1

        result = json.loads(proc.stdout.strip().splitlines()[-1])
        print(f"PASS: runtime smoke proxy ready at {proxy_url}")
        print(
            "PASS: plugin smoke compress/retrieve sentinel_found="
            f"{result.get('sentinel_found')} tokens_saved={result.get('tokens_saved')}"
        )
        print(f"PASS: upstream Headroom runtime smoke complete ({args.spec})")
        return 0
    finally:
        if proxy_proc is not None:
            terminate(proxy_proc, log)
        if args.keep:
            print(f"Keeping temp runtime smoke dir: {tmp_root}")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
