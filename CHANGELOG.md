# Changelog

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
