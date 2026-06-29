# Shared Bash helper for selecting a usable Python on Unix, macOS, WSL, and Git Bash on Windows.
# Honors PYTHON_BIN first. Falls back through python3 -> python -> py -3, but only
# after proving the interpreter can execute Python code. This avoids broken
# Microsoft Store aliases in native Windows/Git Bash environments.
#
# For Hermes-aware checks, resolve_python_with_module also probes the Python
# colocated with the `hermes` executable. This catches native Windows installs
# where global Python is usable but cannot import hermes_cli, while Hermes' own
# venv Python can.

PY_CMD=()

_python_ok() {
  "$@" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

_python_has_module() {
  local module="$1"
  shift
  HERMES_HEADROOM_MODULE="$module" "$@" -c 'import importlib.util, os, sys; raise SystemExit(0 if importlib.util.find_spec(os.environ["HERMES_HEADROOM_MODULE"]) else 1)' >/dev/null 2>&1
}

_set_python_cmd() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    # shellcheck disable=SC2206 # intentional: allow PYTHON_BIN="py -3".
    PY_CMD=(${PYTHON_BIN})
    return 0
  fi
  return 1
}

_try_python_cmd() {
  if _python_ok "$@"; then
    PY_CMD=("$@")
    return 0
  fi
  return 1
}

_try_python_cmd_with_module() {
  local module="$1"
  shift
  if _python_ok "$@" && _python_has_module "$module" "$@"; then
    PY_CMD=("$@")
    return 0
  fi
  return 1
}

_probe_hermes_python_candidates() {
  command -v hermes >/dev/null 2>&1 || return 0

  local hermes_path hermes_dir shebang shebang_cmd
  hermes_path="$(command -v hermes)"
  hermes_dir="$(cd -- "$(dirname -- "$hermes_path")" 2>/dev/null && pwd -P || true)"

  # Windows venv console scripts and Unix venv installs usually keep Python next
  # to the hermes launcher. Git Bash can execute /c/.../Scripts/python.exe.
  if [[ -n "$hermes_dir" ]]; then
    printf '%s\0' \
      "$hermes_dir/python.exe" \
      "$hermes_dir/python" \
      "$hermes_dir/python3"
  fi

  # If `hermes` is a text script, its shebang may point at the exact venv Python.
  if [[ -f "$hermes_path" ]]; then
    IFS= read -r shebang < "$hermes_path" || true
    if [[ "$shebang" == '#!'*python* ]]; then
      shebang_cmd="${shebang#'#!'}"
      # Strip common env wrapper only when it names python directly.
      if [[ "$shebang_cmd" == */env\ python* || "$shebang_cmd" == "env python"* ]]; then
        shebang_cmd="${shebang_cmd#*/env }"
      fi
      [[ -n "$shebang_cmd" ]] && printf '%s\0' "$shebang_cmd"
    fi
  fi
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
    if command -v "$candidate" >/dev/null 2>&1 && _try_python_cmd "$candidate"; then
      return 0
    fi
  done

  if command -v py >/dev/null 2>&1 && _try_python_cmd py -3; then
    return 0
  fi

  while IFS= read -r -d '' candidate; do
    if [[ -x "$candidate" ]] && _try_python_cmd "$candidate"; then
      return 0
    fi
  done < <(_probe_hermes_python_candidates)

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

  # If a Hermes module is required, prefer Hermes' own Python before global
  # interpreters. This avoids native Windows cases where Python 3.14 is global
  # but Hermes lives in a 3.11/3.12 venv.
  local candidate
  if [[ "$module" == hermes* ]]; then
    while IFS= read -r -d '' candidate; do
      if [[ -x "$candidate" ]] && _try_python_cmd_with_module "$module" "$candidate"; then
        return 0
      fi
    done < <(_probe_hermes_python_candidates)
  fi

  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && _try_python_cmd_with_module "$module" "$candidate"; then
      return 0
    fi
  done

  if command -v py >/dev/null 2>&1 && _try_python_cmd_with_module "$module" py -3; then
    return 0
  fi

  while IFS= read -r -d '' candidate; do
    if [[ -x "$candidate" ]] && _try_python_cmd_with_module "$module" "$candidate"; then
      return 0
    fi
  done < <(_probe_hermes_python_candidates)

  echo "FAIL: no usable Python >= 3.10 can import '$module'." >&2
  echo "Hint: set PYTHON_BIN to Hermes' venv Python, for example:" >&2
  echo "  PYTHON_BIN=/path/to/hermes-agent/venv/Scripts/python.exe scripts/test-clean-hermes-install.sh --local" >&2
  echo "  PYTHON_BIN=/path/to/hermes-agent/venv/bin/python scripts/test-clean-hermes-install.sh --local" >&2
  return 127
}

run_python() {
  if [[ ${#PY_CMD[@]} -eq 0 ]]; then
    resolve_python || return $?
  fi
  "${PY_CMD[@]}" "$@"
}
