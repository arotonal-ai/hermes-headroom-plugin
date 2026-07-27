# Changelog

## Unreleased

- Derive `RUNTIME_FULL_DURABLE` from parsed Headroom lifecycle semantics and matching native supervisor evidence; exit code `0` plus a ready HTTP proxy no longer accepts `Status: stopped` or incomplete/ambiguous status output.
- Isolate canonical unit verification from live operator `HOME`, `HERMES_HOME`, `USERPROFILE`, inherited `HEADROOM_*` settings, and global supervisor discovery; retain explicit foreign-supervisor tests.
- Skip the Windows symlink-escape fixture only when `WinError 1314` proves Developer Mode/elevation is unavailable, while preserving the security assertion everywhere symlinks can be created.
- Replace the repository-mutating `uv run --with pytest` path with a full-suite `uv --isolated --no-project` runner; direct pytest runs receive equivalent host-environment isolation.

## v0.6.0 — 2026-07-23

- Promote the public `v0.6.0rc1` behavior unchanged after clean public-install/runtime canary evidence and complete local plus multi-OS release gates.
- Keep P1–P3 optional, P4 compatibility-test-only, `llm_request` default-off, provider routing unchanged, and the managed Headroom 0.32.1/LiteLLM 1.91.3 pair pinned.

## v0.6.0rc1 — 2026-07-23

- Integrate the verified P1 durable-CCR/fail-open reducer and P2/P3 age-aware lifecycle/composite ContextEngine onto the published v0.5.2 runtime-manager and hardening line.
- Preserve Hermes's native compression threshold/protected-region policy across registration, model updates and deepcopy while keeping the engine inert until explicitly selected.
- Keep provider-native P4 schema shaping outside the stable core as an explicit compatibility fixture; Hermes native Tool Search remains the disclosure authority.
- Keep `llm_request` as a copy-on-write, fail-open, default-off safety net for legacy/bypassed tool results; an inert shaping flag no longer shadows that independent lane.
- Preserve the managed Headroom 0.32.1/LiteLLM 1.91.3 default and test Headroom 0.31.0 only as an isolated plugin-compatibility/rollback lane.

## v0.5.2 — 2026-07-21

