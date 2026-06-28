#!/usr/bin/env bash
set -euo pipefail

SPEC="${HEADROOM_AI_SPEC:-headroom-ai[proxy]>=0.26,<0.27}"
KEEP=0

usage() {
  cat <<'USAGE'
Install and verify the upstream Headroom Python dependency in a temporary venv.

Usage:
  scripts/test-headroom-dependency-install.sh [--keep]

Environment:
  HEADROOM_AI_SPEC   Package spec to install; default: headroom-ai[proxy]>=0.26,<0.27

This script does not touch Hermes config, HERMES_HOME, or the system Python environment.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep) KEEP=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

command -v python3 >/dev/null 2>&1 || { echo "FAIL: python3 not found" >&2; exit 127; }

TMP_DIR="$(mktemp -d)"
cleanup() {
  if [[ "$KEEP" -eq 1 ]]; then
    echo "Keeping temp dependency venv: $TMP_DIR"
  else
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT

python3 -m venv "$TMP_DIR/venv"
PYTHON="$TMP_DIR/venv/bin/python"
HEADROOM="$TMP_DIR/venv/bin/headroom"

PIP_LOG="$TMP_DIR/pip-install.log"
"$PYTHON" -m pip install --upgrade pip >"$PIP_LOG" 2>&1
if ! "$PYTHON" -m pip install "$SPEC" >>"$PIP_LOG" 2>&1; then
  echo "FAIL: dependency install failed. pip log: $PIP_LOG" >&2
  tail -120 "$PIP_LOG" >&2 || true
  exit 1
fi

"$PYTHON" - <<'PY'
import importlib.metadata as md
import importlib.util
import os
import subprocess
import sys

version = md.version('headroom-ai')
missing = [name for name in ['headroom', 'fastapi', 'uvicorn'] if importlib.util.find_spec(name) is None]
if missing:
    raise SystemExit(f"FAIL: missing import modules after dependency install: {missing}")

headroom = os.path.join(sys.prefix, 'bin', 'headroom')
checks = [
    ([headroom, '--help'], 'proxy'),
    ([headroom, 'proxy', '--help'], '--port'),
]
for cmd, needle in checks:
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=45)
    output = result.stdout + result.stderr
    if result.returncode != 0:
        raise SystemExit(f"FAIL: {' '.join(cmd)} rc={result.returncode}\n{output[:1000]}")
    if needle not in output:
        raise SystemExit(f"FAIL: {' '.join(cmd)} missing {needle!r}")

print(f"PASS: headroom-ai dependency installed and verified version={version}")
print("PASS: imports available: headroom, fastapi, uvicorn")
print("PASS: CLI available: headroom --help and headroom proxy --help")
PY

"$HEADROOM" --help >/dev/null
"$HEADROOM" proxy --help >/dev/null

echo "PASS: upstream Headroom dependency smoke complete ($SPEC)"
