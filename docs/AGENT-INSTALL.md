# Agent install brief

Use this when another Hermes/AI agent is given only this repository URL and asked to install it in a Hermes instance.

## Goal

Install and enable the Hermes Headroom plugin without exposing secrets, copying owner-local state, or changing global/default provider routing. The plugin does not compress by itself; real compression/retrieval requires a healthy local Headroom proxy. Exact/edit-critical/sensitive content remains exact or blocked. Portable plugin operation should favor eligible-intermediate compression and savings by default. For this repo's own heavy development loops only, use scoped on-demand mode (`HEADROOM_AUTO_COMPRESSION=0` or `context_reduction.auto_compression: false`) so the runtime stays available but middleware auto-compression does not add overhead.

## Platform note

Linux, macOS, and native Windows are covered by this repo's CI/runtime smoke paths. WSL/Termux are expected when Hermes, git, and Python are available but still require target evidence. Native Windows should use native Hermes commands and Python helper scripts; Bash helpers require Git Bash/WSL.

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

Production runtime for process-level `RUNTIME_FULL` — required for `/headroom smoke`, `headroom_retrieve`, middleware compression, and wrapper compression:

```bash
python scripts/install-production-runtime.py
# Unix/Git Bash wrapper:
scripts/install-production-runtime.sh
```

Linux durable gateway/default-cockpit runtime:

```bash
python scripts/install-production-runtime.py --systemd-user
systemctl --user is-enabled hermes-context-reduction.service
systemctl --user is-active hermes-context-reduction.service
```

This must return `RUNTIME_FULL_DURABLE` before claiming restart/logout durability.

Windows PowerShell:

```powershell
python scripts\install-production-runtime.py
# or:
py -3 scripts\install-production-runtime.py
```

The installer creates/updates `~/.cache/hermes-headroom-venv-0.31.0`, installs `headroom-ai[proxy]==0.31.0` with portable constraint `litellm==1.91.3`, defaults CCR to memory with a 1,800-second TTL, starts the loopback proxy, verifies `/readyz`, and runs real compress → retrieve smoke. The LiteLLM pin prevents macOS/Windows from falling back to a Rust source build after `1.92.0` dropped those wheels. `llm-monitor` is opt-in via `--with-llm-monitor-companion` or `--companion-only`. Manual install is acceptable only if the same checks pass. See [portable-core.md](portable-core.md).

No-restart companion-only validation:

```bash
python scripts/install-production-runtime.py --companion-only --hermes-home /tmp/hermes-home --json
```

## Verify

In Hermes:

```text
/headroom status
/headroom on      # read-only compatibility check; does not mutate runtime/provider state
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
- `/headroom status` and `/headroom on` respond after restart/new session;
- no secrets are requested or printed;
- global/default provider routing is unchanged;
- if proxy/runtime is enabled, eligible bulky intermediate tool/lane result compression is available via `tool_execution` middleware; without proxy, middleware returns the exact original result. Use `/headroom cache` only as read-only runtime-owned CCR store visibility; the plugin has no independent CCR cache.

PARTIAL if:

- install succeeds but `/headroom smoke` fails because no Headroom proxy is running; status/audit are usable, but compression/retrieval are not active.

FULL if:

- `scripts/install-production-runtime.py` returns `RUNTIME_FULL`, or dependency smoke plus `/headroom smoke` returns PASS with sentinel retrieval;
- install succeeds and `/headroom smoke` returns PASS with sentinel retrieval;
- runtime-dependent result-compression checks preserve exact/blocked tools such as `read_file`, `patch`, and `git diff`.

DURABLE on Linux if:

- `scripts/install-production-runtime.py --systemd-user` returns `RUNTIME_FULL_DURABLE`;
- `hermes-context-reduction.service` is enabled + active;
- `/headroom smoke` still returns PASS after gateway restart/logout.

Windows native `FULL` is certified by this repo's Runtime Smoke workflow for Python 3.11/3.12, but still require target-host evidence when diagnosing a specific machine. Python 3.13/3.14 are experimental monitor paths, not certified support. Durable Windows supervision is not bundled; use an operator-approved Task Scheduler/service wrapper if restart durability is required.

FAIL if:

- plugin is not listed as enabled;
- `/headroom` command is unavailable after a fresh session/restart;
- install required copying owner-local `~/.hermes` state.

## Analyze without installing

```bash
git clone https://github.com/arotonal-ai/hermes-headroom-plugin.git
cd hermes-headroom-plugin
scripts/audit-repo-readiness.sh
```

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

1. Disable or remove `headroom_retrieve`, then reload only the affected Hermes session/gateway if needed.
2. If durable runtime installation was used, stop/disable only the recorded `hermes-context-reduction.service`, restore or remove its recorded user-unit file, and run `systemctl --user daemon-reload`.
3. Restore the previous plugin commit/snapshot. Treat the versioned runtime venv as a separate artifact; remove or replace it only after confirming no deployment references it.
4. Re-run plugin load/status and, when runtime capability is retained, compress → retrieve smoke. See `docs/portable-core.md` for storage-backend and SQLite recovery caveats.
