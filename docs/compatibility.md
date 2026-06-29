# Compatibility

This page separates **certified support** from **experimental monitoring**. Do not widen dependency or Python support claims just because a package installs; runtime certification requires a real proxy smoke.

## Certified runtime matrix

Certified means the repository's Runtime Smoke workflow installed `headroom-ai[proxy]>=0.26,<0.28`, started a loopback Headroom proxy, and verified plugin compress → retrieve sentinel recovery.

Evidence baseline:

- Runtime Smoke run: <https://github.com/arotonal-ai/hermes-headroom-plugin/actions/runs/28351863009>
- Release: `v0.1.0`
- Commit: `9ccc33ff36ff77ab6c93764924be65074698f842`

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

Python 3.13/3.14 and future `headroom-ai` ranges are monitored separately by the **Future Runtime Monitor** workflow at `.github/workflows/future-runtime-monitor.yml`.

That workflow is intentionally **non-blocking**:

- it may pass or fail without changing certified support;
- failures should be treated as early drift signals, not regressions in supported 3.11/3.12 paths;
- promotion to certified support requires a normal Runtime Smoke matrix update, green runs, and a docs/changelog update.

| Runtime | Current posture | Promotion gate |
|---|---|---|
| Python 3.13 | experimental monitor | Runtime Smoke PASS on Ubuntu/macOS/Windows and no known upstream native dependency failures |
| Python 3.14 | experimental monitor | Runtime Smoke PASS on Ubuntu/macOS/Windows and no known upstream native dependency failures |
| `headroom-ai[proxy]>=0.28` | not accepted | dependency smoke + runtime smoke PASS before widening `pyproject.toml` |

## Policy for version ranges

Use capability checks before pins:

1. Keep plugin install/load independent from the optional proxy runtime.
2. Prefer explicit smoke tests over broad version promises.
3. Use lower bounds for required APIs and upper bounds where upstream compatibility is unverified.
4. Promote a runtime only after **dependency smoke** and **real proxy runtime smoke** pass.
5. Document target-host drift honestly, especially on native Windows where global Python aliases can differ from the Hermes Python.
