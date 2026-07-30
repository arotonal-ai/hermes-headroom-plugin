# v0.6.0 capability decisions (historical)

Status: published historical baseline. Current release contracts live in the changelog, compatibility guide, runtime-manager guide, and release-candidate gate.

## Stable core

The stable default remains the v0.5.2 portable core: `tool_execution` middleware compresses only eligible intermediate results, requires a durable CCR marker before replacement, preserves exact material on ambiguity/failure, and does not reroute model traffic.

P1–P3 add optional context-lifecycle capability without changing that default:

- `headroom-composite` is registered as a selectable Hermes ContextEngine but is not selected by plugin installation.
- Hermes's native threshold, protected head/tail policy, model updates, session reset and fallback compressor remain authoritative.
- Age-aware pruning is bounded to verified cold derived evidence; system/user messages, recent turns, hot tool results and exact-state artifacts remain protected.
- Replacement is allowed only when the reducer returns a durable CCR marker and the retained source contract remains recoverable.

Enable the lifecycle lane only in an isolated profile/session:

```yaml
context:
  engine: headroom-composite
context_reduction:
  min_tool_result_chars: 8000
  lifecycle:
    enabled: true
    materiality_chars: 24000
    hot_tool_results: 4
    warm_tool_results: 8
    aggregate_budget_chars: 16000
```

Rollback is one configuration change: restore the previous `context.engine` value (normally `session`) or set `context_reduction.lifecycle.enabled: false`, then start a fresh session. The plugin/runtime and tool-result middleware do not need to be removed.

## P4 / request shaping: not promoted

Provider-native schema deferral is not a stable v0.6 feature. Hermes native Tool Search owns disclosure, and the current Codex preflight does not accept the alternate `tool_search` wire contract. The implementation remains only as an explicit compatibility-test fixture:

```yaml
context_reduction:
  request_shaping:
    enabled: true
    compatibility_test_mode: true
    disclosure_owner: provider
```

All three values are required. `enabled: true` without `compatibility_test_mode: true` is inert and no longer shadows the independent `llm_request` safety net. Do not use the fixture in a production profile or report it as token savings.

Promotion criteria: provider/Hermes contract proof, a real isolated request canary, no duplication with native Tool Search, exact rollback and measured positive value after safety/fidelity costs.

## `llm_request`: supported opt-in safety net

The common `llm_request` adapter remains off by default and supported only as a copy-on-write, fail-open safety net for eligible legacy/bypassed tool-result text. It supports the four Hermes protocol shapes (`chat_completions`, `codex_responses`, `anthropic_messages`, `bedrock_converse`), skips already-compressed markers, uses logical-source dedupe and never intentionally rewrites user/system prompts, tool arguments/schemas, signed blocks, guardrails, headers or streaming controls.

```yaml
context_reduction:
  llm_request_middleware:
    enabled: true
    mode: tool_results
```

Decision: keep it opt-in rather than promote it to the default hot path. `tool_execution` remains the primary interception point; `llm_request` exists for measured bypasses and alternate integration paths. Remove the block above to roll back.

## Runtime compatibility

- Managed/default runtime: `headroom-ai[proxy]==0.33.0` with `litellm==1.94.0rc3`.
- Compatibility/rollback lane: Headroom `0.31.0`; it is not installed by the runtime manager by default.
- Python 3.13 remains experimental. Native Windows Python 3.14 is a blocking Issue #24 candidate lane and remains uncertified until remote CI plus target-host durable lifecycle evidence pass.

A local Linux compress → retrieve smoke for both 0.33.0 and 0.31.0 is required by the release gate. Cross-OS/Python certification remains a separate remote-CI release gate; local evidence must not be relabeled as multi-platform certification.

## Release boundary

A local RC pass proves build, tests, clean package import/install, isolated Hermes load, package upgrade/rollback and local runtime smokes. It does not authorize push, tag, publication, durable supervisor mutation or public-release claims. Those are separate gates.
