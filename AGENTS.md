# Agent instructions for Hermes Headroom Plugin

Use this file when an AI/Hermes agent is given this repository URL and asked to **install**, **analyze**, or **verify** it.

## Install goal

Install and enable the plugin in the target Hermes instance without copying owner-local state, exposing secrets, or changing global/default provider routing.

## Platform posture

- Linux is the primary tested target.
- macOS/WSL are expected when Hermes, git, and Python are available, but need target evidence before being called certified.
- Native Windows should use native Hermes commands plus Python helper scripts. Bash helpers require Git Bash/WSL and resolve `PYTHON_BIN`, Hermes' own Python, `python3`, `python`, then `py -3` to avoid broken Microsoft Store aliases and global Python/venv drift.
- The packaged worker/background/preflight wrapper entry points are intentionally pending P1 migration; do not advertise owner-local wrapper behavior as packaged behavior.
- Current P0 does not automatically compress live Hermes traffic; it provides retrieval/status/smoke/audit/policy scaffolding only.

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

For full compression runtime, install upstream Headroom in an isolated venv and start the local proxy.

Unix/macOS/WSL:

```bash
python3 -m venv ~/.cache/hermes-headroom-venv
~/.cache/hermes-headroom-venv/bin/python -m pip install --upgrade pip
~/.cache/hermes-headroom-venv/bin/python -m pip install 'headroom-ai[proxy]>=0.26,<0.28'
~/.cache/hermes-headroom-venv/bin/headroom proxy --host 127.0.0.1 --port 28787
```

Windows PowerShell:

```powershell
py -m venv $env:USERPROFILE\.cache\hermes-headroom-venv
& $env:USERPROFILE\.cache\hermes-headroom-venv\Scripts\python.exe -m pip install --upgrade pip
& $env:USERPROFILE\.cache\hermes-headroom-venv\Scripts\python.exe -m pip install 'headroom-ai[proxy]>=0.26,<0.28'
& $env:USERPROFILE\.cache\hermes-headroom-venv\Scripts\headroom.exe proxy --host 127.0.0.1 --port 28787
```

If a Headroom proxy is running, also verify:

```text
/headroom smoke
```

For dependency evidence without starting the proxy, use the repo helper:

```bash
python scripts/test-headroom-dependency-install.py
# Unix wrapper:
scripts/test-headroom-dependency-install.sh
```

## Acceptance states

| State | Meaning | Required evidence |
|---|---|---|
| `INSTALL_PASS` | Plugin installed and Hermes can load it | `headroom_retrieve` appears in `hermes plugins list --enabled --user --plain`; `/headroom status` responds after restart/new session |
| `RUNTIME_PARTIAL` | Plugin works, but Headroom proxy is unavailable | `/headroom status` reports proxy unavailable or `/headroom smoke` fails at `readyz` |
| `RUNTIME_FULL` | Plugin, upstream `headroom-ai[proxy]>=0.26,<0.28`, and proxy all work | dependency smoke PASS and `/headroom smoke` returns PASS with compress → retrieve sentinel; prefer Python 3.11/3.12 on Windows until newer runtimes pass target smoke |
| `FAIL` | Plugin not installed/loaded | plugin not enabled, `/headroom` unavailable after restart/new session, or install required copying owner-local `~/.hermes` state |

## Do not do these things

- Do **not** point `HEADROOM_PROXY_URL` at a non-loopback host unless the endpoint is controlled and trusted; future compression wrappers may send compressible intermediate content to that endpoint.
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
