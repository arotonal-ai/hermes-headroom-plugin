---
name: headroom-token-cost-evaluation
description: "Use when operating the installable Hermes Headroom plugin: install, verify, retrieve CCR content, classify exact/compress/blocked data, and publish evidence-backed savings without changing global routing."
author: Hermes Headroom contributors
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [headroom, context-reduction, hermes-plugin, retrieval, token-savings]
---

# Headroom plugin operations

## Overview

This skill is bundled with the `headroom_retrieve` Hermes plugin. Load it with the qualified plugin name:

```text
skill_view(name="headroom_retrieve:headroom-token-cost-evaluation")
```

It is for **portable plugin operation**: installing the Hermes plugin, checking whether the optional Headroom proxy works, retrieving exact CCR content, applying safe admission policy, and publishing savings only from retained evidence.

Do **not** treat this bundled skill as an owner-local deployment manual. It must not depend on private paths, local profile state, or unpublished wrappers.

## When to Use

Use this skill when you need to:

- install or verify `arotonal-ai/hermes-headroom-plugin` in a Hermes instance;
- decide whether a Headroom result is `INSTALL_PASS`, `RUNTIME_PARTIAL`, `RUNTIME_FULL`, or `FAIL`;
- use `headroom_retrieve` to resolve an exact CCR marker;
- validate the upstream `headroom-ai[proxy]` dependency without touching the real Python environment;
- check `/headroom status`, `/headroom smoke`, or `/headroom audit`;
- classify payloads as compressible, exact, or blocked;
- generate weekly savings tables from JSONL evidence.

The installable repo includes fail-open `tool_execution` middleware for eligible bulky intermediate tool/lane results, including `delegate_task`, plus packaged `headroom-worker-lane`, `headroom-background-lane`, and `headroom-command-preflight` wrappers for explicit operator commands. These wrappers retain exact sidecars/final packets and compress only eligible bulky intermediate traces; they do not change provider/model routing.

## Support Posture

| Platform | Posture | Operator note |
|---|---|---|
| Linux | tested path | Bash and Python helpers should work. |
| WSL2 | expected | Verify on target before calling it certified. |
| macOS | expected | Prefer Python helper scripts for checks; run CI/target evidence. |
| Windows native | possible via Hermes | Use native `hermes` commands and Python helpers; Bash helpers require Git Bash or WSL. |
| Termux | expected when Hermes/Python/git work | No systemd assumptions. |

Do not print or advertise a plugin/skill version from this skill. If a version, commit, or release matters, inspect live repo metadata (`git rev-parse`, `pyproject.toml`, GitHub release data) and report that evidence instead of hardcoding a displayed version here.

## Install and Reload

Run on the owner/target Hermes instance:

```bash
hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
hermes plugins list --enabled --user --plain
hermes gateway restart   # gateway/platform sessions
# or start /new in an active CLI/chat session
```

Verify in Hermes:

```text
/headroom status
```

If this command responds, plugin install succeeded. A missing proxy is `RUNTIME_PARTIAL`, not a failed install.

For real compression / `RUNTIME_FULL`, run the production runtime installer from a repo/plugin checkout:

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

The installer creates/updates `~/.cache/hermes-headroom-venv`, installs latest `headroom-ai[proxy]` by default, starts `headroom proxy --host 127.0.0.1 --port 28787` if no proxy is ready, verifies `/readyz`, and runs real compress → retrieve smoke. Manual install is acceptable only if those same checks pass.

Then verify in Hermes:

```text
/headroom smoke
```

## Acceptance States

| State | Meaning | Required evidence |
|---|---|---|
| `INSTALL_PASS` | Hermes installed and loaded the plugin | `headroom_retrieve` appears in `hermes plugins list --enabled --user --plain`; `/headroom status` responds after restart/new session. |
| `RUNTIME_PARTIAL` | Plugin loads, but no proxy is reachable | `/headroom status` reports unavailable or `/headroom smoke` fails at `readyz`; plugin does not crash. |
| `RUNTIME_FULL` | Plugin, dependency, and proxy work | `scripts/install-production-runtime.py` reports `RUNTIME_FULL`, or dependency smoke plus `/headroom smoke` returns PASS with sentinel retrieval. |
| `FAIL` | Plugin cannot be used | plugin not enabled, `/headroom` unavailable after reload, or install required copying another machine/profile state. |

