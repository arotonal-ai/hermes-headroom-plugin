#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/python-resolver.sh
source "$SCRIPT_DIR/python-resolver.sh"
resolve_python || exit $?
exec "${PY_CMD[@]}" "$SCRIPT_DIR/install-production-runtime.py" "$@"
