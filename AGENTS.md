# Agent instructions for Hermes Headroom Plugin

Use this file when an AI/Hermes agent is given this repository URL and asked to **install**, **analyze**, or **verify** it.

## Install goal

Install and enable the plugin in the target Hermes instance without copying owner-local state, exposing secrets, or changing global/default provider routing.

## Platform posture

- Linux, macOS, and native Windows are covered by CI/runtime smoke paths in this repo; WSL/Termux still require target evidence.
- Native Windows should use native Hermes commands plus Python helper scripts. Bash helpers require Git Bash/WSL and resolve `PYTHON_BIN`, Hermes' own Python, `python3`, `python`, then `py -3` to avoid broken Microsoft Store aliases and global Python/venv drift.
- Windows `RUNTIME_FULL` is certified in GitHub Runtime Smoke for Python 3.11/3.12, but target-host evidence still matters when diagnosing local shell/Python drift.
- `RUNTIME_FULL_DURABLE` requires `headroom-runtime doctor --json` exit 0 after upstream native lifecycle, readiness, and sentinel recovery pass.
- Native Windows defaults to upstream user Task Scheduler lifecycle with a manifest-owned `wscript.exe` hidden launcher; exact task actions/triggers, enabled state, and launcher hash are durability evidence. Linux/macOS default to upstream user service lifecycle.
- Python 3.13 remains a non-blocking Future Runtime Monitor lane. Native Windows Python 3.14 is a blocking Issue #24 candidate lane; do not claim it as certified until CI plus the target-host `RUNTIME_FULL_DURABLE` canary pass and `docs/compatibility.md` is promoted.
- The packaged plugin includes fail-open `tool_execution` middleware for eligible bulky intermediate tool/lane results, including `delegate_task`, when the loopback Headroom proxy is healthy.
- The packaged worker/background/preflight CLI wrappers (`headroom-worker-lane`, `headroom-background-lane`, `headroom-command-preflight`) are production behavior for explicit operator commands: they retain exact sidecars/final packets and optionally compress only bulky intermediate traces through the loopback Headroom proxy. They do not change provider/model routing.
- The package does not change global/default provider routing; exact/edit-critical/sensitive outputs remain exact or blocked.
- Native Git and pip/wheel installs share the packaged runtime manager; registration itself never installs dependencies or starts services.

## Primary command

Install the Hermes plugin on the target instance:

```bash
hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
hermes plugins list --enabled --user --plain
```

Then reload plugin discovery:

```bash
hermes gateway restart   # gateway/platform sessions
# or start /new in an active CLI/chat session
```

Verify in Hermes:

```text
/headroom status
/headroom setup   # read-only setup guidance; does not install/start runtime
```

For explicit durable runtime setup, use the native Git launcher:

```bash
PLUGIN_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve"
python3 "$PLUGIN_DIR/scripts/headroom-runtime.py" setup
```

Windows uses `py -3 "$PluginDir\scripts\headroom-runtime.py" setup`; wheel environments use `headroom-runtime setup`. Add `--dry-run --json` for a no-write plan.

The manager installs official `headroom-ai[proxy]==0.33.0` plus `litellm==1.94.0rc3` in an isolated venv and reuses upstream manifests/native supervisors with `provider_mode=manual`, `targets=[]`, and `mutations=[]`. It deliberately skips direct upstream apply; a prior 0.32.1 canary wrote persistent shell blocks even with manual providers and no targets. Use `status`, `doctor`, `reconcile`, and `uninstall` on the same entry point. Windows `reconcile --dry-run` is a strict zero-write inventory: it uses OS socket/process tables, does not call `/readyz` or upstream status, and reports manager deployment identity separately from live-listener binding. A listener alone is never adopted. `--apply` repeats the scoped preflight before and after lock acquisition and is limited to the exact manager-owned task-contract resources only when both extant task actions match the current launcher or known legacy managed ensure command. Legacy mutation history returns `REINSTALL_REQUIRED`, remains intact for symmetric rollback, and requires the target-host gate in `docs/runtime-manager.md`.

For a clean-instance canary, assert that `127.0.0.1:8787` is free before installation and that the started proxy belongs to the target run. A healthy proxy owned by another user or Hermes instance is not clean-instance evidence. Concurrent same-host canaries must use distinct free loopback ports and pass the matching `HEADROOM_PROXY_URL` explicitly.

