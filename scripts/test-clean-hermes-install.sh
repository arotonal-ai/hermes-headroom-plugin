#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="local"
KEEP=0

usage() {
  cat <<'USAGE'
Test Hermes Headroom plugin installation in a temporary HERMES_HOME.

Usage:
  scripts/test-clean-hermes-install.sh [--local|--remote] [--keep]

Modes:
  --local   install this checkout via scripts/install-hermes-plugin.sh --local (default)
  --remote  install arotonal-ai/hermes-headroom-plugin via `hermes plugins install`

This does not touch the real Hermes home unless HERMES_HOME is already set by the caller.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --local) MODE="local" ;;
    --remote) MODE="remote" ;;
    --keep) KEEP=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

command -v hermes >/dev/null 2>&1 || { echo "FAIL: hermes not found" >&2; exit 127; }
command -v git >/dev/null 2>&1 || { echo "FAIL: git not found" >&2; exit 127; }

# Use the cross-platform Python resolver instead of hardcoding python3.
# For this test we need the Python environment that can import Hermes internals.
RESOLVER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/python-resolver.sh
source "$RESOLVER_DIR/python-resolver.sh"
resolve_python_with_module hermes_cli

TMP_HOME="$(mktemp -d)"
cleanup() {
  if [[ "$KEEP" -eq 1 ]]; then
    echo "Keeping temp HERMES_HOME: $TMP_HOME"
  else
    rm -rf "$TMP_HOME"
  fi
}
trap cleanup EXIT

export HERMES_HOME="$TMP_HOME"
echo "Temp HERMES_HOME=$HERMES_HOME"

if [[ "$MODE" == "remote" ]]; then
  hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
else
  "$ROOT/scripts/install-hermes-plugin.sh" --local --no-restart-hint
fi

hermes plugins list --enabled --user --plain | tee "$TMP_HOME/plugins-list.txt"
grep -Fq 'headroom_retrieve' "$TMP_HOME/plugins-list.txt" || { echo "FAIL: headroom_retrieve not enabled" >&2; exit 1; }

"${PY_CMD[@]}" - <<'PY'
import json
from hermes_cli.plugins import PluginManager
pm = PluginManager()
pm.discover_and_load(force=True)
loaded = pm._plugins.get('headroom_retrieve')
data = {
    'seen': loaded is not None,
    'enabled': bool(getattr(loaded, 'enabled', False)) if loaded else False,
    'error': getattr(loaded, 'error', None) if loaded else None,
    'tools': getattr(loaded, 'tools_registered', []) if loaded else [],
    'commands': getattr(loaded, 'commands_registered', []) if loaded else [],
}
print(json.dumps(data, sort_keys=True))
assert data['seen'], data
assert data['enabled'], data
assert data['error'] is None, data
assert 'headroom_retrieve' in data['tools'], data
assert 'headroom' in data['commands'], data
PY

echo "PASS: clean Hermes temp-home install/load works ($MODE)"
