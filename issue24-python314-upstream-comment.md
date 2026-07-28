@arotonal-ai — downstream Windows Python 3.14 runtime-manager fix, verified.

## Root cause

`_ensure_runtime()` / `_runtime_env()` in `runtime_manager.py` built subprocess
environments from `os.environ.copy()` (or an unfiltered `os.environ` dict)
without stripping `PYTHONPATH` / `PYTHONHOME`. When the runtime manager is
invoked from inside another Python process that has these set (e.g. the
Hermes agent process), the isolated venv's own `python.exe` inherited the
parent's `PYTHONPATH` and imported `fastapi`/`pydantic`/`pydantic_core` from
the *parent's* site-packages instead of the new venv's. On Python 3.14 this
surfaced as:

```
ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'
```

during `headroom.cli` import inside the freshly created runtime venv — the
isolated candidate never got a clean interpreter environment despite venv
creation and package installation succeeding.

## Fix

Added `_isolated_python_env()` and routed every subprocess invocation that
touches the managed venv (venv creation, pip install, version probe,
`_runtime_env()` for `status`/`doctor`/`reconcile`/`uninstall`) through it.
It strips `PYTHONPATH`/`PYTHONHOME` (case-insensitive) from the inherited
environment before use, leaving all other variables untouched.

## Verification

- Reproduced the exact failure on Windows/Python 3.14.5 with a contaminated
  `PYTHONPATH` inherited from a parent Python process.
- Patched `runtime_manager.py`; added regression tests:
  - `test_isolated_python_env_drops_pythonpath_and_pythonhome_only`
  - `test_runtime_env_drops_uncontrolled_headroom_variables` (extended to
    assert `PYTHONPATH`/`PYTHONHOME` are also dropped)
  - `test_runtime_install_uses_isolated_official_pypi` (extended to inject a
    contaminated `PYTHONPATH`/`PYTHONHOME` and assert none of the recorded
    subprocess calls received them)
- Focused suite: `tests/test_runtime_manager.py` — **68 passed, 3 skipped**
  (skips are POSIX-only markers, expected on Windows).
- Full suite: `python -m pytest tests/ -o 'addopts='` — **314 passed, 7
  skipped, 29 subtests passed**. No regressions.
- Re-ran the isolated candidate setup (`headroom-runtime setup`, profile
  `hermes-plugin-issue24-clean`, port `18787`, Python 3.14.5) end-to-end
  through venv creation, isolated pip install (`headroom-ai[proxy]==0.32.1`,
  `litellm==1.94.0rc3`), and version probe: all completed cleanly, the
  `pydantic_core` import failure is gone.

## Separate, unrelated blocker hit during the candidate canary

The candidate setup then failed at Windows Scheduled Task creation
(`schtasks /Create ... /SC ONSTART`) with `Error: Acceso denegado.` — this
requires administrator privileges on this host and is unrelated to the
Python 3.14 / `PYTHONPATH` fix. The candidate is left in `RUNTIME_PARTIAL`;
no legacy deployment, task, or listener was touched. Full `RUNTIME_FULL`
canary completion needs a separately authorized elevated session and is
tracked as follow-up, not part of this fix.

## Requested upstream action

1. Review/merge the `_isolated_python_env()` fix for `PYTHONPATH`/`PYTHONHOME`
   leakage into managed runtime subprocesses.
2. Consider adding a regression test that launches the runtime manager from a
   parent process with a contaminated `PYTHONPATH` (this bug only reproduces
   under process nesting, not a bare CLI invocation).
3. Track the Windows admin-privilege requirement for `ONSTART` scheduled
   tasks separately (already flagged in prior candidate preflight work).

Evidence boundary: source-suite and isolated dependency-install verification
are green; `RUNTIME_FULL_DURABLE` candidate canary is still pending on an
elevated-session retry, which is not authorized in this session.
