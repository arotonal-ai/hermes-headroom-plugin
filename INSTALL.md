# Install Hermes Headroom Plugin

This guide is the shortest safe path for a fresh Hermes instance. It separates the **Hermes plugin** from the optional **Headroom proxy/backend** so operators do not confuse “plugin loaded” with “compression runtime running”.

## 0. Prerequisites

On the target machine:

```bash
hermes --version
git --version
python --version  # or python3 --version
```

If `hermes` is missing, install/fix Hermes Agent first: <https://hermes-agent.nousresearch.com/docs/getting-started/installation>.

| Requirement | Needed for | Note |
|---|---|---|
| Hermes Agent | plugin install/load | must be on `PATH` |
| Git | `hermes plugins install owner/repo` | required by native plugin install |
| Python | helper scripts and optional proxy venv | use a Python supported by Hermes and upstream Headroom |
| `headroom-ai[proxy]` | full compression runtime | optional; not required for plugin load |
| API keys | not needed | do not paste secrets into install commands or issues |

## 1. Install the Hermes plugin

Run on the owner/target Hermes instance:

```bash
hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
hermes plugins list --enabled --user --plain
hermes gateway restart
```

For a CLI-only or active chat session, start a fresh session with `/new` instead of restarting the gateway.

Verify inside Hermes:

```text
/headroom status
```

Expected: the command exists and returns proxy status. If no proxy is running, it may report unavailable; that is `RUNTIME_PARTIAL`, not a failed plugin install.

## 2. Optional: install Headroom runtime for real compression

The plugin can be used for `/headroom status` and `headroom_retrieve` without installing the upstream Headroom runtime. Install the runtime only when the owner/instance wants `RUNTIME_FULL`.

### Unix/macOS/WSL

```bash
python3 -m venv ~/.cache/hermes-headroom-venv
~/.cache/hermes-headroom-venv/bin/python -m pip install --upgrade pip
~/.cache/hermes-headroom-venv/bin/python -m pip install 'headroom-ai[proxy]>=0.26,<0.28'
~/.cache/hermes-headroom-venv/bin/headroom proxy --host 127.0.0.1 --port 28787
```

### Windows PowerShell

```powershell
py -m venv $env:USERPROFILE\.cache\hermes-headroom-venv
& $env:USERPROFILE\.cache\hermes-headroom-venv\Scripts\python.exe -m pip install --upgrade pip
& $env:USERPROFILE\.cache\hermes-headroom-venv\Scripts\python.exe -m pip install 'headroom-ai[proxy]>=0.26,<0.28'
& $env:USERPROFILE\.cache\hermes-headroom-venv\Scripts\headroom.exe proxy --host 127.0.0.1 --port 28787
```

### Windows Git Bash / MSYS

Do not rely on `python3` on native Windows; it may be the broken Microsoft Store alias. Use `python`, `py -3`, or set `PYTHON_BIN` for the Bash helpers.

```bash
python -m venv "$HOME/.cache/hermes-headroom-venv"
"$HOME/.cache/hermes-headroom-venv/Scripts/python.exe" -m pip install --upgrade pip
"$HOME/.cache/hermes-headroom-venv/Scripts/python.exe" -m pip install 'headroom-ai[proxy]>=0.26,<0.28'
"$HOME/.cache/hermes-headroom-venv/Scripts/headroom.exe" proxy --host 127.0.0.1 --port 28787
```

Windows `RUNTIME_FULL` is certified by this repo's Runtime Smoke workflow for Python 3.11 and 3.12. Still require target-host evidence when diagnosing a user machine, because global Python installs and shell aliases can drift. Prefer Python 3.11/3.12 for the proxy venv on Windows; newer global Python versions may install but still fail native runtime imports.

Then verify inside Hermes:

```text
/headroom smoke
```

Expected: smoke PASS with sentinel retrieval. With a healthy proxy, the plugin can compress eligible bulky intermediate `tool_execution` results such as `delegate_task`, terminal/process, browser/debug, `web_extract`, and `session_search`. Exact/edit-critical/sensitive outputs still fail closed to the original result. This repo does **not** enable global/default provider routing by default.

## 3. Acceptance matrix

| State | Meaning | Evidence |
|---|---|---|
| `INSTALL_PASS` | Plugin installed and Hermes can load it | `hermes plugins list --enabled --user --plain` includes `headroom_retrieve`; `/headroom status` responds after restart/new session |
| `RUNTIME_PARTIAL` | Plugin works, proxy unavailable | `/headroom status` reports unavailable or `/headroom smoke` fails at `readyz` |
| `RUNTIME_FULL` | Plugin, dependency, and proxy all work | dependency smoke passes and `/headroom smoke` returns PASS with sentinel retrieval |
| `FAIL` | Plugin not usable | plugin not enabled, `/headroom` unavailable after restart/new session, or install required copying owner-local `~/.hermes` state |