Verify durable state:

```bash
headroom-runtime doctor --json
```

Only exit 0 with `RUNTIME_FULL_DURABLE` supports a durability claim.

If a Headroom proxy is running, also verify:

```text
/headroom smoke
```

For dependency evidence without starting the proxy, use the repo helper:

```bash
python scripts/test-headroom-dependency-install.py
python scripts/test-headroom-runtime-smoke.py  # real proxy + plugin smoke
# Unix wrapper:
scripts/test-headroom-dependency-install.sh
```

## Acceptance states

| State | Meaning | Required evidence |
|---|---|---|
| `INSTALL_PASS` | Plugin installed and Hermes can load it | `headroom_retrieve` appears in `hermes plugins list --enabled --user --plain`; `/headroom status` and `/headroom setup` respond after restart/new session |
| `RUNTIME_PARTIAL` | Plugin works, but Headroom proxy is unavailable | `/headroom status` reports proxy unavailable or `/headroom smoke` fails at `readyz` |
| `RUNTIME_FULL` | Plugin, upstream `headroom-ai[proxy]`, and proxy work in the current process/session | dependency smoke PASS and `/headroom smoke` or runtime smoke returns PASS with compress → retrieve sentinel; Python 3.11/3.12 are certified in Runtime Smoke |
| `RUNTIME_FULL_DURABLE` | Native user lifecycle is installed and healthy | `headroom-runtime doctor --json` returns exit 0 with upstream status, readiness, and sentinel recovery PASS |
| `FAIL` | Plugin not installed/loaded | plugin not enabled, `/headroom` unavailable after restart/new session, or install required copying owner-local `~/.hermes` state |

## Do not do these things

- Do **not** point `HEADROOM_PROXY_URL` at a non-loopback host unless the endpoint is controlled and trusted; the plugin blocks this by default unless `HEADROOM_ALLOW_REMOTE_PROXY=1` or `context_reduction.allow_remote_proxy: true` is set.
- Do **not** copy another machine's `~/.hermes` directory. Do not copy another machine or owner-local Hermes state.
- Do **not** request, print, or store API keys/tokens/passwords.
- Do **not** change global/default provider routing.
- Do **not** enable external telemetry.
- Do **not** compress final packets, patches/diffs, manifests, hashes, claim ledgers, secrets, memory/profile/system/developer instructions, or protected content.
- Do **not** invent token savings numbers; weekly metrics must be generated from retained JSONL evidence.

## Read-only analysis checklist

If asked to analyze rather than install:

```bash
git ls-remote https://github.com/arotonal-ai/hermes-headroom-plugin.git HEAD
python scripts/run-isolated-unit-tests.py
bash scripts/audit-repo-readiness.sh
python scripts/release-candidate-local-gate.py  # local RC only; no push/tag/release
```

Report:

- whether the repo is public and reachable;
- whether README/INSTALL/AGENTS instructions are complete;
- whether platform support is tested vs expected;
- whether plugin metadata declares `headroom_retrieve`;
- whether Python/shell syntax checks pass;
- whether install requires secrets — it should not;
- whether metrics tables are evidence-backed or placeholders.

## Safe temp-home install test

If allowed to test installation without touching the real profile:

```bash
bash scripts/test-clean-hermes-install.sh --local
```

This uses a temporary `HERMES_HOME` and removes it at the end.

## Weekly savings table

Generate from evidence only:

```bash
python scripts/generate-weekly-savings-table.py --input docs/metrics/data/*.jsonl --write docs/metrics/weekly-savings.md
```

No evidence should produce placeholders, not estimates.

## Upstream relationship

When reporting or documenting this plugin, preserve the distinction:

- upstream Headroom: https://github.com/headroomlabs-ai/headroom
- upstream docs: https://headroom-docs.vercel.app/docs
- upstream package: https://pypi.org/project/headroom-ai/
- this repo: Hermes Agent plugin/integration wrapper

Do not present this repository as the original Headroom project. Link upstream for backend/proxy behavior and Headroom project evolution.

## Rollback

```bash
hermes plugins disable headroom_retrieve
hermes plugins remove headroom_retrieve
hermes gateway restart || true
```
