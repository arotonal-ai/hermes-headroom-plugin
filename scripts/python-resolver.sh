# Shared Bash helper for selecting a usable Python on Unix, macOS, WSL, and Git Bash on Windows.
# Honors PYTHON_BIN first. Falls back through python3 -> python -> py -3, but only
# after proving the interpreter can execute Python code. This avoids broken
# Microsoft Store aliases in native Windows/Git Bash environments.

PY_CMD=()

_python_ok() {
  "$@" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

_python_has_module() {
  local module="$1"
  shift
  "$@" -c "import importlib.util, sys; raise SystemExit(0 if importlib.util.find_spec(${module@Q}) else 1)" >/dev/null 2>&1
}

_set_python_cmd() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    # shellcheck disable=SC2206 # intentional: allow PYTHON_BIN="py -3".
    PY_CMD=(${PYTHON_BIN})
    return 0
  fi
  return 1
}

resolve_python() {
  if _set_python_cmd; then
    if _python_ok "${PY_CMD[@]}"; then
      return 0
    fi
    echo "FAIL: PYTHON_BIN is set but is not a usable Python >= 3.10: ${PYTHON_BIN}" >&2
    return 127
  fi

  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && _python_ok "$candidate"; then
      PY_CMD=("$candidate")
      return 0
    fi
  done

  if command -v py >/dev/null 2>&1 && _python_ok py -3; then
    PY_CMD=(py -3)
    return 0
  fi

  echo "FAIL: no usable Python >= 3.10 found. Set PYTHON_BIN to the Hermes/target Python." >&2
  return 127
}

resolve_python_with_module() {
  local module="$1"
  if _set_python_cmd; then
    if _python_ok "${PY_CMD[@]}" && _python_has_module "$module" "${PY_CMD[@]}"; then
      return 0
    fi
    echo "FAIL: PYTHON_BIN cannot import required module '$module': ${PYTHON_BIN}" >&2
    return 127
  fi

  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && _python_ok "$candidate" && _python_has_module "$module" "$candidate"; then
      PY_CMD=("$candidate")
      return 0
    fi
  done

  if command -v py >/dev/null 2>&1 && _python_ok py -3 && _python_has_module "$module" py -3; then
    PY_CMD=(py -3)
    return 0
  fi

  echo "FAIL: no usable Python >= 3.10 can import '$module'. Set PYTHON_BIN to the Hermes Python." >&2
  return 127
}

run_python() {
  if [[ ${#PY_CMD[@]} -eq 0 ]]; then
    resolve_python || return $?
  fi
  "${PY_CMD[@]}" "$@"
}
