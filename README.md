# Hermes Headroom Plugin

Installable Hermes plugin for Headroom / Context Reduction Layer controls.

> **Primary install path:** this repo is meant to be installed by another Hermes instance directly with `hermes plugins install`.


## Relationship to upstream Headroom

This repository is a **Hermes Agent integration plugin** for Headroom. It is not the upstream Headroom project and should not be read as a fork or replacement.

Upstream Headroom resources:

- Original/open-source project: [chopratejas/headroom](https://github.com/chopratejas/headroom)
- Documentation site: [headroomlabs-ai.github.io/headroom](https://headroomlabs-ai.github.io/headroom/)
- Python package: [`headroom-ai` on PyPI](https://pypi.org/project/headroom-ai/)

Acknowledgement: this plugin builds on the Headroom project's context-reduction ideas and Python package surface. The Hermes-specific work here is the installable plugin wrapper, safe admission policy, retrieval command, smoke/audit commands, and agent/human installation harnesses. See [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md).

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

## Expected states

| State | What it means | Expected result |
|---|---|---|
| `INSTALL_PASS` | Hermes installed and loaded the plugin | `headroom_retrieve` appears in `hermes plugins list --enabled --user --plain`; `/headroom status` responds |
| `RUNTIME_PARTIAL` | Plugin is installed but no Headroom proxy is reachable | `/headroom status` reports unavailable, or `/headroom smoke` fails at `readyz` |
| `RUNTIME_FULL` | Plugin and proxy both work | `/headroom smoke` passes a real compress → retrieve sentinel check |
| `FAIL` | Plugin cannot be used | plugin is not enabled, `/headroom` is unavailable after restart/new session, or install required copying owner-local state |

If `/headroom status` says the proxy is down, the plugin is installed correctly but the optional Headroom compression service is not running yet. See [INSTALL.md](INSTALL.md).

## Headroom dependency and proxy layer

Pedagogical split:

| Layer | What installs it | Required for |
|---|---|---|
| Hermes plugin | `hermes plugins install arotonal-ai/hermes-headroom-plugin --enable` | `INSTALL_PASS`: Hermes can load `headroom_retrieve` and `/headroom status` |
| Upstream Headroom Python package | `python -m pip install "headroom-ai[proxy]>=0.26,<0.27"` | running a local Headroom proxy/backend |
| Runtime proxy | `headroom proxy --port 28787` or another configured endpoint | `RUNTIME_FULL`: `/headroom smoke` can compress → retrieve |

The plugin intentionally does **not** require `headroom-ai` just to load. That keeps first install safe and lightweight. But if you want real compression/retrieval, verify the dependency layer:

```bash
scripts/test-headroom-dependency-install.sh
```

After native Hermes install, the same check is available from the installed plugin directory:

```bash
"${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve/scripts/test-headroom-dependency-install.sh"
```

This creates a temporary Python virtualenv, installs `headroom-ai[proxy]>=0.26,<0.27`, verifies imports, and checks `headroom --help` plus `headroom proxy --help`. It does not touch Hermes config or your real Python environment.

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

## Verification without touching the real Hermes profile

Read-only repo audit:

```bash
scripts/audit-repo-readiness.sh
```

Temporary `HERMES_HOME` install test:

```bash
scripts/test-clean-hermes-install.sh --local
```

Maintainers can test the remote install path with a temporary home:

```bash
TMP_HOME="$(mktemp -d)"
HERMES_HOME="$TMP_HOME" hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
HERMES_HOME="$TMP_HOME" hermes plugins list --enabled --user --plain
rm -rf "$TMP_HOME"
```

A passing install shows `headroom_retrieve` enabled.

## Local development checks

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m py_compile $(find src tests -name '*.py' | sort)
bash -n scripts/*.sh
scripts/audit-repo-readiness.sh
```

Verify upstream Headroom dependency in a temporary venv:

```bash
scripts/test-headroom-dependency-install.sh
```

If a local proxy is running:

```bash
python3 src/hermes_headroom_plugin/proxy.py smoke --json
```

## Documentation

- [AGENTS.md](AGENTS.md) — root instructions for another Hermes/AI agent.
- [INSTALL.md](INSTALL.md) — complete installation, verification, update, rollback, and troubleshooting guide.
- [docs/AGENT-INSTALL.md](docs/AGENT-INSTALL.md) — compact install brief for agents.
- [SECURITY.md](SECURITY.md) — security reporting and secret-handling policy.
- [PRIVACY.md](PRIVACY.md) — privacy and telemetry posture.

## Non-goals in this repo stage

- No default/global provider proxy route.
- No owner profile/session/memory/project evidence copied into package payload.
- No external telemetry.
- No compression of patches, diffs, manifests, hashes, claim ledgers, final packets, secrets, memory/profile/system/developer instructions, or protected content.
