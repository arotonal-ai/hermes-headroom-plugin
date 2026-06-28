# Install Hermes Headroom Plugin

This guide is self-contained for a fresh Hermes instance.

## Prerequisites

On the target machine:

```bash
hermes --version
git --version
```

If `hermes` is not installed or not on `PATH`, install/fix Hermes first. This repository does not install Hermes Agent itself.

No API keys are required for plugin installation. Do not paste secrets into shell commands or GitHub issues.

Important distinction:

- **Hermes plugin install** does not require `headroom-ai`; it only needs Hermes + git.
- **Full compression runtime** requires the upstream Headroom Python package/proxy.
- This guide verifies both layers separately so an agent does not confuse “plugin installed” with “proxy running”.

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
| `RUNTIME_FULL` | Plugin and proxy both work | `/headroom smoke` returns PASS with sentinel retrieval |
| `FAIL` | Plugin not usable | plugin not enabled, `/headroom` unavailable after restart/new session, or install required copying owner-local `~/.hermes` state |

## One-command install script

```bash
curl -fsSL https://raw.githubusercontent.com/arotonal-ai/hermes-headroom-plugin/main/scripts/install-hermes-plugin.sh | bash
```

Options when running from a clone:

```bash
scripts/install-hermes-plugin.sh --local      # symlink/copy this checkout into HERMES_HOME/plugins/headroom_retrieve
scripts/install-hermes-plugin.sh --force      # reinstall existing git plugin
scripts/install-hermes-plugin.sh --no-restart-hint
```

## What gets installed

Native Hermes install clones this repository into:

```text
$HERMES_HOME/plugins/headroom_retrieve
```

Usually `HERMES_HOME` is `~/.hermes`.

The plugin registers:

```text
tool:    headroom_retrieve
command: /headroom status|smoke|audit
```

## Optional proxy/backend setup

The plugin can be installed without a running Headroom proxy. In that state, `/headroom status` should report that the proxy is unavailable. That is `RUNTIME_PARTIAL`, not a failed plugin install.

### Step 1 — Verify the upstream Headroom dependency safely

From a cloned checkout of this repo:

```bash
scripts/test-headroom-dependency-install.sh
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

- https://github.com/chopratejas/headroom
- https://headroomlabs-ai.github.io/headroom/quickstart/
- https://pypi.org/project/headroom-ai/

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
scripts/test-headroom-dependency-install.sh
```

Validate plugin install in a temporary Hermes home:

```bash
scripts/test-clean-hermes-install.sh --local
```

The first script removes its temporary virtualenv. The second creates a temporary `HERMES_HOME`, installs/enables the plugin there, verifies that Hermes loads `headroom_retrieve` and `/headroom`, then removes the temp home.

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
python3 --version
python3 -m pip --version
scripts/test-headroom-dependency-install.sh --keep
```

Then inspect the kept temporary venv/logs and compare against upstream Headroom docs.

### Plugin is enabled but smoke fails at `readyz`

The plugin is installed; the Headroom proxy is not reachable. Start/configure the proxy, or set:

```bash
export HEADROOM_PROXY_URL="http://host:port"
```

### Validate remote install with a temp Hermes home

```bash
TMP_HOME="$(mktemp -d)"
HERMES_HOME="$TMP_HOME" hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
HERMES_HOME="$TMP_HOME" hermes plugins list --enabled --user --plain
rm -rf "$TMP_HOME"
```

## Security notes

- No secrets are required to install this plugin.
- Do not put API keys in this repo.
- `.env`, logs, caches, build outputs, and Python bytecode are ignored.
- The plugin does not enable external telemetry.
- The first-install path does not change global/default provider routing.
