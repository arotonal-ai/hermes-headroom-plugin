# Context Economy Loop

This document describes the portable context-economy capability provided by the Hermes Headroom plugin. It is intentionally instance-neutral: do not add operator names, private paths, chat transcripts, local profile state, customer/project facts, or one-off event details here.

## Purpose

Headroom compression is one mechanism inside a broader context-economy loop. A healthy instance should optimize for useful work per context window, not for compression counts alone. In this plugin, compression is not embedded in the Hermes wrapper: the plugin calls a configured Headroom proxy/runtime for compress and retrieve operations.

The loop has four goals:

1. avoid unnecessary context before it enters the model;
2. compress only eligible bulky intermediate material;
3. keep exact source authority retrievable for claims and final work;
4. feed compact evidence back into operator and agent behavior.

## Surface scope

This loop is surface-agnostic. A single chat renderer can be a first visible canary, but Context Economy must also apply to CLI/TUI/Desktop, API/dashboard, scheduled/background, worker/delegation, browser/debug, research, document, and coding lanes. Implementations should tag events with `platform`, `surface`, and `lane`; evaluate savings and pressure per surface without changing the global data-class admission policy.

Do not infer cross-surface readiness from a single-surface canary. A surface-specific PASS proves visible UX only for that surface; broader promotion still needs evidence from the relevant surface or a portable adapter contract.

## Portable loop

```text
observe -> classify -> act -> verify -> learn
```

| Step | Portable capability | Required evidence |
|---|---|---|
| Observe | Collect local event metadata and context-pressure aggregates. | Event log, proxy stats, local message/tool-output store, or equivalent telemetry-free sidecars. |
| Classify | Separate exact/final/source-truth material from compressible bulky intermediates. | Admission decision, data class, reason, sensitivity/protection hits. |
| Act | Apply the smallest safe intervention: avoid read, bound read, shape sidecar, compress intermediate, or keep exact. | Chosen action, fallback, exact source pointer. |
| Verify | Prove runtime health and claim accuracy. | Compress -> retrieve smoke, exact sidecar hash/path, focused retrieval or source readback. |
| Learn | Update local guidance and thresholds from evidence, not anecdotes. | Compact report with top offenders, saved tokens/chars, failures, and next intervention. |

## Runtime boundary

The portable product has two layers:

| Layer | Portable responsibility | Requires Headroom runtime? |
|---|---|---:|
| Hermes plugin | register tool/command/skill/middleware, classify data, preserve exact/blocked outputs, expose status/audit/readiness | No |
| Headroom runtime/proxy | execute `/v1/compress`, store/retrieve CCR payloads, return stats, pass compress -> retrieve smoke | Yes |

`RUNTIME_PARTIAL` means the first layer works and the second layer is unavailable. It is acceptable for install verification, but it is not enough to claim active context reduction. `RUNTIME_FULL` or `RUNTIME_FULL_DURABLE` is required before expecting automatic eligible-intermediate compression.

## Admission policy

Portable context economy depends on data class, not tool names.

```text
avoid      = context not needed for the current task
exact      = final/canonical/edit-critical/source-truth/claim-ledger/manifest/hash
compress   = bulky + intermediate/diagnostic + retained source + retrievable + non-sensitive
blocked    = secrets, credentials, protected context, memory/profile/system/developer instructions
```

If classification is uncertain, keep content exact or blocked. Do not compress to make a result look efficient.

## Intake discipline

The highest-leverage optimization is usually avoiding broad exact input before compression is considered.

Portable default:

```text
search or index first
read bounded slices second
retrieve focused exact evidence for claims
compress only eligible bulky intermediates
emit compact final receipts
```

Suggested broad-read lint:

| Signal | Default classification | Recommended action |
|---|---|---|
| Missing read limit | `broad_read` | Require offset/limit or a precise reason. |
| Read limit > 120 lines | `broad_read` | Use search/index first; split into focused windows. |
| Tool output >= 20k chars | `large_exact_output` | Store pointer/sidecar; summarize handles, not content. |
| Repeated same-file broad reads | `loop_pressure` | Promote a compact status/index artifact or focused query path. |

These thresholds are defaults, not universal truth. Each instance may tune them after measuring quality and false positives.

## Required local signals

A portable implementation needs only local, telemetry-free evidence:

- proxy health: ready/unavailable plus compress -> retrieve smoke result;
- event metadata: action, reason, tool/lane, platform/source, chars/tokens, saved amount, marker/source pointer when present;
- surface attribution: platform/surface/lane enough to separate chat, CLI/TUI/Desktop, API/dashboard, scheduled/background, and worker/delegation pressure;
- local context-pressure aggregate: top tool/lane outputs by chars/tokens over a window;
- exact authority pointer: sidecar path, message id, artifact id, hash, or other local handle;
- compact next intervention: one recommended action with supporting evidence.