Never call proxy-down `RUNTIME_PARTIAL` a failed install. It is a valid degraded state.

## Dependency and Proxy Split

The Hermes plugin and upstream Headroom runtime are separate layers:

| Layer | Installed by | Required for |
|---|---|---|
| Hermes plugin | `hermes plugins install arotonal-ai/hermes-headroom-plugin --enable` | `headroom_retrieve` tool, `/headroom` command, and fail-open `tool_execution` middleware for eligible bulky intermediate results. |
| Upstream Headroom package | `headroom-ai[proxy]` | local proxy/backend. |
| Runtime proxy | `headroom proxy --host 127.0.0.1 --port 28787` or configured endpoint | real compress → retrieve smoke. |

Use the production installer or cross-platform smoke helpers before claiming runtime capability:

```bash
python scripts/install-production-runtime.py
python scripts/test-headroom-dependency-install.py
python scripts/test-headroom-runtime-smoke.py
```

Unix-compatible wrapper:

```bash
scripts/test-headroom-dependency-install.sh
```

The dependency smoke creates a temporary virtual environment, installs `headroom-ai[proxy]` using the current default unless `--spec`/`HEADROOM_AI_SPEC` overrides it, verifies imports for `headroom`, `fastapi`, `uvicorn`, and `pydantic_core._pydantic_core`, then checks `headroom --help` and `headroom proxy --help`. It must not mutate Hermes config, `HERMES_HOME`, or the caller's system Python. Treat Python 3.13/3.14 as experimental monitor paths until `docs/compatibility.md` promotes them.

## Safe Admission Policy

Use Headroom only for eligible bulky intermediates:

```text
eligible = bulky + intermediate/diagnostic + retained exact source + retrievable/verifiable + non-sensitive + material savings
exact    = final/canonical/edit-critical/claim-ledger/manifest/hash/final packet
blocked  = secrets/config/memory/profile/system/developer instructions/protected content
```

Common classes:

| Class | Policy |
|---|---|
| `raw_log`, `worker_trace_raw`, `browser_debug_trace`, `ocr_raw_text`, `research_corpus_raw`, `qa_trace` | compressible candidate |
| `final_packet`, `final_pdf`, `canonical_html_css`, `manifest_hashes`, `claim_ledger`, `patch_diff` | exact |
| `memory_profile_instruction`, `secret_or_sensitive`, protected/private contamination | blocked |

Final answers, diffs, manifests, hashes, claim ledgers, rollback instructions, and edit-critical source context remain exact. If unsure, fail closed to exact output.

## Retrieval Workflow

When you see a CCR marker such as `<<ccr:abc123>>` or `<<ccr:abc123,base64,4.5KB>>`:

1. Extract the hash after `ccr:`.
2. Call `headroom_retrieve` with that hash.
3. If available, pass a focused query to retrieve only relevant parts.
4. Verify final claims against retrieved exact content or retained exact source.
5. Do not compress retrieval output again; marker loops are possible.

Success criterion: the exact source needed for the claim is visible and matches the claim. If retrieval fails, say so and use retained source or exact fallback.

## Metrics and Weekly Savings

Savings must be evidence-backed. Do not invent token savings from examples, screenshots, or expectations.

Generate weekly Monday rollups from JSONL evidence:

```bash
python scripts/generate-weekly-savings-table.py --input docs/metrics/data/*.jsonl --write docs/metrics/weekly-savings.md
```

Expected JSONL fields include:

```json
{"timestamp":"2026-06-29T12:00:00Z","lane":"debug","data_class":"raw_log","tokens_before":120000,"tokens_after":18000,"retrieval_verified":true,"fail_closed":false}
```

If no evidence exists, the metrics page should show placeholders and `pending real data`, not estimates.

## Repository Verification

