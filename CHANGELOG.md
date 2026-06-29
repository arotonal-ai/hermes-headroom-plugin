# Changelog

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
