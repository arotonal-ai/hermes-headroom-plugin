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

It is for **portable plugin operation**: installing the Hermes plugin, checking whether the separate Headroom runtime/proxy works, retrieving exact CCR content, applying safe admission policy, and publishing savings only from retained evidence. The plugin does not compress by itself; real compression/retrieval requires the configured Headroom proxy.

Do **not** treat this bundled skill as an owner-local deployment manual. It must not depend on private paths, local profile state, or unpublished wrappers.

## When to Use

Use this skill when you need to:

- install or verify `arotonal-ai/hermes-headroom-plugin` in a Hermes instance;
- decide whether a Headroom result is `INSTALL_PASS`, `RUNTIME_PARTIAL`, `RUNTIME_FULL`, `RUNTIME_FULL_DURABLE`, or `FAIL`;
- use `headroom_retrieve` to resolve an exact CCR marker;
- validate the upstream `headroom-ai[proxy]` dependency without touching the real Python environment;
- check `/headroom status`, `/headroom setup`, `/headroom smoke`, `/headroom cache`, or `/headroom audit`;
- classify payloads as compressible, exact, or blocked;
- operate the portable Context Economy Loop contract (`docs/context-economy-loop.md`) without copying private instance state;
- generate weekly savings tables from JSONL evidence.

The portable core is the fail-open `tool_execution` middleware plus `headroom_retrieve`, status/smoke and the loopback Headroom runtime. An optional `llm_request` safety net can be enabled with `context_reduction.llm_request_middleware: {enabled: true, mode: tool_results}` (or `HEADROOM_LLM_REQUEST_COMPRESSION=1`) after protocol conformance tests; it compresses eligible tool-result text at Hermes's native post-build/pre-transport boundary without changing provider/model routing. It is copy-on-write, fail-open, and supports `chat_completions`, `codex_responses`, `anthropic_messages`, and `bedrock_converse`. `tool_execution` is the primary savings-attribution surface: a result already carrying its Headroom marker is not recompressed or recredited by `llm_request`. Repeated API requests reuse a bounded transformed-fragment cache keyed by a logical source fingerprint (session, tool call, protocol family, tool name, and source-content digest); cache reuse, retained marker correlation, and duplicate logical sources are never counted as new savings. The compact final-answer marker (`[HR✓]` / `[HR!]`), first-turn hint, bundled `llm-monitor` companion, wrappers and extended reporting are optional extras and default off/not installed where applicable. The marker reports runtime readiness only, never per-message compression. Wrappers retain exact sidecars/final packets and do not change provider/model routing. For heavy iterative improvement loops, on-demand mode (`HEADROOM_AUTO_COMPRESSION=0` or `context_reduction.auto_compression: false`) keeps status/smoke/cache/retrieve available while preventing automatic middleware compression overhead.

## Portable Context Economy Loop

Use Headroom as one mechanism inside a bounded local decision protocol. The loop is not an autonomous meta-agent, watcher, subjective oracle, or self-tuning controller:

```text
observe -> classify -> act -> verify -> learn
```

Portable contract:

- observe local event metadata and context-pressure aggregates without external telemetry;
- classify by data class: avoid, exact, compress, or blocked;
- act with the smallest safe intervention: avoid context, bound reads, shape sidecars, compress intermediates, keep exact, or block;
- verify with compress -> retrieve smoke and exact source readback for claims;
- learn from compact reports and promote only repeated operator value into stable commands;
- prove controls: `HEADROOM_AUTO_COMPRESSION=0` or `context_reduction.auto_compression: false` disables middleware auto-compression only, while the runtime and retrieval remain available.

