# Agent install brief

Use this when another Hermes/AI agent is given only this repository URL and asked to install it in a Hermes instance.

## Goal

Install and enable the Hermes Headroom plugin without exposing secrets, copying owner-local state, or changing global/default provider routing. The plugin does not compress by itself; real compression/retrieval requires a healthy local Headroom proxy. Exact/edit-critical/sensitive content remains exact or blocked. Portable plugin operation should favor eligible-intermediate compression and savings by default. For this repo's own heavy development loops only, use scoped on-demand mode (`HEADROOM_AUTO_COMPRESSION=0` or `context_reduction.auto_compression: false`) so the runtime stays available but middleware auto-compression does not add overhead.

## Platform note

Linux, macOS, and native Windows are covered by this repo's CI/runtime smoke paths. WSL/Termux are expected when Hermes, git, and Python are available but still require target evidence. Native Windows should use native Hermes commands and the Python launcher; Bash helpers require Git Bash/WSL. Durable lifecycle uses the upstream user Task Scheduler adapter.

## Commands

Plugin install on the target Hermes instance:

```bash
hermes --version
git --version
hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
hermes plugins list --enabled --user --plain
hermes gateway restart || true
```

If operating inside an active Hermes chat instead of gateway shell, start a fresh session with `/new` after install.

Production runtime — required for `/headroom smoke`, `headroom_retrieve`, middleware compression, and wrapper compression. Native Hermes install clones the full repo; invoke its deterministic launcher:

```bash
PLUGIN_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve"
python3 "$PLUGIN_DIR/scripts/headroom-runtime.py" setup
```

Windows PowerShell:

```powershell
$PluginDir = if ($env:HERMES_HOME) { "$env:HERMES_HOME\plugins\headroom_retrieve" } else { "$HOME\.hermes\plugins\headroom_retrieve" }
py -3 "$PluginDir\scripts\headroom-runtime.py" setup
```

Wheel environments use the packaged entry point:

```bash
headroom-runtime setup
```

Before mutation, an agent may run `setup --dry-run --json`. The manager installs official `headroom-ai[proxy]==0.33.0` plus `litellm==1.94.0rc3` in an isolated versioned venv and reuses upstream manifests/native supervisors with `provider_mode=manual`, `targets=[]`, and `mutations=[]`. It skips direct upstream apply because a prior 0.32.1 canary wrote persistent shell blocks, then verifies `/readyz` and real compress → retrieve smoke. No provider API key or global routing change is required.

Status, full verification, and rollback:

```bash
headroom-runtime status --json
headroom-runtime doctor --json
headroom-runtime uninstall --json
```

The native Git launcher accepts the same subcommands. Claim `RUNTIME_FULL_DURABLE` only when `doctor` verifies the expected upstream profile/preset/port as `Status: running` and `Healthy: yes`, finds the matching native supervisor artifact, and passes readiness plus sentinel recovery. Exit code `0` alone is insufficient. See [runtime-manager.md](runtime-manager.md).

The old `install-production-runtime.py` remains only for v0.4 compatibility and optional `llm-monitor` companion operations.

## Verify

In Hermes:

```text
/headroom status
/headroom setup   # read-only setup guidance; does not install/start runtime
```

If full runtime/proxy validation is requested, verify the upstream Headroom dependency/runtime without touching the real environment:

```bash
python scripts/test-headroom-dependency-install.py
python scripts/test-headroom-runtime-smoke.py
# Unix wrapper:
scripts/test-headroom-dependency-install.sh
# or, after native Hermes install:
"${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve/scripts/test-headroom-dependency-install.sh"
```

Normal final answers do not show a Headroom marker by default. Operators may explicitly enable `[HR✓]`/`[HR!]`; the marker is readiness-only, not proof that a specific answer was compressed.

If a proxy is running:

```text
/headroom smoke
```

## Acceptance

PASS if:

- `hermes plugins list --enabled --user --plain` includes `headroom_retrieve`;
- `/headroom status` and `/headroom setup` respond after restart/new session;
- no secrets are requested or printed;
- global/default provider routing is unchanged;
- if proxy/runtime is enabled, eligible bulky intermediate tool/lane result compression is available via `tool_execution` middleware; without proxy, middleware returns the exact original result. Use `/headroom cache` only as read-only runtime-owned CCR store visibility; the plugin has no independent CCR cache.

PARTIAL if:

- install succeeds but `/headroom smoke` fails because no Headroom proxy is running; status/audit are usable, but compression/retrieval are not active.

FULL if:

- `headroom-runtime doctor --json` returns `RUNTIME_FULL_DURABLE`;
- install succeeds and `/headroom smoke` returns PASS with sentinel retrieval;
- runtime-dependent result-compression checks preserve exact/blocked tools such as `read_file`, `patch`, and `git diff`.

DURABLE if:

- `headroom-runtime doctor --json` returns `RUNTIME_FULL_DURABLE`;
- the upstream native user lifecycle reports the matching identity as running and healthy;
- the expected user-level supervisor artifact is present for the target platform;
- `/headroom smoke` still returns PASS after the relevant restart/login lifecycle.

Published v0.6.3 certifies the plugin Python `>=3.11,<3.15` range through blocking 3.11/3.14 unit, dependency, and managed-lifecycle boundary jobs on Linux, macOS, and native Windows; the v0.6.4 Headroom 0.33 pin requires a fresh blocking run before a tagged release. Python 3.12/3.13 remain inside that package contract; every real host still needs target-host `RUNTIME_FULL_DURABLE` evidence. WSL2/Termux remain target-evidence lanes. Existing-deployment reconciliation is separately tracked and is not a clean-install Python support gate.

FAIL if:

- plugin is not listed as enabled;
- `/headroom` command is unavailable after a fresh session/restart;
- install required copying owner-local `~/.hermes` state.

## Analyze without installing

```bash
git clone https://github.com/arotonal-ai/hermes-headroom-plugin.git
cd hermes-headroom-plugin
python scripts/run-isolated-unit-tests.py
scripts/audit-repo-readiness.sh
```

The unit runner bootstraps pinned pytest/PyYAML in an ephemeral `uv` environment when the selected interpreter lacks pytest or can import a live Hermes host. It never installs pytest into the production/runtime venv.

## Temp-home test when allowed

```bash
scripts/test-clean-hermes-install.sh --local
```

## Packaged wrappers

When a target has `RUNTIME_FULL`, the package also provides explicit command wrappers:

```bash
headroom-command-preflight --expected-chars 80000 -- pytest tests
headroom-worker-lane --lane tests --query "failures warnings verification" -- pytest tests
headroom-background-lane --lane build -- npm test
```

PASS if wrappers retain exact sidecars/final packets and only compress eligible bulky intermediate traces. FAIL if a wrapper changes global/default provider routing or requires owner-local scripts.

## Metrics

Weekly savings tables must be generated from retained JSONL evidence:

```bash
python scripts/generate-weekly-savings-table.py --input docs/metrics/data/*.jsonl --write docs/metrics/weekly-savings.md
```

## Rollback

1. Run `headroom-runtime uninstall --json` or the native Git launcher with `uninstall`.
2. On `UNINSTALL_PARTIAL`, preserve manager files and inspect private logs; do not delete supervisor artifacts blindly.
3. Disable or remove `headroom_retrieve`, then reload only the affected Hermes session/gateway if needed.
4. Restore the previous plugin release and its recorded runtime lifecycle when required.
5. Re-run plugin load/status and, when runtime capability is retained, compress → retrieve smoke.
