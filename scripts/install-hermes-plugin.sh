#!/usr/bin/env bash
set -euo pipefail

REPO="${HEADROOM_PLUGIN_REPO:-arotonal-ai/hermes-headroom-plugin}"
PLUGIN="${HEADROOM_PLUGIN_NAME:-headroom_retrieve}"
HERMES_HOME_EFFECTIVE="${HERMES_HOME:-$HOME/.hermes}"
LOCAL_MODE=0
FORCE=0
RESTART_HINT=1

usage() {
  cat <<'USAGE'
Install Hermes Headroom plugin into the active Hermes profile.

Usage:
  scripts/install-hermes-plugin.sh [--local] [--force] [--no-restart-hint]

Env:
  HEADROOM_PLUGIN_REPO   GitHub owner/repo or Git URL. Default: arotonal-ai/hermes-headroom-plugin
  HEADROOM_PLUGIN_NAME   Hermes plugin name. Default: headroom_retrieve
  HERMES_HOME            Target Hermes home. Default: ~/.hermes

Examples:
  hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
  scripts/install-hermes-plugin.sh
  scripts/install-hermes-plugin.sh --local
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --local) LOCAL_MODE=1 ;;
    --force|-f) FORCE=1 ;;
    --no-restart-hint) RESTART_HINT=0 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing required command: $1" >&2; exit 127; }
}

need hermes
need git

# Source the cross-platform Python resolver instead of hardcoding python3.
# shellcheck source=scripts/python-resolver.sh
SCRIPT_DIR_RESOLVER="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR_RESOLVER/python-resolver.sh"
resolve_python

printf 'Hermes: '; hermes --version
printf 'Target HERMES_HOME: %s\n' "$HERMES_HOME_EFFECTIVE"

if [[ "$LOCAL_MODE" -eq 1 ]]; then
  SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  REPO_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
  TARGET="$HERMES_HOME_EFFECTIVE/plugins/$PLUGIN"
  mkdir -p "$(dirname "$TARGET")"
  if [[ -e "$TARGET" || -L "$TARGET" ]]; then
    if [[ "$FORCE" -ne 1 ]]; then
      echo "Plugin target already exists: $TARGET" >&2
      echo "Re-run with --force or use: hermes plugins update $PLUGIN" >&2
      exit 1
    fi
    rm -rf "$TARGET"
  fi
  ln -s "$REPO_DIR" "$TARGET" 2>/dev/null || cp -R "$REPO_DIR" "$TARGET"
  hermes plugins enable "$PLUGIN"
else
  args=(plugins install "$REPO" --enable)
  if [[ "$FORCE" -eq 1 ]]; then
    args+=(--force)
  fi
  hermes "${args[@]}"
fi

hermes plugins list --enabled --user --plain

if hermes plugins list --enabled --user --plain | grep -Fq "$PLUGIN"; then
  echo "PASS: $PLUGIN is enabled."
else
  echo "FAIL: $PLUGIN not found in enabled user plugins." >&2
  exit 1
fi

if [[ "$RESTART_HINT" -eq 1 ]]; then
  cat <<EOF

Next step:
  - Gateway/platform sessions: hermes gateway restart
  - Active CLI/chat session: start /new
  - Then run in Hermes: /headroom status
EOF
fi
