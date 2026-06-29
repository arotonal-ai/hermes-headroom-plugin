# Agent install brief

Use this when another Hermes/AI agent is given only this repository URL and asked to install it in a Hermes instance.

## Goal

Install and enable the Hermes Headroom plugin without exposing secrets, copying owner-local state, or changing global/default provider routing. With a healthy local proxy, the plugin may compress eligible bulky intermediate `tool_execution` results; exact/edit-critical/sensitive content remains exact or blocked.

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

Production runtime for `RUNTIME_FULL`:

```bash
python scripts/install-production-runtime.py
# Unix/Git Bash wrapper:
scripts/install-production-runtime.sh
```

Windows PowerShell:

```powershell
python scripts\install-production-runtime.py
# or:
py -3 scripts\install-production-runtime.py
```

The installer creates/updates `~/.cache/hermes-headroom-venv`, installs latest `headroom-ai[proxy]` by default, starts `headroom proxy --host 127.0.0.1 --port 28787` if needed, verifies `/readyz`, and runs real compress → retrieve smoke. Manual install is acceptable only if those same checks pass.

## Verify

In Hermes:

```text
/headroom status
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

If a proxy is running:

```text
/headroom smoke
```

## Acceptance

PASS if:

- `hermes plugins list --enabled --user --plain` includes `headroom_retrieve`;
- `/headroom status` responds after restart/new session;
- no secrets are requested or printed;
- global/default provider routing is unchanged;
- if proxy/runtime is enabled, eligible bulky intermediate tool/lane result compression is available via `tool_execution` middleware.

PARTIAL if:

- install succeeds but `/headroom smoke` fails because no Headroom proxy is running.

FULL if:

- `scripts/install-production-runtime.py` returns `RUNTIME_FULL`, or dependency smoke plus `/headroom smoke` returns PASS with sentinel retrieval;
- install succeeds and `/headroom smoke` returns PASS with sentinel retrieval;
- optional result-compression checks preserve exact/blocked tools such as `read_file`, `patch`, and `git diff`.

Windows native `FULL` is certified by this repo's Runtime Smoke workflow for Python 3.11/3.12, but still require target-host evidence when diagnosing a specific machine. Python 3.13/3.14 are experimental monitor paths, not certified support.

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

```bash
hermes plugins disable headroom_retrieve
hermes plugins remove headroom_retrieve
hermes gateway restart || true
```
