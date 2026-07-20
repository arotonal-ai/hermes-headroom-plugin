# Compatibility

This page separates **published certified releases** from withdrawn releases and non-blocking future monitoring.

- Published `v0.4.1`: `headroom-ai[proxy]==0.31.0` plus `litellm==1.91.3`.
- Withdrawn `v0.5.0`: packaged `proxy` metadata still required Headroom 0.32.0; its GitHub release remains draft and its tag/artifacts are not rewritten.
- Published `v0.5.1`: `headroom-ai[proxy]==0.32.1` plus `litellm==1.91.3`, managed through `headroom-runtime`.

The v0.5.1 pair passed its blocking setup → status → doctor → uninstall matrix on GitHub-hosted Ubuntu, macOS, and native Windows for Python 3.11 and 3.12, plus the final release-candidate, exact-pin documentation, and release-control gates. This evidence supports the published lifecycle; it does not claim production token savings.

## Published v0.4.x baseline

The v0.4 Runtime Smoke workflow installed the runtime, started a loopback proxy, and verified plugin compress → retrieve sentinel recovery.

Evidence baseline:

- Runtime Smoke run: <https://github.com/arotonal-ai/hermes-headroom-plugin/actions/runs/29527241673>
- Release: [`v0.4.0`](https://github.com/arotonal-ai/hermes-headroom-plugin/releases/tag/v0.4.0)
- Commit: `c047be05f2d29f784cd4e91b7711f6b1a0210706`

| OS | Python | Plugin CI | Foreground runtime smoke | Published status |
|---|---:|---:|---:|---|
| Ubuntu | 3.11 | ✅ | ✅ | v0.4 certified |
| Ubuntu | 3.12 | — | ✅ | v0.4 certified |
| macOS | 3.11 | ✅ | ✅ | v0.4 certified |
| macOS | 3.12 | — | ✅ | v0.4 certified |
| Windows native | 3.11 | ✅ | ✅ | v0.4 certified |
| Windows native | 3.12 | — | ✅ | v0.4 certified |
| WSL2 | target evidence required | 🟡 expected | 🟡 expected | not certified here |
| Termux | target evidence required | 🟡 expected | 🟡 expected | not certified here |

## Published v0.5.1 runtime manager

Headroom `0.32.1` is the pinned v0.5.1 runtime. The direct upstream user-scope apply path was rejected because a real 0.32.0 canary wrote persistent `HEADROOM_*` blocks to `.bashrc`, `.zshrc`, and `.profile` despite manual provider mode and no provider targets. `headroom/cli/install.py` is byte-identical between that reviewed 0.32.0 source and the verified 0.32.1 PyPI sdist; the other wrapped lifecycle functions are AST-equivalent.

The replacement manager uses the pinned upstream manifest/native-supervisor implementation while requiring:

```json
{"provider_mode":"manual","targets":[],"mutations":[]}
```

Local Linux evidence on Python 3.11.15 passed the 0.32.1 lifecycle through the native Git launcher:

- setup returned `RUNTIME_FULL_DURABLE`;
- status and doctor returned exit `0`;
- compress → retrieve recovered the sentinel;
- the saved upstream manifest retained `mutations=[]` and matched the complete profile/port/scope/supervisor/environment/proxy-argument identity contract;
- shell-profile SHA-256 values were unchanged;
- uninstall removed the unit, listener, artifacts, and managed runtime root;
- the existing owner runtime remained healthy and unchanged.

Sanitized release evidence: [`docs/evidence/headroom-0321-runtime-manager-canary-20260720.json`](evidence/headroom-0321-runtime-manager-canary-20260720.json).

The blocking `.github/workflows/runtime-smoke.yml` matrix passed the same lifecycle canary in all six Ubuntu/macOS/Windows Python 3.11/3.12 jobs: [run 29716326909](https://github.com/arotonal-ai/hermes-headroom-plugin/actions/runs/29716326909).

Some native Windows hosts report Microsoft Defender quarantining the base dependency `ast-grep-cli` (`sg.exe`), tracked upstream in [headroom#2267](https://github.com/headroomlabs-ai/headroom/issues/2267). The blocking matrix did not reproduce it. This project does not add or recommend Defender exclusions; a target-host detection remains a fail-closed deployment blocker.

## Experimental future runtimes

Python 3.13/3.14, future `headroom-ai` ranges, and newer LiteLLM releases are monitored separately by the **Future Runtime Monitor** workflow at `.github/workflows/future-runtime-monitor.yml`. The latest-LiteLLM lane holds Headroom at `0.32.1`, uses Python 3.12, and varies only the allowed LiteLLM range across Ubuntu, macOS, and Windows.

That workflow is intentionally **non-blocking**:

- it may pass or fail without changing certified support;
- failures are early drift signals, not regressions in supported 3.11/3.12 paths;
- the certified `litellm==1.91.3` path remains blocking and unchanged when a latest-dependency lane fails;
- promotion requires a normal blocking lifecycle matrix, release gate, and docs/changelog update.

| Runtime | Current posture | Promotion gate |
|---|---|---|
| Python 3.13 | experimental monitor | lifecycle PASS on Ubuntu/macOS/Windows and no known upstream native dependency failures |
| Python 3.14 | experimental monitor | lifecycle PASS on Ubuntu/macOS/Windows and no known upstream native dependency failures |
| Upstream `headroom-ai[proxy]` latest | experimental monitor | blocking dependency + manager lifecycle PASS before changing the exact pin |
| LiteLLM latest allowed `<2.0` | experimental monthly monitor | repeated multi-OS PASS, advisory review, and blocking release-candidate gate |

## Runtime-version policy

1. Keep plugin install/load independent from the separate proxy runtime; active compression requires a healthy loopback proxy.
2. Keep runtime versions exact in release paths. The v0.5.1 release pair is `headroom-ai[proxy]==0.32.1` plus `litellm==1.91.3`.
3. Treat `--headroom-spec` / `HEADROOM_AI_SPEC` and `--litellm-spec` / `HEADROOM_LITELLM_SPEC` overrides as explicit incident, target-host diagnostic, or non-blocking canary controls.
4. Promote or demote support only from dependency, real lifecycle, readiness, sentinel-recovery, and rollback evidence.
5. Document target-host drift honestly, especially on native Windows where Python aliases and Task Scheduler policy may differ from CI.
