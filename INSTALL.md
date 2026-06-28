# Install Hermes Headroom Plugin

This guide is self-contained for a fresh Hermes instance. It separates **plugin install** from the optional **Headroom proxy/backend runtime** so an operator does not confuse “Hermes loaded the plugin” with “compression is running”.

## Support posture

The plugin itself is Python/Hermes code and is intended to be platform-neutral. Current Bash helper scripts are Unix-shell first; Python helper scripts are provided for cross-platform checks. Do not claim a target OS is certified until the relevant checks pass on that OS.

| Platform | Plugin install | Helper scripts | Notes |
|---|---:|---:|---|
| Linux | ✅ tested | ✅ Bash + Python | Primary validated path |
| WSL2 | 🟡 expected | 🟡 Bash + Python | Needs explicit target run |
| macOS | 🟡 expected | 🟡 Bash + Python | CI target planned/maintained when enabled |
| Windows native | 🟡 possible via Hermes | 🟡 Python helpers; Bash requires Git Bash/WSL | Prefer `python scripts/...` commands |
| Termux | 🟡 expected | 🟡 Bash + Python | Depends on Hermes/Python/git availability |

## Prerequisites

On the target machine:

```bash
hermes --version
git --version
python --version  # or python3 --version
```

If `hermes` is not installed or not on `PATH`, install/fix Hermes first. This repository does not install Hermes Agent itself.

| Requirement | Needed for | Version / note | Link |
|---|---|---|---|
| Hermes Agent | Plugin install/load | Installed and on `PATH` | <https://hermes-agent.nousresearch.com/docs/getting-started/installation> |
| Git | `hermes plugins install owner/repo` | Any modern Git | <https://git-scm.com/downloads> |
| Python | Plugin package/scripts | Plugin package: `>=3.11`; upstream Headroom: `>=3.10` | <https://www.python.org/downloads/> |
| PyYAML | Plugin runtime dependency | `>=6,<7` | <https://pypi.org/project/PyYAML/> |
| `headroom-ai[proxy]` | Full compression runtime | Certified here: `>=0.26,<0.27`; run smoke before using newer upstream versions | <https://pypi.org/project/headroom-ai/> |
| API keys | Not required | Do not paste secrets into shell commands or issues | [SECURITY.md](SECURITY.md) |

Important distinction:

- **Hermes plugin install** does not require `headroom-ai`; it only needs Hermes + git.
- **Full compression runtime** requires the upstream Headroom Python package/proxy.
- This guide verifies both layers separately.

## Recommended install

```bash
hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
hermes plugins list --enabled --user --plain
hermes gateway restart
```

For a non-gateway CLI-only session, use `/new` instead of restarting the gateway.

## Verify in Hermes

In a fresh Hermes session:

```text
/headroom status
```

Expected behavior:

- command exists;
- it returns proxy status;
- if no Headroom proxy is running, it may report `ok=False` or connection failure, but the plugin should not crash.

If a Headroom proxy is already running at `http://127.0.0.1:28787` or `HEADROOM_PROXY_URL`, run:

```text
/headroom smoke
```

Expected successful smoke:

```text
Headroom smoke PASS ... sentinel_found=True
```

## Acceptance matrix

| State | Meaning | Evidence |
|---|---|---|
| `INSTALL_PASS` | Plugin installed and Hermes can load it | `hermes plugins list --enabled --user --plain` includes `headroom_retrieve`; `/headroom status` responds after restart/new session |
| `RUNTIME_PARTIAL` | Plugin works, proxy unavailable | `/headroom status` reports unavailable or `/headroom smoke` fails at `readyz` |
| `RUNTIME_FULL` | Plugin, dependency, and proxy all work | dependency smoke passes and `/headroom smoke` returns PASS with sentinel retrieval |
| `FAIL` | Plugin not usable | plugin not enabled, `/headroom` unavailable after restart/new session, or install required copying owner-local `~/.hermes` state |

## One-command install script

Unix shell path:

```bash
curl -fsSL https://raw.githubusercontent.com/arotonal-ai/hermes-headroom-plugin/main/scripts/install-hermes-plugin.sh | bash
```

Options when running from a clone:

```bash
scripts/install-hermes-plugin.sh --local      # symlink/copy this checkout into HERMES_HOME/plugins/headroom_retrieve
scripts/install-hermes-plugin.sh --force      # reinstall existing git plugin
scripts/install-hermes-plugin.sh --no-restart-hint
```

Windows/native-shell operators should prefer the native Hermes command directly:

```powershell
hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
hermes plugins list --enabled --user --plain
```

## What gets installed

Native Hermes install clones this repository into:

```text
$HERMES_HOME/plugins/headroom_retrieve
```

Usually `HERMES_HOME` is `~/.hermes` on Unix-like systems. On Windows, use the Hermes-reported home/config path rather than assuming a Unix path.

The plugin registers:

```text
tool:    headroom_retrieve
command: /headroom status|smoke|audit
```

## Optional proxy/backend setup

The plugin can be installed without a running Headroom proxy. In that state, `/headroom status` should report that the proxy is unavailable. That is `RUNTIME_PARTIAL`, not a failed plugin install.

