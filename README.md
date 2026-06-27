# Hermes Headroom Plugin

Installable Hermes plugin for Headroom / Context Reduction Layer controls.

> **Primary install path:** this repo is meant to be installed by another Hermes instance directly with `hermes plugins install`.

## Quick install in Hermes

Run this from the target Hermes machine:

```bash
hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
hermes plugins list --enabled --user --plain
```

Then start a fresh session so Hermes reloads plugin discovery:

```bash
hermes gateway restart   # for Telegram/Discord/etc. gateway sessions
# or use /new in an active chat/CLI session
```

Verify inside Hermes:

```text
/headroom status
```

If a Headroom proxy is running, also run:

```text
/headroom smoke
```

Expected minimum result after install:

```text
headroom_retrieve appears as enabled in `hermes plugins list`
/headroom status responds without crashing
```

If `/headroom status` says the proxy is down, the plugin is installed correctly but the optional Headroom compression service is not running yet. See [INSTALL.md](INSTALL.md).

## One-command installer

For humans or another Hermes instance operating a shell:

```bash
curl -fsSL https://raw.githubusercontent.com/arotonal-ai/hermes-headroom-plugin/main/scripts/install-hermes-plugin.sh | bash
```

For a cloned checkout:

```bash
scripts/install-hermes-plugin.sh
scripts/verify-hermes-install.sh
```

## What this plugin adds

- `headroom_retrieve` tool for retrieving exact content behind Headroom CCR markers.
- `/headroom status` to check proxy reachability.
- `/headroom smoke` to run a real compress → retrieve synthetic sentinel check when the proxy is available.
- `/headroom audit` for local health/audit output.
- Conservative exact/compress/blocked policy helpers.

## Safe defaults

```text
Headroom = bounded context-reduction for eligible intermediates
not = global/default provider proxy routing
```

Defaults:

- plugin disabled until explicitly enabled by Hermes install command;
- no global/default provider routing;
- no external telemetry;
- secrets, memory, profile, system/developer instructions, patches, diffs, manifests, hashes, claim ledgers, final packets, and protected content are exact or blocked, not compressed;
- routing/canary/provider-cost features are opt-in advanced work, not first-install behavior.

## Install paths

### Recommended: native Hermes Git plugin

```bash
hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
hermes plugins list --enabled --user --plain
hermes gateway restart
```

This clones the repo under the target Hermes home, enables `headroom_retrieve`, and makes the slash command available after a fresh session/restart.

### Local checkout / development

```bash
git clone https://github.com/arotonal-ai/hermes-headroom-plugin.git
cd hermes-headroom-plugin
scripts/install-hermes-plugin.sh --local
scripts/verify-hermes-install.sh
```

### Python package / entry point path

The package also declares:

```toml
[project.entry-points."hermes_agent.plugins"]
headroom_retrieve = "hermes_headroom_plugin"
```

Use this path only when you intentionally install into the same Python environment that runs Hermes:

```bash
python -m pip install git+https://github.com/arotonal-ai/hermes-headroom-plugin.git
hermes plugins enable headroom_retrieve
hermes gateway restart
```

For most Hermes users, prefer `hermes plugins install`.

## Verification from a clean Hermes home

Maintainers can test the install path without touching their real profile:

```bash
TMP_HOME="$(mktemp -d)"
HERMES_HOME="$TMP_HOME" hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
HERMES_HOME="$TMP_HOME" hermes plugins list --enabled --user --plain
```

A passing install shows `headroom_retrieve` enabled.

## Local development checks

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m py_compile $(find src tests -name '*.py' | sort)
bash -n scripts/*.sh
```

If a local proxy is running:

```bash
python3 src/hermes_headroom_plugin/proxy.py smoke --json
```

## Documentation

- [INSTALL.md](INSTALL.md) — complete installation, verification, update, rollback, and troubleshooting guide.
- [docs/AGENT-INSTALL.md](docs/AGENT-INSTALL.md) — compact instructions for another Hermes/AI agent installing this repo.

## Non-goals in this repo stage

- No default/global provider proxy route.
- No owner profile/session/memory/project evidence copied into package payload.
- No external telemetry.
- No compression of patches, diffs, manifests, hashes, claim ledgers, final packets, secrets, memory/profile/system/developer instructions, or protected content.
