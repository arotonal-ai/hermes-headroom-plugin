# Agent instructions for Hermes Headroom Plugin

Use this file when an AI/Hermes agent is given this repository URL and asked to **install**, **analyze**, or **verify** it.

## Install goal

Install and enable the plugin in the target Hermes instance without copying owner-local state, exposing secrets, or changing global/default provider routing.

## Platform posture

- Linux, macOS, and native Windows are covered by CI/runtime smoke paths in this repo; WSL/Termux still require target evidence.
- Native Windows should use native Hermes commands plus Python helper scripts. Bash helpers require Git Bash/WSL and resolve `PYTHON_BIN`, Hermes' own Python, `python3`, `python`, then `py -3` to avoid broken Microsoft Store aliases and global Python/venv drift.
- Windows `RUNTIME_FULL` is certified in GitHub Runtime Smoke for Python 3.11/3.12, but target-host evidence still matters when diagnosing local shell/Python drift.
- Python 3.13/3.14 are monitored by the non-blocking Future Runtime Monitor; do not claim them as certified until promoted in `docs/compatibility.md`.
- The packaged plugin includes fail-open `tool_execution` middleware for eligible bulky intermediate tool/lane results, including `delegate_task`, when the loopback Headroom proxy is healthy.
- The packaged worker/background/preflight CLI wrapper entry points are still pending migration; do not advertise owner-local wrapper scripts as packaged behavior until repo tests cover them.
- The package does not change global/default provider routing; exact/edit-critical/sensitive outputs remain exact or blocked.

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
```

For `RUNTIME_FULL`, prefer the production runtime installer from a repo/plugin checkout:

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

The installer creates/updates `~/.cache/hermes-headroom-venv`, installs latest `headroom-ai[proxy]` by default, starts `headroom proxy --host 127.0.0.1 --port 28787` if needed, verifies `/readyz`, and runs real compress → retrieve smoke. Manual fallback is allowed only if it performs those same checks.

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
| `INSTALL_PASS` | Plugin installed and Hermes can load it | `headroom_retrieve` appears in `hermes plugins list --enabled --user --plain`; `/headroom status` responds after restart/new session |
| `RUNTIME_PARTIAL` | Plugin works, but Headroom proxy is unavailable | `/headroom status` reports proxy unavailable or `/headroom smoke` fails at `readyz` |
| `RUNTIME_FULL` | Plugin, upstream `headroom-ai[proxy]`, and proxy all work | dependency smoke PASS and `/headroom smoke` or runtime smoke returns PASS with compress → retrieve sentinel; Python 3.11/3.12 are certified in Runtime Smoke |
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
bash scripts/audit-repo-readiness.sh
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

- upstream Headroom: https://github.com/chopratejas/headroom
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
