# Finish Headroom setup

The Hermes plugin is installed, but **real savings are not active until the local Headroom runtime passes smoke**. Hermes cloned this full repository to:

```text
${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve
```

## Linux gateway: durable runtime

```bash
PLUGIN_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve"
python3 "$PLUGIN_DIR/scripts/install-production-runtime.py" --systemd-user
```

## macOS, Linux without systemd, or process-level validation

```bash
PLUGIN_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve"
python3 "$PLUGIN_DIR/scripts/install-production-runtime.py"
```

## Windows PowerShell

```powershell
$PluginDir = if ($env:HERMES_HOME) { "$env:HERMES_HOME\plugins\headroom_retrieve" } else { "$HOME\.hermes\plugins\headroom_retrieve" }
py -3 "$PluginDir\scripts\install-production-runtime.py"
```

The installer uses the certified `headroom-ai[proxy]` release from PyPI, the package published by the [official Headroom project](https://github.com/headroomlabs-ai/headroom). Cloning upstream Headroom source is **not** required. It creates an isolated versioned venv, binds only to `127.0.0.1:8787` by default, disables telemetry, checks `/readyz`, and runs a real compress → retrieve smoke.

Then reload Hermes and verify:

```text
/headroom status
/headroom smoke
```

Expected state: `RUNTIME_FULL` or, on Linux durable mode, `RUNTIME_FULL_DURABLE`. With a healthy runtime, safe eligible-intermediate auto-compression is on by default. If you intentionally skip runtime setup, the plugin remains `RUNTIME_PARTIAL`: status/audit work, but no compression savings occur.

The setup does not change global model/provider routing and does not require provider API keys.
