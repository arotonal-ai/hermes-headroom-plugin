# Release-candidate local gate

Use this gate before claiming a checkout is ready for owner review as a portable Hermes Headroom Plugin release candidate.

```bash
python scripts/release-candidate-local-gate.py
```

The gate is local-only. It does **not** push, tag, publish, mutate the real Hermes profile, change provider/model routing, or enable external telemetry. Evidence is written under:

```text
release-candidate-runs/<UTC>-release-candidate-local-gate/
```

By default the gate removes its allowlisted reproducible per-run virtualenvs after
writing reports, command receipts, logs, package artifacts and workload
matrices.  This prevents repeated local gates from retaining roughly one full
runtime environment per stage.  Use `--keep-ephemeral-envs` only for a bounded
debugging run; it is not the normal evidence-retention mode.

## Passing decision

```text
PLUGIN_RELEASE_CANDIDATE_LOCAL_PASS
```

A default local pass means the checkout is ready for owner review and remote CI readback and includes a verified clean temporary Hermes installation. It is **not** public-release authorization. Generic CI may pass `--allow-hermes-install-deferred`, but that produces `deferred: true` / `verified: false` for this subgate and never substitutes for the final target-host RC.

## Gate coverage

| Gate | Purpose |
|---|---|
| repo readiness audit | required docs/scripts/manifests, syntax checks, local-link checks, basic secret scan |
| context economy loop gate | runs the portable Context Economy Loop gate, including portable doc scan, clean temp install when Hermes CLI is available, synthetic pressure adapter, stable command-surface check, and loopback runtime smoke |
| public path / secret scan | blocks owner-local absolute paths and high-risk secret patterns in public package surfaces |
| unit/contract tests | runs the package test suite in a temporary venv with `.[test]` |
| build/archive inspection | builds wheel + sdist and scans archives for forbidden members, owner-local paths, and secrets |
| wheel install/entrypoints | installs the built wheel in a fresh venv and verifies packaged console scripts |
| package upgrade/rollback | builds published `v0.5.2`, then proves `0.5.2 → 0.6.2 → 0.5.2` in a fresh venv |
| clean temp Hermes install | installs the local checkout into a temporary `HERMES_HOME` without touching the real profile; it is required by default. Generic CI without Hermes may explicitly defer it with `--allow-hermes-install-deferred`, which preserves package/runtime evidence but does not certify clean Hermes installation. |
| runtime compress/retrieve smoke | verifies the managed Headroom 0.32.1/LiteLLM 1.94.0rc3 pair |
| compatibility runtime smoke | separately verifies Headroom 0.31.0 as an isolated plugin-compatibility/rollback lane, not the managed default |
| bulky workload matrix | verifies real plugin middleware over terminal/QA, delegate/subagent, browser/debug, and research-corpus lanes plus negative exact controls |
| no new leftover proxy | snapshots pre-existing owner runtimes and verifies the gate leaks no additional Headroom proxy process |
| durable lifecycle boundary | local default defers native supervisor mutation; pass `--run-durable-lifecycle` only at an explicitly authorized gate, or use the separate cross-OS Runtime Manager Lifecycle workflow |

## Workload expectations

Compressed lanes must prove:

- Headroom auto-compression marker/header is present;
- exact redacted sidecar is retained;
- case sentinel remains in the exact sidecar;
- token savings are material;
- private-key-like text is not emitted in compressed output.

Exact/blocked controls must remain uncompressed:

- patch/diff-like output;
- secret/private-key-like material, with no provider report, marker, or retained sidecar;
- worker final packets / claim ledgers.

## Release boundary

Before any public push/tag/release, require:

1. explicit owner approval for remote write/release;
2. exact `git diff` review;
3. local `PLUGIN_RELEASE_CANDIDATE_LOCAL_PASS` evidence path;
4. `clean_temp_hermes_install` shows `verified: true` and `deferred: false` on a target host;
5. GitHub Actions CI/runtime readback after push;
6. release notes that distinguish upstream Headroom from this Hermes integration wrapper;
7. rollback instructions: disable/remove `headroom_retrieve` and stop local proxy.

## Common commands

```bash
# Standard local RC gate
python scripts/release-candidate-local-gate.py

# Generic CI/package-runtime evidence only; not the final target-host RC
python scripts/release-candidate-local-gate.py --allow-hermes-install-deferred

# Use a specific upstream Headroom package spec for rollback diagnostics
python scripts/release-candidate-local-gate.py --headroom-spec 'headroom-ai[proxy]==0.28.0'

# Explicitly run the native user-supervisor lifecycle (mutating, reversible)
python scripts/release-candidate-local-gate.py --run-durable-lifecycle

# Write evidence somewhere else
python scripts/release-candidate-local-gate.py --run-root /tmp/hermes-headroom-rc
```