- Honor `context_reduction.allow_remote_proxy: true` when a non-loopback proxy URL is resolved from YAML and then revalidated by `readyz`, `smoke`, `retrieve`, `retrieve_stats`, or `compress_messages`.
- Keep remote proxies blocked by default and preserve the existing `HEADROOM_ALLOW_REMOTE_PROXY=1` opt-in path.
- Add no-network regressions covering the YAML and environment paths across the affected helpers ([#20](https://github.com/arotonal-ai/hermes-headroom-plugin/issues/20)).

## v0.5.1 — 2026-07-20

- Correct the packaged `proxy` extra to require `headroom-ai[proxy]==0.32.1`, matching the managed runtime, workflows, documentation, and validated multi-OS release candidate.
- Add a regression test and readiness-audit assertion that require exact package-metadata parity with the certified Headroom and LiteLLM runtime pins.

## v0.5.0 — 2026-07-20 (withdrawn)

> Withdrawn before any GitHub asset download was recorded: the packaged `proxy` extra still required Headroom 0.32.0 while the validated managed runtime used 0.32.1. The v0.5.1 hotfix corrects the metadata without rewriting the v0.5.0 tag or artifacts.

- Package a portable `headroom-runtime setup|status|doctor|uninstall` entry point plus a native-Git launcher so wheel and repository installs share one explicit lifecycle manager.
- Wrap Headroom 0.32's pinned manifest/supervisor lifecycle plus public `install status|remove`, while avoiding both `deploy` and direct `install apply`; force manual provider selection with no targets, telemetry off, isolated versioned venvs, loopback-only defaults, and compress → retrieve verification.
- Promote the managed runtime candidate to `headroom-ai[proxy]==0.32.1` with `litellm==1.91.3` after the official Windows wheel and sdist shipped; the blocking Ubuntu/macOS/Windows Python 3.11/3.12 lifecycle matrix passes, while target-host Defender detections remain fail-closed and never require exclusions.
- Add dry-run, unmanaged-port and foreign-supervisor/manifest conflict detection, official-PyPI spec validation, transaction-wide locking, complete manifest-identity-guarded uninstall, partial-state recovery, marker-guarded purge, and symmetric uninstall semantics.

## v0.4.1 — 2026-07-18

- Add an owner/agent actionable `after-install.md`, document the deterministic native Git-installed plugin path, and clarify that active savings begin only after the official PyPI Headroom runtime passes smoke; cloning upstream source is not required.
- Refresh canonical upstream repository/docs links and record a non-blocking, single-host `headroom-ai==0.32.0` runtime smoke PASS without promoting the certified `0.31.0` multi-OS pin.
- Replace ambiguous primary `/headroom on` guidance with platform-aware, read-only `/headroom setup`; retain `on` as an explicit legacy alias and never install dependencies or start daemons from the slash command.

- Align the portable plugin/runtime endpoint with upstream Headroom 0.31's default loopback port `8787`; keep explicit per-instance overrides for concurrent same-host labs instead of reserving the previous integration-specific port `28787`.
- Modernize GitHub Actions workflows to Node.js 24-compatible majors.
- Keep the certified LiteLLM pin blocking while adding a separate monthly, non-blocking multi-OS canary for newer allowed LiteLLM releases.
- Record resolved Headroom/LiteLLM versions in runtime smoke evidence and correct compatibility docs that still described unpinned dependencies as the production default.

## v0.4.0 — 2026-07-16

- Promote the verified v0.4 architecture with `tool_execution` primary and `llm_request/tool_results` remaining an explicit default-off opt-in.
- Retain the bounded five-minute cross-surface negative-outcome cache added in RC2 so unchanged sources that returned `compression_not_useful` are not submitted or attributed again; provider failures remain retryable.
- Normalize exact Headroom retrieval across live `content`/`original_content` and top-level/nested response shapes, reject responses that omit exact content, and fail closed on a mismatched response hash.
- Verify the final candidate through focused retrieval tests, the full local suite, a fresh-process cross-surface canary, and the release-candidate local gate before active-instance promotion.
- Pin the portable proxy runtime to `litellm==1.91.3`; `1.92.0` dropped macOS/Windows wheels and otherwise forces an undeclared Rust build toolchain during installation.

## v0.4.0-rc2 — 2026-07-16

- Add a process-local, bounded five-minute negative-outcome cache for unchanged logical tool sources that already returned `compression_not_useful`.
- Share that suppression across `tool_execution` and opt-in `llm_request` so request replays do not repeat provider calls or create duplicate reports/events after the first authoritative skipped result.
- Keep runtime-unavailable and provider-error outcomes uncached so fail-open recovery remains retryable; preserve content digest, tool/session/call identity, thread safety, and runtime-resolved cache bounds.
- Add regressions for cross-surface suppression, TTL/eviction, and retryable provider failures after a live active-gateway canary exposed the rc1 gap.

## v0.4.0-rc1 — 2026-07-16

- Make CCR retrieval hash-only across the tool schema, proxy payload, smoke/benchmark paths, bundled skill, docs, and release-candidate replay.
- Add provider-neutral typed reduction contracts and fake-provider contract coverage.
- Add one typed effective configuration resolver with explicit override → environment → YAML → default precedence and legacy aliases at the resolver boundary.
- Preserve loopback-first endpoint validation, exact/protected behavior, direct provider routing, and copy-on-write fail-open behavior.
- Split the Hermes middleware hot path into focused config, policy, observability, reduction, provider, tool-adapter, and request-adapter modules without changing plugin registration.
- Keep `middleware.py` as an explicit compatibility facade; all 97 pre-v0.4 top-level symbols remain importable while internal dependency patching moves to the owning modules.
- Route core thresholds, request-cache bounds, optional markers/hints, experimental aggregation, and report retention through the typed effective configuration authority; retain legacy settings as warned migration shims rather than parallel authorities.
- Enforce and test the attribution invariant that `tool_execution` is primary, marked results are not recompressed/recredited at `llm_request`, and logical request fingerprints separate protocol, tool-call, and session identities.
- Reconcile runtime installer defaults, canonical/legacy configuration documentation, marker defaults, and end-to-end plugin/runtime/venv rollback guidance.
- Bypass provider readiness and compression entirely for exact or protected tool-result paths, with regression coverage for final packets, diffs, existing markers, and protected controls.
- Validate the release candidate in an isolated loopback A/B canary; retain `tool_execution` as the default lane and keep `llm_request` opt-in.

## v0.3.21 — 2026-07-15

- Add an opt-in, provider-agnostic `llm_request` safety net at Hermes's native post-build/pre-transport boundary.
- Adapt tool-result text for `chat_completions`, `codex_responses`, `anthropic_messages`, and `bedrock_converse` without changing provider/model routing.
- Preserve system/user prompts, tool schemas and arguments, images, Anthropic signed-thinking/cache blocks, Bedrock sentinels/guardrails, headers, auth fields, and streaming controls.
- Keep the adapter copy-on-write and fail-open; protected/exact content remains unchanged and Attribution v2 records `surface=llm_request` with protocol-scoped model-facing metrics.
- Reuse each logical request-boundary transform from a bounded process cache and derive a stable source fingerprint/dedupe key so repeated API requests do not re-compress or re-attribute the same canonical tool result.

## v0.3.20 — 2026-07-15

- Make per-request duplicate counts marker-scoped instead of reporting duplicate rows from the entire retained event tail.
- Expose marker-correlation and model-facing-metric completeness separately, including mixed legacy/v2 retained contexts.
- Bump the bundled `llm-monitor` companion to `0.4.1` after the live activation canary identified the completeness ambiguity.

## v0.3.19 — 2026-07-15

- Add Headroom Attribution v2 events with unique `event_id`, stable logical `dedupe_key`, exact model-facing character deltas, labelled token estimates, internal-service counters, compression latency, and measurement scope.
- Make `llm-monitor` aggregation idempotent and render model-facing `before→after`, saved tokens, percentage, duplicate count, and legacy internal metrics without mixing denominators.
- Correlate retained Headroom markers with individual Hermes `pre_api_request` events across Chat/Responses-style tool-result shapes while explicitly excluding retained pressure from new-savings totals.
- Keep provider/model routing unchanged; the companion remains an observer and tool-result middleware remains fail-open.

## v0.3.18 — 2026-07-15

- Reject unsafe `--service-name` / `HEADROOM_SERVICE` values before writing a systemd user unit.
- Constrain durable-service names to a single ASCII `.service` basename, preventing path traversal and writes outside the intended user-unit directory.
- Add regression coverage for POSIX and Windows-style path separators plus valid custom service basenames.

## v0.3.17 — 2026-07-15

- Isolated both the release-candidate proxy and the standalone runtime-smoke proxy under disposable `HOME`, `USERPROFILE`, and explicit Headroom workspace/config roots.
- Forced verification runtimes to in-memory CCR with a 1,800-second TTL so tests cannot create or mutate the operator's default SQLite store.
- Added regression coverage and allowlisted cleanup for isolated Headroom state.

## v0.3.16 — 2026-07-15

- Defined a minimal portable tool-layer core while keeping provider/model routing direct and unchanged.
- Pinned the reproducible runtime and blocking smoke default to `headroom-ai[proxy]==0.31.0` in a versioned venv.
- Made in-memory CCR with a 1,800-second TTL the portable default; SQLite remains an explicit persistence tradeoff.
- Added fail-open, grouped report retention: 14 days, a 256 MiB soft threshold enforced on the next prune cycle, explicit pin markers, and no timer/watcher.
- Made the final status marker, first-turn hint, and bundled `llm-monitor` companion opt-in defaults.
- Made systemd deployment convergent by writing backend/TTL explicitly, reloading, enabling, and restarting the Headroom service before smoke.
- Required runtime store readback before `RUNTIME_FULL`; an already-running proxy with a backend/TTL mismatch now fails closed as `RUNTIME_PARTIAL`.
- Added `docs/portable-core.md` as the canonical storage, install, verification, and rollback contract.

## v0.3.15 — 2026-07-03

- Added `headroom-cache-effectiveness`, a read-only report for CCR store posture, middleware savings, TTL risk, active Hermes model path, and provider-cache observability.
- Added repository wrapper `scripts/headroom-cache-effectiveness.py` and tests for cache-effectiveness decisions.
- The report returns `KEEP_PROXY_HOT_PATH`, `ADD_CACHE_UX`, `TEST_PROVIDER_CACHE_LANE`, or `DO_NOT_USE_PROVIDER_CACHE`; it does not mutate runtime cache, provider/model routing, Hermes config, or plugin registration.
- Clarified that current plugin value is context reduction + CCR retrieval; provider prompt/KV cache requires a separate isolated lane before promotion.

## v0.3.14 — 2026-07-03

- Added `headroom-adoption-benchmark`, a bounded benchmark for deciding whether a new Hermes instance should adopt the Context Economy Loop reporting layer or stay compression-only.
- The benchmark returns `ADOPT_LOOP`, `COMPRESSION_ONLY`, or `DISABLE_LOOP_REPORTING` based on saved context, loop/report overhead, runtime health, and retrieval quality.
- Added repository wrapper `scripts/headroom-adoption-benchmark.py`, package module tests, and docs for fresh-instance adoption.
- The benchmark is read-only: it does not mutate Hermes config, runtime cache, provider routing, or plugin registration.

## v0.3.13 — 2026-07-03

- Clarified the Context Economy Loop as a bounded decision protocol, not an autonomous meta-agent, watcher, subjective scoring layer, or self-tuning controller.
- Added portable controls for disabling only middleware auto-compression while leaving runtime smoke/cache/retrieval available.
- Added a fresh-instance efficiency test: compare saved/avoided context against loop overhead, verify quality, and prove rollback/control.
- Added documentation/gate coverage so the loop cannot be promoted without controllability language.

## v0.3.12 — 2026-07-03

- Fixed scoped on-demand mode for YAML boolean config: `context_reduction.auto_compression: false` now disables middleware auto-compression as intended for development/operator loops.
- Added regression coverage for boolean config and manual mode precedence over `auto_terminal`.
- Clarified that the portable plugin default remains compression/savings-oriented; on-demand mode is a scoped development-loop override, not a product default.

## v0.3.11 — 2026-07-03

- Added `/headroom cache` and `headroom-events-summary cache` as read-only views of the runtime-owned CCR cache/store.
- Clarified that the plugin has no independent CCR cache; cache/TTL/entry limits live in the Headroom runtime/proxy.
- Documented cache risk: CCR markers can expire with runtime TTL, so exact sidecars/reports remain the audit fallback.
- Kept cache visibility read-only: no admin/debug endpoint exposure, no purge mutation, no provider routing mutation, and no telemetry.
- Added on-demand auto-compression control: `HEADROOM_AUTO_COMPRESSION=0` or `context_reduction.auto_compression: false` disables middleware auto-compression while keeping status, smoke, cache, and retrieval available.

## v0.3.10 — 2026-07-03

- Clarified the runtime boundary: the Hermes plugin does not implement compression by itself; it calls a configured Headroom proxy (`headroom-ai[proxy]`) for `/v1/compress`, `/v1/retrieve`, stats, and smoke tests.
- Reframed `RUNTIME_PARTIAL` as install/status-only operation, not production context reduction. Real compression, retrieval, `/headroom smoke`, middleware compression, and wrapper compression require `RUNTIME_FULL` or `RUNTIME_FULL_DURABLE`.
- Updated README, install guide, compact agent brief, Context Economy docs, and bundled skill so fresh instances know what the plugin alone covers versus what the Headroom runtime covers.
- Switched package license metadata to SPDX string form to remove the setuptools deprecation warning.
- No provider/model routing mutation, external telemetry, core Hermes mutation, or automatic cross-profile propagation.

## v0.3.9 — 2026-07-03

- Added portable Context Economy Loop documentation and gate: observe → classify → act → verify → learn, with exact-authority preservation and no instance-specific state.
- Added release-candidate enforcement for the Context Economy loop gate plus audit coverage for the new doc/script surfaces.
- Hardened repo readiness scanning to ignore local QA/gate evidence directories while keeping public package/archive scans active.
- Kept stable `/headroom` command surface; experimental context-economy analysis remains in docs/tests/gates rather than new slash commands.
- No provider/model routing mutation, external telemetry, core Hermes mutation, push/tag/release, or automatic cross-profile propagation.

## v0.3.8 — 2026-07-02

- Added local Headroom observability events and read-only `/headroom usage`, `/headroom usage turn`, `/headroom lanes`, and `/headroom tail` summaries.
- Added bundled `llm-monitor` companion packaging plus safe companion-only installer controls.
- Added `headroom-events-summary` CLI/cron renderer for local Headroom event summaries.
- Improved live turn scoping for HR summaries: prefer `turn_id`; use `task_id` only as fallback.
- Added S7 observability polish: plugin-local platform context capture, bounded event-log rotation, platform rendering in usage/tail, clearer exact-safe zero-savings HR copy, and clean companion import fallback outside Hermes core.
- No provider/model routing mutation, external telemetry, remote push, PyPI publish, or automatic core mutation.

## v0.3.7 — 2026-07-02

- Added Linux durable runtime installation mode: `scripts/install-production-runtime.py --systemd-user` writes/enables/starts `hermes-context-reduction.service` and reports `RUNTIME_FULL_DURABLE` only when the user service is enabled + active and real compress → retrieve smoke passes.
- Clarified that plain `RUNTIME_FULL` is process/session-level evidence, not proof that the proxy survives gateway restart/logout.
- Updated README, INSTALL, AGENTS, and compact agent install docs with Linux durable verification and Windows durability boundaries.
- Added regression coverage for durable installer flags, state naming, and documentation contract.

## v0.3.6 — 2026-07-01

- Added a local release-candidate gate (`scripts/release-candidate-local-gate.py`) that builds wheel/sdist, scans public package surfaces and archives for owner-local paths/secrets, installs the built wheel in a fresh venv, verifies temp `HERMES_HOME` install when Hermes CLI is available, runs real runtime smoke, exercises bulky middleware lanes, and checks no proxy is left running.
- Added `docs/release-candidate.md` and a manual GitHub Actions workflow for RC evidence upload.
- Added `test` and `release` optional dependencies for reproducible local gate setup.
- Remote CI runners without Hermes CLI now record the temp-Hermes install subgate as `skipped: hermes_cli_not_available`; package portability remains covered by wheel/entrypoint and runtime-smoke gates.

## v0.3.5 — 2026-06-30

- Bounded worker/background wrapper compression input for oversized traces using deterministic head + query-matching lines + tail windows while retaining the exact full raw sidecar as source of truth.
- Added `--max-compress-chars` / `--wrapper-max-compress-chars` controls and report metadata for `compression_input.bounded`, `original_chars`, `input_chars`, query terms, and matching line count.
- Added regression coverage for oversized wrapper compression bounding so selected query-matching evidence remains present in the bounded compression input.
- No provider/model routing, runtime config, telemetry, global/default routing, or unwrapped Kanban terminal transcript replacement.

## v0.3.4 — 2026-06-30

- Added structured tool-result handling for Headroom middleware: large string fields inside dictionary-shaped tool outputs can now be compressed while preserving the surrounding metadata exactly.
- Added regression coverage for structured `execute_code`-style results so eligible bulky `output` fields no longer bypass compression merely because the tool result is a mapping.
- No provider/model routing, runtime config, telemetry, or product-default/global routing promotion.

## v0.3.3 — 2026-06-30

- Added data-class exact-header gates for eligible bulky intermediate tool results before compression.
- Added protected/control fail-closed handling before Headroom sidecar/proxy creation, including full-result scans for late secrets/cookies/control blobs.
- Made `browser_vision` exact-by-default unless explicitly marked intermediate/debug/OCR/diagnostic/QA.
- Added deterministic quality-parity tests plus real local-loopback marker/retrieve smoke coverage.
- Verified GitHub CI on Ubuntu, macOS, and Windows py3.11, remote temp-home install, and a real Windows/other-instance `/headroom smoke` PASS.
- No provider/model routing, runtime config, or product-default/global routing promotion.

## v0.3.2 — 2026-06-29

- Added compact visible final-answer status marker via `transform_llm_output`: `[HR✓]` when Headroom proxy readiness is healthy and `[HR!]` when the marker is enabled but runtime readiness fails.
- The marker reports runtime readiness only; it does not claim that a specific final answer was compressed.
- Marker is enabled by default for parity with the owner-local product contract and can be disabled with `context_reduction.visible_status_marker: false` or `HEADROOM_VISIBLE_STATUS_MARKER=0`.
- `/headroom status` and `/headroom on` now show `visible_marker=...` so operators can distinguish runtime readiness from visible UX.

## v0.3.1 — 2026-06-29

- Added read-only `/headroom on` / `/headroom enable` compatibility response so installs coming from owner-local muscle memory do not fall through to generic usage. The command does not mutate gateway/provider/runtime state; it reports whether the current proxy is already active and points operators to `/headroom smoke` for full verification.
- Expanded `/headroom status` failure output with bounded detail to make transient readyz failures diagnosable when smoke later passes.
- Added regression coverage for slash-command contract drift: registration metadata, unit handler behavior, directory-plugin discovery, and clean temp-home install now validate `/headroom on` compatibility.

## v0.3.0 — 2026-06-29

- Promoted packaged worker/background/preflight CLI wrappers from placeholders to tested production behavior for explicit operator commands.
- `headroom-worker-lane` and `headroom-background-lane` retain exact stdout/stderr sidecars and exact worker final packets, then compress only eligible bulky intermediate traces through the configured loopback proxy.
- `headroom-command-preflight` recommends direct vs wrapped execution without mutating Hermes runtime/provider/model config.
- Removed packaged explicit provider-route console scripts from the product surface; global/default provider routing remains unchanged.

## v0.2.0 — 2026-06-29

- Added fail-open `tool_execution` middleware for eligible bulky intermediate tool/lane results, including `delegate_task`, while preserving exact/blocked classes.
- Added marker extraction for both `<<ccr:...>>` and Headroom `hash=...` forms in result compression paths.
- Added production runtime installer (`scripts/install-production-runtime.py` / `.sh`) that creates a persistent venv, installs latest `headroom-ai[proxy]` by default, starts the loopback proxy on `127.0.0.1:28787`, verifies `/readyz`, and reports `RUNTIME_FULL` only after compress → retrieve smoke passes.
- Changed runtime dependency default from a historical version range to unpinned `headroom-ai[proxy]`; `--spec` / `HEADROOM_AI_SPEC` remain available for explicit rollback diagnostics.


## v0.1.1 — 2026-06-29

Compatibility and observability polish.

### Added

- Non-blocking Future Runtime Monitor workflow for Python 3.13/3.14 drift signals.
- `docs/compatibility.md` separating certified support from experimental monitoring.

### Changed

- Documented that future Python versions are capability-monitored, not accepted by optimistic version widening.
- Bumped package metadata version to `0.1.1`.

## v0.1.0 — 2026-06-29

Initial stable public Hermes Headroom plugin release.

### Added

- Native Hermes plugin manifest and Python entry point for `headroom_retrieve`.
- `/headroom status`, `/headroom smoke`, and `/headroom audit` command surface.
- Safe default policy scaffolding: exact, exact-bounded, compressible, and blocked classes.
- Bundled `headroom-token-cost-evaluation` operating skill.
- Portable install/audit/test helpers for humans and agents.
- Temporary `HERMES_HOME` install smoke for local and remote plugin paths.
- Upstream dependency smoke for `headroom-ai[proxy]`.
- Full runtime smoke that starts a real loopback proxy and validates compress → retrieve sentinel recovery.
- Runtime Smoke GitHub workflow across Ubuntu/macOS/Windows and Python 3.11/3.12.
- Remote proxy guardrail: non-loopback `HEADROOM_PROXY_URL` is blocked unless explicitly allowed.
- Human and agent documentation: README, INSTALL, AGENTS, compact agent brief, SECURITY, PRIVACY, acknowledgements, and metrics placeholders.

### Verified

- CI: Ubuntu, macOS, Windows.
- Runtime Smoke: Ubuntu 3.11/3.12, macOS 3.11/3.12, Windows 3.11/3.12.
- Secret-pattern scan over tracked files: no high-risk hits at release time.

### Safety posture

- No API keys required.
- No external telemetry.
- No mutation of global/default Hermes provider routing.
- No automatic live Hermes traffic compression in P0.
- Final answers, secrets, memory/profile/system/developer instructions, patches/diffs, manifests, hashes, claim ledgers, and protected content remain exact or blocked.
