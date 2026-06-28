# Agent instructions for Hermes Headroom Plugin

Use this file when an AI/Hermes agent is given this repository URL and asked to **install**, **analyze**, or **verify** it.

## Install goal

Install and enable the plugin in the target Hermes instance without copying owner-local state, exposing secrets, or changing global/default provider routing.

## Platform posture

- Linux is the primary tested target.
- macOS/WSL are expected when Hermes, git, and Python are available, but need target evidence before being called certified.
- Native Windows should use native Hermes commands plus Python helper scripts. Bash helpers require Git Bash/WSL.
- The packaged worker/background/preflight wrapper entry points are intentionally pending P1 migration; do not advertise owner-local wrapper behavior as packaged behavior.

## Primary command

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

If full runtime verification is requested, verify the upstream Headroom dependency first:

```bash
python scripts/test-headroom-dependency-install.py
# or Unix wrapper:
bash scripts/test-headroom-dependency-install.sh
# or, after native Hermes install:
"${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve/scripts/test-headroom-dependency-install.sh"
```

If a Headroom proxy is running, also verify:

```text
/headroom smoke
```

## Acceptance states

| State | Meaning | Required evidence |
|---|---|---|
| `INSTALL_PASS` | Plugin installed and Hermes can load it | `headroom_retrieve` appears in `hermes plugins list --enabled --user --plain`; `/headroom status` responds after restart/new session |
| `RUNTIME_PARTIAL` | Plugin works, but Headroom proxy is unavailable | `/headroom status` reports proxy unavailable or `/headroom smoke` fails at `readyz` |
| `RUNTIME_FULL` | Plugin, upstream `headroom-ai[proxy]>=0.26,<0.27`, and proxy all work | dependency smoke PASS and `/headroom smoke` returns PASS with compress → retrieve sentinel |
| `FAIL` | Plugin not installed/loaded | plugin not enabled, `/headroom` unavailable after restart/new session, or install required copying owner-local `~/.hermes` state |

## Do not do these things

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
