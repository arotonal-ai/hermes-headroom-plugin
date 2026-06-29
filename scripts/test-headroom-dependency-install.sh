#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/python-resolver.sh
source "$SCRIPT_DIR/python-resolver.sh"
resolve_python
echo "Using Python for Headroom dependency smoke: ${PY_CMD[*]}" >&2
exec "${PY_CMD[@]}" "$SCRIPT_DIR/test-headroom-dependency-install.py" "$@"