### Step 1 — Verify the upstream Headroom dependency safely

Cross-platform Python helper:

```bash
python scripts/test-headroom-dependency-install.py
```

Unix shell wrapper:

```bash
scripts/test-headroom-dependency-install.sh
```

Or, after native Hermes install:

```bash
"${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve/scripts/test-headroom-dependency-install.sh"
```

What this proves:

- `headroom-ai[proxy]>=0.26,<0.27` installs in a clean temporary virtualenv;
- Python can import the Headroom package and proxy dependencies;
- the `headroom` CLI exists;
- `headroom proxy --help` exposes proxy startup options.

Equivalent manual check:

```bash
TMP_DIR="$(mktemp -d)"
python3 -m venv "$TMP_DIR/venv"
"$TMP_DIR/venv/bin/python" -m pip install 'headroom-ai[proxy]>=0.26,<0.27'
"$TMP_DIR/venv/bin/headroom" --help
"$TMP_DIR/venv/bin/headroom" proxy --help
rm -rf "$TMP_DIR"
```

On Windows PowerShell, use the Python helper instead of translating venv paths by hand.

### Step 2 — Run or point to a Headroom proxy

To use real compression/retrieval, run a compatible Headroom proxy and point the plugin to it. The plugin default is `http://127.0.0.1:28787`; upstream Headroom's CLI default may differ, so be explicit:

```bash
headroom proxy --host 127.0.0.1 --port 28787
```

Or point Hermes to an already-running proxy:

```bash
export HEADROOM_PROXY_URL="http://127.0.0.1:28787"
```

Or set Hermes config:

```yaml
context_reduction:
  proxy_url: http://127.0.0.1:28787
```

Then restart/fresh-session and run:

```text
/headroom smoke
```

This repo intentionally does **not** enable global provider routing by default.

Backend note: this repository installs the Hermes plugin and provides a dependency smoke test. For deeper Headroom backend/proxy lifecycle behavior, follow upstream Headroom documentation rather than duplicating it here:

- <https://github.com/chopratejas/headroom>
- <https://headroom-docs.vercel.app/docs>
- <https://pypi.org/project/headroom-ai/>

## Analyze without installing

```bash
git clone https://github.com/arotonal-ai/hermes-headroom-plugin.git
cd hermes-headroom-plugin
scripts/audit-repo-readiness.sh
```

This checks metadata, docs, syntax, shell scripts, markdown links, and obvious secret patterns without mutating Hermes.

## Validate dependency and install without touching real environments

Validate upstream Headroom dependency in a temporary Python venv:

```bash
python scripts/test-headroom-dependency-install.py
```

Validate plugin install in a temporary Hermes home:

```bash
scripts/test-clean-hermes-install.sh --local
```

The first script removes its temporary virtualenv. The second creates a temporary `HERMES_HOME`, installs/enables the plugin there, verifies that Hermes loads `headroom_retrieve` and `/headroom`, then removes the temp home.

## Metrics and savings table

Savings tables are generated from JSONL evidence, grouped by Monday. They are intentionally blank until real evidence exists:

```bash
python scripts/generate-weekly-savings-table.py --input docs/metrics/data/*.jsonl --write docs/metrics/weekly-savings.md
```

See [docs/metrics/weekly-savings.md](docs/metrics/weekly-savings.md).

## Update

```bash
hermes plugins update headroom_retrieve
hermes gateway restart
```

## Disable / remove / rollback

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

If installed from a local checkout with `--local`, remove the plugin directory/symlink manually if needed:

```bash
rm -rf "${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve"
```

## Troubleshooting

### `hermes plugins install` works but `/headroom` is unknown

Start a fresh session or restart the gateway:

```bash
hermes gateway restart
```

### GitHub page works but install fails

Check that the target machine has `git` and can reach GitHub:

```bash
git ls-remote https://github.com/arotonal-ai/hermes-headroom-plugin.git HEAD
```

### `headroom-ai` dependency install fails

This is a dependency/backend issue, not a Hermes plugin install issue. Capture:

```bash
python --version
python -m pip --version
python scripts/test-headroom-dependency-install.py --keep
```

Then inspect the kept temporary venv/logs and compare against upstream Headroom docs.

### Plugin is enabled but smoke fails at `readyz`

The plugin is installed; the Headroom proxy is not reachable. Start/configure the proxy, or set:

```bash
export HEADROOM_PROXY_URL="http://host:port"
```

### I need a clean proof without touching my real profile

```bash
TMP_HOME="$(mktemp -d)"
HERMES_HOME="$TMP_HOME" hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
HERMES_HOME="$TMP_HOME" hermes plugins list --enabled --user --plain
rm -rf "$TMP_HOME"
```

### Are worker/background wrappers included?

The entry points are declared so downstream commands have stable names, but full wrapper behavior is **pending P1 migration** in this installable repo. Owner-local deployments may have stronger wrappers outside this package. Do not assume packaged wrappers are production-ready until tests cover them.

### Is systemd required?

No. The bundled systemd template is Linux-only and optional. It is not required for plugin install or `/headroom status`; it is only a convenience for running a local proxy as a user service on compatible Linux hosts.