Do not require external telemetry, API keys, remote dashboards, or private profile exports.

## Generic report shape

A portable context-economy report should expose compact JSON and Markdown with this shape:

```yaml
status: PASS | PASS_WITH_CONTEXT_ECONOMY_WARN | PASS_WITH_CONTEXT_ECONOMY_FAIL | PARTIAL
runtime:
  proxy_ready: true|false
  smoke_passed: true|false
context_economy:
  status: PASS|WARN|FAIL
  reason: <short reason>
  source_truth_exact_chars: <number>
  leader:
    tool_or_lane: <name>
    events: <number>
    chars: <number>
top_offenders:
  - pointer: <local handle, not raw content>
    path_or_source: <local source if safe>
    offset: <optional>
    limit: <optional>
    classification: broad_read|bounded_read|large_exact_output|compressible_intermediate
next_intervention:
  action: avoid_context|tighten_context_intake|shape_sidecar|compress_intermediate|keep_exact|block
  rule: <operator rule>
```

The report should keep raw content out of chat and out of portable documentation. Raw evidence stays in local sidecars or the local message/artifact store.

## Effective loop criteria

A context-economy tool is effective only when all of these hold:

1. **Safety:** secrets, protected context, final packets, diffs, manifests, hashes, and edit-critical source remain exact or blocked.
2. **Recoverability:** compressed content has an exact retained source and retrieval path.
3. **Operator value:** the report changes the next action, not just a graph.
4. **Behavior change:** later runs show lower broad exact intake or higher useful work per context window.
5. **Portability:** instance-specific storage is behind adapters; the product contract is event schema + local handles + gates.
6. **Low noise:** status surfaces are stable and compact; experimental analyses stay in reports/tests/harnesses, not user-facing command sprawl.

## What is portable vs local

| Layer | Portable | Local/overlay |
|---|---|---|
| Admission classes | Yes | Threshold tuning by instance. |
| Compress -> retrieve smoke | Yes | Proxy install/supervisor details. |
| Event schema | Yes | Storage path and rotation policy. |
| Context-pressure report shape | Yes | How to query a specific session DB or artifact store. |
| Broad-read classification | Yes | Exact line/char thresholds. |
| Operator-facing slash commands | Keep small/stable | Experimental analyses should stay local until promoted by a command-surface gate. |
| Knowledge/memory updates | Principle only | Each instance chooses its own vault/graph/RAG system. |

## Loop documentation rule

Portable docs should describe capabilities and contracts, not the one-off source event that produced them.

Allowed:

- generic loop stages;
- event fields;
- safe defaults;
- acceptance criteria;
- example schemas with fake placeholders;
- failure modes and rollback principles.

Not allowed:

- private paths;
- owner/operator names;
- chat excerpts;
- project-specific counts as universal benchmarks;
- local profile names;
- secrets or protected context;
- one-off conclusions framed as product guarantees.

## Portable gate

From a plugin checkout, run:

```bash
python scripts/context-economy-loop-gate.py
```

The gate scans the portable docs, runs loopback runtime smoke, analyzes a synthetic local context-pressure store, and runs temporary Hermes-home install when the Hermes CLI is available. Generic CI runners without Hermes CLI record that subcheck as skipped while package portability remains covered by the release-candidate wheel/entrypoint gate. It must end with `CONTEXT_ECONOMY_LOOP_GATE_PASS` before claiming the loop contract is portable. Use `--skip-runtime-smoke` only for docs-only CI jobs that explicitly test runtime elsewhere.

## Adoption checklist for another instance

- [ ] Install plugin with native Hermes plugin commands.
- [ ] Verify `/headroom status` or equivalent plugin health.
- [ ] Verify compress -> retrieve smoke before claiming runtime capability.
- [ ] Confirm exact/blocked classes are preserved.
- [ ] Enable local event metadata capture with no external telemetry.
- [ ] Run a context-pressure report over recent activity.
- [ ] Identify the first broad-intake or bulky-intermediate leader.
- [ ] Apply one intervention.
- [ ] Re-run report and compare the same metric.
- [ ] Promote only stable, repeated operator value into user-facing commands.

## Non-goals

- replacing the model provider router;
- compressing everything;
- hiding source truth behind summaries;
- exporting private telemetry;
- turning every analysis into a slash command;
- claiming savings without retained evidence.
