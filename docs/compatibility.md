# Compatibility

This page separates **certified support** from **experimental monitoring**. The portable-core installer and blocking smoke default to the certified pair `headroom-ai[proxy]==0.31.0` plus `litellm==1.91.3`; runtime certification still requires a real proxy smoke, not just a successful pip install. Future versions remain explicit non-blocking canaries until promoted.

## Certified runtime matrix

Certified means the repository's Runtime Smoke workflow installed `headroom-ai[proxy]`, started a loopback Headroom proxy, and verified plugin compress → retrieve sentinel recovery.

Evidence baseline:

- Runtime Smoke run: <https://github.com/arotonal-ai/hermes-headroom-plugin/actions/runs/29527241673>
- Release: [`v0.4.0`](https://github.com/arotonal-ai/hermes-headroom-plugin/releases/tag/v0.4.0)
- Commit: `c047be05f2d29f784cd4e91b7711f6b1a0210706`

| OS | Python | Plugin CI | Runtime Smoke | Status |
|---|---:|---:|---:|---|
| Ubuntu | 3.11 | ✅ | ✅ | certified |
| Ubuntu | 3.12 | — | ✅ | certified |
| macOS | 3.11 | ✅ | ✅ | certified |
| macOS | 3.12 | — | ✅ | certified |
| Windows native | 3.11 | ✅ | ✅ | certified |
| Windows native | 3.12 | — | ✅ | certified |
| WSL2 | target evidence required | 🟡 expected | 🟡 expected | not certified here |
| Termux | target evidence required | 🟡 expected | 🟡 expected | not certified here |

## Experimental future runtimes

Python 3.13/3.14, future `headroom-ai` ranges, and newer LiteLLM releases are monitored separately by the **Future Runtime Monitor** workflow at `.github/workflows/future-runtime-monitor.yml`. The LiteLLM lane holds Headroom at `0.31.0`, uses Python 3.12, and varies only the allowed LiteLLM range across Ubuntu, macOS, and Windows.

That workflow is intentionally **non-blocking**:

- it may pass or fail without changing certified support;
- failures should be treated as early drift signals, not regressions in supported 3.11/3.12 paths;
- the certified `litellm==1.91.3` path remains blocking and unchanged when the latest-dependency lane fails;
- promotion to certified support requires a normal Runtime Smoke matrix update, green runs, and a docs/changelog update.

| Runtime | Current posture | Promotion gate |
|---|---|---|
| Python 3.13 | experimental monitor | Runtime Smoke PASS on Ubuntu/macOS/Windows and no known upstream native dependency failures |
| Python 3.14 | experimental monitor | Runtime Smoke PASS on Ubuntu/macOS/Windows and no known upstream native dependency failures |
| Upstream `headroom-ai[proxy]` latest | experimental monitor | blocking dependency smoke + Runtime Smoke PASS before changing the certified exact version |
| LiteLLM latest allowed `<2.0` | experimental monthly monitor | repeated Runtime Smoke PASS on Ubuntu/macOS/Windows, advisory review, and blocking release-candidate gate before changing the certified pin |

## Policy for runtime versions

Use evidence before changing certified pins:

1. Keep plugin install/load independent from the separate proxy runtime; only install/status can pass without it, while active compression requires it.
2. Default production install to the exact certified pair `headroom-ai[proxy]==0.31.0` and `litellm==1.91.3`.
3. Use `--spec` / `HEADROOM_AI_SPEC` and `--litellm-spec` / `HEADROOM_LITELLM_SPEC` only as explicit incident, target-host diagnostic, or non-blocking canary overrides until promoted.
4. Promote or demote support only after **dependency smoke** and **real proxy runtime smoke** pass/fail with evidence.
5. Document target-host drift honestly, especially on native Windows where global Python aliases can differ from the Hermes Python.
