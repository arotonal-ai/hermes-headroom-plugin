#!/usr/bin/env bash
set -euo pipefail

PLUGIN="${HEADROOM_PLUGIN_NAME:-headroom_retrieve}"
HERMES_HOME_EFFECTIVE="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME_EFFECTIVE/plugins/$PLUGIN"
RUN_SMOKE=0

usage() {
  cat <<'USAGE'
Verify Hermes Headroom plugin installation.

Usage:
  scripts/verify-hermes-install.sh [--smoke]

Checks:
  - hermes CLI exists
  - plugin is enabled in Hermes
  - plugin source directory exists when installed as a Git/directory plugin
  - optional proxy status/smoke helper can run from plugin source
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --smoke) RUN_SMOKE=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

command -v hermes >/dev/null 2>&1 || { echo "FAIL: hermes not found" >&2; exit 127; }
command -v python3 >/dev/null 2>&1 || { echo "FAIL: python3 not found" >&2; exit 127; }

printf 'Hermes: '; hermes --version
printf 'HERMES_HOME: %s\n' "$HERMES_HOME_EFFECTIVE"

LIST="$(hermes plugins list --enabled --user --plain || true)"
printf '%s\n' "$LIST"
if ! printf '%s\n' "$LIST" | grep -Fq "$PLUGIN"; then
  echo "FAIL: $PLUGIN is not enabled." >&2
  echo "Install: hermes plugins install arotonal-ai/hermes-headroom-plugin --enable" >&2
  exit 1
fi

echo "PASS: $PLUGIN is enabled."

if [[ -d "$PLUGIN_DIR/src" ]]; then
  export PYTHONPATH="$PLUGIN_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
  if [[ "$RUN_SMOKE" -eq 1 ]]; then
    python3 "$PLUGIN_DIR/src/hermes_headroom_plugin/proxy.py" smoke --json
  else
    python3 "$PLUGIN_DIR/src/hermes_headroom_plugin/proxy.py" status || true
  fi
else
  echo "INFO: plugin source directory not found at $PLUGIN_DIR/src; skipping source-level proxy helper."
fi

echo "Next in Hermes: /headroom status"
