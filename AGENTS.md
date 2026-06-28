# Agent instructions for Hermes Headroom Plugin

Use this file when an AI/Hermes agent is given this repository URL and asked to **install**, **analyze**, or **verify** it.

## Install goal

Install and enable the plugin in the target Hermes instance without copying owner-local state, exposing secrets, or changing global/default provider routing.

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

If a Headroom proxy is running, also verify:

```text
/headroom smoke
```

## Acceptance states

| State | Meaning | Required evidence |
|---|---|---|
| `INSTALL_PASS` | Plugin installed and Hermes can load it | `headroom_retrieve` appears in `hermes plugins list --enabled --user --plain`; `/headroom status` responds after restart/new session |
| `RUNTIME_PARTIAL` | Plugin works, but Headroom proxy is unavailable | `/headroom status` reports proxy unavailable or `/headroom smoke` fails at `readyz` |
| `RUNTIME_FULL` | Plugin and proxy both work | `/headroom smoke` returns PASS with compress → retrieve sentinel |
| `FAIL` | Plugin not installed/loaded | plugin not enabled, `/headroom` unavailable after restart/new session, or install required copying owner-local `~/.hermes` state |

## Do not do these things

- Do **not** copy another machine's `~/.hermes` directory. Do not copy another machine or owner-local Hermes state.
- Do **not** request, print, or store API keys/tokens/passwords.
- Do **not** change global/default provider routing.
- Do **not** enable external telemetry.
- Do **not** compress final packets, patches/diffs, manifests, hashes, claim ledgers, secrets, memory/profile/system/developer instructions, or protected content.

## Read-only analysis checklist

If asked to analyze rather than install:

```bash
git ls-remote https://github.com/arotonal-ai/hermes-headroom-plugin.git HEAD
bash scripts/audit-repo-readiness.sh
```

Report:

- whether the repo is public and reachable;
- whether README/INSTALL/AGENTS instructions are complete;
- whether plugin metadata declares `headroom_retrieve`;
- whether Python/shell syntax checks pass;
- whether install requires secrets — it should not.

## Safe temp-home install test

If allowed to test installation without touching the real profile:

```bash
bash scripts/test-clean-hermes-install.sh --local
```

This uses a temporary `HERMES_HOME` and removes it at the end.


## Upstream relationship

When reporting or documenting this plugin, preserve the distinction:

- upstream Headroom: https://github.com/chopratejas/headroom
- upstream docs: https://headroomlabs-ai.github.io/headroom/
- this repo: Hermes Agent plugin/integration wrapper

Do not present this repository as the original Headroom project. Link upstream for backend/proxy behavior and Headroom project evolution.

## Rollback

```bash
hermes plugins disable headroom_retrieve
hermes plugins remove headroom_retrieve
hermes gateway restart || true
```