## 4. Optional validation helpers

Use these from a repo checkout when you want evidence without mutating real environments.

Analyze without installing:

```bash
git clone https://github.com/arotonal-ai/hermes-headroom-plugin.git
cd hermes-headroom-plugin
scripts/audit-repo-readiness.sh
```

Validate upstream Headroom dependency in a temporary Python venv:

```bash
python scripts/test-headroom-dependency-install.py
python scripts/test-headroom-runtime-smoke.py  # starts real loopback proxy + plugin smoke
# Unix wrappers:
scripts/test-headroom-dependency-install.sh
```

Validate plugin install in a temporary Hermes home:

```bash
scripts/test-clean-hermes-install.sh --local
```

After native Hermes install, the dependency helper is also available from the installed plugin directory:

```bash
"${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve/scripts/test-headroom-dependency-install.sh"
```

On Windows, local `--local` development install may copy the repo instead of creating a symlink if symlink privileges are unavailable. That fallback is expected.

The helper proves that the upstream package installs in an isolated venv, imports `headroom`, `fastapi`, `uvicorn`, and `pydantic_core._pydantic_core`, and exposes `headroom --help` plus `headroom proxy --help`.

Compatibility: this repo currently accepts `headroom-ai[proxy]>=0.26,<0.28`. Do not widen beyond that range until dependency smoke and runtime smoke pass. See [docs/compatibility.md](docs/compatibility.md) for certified vs experimental runtime support; Python 3.13/3.14 are monitored separately and are not certified by default.

## 5. Proxy endpoint configuration

Default plugin target:

```text
http://127.0.0.1:28787
```

To point Hermes at another local/controlled endpoint:

```bash
export HEADROOM_PROXY_URL="http://127.0.0.1:28787"
```

Or set Hermes config:

```yaml
context_reduction:
  proxy_url: http://127.0.0.1:28787
```

Restart/fresh-session before rechecking `/headroom status`.

**Remote proxy guardrail:** loopback (`127.0.0.1` / `localhost`) is allowed by default. Non-loopback `HEADROOM_PROXY_URL` is blocked unless you explicitly set `HEADROOM_ALLOW_REMOTE_PROXY=1` or `context_reduction.allow_remote_proxy: true`; use that only for controlled, trusted endpoints.

## 6. Update

```bash
hermes plugins update headroom_retrieve
hermes gateway restart
```

## 7. Disable / remove / rollback

Disable but keep files:

```bash
hermes plugins disable headroom_retrieve
hermes gateway restart
```

Remove plugin files:

```bash
hermes plugins remove headroom_retrieve
hermes gateway restart
```

If installed from a local checkout with `--local`, remove the plugin directory/symlink manually only after confirming it is the intended target:

```bash
rm -rf "${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve"
```

## 8. Metrics and savings table

Savings tables are generated from JSONL evidence, grouped by Monday. If no evidence exists, the table stays as placeholders rather than estimated numbers.

```bash
python scripts/generate-weekly-savings-table.py --input docs/metrics/data/*.jsonl --write docs/metrics/weekly-savings.md
```

See [docs/metrics/weekly-savings.md](docs/metrics/weekly-savings.md).

## 9. Troubleshooting

### `hermes plugins install` works but `/headroom` is unknown

Start a fresh session or restart the gateway:

```bash
hermes gateway restart
```

### GitHub page works but install fails

Check network/Git access from the target machine:

```bash
git ls-remote https://github.com/arotonal-ai/hermes-headroom-plugin.git HEAD
```

### Dependency install fails

This is a backend/runtime issue, not a Hermes plugin install issue. On Windows, prefer a Hermes-compatible Python 3.11/3.12 venv for `RUNTIME_FULL`; if using a newer global Python, verify native imports before trusting proxy startup. Capture the environment and rerun with kept temp files:

```bash
python --version
python -m pip --version
python scripts/test-headroom-dependency-install.py --keep
```

Then compare against upstream Headroom docs:

- <https://github.com/chopratejas/headroom>
- <https://headroom-docs.vercel.app/docs>
- <https://pypi.org/project/headroom-ai/>

### Smoke fails at `readyz`

The plugin is installed; the proxy is not reachable. Start/configure the proxy, or set:

```bash
export HEADROOM_PROXY_URL="http://host:port"
```

### Native Windows shell

Use native `hermes` commands and Python helpers. Bash helpers require Git Bash or WSL.

```powershell
hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
hermes plugins list --enabled --user --plain
python scripts/test-headroom-dependency-install.py
```

### Is systemd required?

No. The bundled systemd template is Linux-only and optional.

### Are worker/background wrappers included?

The entry points are declared so downstream commands have stable names, but full wrapper behavior is **pending P1 migration** in this installable repo. Owner-local deployments may have stronger wrappers outside this package. Do not assume packaged wrappers are production-ready until tests cover them.