From a checkout of the plugin repo:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m py_compile $(find src tests scripts -name '*.py' | sort)
bash -n scripts/*.sh
bash scripts/audit-repo-readiness.sh
python scripts/install-production-runtime.py --no-start
python scripts/test-headroom-dependency-install.py
python scripts/test-headroom-runtime-smoke.py
```

Clean temp-home plugin load test when Hermes CLI is available:

```bash
scripts/test-clean-hermes-install.sh --local
```

A passing clean-home test should prove:

- `headroom_retrieve` is enabled in a temporary `HERMES_HOME`;
- plugin discovery loads without copying real profile state;
- tool `headroom_retrieve` and command `/headroom` register;
- the bundled skill is registered as `headroom_retrieve:headroom-token-cost-evaluation`.

## Packaged vs Local Capability Boundary

Packaged now:

- `headroom_retrieve` tool;
- `/headroom status`, `/headroom smoke`, `/headroom audit`;
- fail-open `tool_execution` middleware for eligible bulky intermediate tool/lane results such as `delegate_task`, terminal/process, browser/debug, `web_extract`, and `session_search`;
- conservative policy helpers;
- dependency and clean-home verification scripts;
- evidence-backed weekly savings generator;
- this bundled plugin skill.

Packaged as active behavior:

- `headroom-worker-lane`, `headroom-background-lane`, and `headroom-command-preflight` for explicit operator commands;
- exact sidecar/final-packet retention plus optional compression of bulky intermediate traces;
- no provider/model routing mutation.

Not packaged as active behavior:

- owner-local natural wrappers such as `hr-nav`, `hr-debug`, `hr-research`, or `hr-fanin`;
- smart-route/provider-routing helpers;
- global/default provider route mutation;
- external telemetry.

If another environment has stronger local wrappers, treat them as local overlays, not portable repo guarantees.

## Security and Privacy Rules

- Do not request, print, or store API keys, tokens, cookies, private keys, memory files, profile state, or protected context.
- Non-loopback proxy URLs are blocked by default; allow only controlled/trusted endpoints with `HEADROOM_ALLOW_REMOTE_PROXY=1` or `context_reduction.allow_remote_proxy: true`.
- Do not copy another machine's Hermes home or profile directories.
- Do not enable external telemetry.
- Do not change global/default provider routing during first install.
- Prefer loopback/local proxy endpoints for smoke tests.
- State `RUNTIME_PARTIAL` honestly when the plugin works but the proxy is unavailable.

## Rollback

```bash
hermes plugins disable headroom_retrieve
hermes plugins remove headroom_retrieve
hermes gateway restart || true
```

If installed from a local checkout with a symlink or copy, remove the checkout-installed plugin directory according to the target Hermes home after confirming it is the intended path.

## Common Pitfalls

1. **Confusing install success with runtime success.** `/headroom status` responding is install evidence; `/headroom smoke` passing is runtime evidence.
2. **Using Bash-only helpers on native Windows.** Prefer Python helpers or run Bash under Git Bash/WSL.
3. **Publishing estimated savings.** Generate tables from JSONL evidence only.
4. **Compressing exact/final material.** The result middleware is for bulky intermediates only; final packets, diffs, hashes, manifests, claim ledgers, secrets, and edit-critical context stay exact or blocked.
5. **Advertising local overlays as packaged features.** Packaged worker/background/preflight wrappers are covered; natural `hr-*` aliases and provider-routing helpers remain local-only unless tests/release promote them.
6. **Hardcoding version or environment facts.** Inspect live metadata when needed; do not paint a static version in this skill.

## Verification Checklist

- [ ] Plugin appears in `hermes plugins list --enabled --user --plain`.
- [ ] Fresh session/restart completed before checking slash commands.
- [ ] `/headroom status` returns without crashing.
- [ ] Dependency smoke uses a temporary venv and passes before runtime claims.
- [ ] `/headroom smoke` passes before claiming `RUNTIME_FULL`.
- [ ] CCR retrieval is verified against exact content before final claims.
- [ ] Metrics are generated from retained JSONL evidence, or placeholders remain.
- [ ] No global/default routing, telemetry, secrets, or profile-state copying occurred.