Do not copy another instance's private paths, profile state, chat history, or case-specific thresholds. Full repo documentation: `docs/context-economy-loop.md`; portable gate: `python scripts/context-economy-loop-gate.py`.

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
/headroom setup   # read-only setup guidance; does not install/start runtime
```

If this command responds, plugin install succeeded. A missing proxy is `RUNTIME_PARTIAL`, not a failed install, but it is not active context reduction: `/headroom smoke`, `headroom_retrieve`, middleware compression, and wrapper compression require a reachable proxy.

For explicit durable runtime setup, use the portable manager from a repo/plugin checkout:

```bash
python scripts/headroom-runtime.py setup
```

Wheel environment:

```bash
headroom-runtime setup
```

Windows native Git checkout:

```powershell
py -3 scripts\headroom-runtime.py setup
```

Run `setup --dry-run --json` first when a no-write plan is required. The manager installs official `headroom-ai[proxy]==0.32.1` plus `litellm==1.91.3` in an isolated venv and reuses upstream manifests/native supervisors with `provider_mode=manual`, `targets=[]`, and `mutations=[]`. It deliberately skips direct 0.32.1 apply because that path writes persistent shell blocks, verifies `/readyz`, and runs real compress → retrieve smoke. Use `status`, `doctor`, and `uninstall` on the same launcher/entry point. Claim `RUNTIME_FULL_DURABLE` only when `doctor --json` returns exit 0. See `docs/runtime-manager.md` and `docs/portable-core.md`.

Clean-instance evidence must bind the proxy to the target run: verify the default `127.0.0.1:8787` is free before a canonical default-port canary, or allocate a distinct free loopback port and pass the same `HEADROOM_PROXY_URL` for concurrent same-host runs. Never count another user's ready proxy as a clean runtime PASS.

Then verify in Hermes:

```text
/headroom smoke
```

## Acceptance States

| State | Meaning | Required evidence |
|---|---|---|
| `INSTALL_PASS` | Hermes installed and loaded the plugin | `headroom_retrieve` appears in `hermes plugins list --enabled --user --plain`; `/headroom status` and `/headroom setup` respond after restart/new session. |
| `RUNTIME_PARTIAL` | Plugin loads, but no proxy is reachable | `/headroom status` reports unavailable or `/headroom smoke` fails at `readyz`; status/audit work, but compression/retrieval/middleware compression are not active. |
| `RUNTIME_FULL` | Plugin, dependency, and proxy work in the current process/session | dependency smoke plus `/headroom smoke` returns PASS with sentinel retrieval. |
| `RUNTIME_FULL_DURABLE` | Native user lifecycle is installed and healthy | `headroom-runtime doctor --json` returns exit 0 with upstream status, readiness, and sentinel recovery PASS. |
| `FAIL` | Plugin cannot be used | plugin not enabled, `/headroom` unavailable after reload, or install required copying another machine/profile state. |

Never call proxy-down `RUNTIME_PARTIAL` a failed install. It is a valid degraded state. Also do not call the runtime CCR store a plugin cache: `/headroom cache` is read-only visibility into the runtime-owned store/TTL, and expired/cleared runtime entries can make older CCR markers unretrievable.

## Dependency and Proxy Split

The Hermes plugin and upstream Headroom runtime are separate layers. The plugin is an integration/control plane; the runtime is the compression/retrieval engine:

| Layer | Installed by | Required for |
|---|---|---|
| Hermes plugin | `hermes plugins install arotonal-ai/hermes-headroom-plugin --enable` | registers `headroom_retrieve`, `/headroom`, bundled skill, visible readiness marker, and fail-open middleware; does not perform compression locally. |
| Upstream Headroom package | `headroom-ai[proxy]` | provides the compressor/retriever service used by the plugin. |
| Runtime proxy | `headroom-runtime setup` or the native Git launcher | handles `/readyz`, `/v1/compress`, `/v1/retrieve`, runtime-owned CCR cache/store stats, native lifecycle, and real compress → retrieve smoke. |

Use the production installer or cross-platform smoke helpers before claiming runtime capability:

```bash
python scripts/headroom-runtime.py doctor --json
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
3. Treat the returned payload as the complete exact retained content for that hash.
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
- `/headroom status`, `/headroom setup`, `/headroom smoke`, `/headroom audit`;
- visible `[HR✓]` / `[HR!]` readiness marker for final answers;
- fail-open `tool_execution` middleware for eligible bulky intermediate tool/lane results such as `delegate_task`, terminal/process, browser/debug, `web_extract`, and `session_search`;
- conservative policy helpers;
- dependency and clean-home verification scripts;
- evidence-backed weekly savings generator;
- this bundled plugin skill.

Packaged as active behavior:

- `headroom-worker-lane`, `headroom-background-lane`, and `headroom-command-preflight` for explicit operator commands;
- exact sidecar/final-packet retention plus optional compression of bulky intermediate traces;
- bounded compression input for oversized traces while retaining exact full raw sidecars as source of truth;
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
6. **Treating bounded compressed traces as exact evidence.** Bounded wrapper compression is triage; verify material claims against the exact raw sidecar or exact final packet.
7. **Hardcoding version or environment facts.** Inspect live metadata when needed; do not paint a static version in this skill.

## Verification Checklist

- [ ] Plugin appears in `hermes plugins list --enabled --user --plain`.
- [ ] Fresh session/restart completed before checking slash commands.
- [ ] `/headroom status` and `/headroom setup` return without crashing.
- [ ] Dependency smoke uses a temporary venv and passes before runtime claims.
- [ ] `/headroom smoke` passes before claiming `RUNTIME_FULL`.
- [ ] CCR retrieval is verified against exact content before final claims.
- [ ] Metrics are generated from retained JSONL evidence, or placeholders remain.
- [ ] No global/default routing, telemetry, secrets, or profile-state copying occurred.
