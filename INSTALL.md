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

The plugin can be installed without a running Headroom proxy. In that state, `/headroom status` should report that the proxy is unavailable.

To use real compression/retrieval, run a compatible Headroom proxy and point the plugin to it:

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

## Analyze without installing

```bash
git clone https://github.com/arotonal-ai/hermes-headroom-plugin.git
cd hermes-headroom-plugin
scripts/audit-repo-readiness.sh
```

This checks metadata, docs, syntax, shell scripts, markdown links, and obvious secret patterns without mutating Hermes.

## Validate install without touching real Hermes home

```bash
scripts/test-clean-hermes-install.sh --local
```

This creates a temporary `HERMES_HOME`, installs/enables the plugin there, verifies that Hermes loads `headroom_retrieve` and `/headroom`, then removes the temp home.

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
