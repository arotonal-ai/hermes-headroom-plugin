# Finish Headroom setup

The Hermes plugin is installed, but **real savings are not active until the local Headroom runtime passes compress → retrieve smoke**.

## Native Hermes Git install

Hermes cloned the full repository to:

```text
${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve
```

Linux or macOS:

```bash
PLUGIN_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve"
python3 "$PLUGIN_DIR/scripts/headroom-runtime.py" setup
```

Windows PowerShell:

```powershell
$PluginDir = if ($env:HERMES_HOME) { "$env:HERMES_HOME\plugins\headroom_retrieve" } else { "$HOME\.hermes\plugins\headroom_retrieve" }
py -3 "$PluginDir\scripts\headroom-runtime.py" setup
```

## Wheel install

```bash
headroom-runtime setup
```

The base wheel is enough; setup creates a separate versioned runtime venv.

## Inspect first without changing state

Add `--dry-run --json` to either route:

```bash
headroom-runtime setup --dry-run --json
```

## Verify

```bash
headroom-runtime status --json
headroom-runtime doctor --json
```

Then reload Hermes and run:

```text
/headroom status
/headroom smoke
```

Expected durable state: `RUNTIME_FULL_DURABLE`.

The v0.5.1 manager installs the pinned official `headroom-ai[proxy]==0.32.1` package plus `litellm==1.91.3` in an isolated venv, binds only to `127.0.0.1:8787` by default, disables telemetry, uses manual provider selection with no targets, checks upstream lifecycle/readiness, and runs real compress → retrieve smoke.

Setup does **not** change global model/provider routing and does not require provider API keys. If you skip it, the plugin remains `RUNTIME_PARTIAL`: status/audit work, but no compression savings occur.

## Rollback

```bash
headroom-runtime uninstall --json
```

For native Git installs, use the same launcher:

```bash
python3 "$PLUGIN_DIR/scripts/headroom-runtime.py" uninstall --json
```

The command preserves runtime files and returns `UNINSTALL_PARTIAL` if upstream removal fails or the listener remains ready. See [`docs/runtime-manager.md`](docs/runtime-manager.md) for the full contract and recovery limits.
