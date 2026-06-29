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
/headroom on      # read-only compatibility check; does not mutate runtime/provider state
```

Expected: the commands exist and return proxy/status guidance, including `visible_marker=on:[HR✓]` when the marker is enabled and proxy readiness is healthy. If no proxy is running, it may report unavailable; that is `RUNTIME_PARTIAL`, not a failed plugin install.

## 2. Install Headroom runtime for real compression

The plugin can be used for `/headroom status` and `headroom_retrieve` without the upstream runtime. That state is `RUNTIME_PARTIAL`. For production `RUNTIME_FULL`, run the bundled installer from a repo/plugin checkout:

```bash
python scripts/install-production-runtime.py
# Unix/Git Bash wrapper:
scripts/install-production-runtime.sh
```

Windows PowerShell:

```powershell
python scripts\install-production-runtime.py
# or:
py -3 scripts\install-production-runtime.py
```

What the installer does:

1. Creates/updates a persistent venv: `~/.cache/hermes-headroom-venv`.
2. Installs latest available `headroom-ai[proxy]` by default; override only for rollback with `--spec` or `HEADROOM_AI_SPEC`.
3. Starts `headroom proxy --host 127.0.0.1 --port 28787` when no proxy is ready.
4. Verifies `/readyz`.
5. Runs real plugin compress → retrieve smoke.
6. Prints `RUNTIME_FULL` only when all checks pass.

Manual fallback on Unix/macOS/WSL:

```bash
python3 -m venv ~/.cache/hermes-headroom-venv
~/.cache/hermes-headroom-venv/bin/python -m pip install --upgrade pip
~/.cache/hermes-headroom-venv/bin/python -m pip install 'headroom-ai[proxy]'
~/.cache/hermes-headroom-venv/bin/headroom proxy --host 127.0.0.1 --port 28787
```

Manual Windows fallback: use `py -3 -m venv $env:USERPROFILE\.cache\hermes-headroom-venv`, install `headroom-ai[proxy]` with the venv Python, then run `Scripts\headroom.exe proxy --host 127.0.0.1 --port 28787`.

### Windows Git Bash / MSYS

Do not rely on `python3` on native Windows; it may be the broken Microsoft Store alias. Use the Python installer helper above, `python`, `py -3`, or set `PYTHON_BIN` for Bash wrappers.

Windows `RUNTIME_FULL` is certified by this repo's Runtime Smoke workflow for Python 3.11 and 3.12. Still require target-host evidence when diagnosing a user machine, because global Python installs and shell aliases can drift. Prefer Python 3.11/3.12 for the proxy venv on Windows; newer global Python versions may install but still fail native runtime imports.

Then verify inside Hermes:

```text
/headroom smoke
```

Expected: smoke PASS with sentinel retrieval. With a healthy proxy, the plugin can compress eligible bulky intermediate `tool_execution` results such as `delegate_task`, terminal/process, browser/debug, `web_extract`, and `session_search`. Exact/edit-critical/sensitive outputs still fail closed to the original result. This repo does **not** enable global/default provider routing by default.

## 3. Acceptance matrix

| State | Meaning | Evidence |
|---|---|---|
| `INSTALL_PASS` | Plugin installed and Hermes can load it | `hermes plugins list --enabled --user --plain` includes `headroom_retrieve`; `/headroom status` and `/headroom on` respond after restart/new session |
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

Compatibility: production install defaults to latest available `headroom-ai[proxy]` rather than pinning a historical version. If upstream releases regress, use `--spec` / `HEADROOM_AI_SPEC` as an explicit rollback override and capture dependency + runtime smoke evidence before changing the documented default. See [docs/compatibility.md](docs/compatibility.md) for certified vs experimental runtime support; Python 3.13/3.14 are monitored separately and are not certified by default.

## 5. Proxy endpoint configuration

Default plugin/runtime target:

```text
http://127.0.0.1:28787
```

By default, assistant final answers include `[HR✓]` when proxy readiness is healthy and `[HR!]` when the visible marker is enabled but readiness fails. This reports runtime readiness only, not per-message compression. Disable it with `context_reduction.visible_status_marker: false` or `HEADROOM_VISIBLE_STATUS_MARKER=0`.

This integration intentionally uses `28787` for the Hermes-facing Headroom proxy. Upstream Headroom may have a different CLI default; do not rely on that default. Start production runtime with `headroom proxy --host 127.0.0.1 --port 28787` or use `scripts/install-production-runtime.py`, which passes the port explicitly.

To point Hermes at another local/controlled endpoint:

```bash
export HEADROOM_PROXY_URL="http://127.0.0.1:28787"
```

Or set Hermes config:

```yaml
context_reduction:
  proxy_url: http://127.0.0.1:28787
```

Restart/fresh-session before rechecking `/headroom status` and `/headroom on`.

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

### Is systemd required?

No. The bundled systemd template is Linux-only and optional.

### Are worker/background wrappers included?

Yes. `headroom-worker-lane`, `headroom-background-lane`, and `headroom-command-preflight` are packaged production wrappers for explicit operator commands. They retain exact stdout/stderr sidecars and exact `worker-final-packet.md`, then compress only eligible bulky intermediate traces through the configured loopback Headroom proxy. They do not mutate Hermes provider/model routing. Natural `hr-*` smart-route aliases are not packaged behavior.
