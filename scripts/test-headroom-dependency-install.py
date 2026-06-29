#!/usr/bin/env python3
"""Cross-platform upstream Headroom dependency smoke test.

Creates a temporary virtual environment, installs the configured headroom-ai spec,
verifies imports, and checks the headroom CLI/proxy help. It does not touch
Hermes config, HERMES_HOME, or the caller's system Python environment.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

DEFAULT_SPEC = "headroom-ai[proxy]>=0.26,<0.28"


def bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def exe_name(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _timeout_output(exc: subprocess.TimeoutExpired) -> str:
    stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    return stdout + stderr


def run(cmd: list[str], *, timeout: int = 120, log: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        output = _timeout_output(exc)
        if log is not None:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(f"\n$ {' '.join(cmd)}\n")
                fh.write(output)
                fh.write(f"\nTIMEOUT after {timeout}s\n")
        return subprocess.CompletedProcess(cmd, 124, output + f"\nTIMEOUT after {timeout}s\n")
    if log is not None:
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"\n$ {' '.join(cmd)}\n")
            fh.write(proc.stdout)
    return proc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify headroom-ai[proxy] in a temporary venv.")
    parser.add_argument("--spec", default=os.environ.get("HEADROOM_AI_SPEC", DEFAULT_SPEC), help=f"package spec (default: {DEFAULT_SPEC})")
    parser.add_argument("--install-timeout", type=int, default=int(os.environ.get("HEADROOM_DEP_INSTALL_TIMEOUT", "600")), help="seconds allowed for each pip install command")
    parser.add_argument("--keep", action="store_true", help="keep the temporary venv for inspection")
    args = parser.parse_args(argv)

    tmp_root = Path(tempfile.mkdtemp(prefix="headroom-dep-"))
    venv_dir = tmp_root / "venv"
    log = tmp_root / "dependency-smoke.log"
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
        python = bin_dir(venv_dir) / exe_name("python")
        headroom = bin_dir(venv_dir) / exe_name("headroom")

        for cmd in ([str(python), "-m", "pip", "install", "--upgrade", "pip"], [str(python), "-m", "pip", "install", args.spec]):
            proc = run(cmd, timeout=args.install_timeout, log=log)
            if proc.returncode != 0:
                print(f"FAIL: dependency install command failed rc={proc.returncode}; log={log}", file=sys.stderr)
                print(proc.stdout[-4000:], file=sys.stderr)
                return proc.returncode or 1

        code = """
import importlib.metadata as md
import importlib.util
missing = [name for name in ['headroom', 'fastapi', 'uvicorn'] if importlib.util.find_spec(name) is None]
if missing:
    raise SystemExit(f'missing modules: {missing}')
print(md.version('headroom-ai'))
""".strip()
        proc = run([str(python), "-c", code], timeout=60, log=log)
        if proc.returncode != 0:
            print(f"FAIL: import/version verification failed; log={log}", file=sys.stderr)
            print(proc.stdout[-4000:], file=sys.stderr)
            return proc.returncode or 1
        version = proc.stdout.strip().splitlines()[-1]

        checks = [([str(headroom), "--help"], "proxy"), ([str(headroom), "proxy", "--help"], "--port")]
        for cmd, needle in checks:
            proc = run(cmd, timeout=90, log=log)
            if proc.returncode != 0 or needle not in proc.stdout:
                print(f"FAIL: {' '.join(cmd)} missing {needle!r} or failed rc={proc.returncode}; log={log}", file=sys.stderr)
                print(proc.stdout[-4000:], file=sys.stderr)
                return proc.returncode or 1

        print(f"PASS: headroom-ai dependency installed and verified version={version}")
        print("PASS: imports available: headroom, fastapi, uvicorn")
        print("PASS: CLI available: headroom --help and headroom proxy --help")
        print(f"PASS: upstream Headroom dependency smoke complete ({args.spec})")
        return 0
    finally:
        if args.keep:
            print(f"Keeping temp dependency venv: {tmp_root}")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
