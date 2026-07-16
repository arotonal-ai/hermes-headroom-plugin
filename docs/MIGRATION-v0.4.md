# Migrating from v0.3.x to v0.4

v0.4 keeps the installed plugin name, entry point, command namespace, and default `tool_execution` behavior. The migration is intended to be reversible and does not require changing the Headroom service or provider route.

## Before upgrading

1. Record the installed plugin revision and effective `/headroom status` output.
2. Preserve the current plugin checkout or wheel as the rollback artifact.
3. Confirm the loopback Headroom runtime is healthy.
4. Keep `llm_request` disabled unless you are running an explicitly approved canary.

## Compatibility table

| Surface | v0.4 behavior | Migration action |
|---|---|---|
| Plugin package and entry point | Preserved | None |
| `/headroom` commands | Preserved; canonical diagnostics are documented in README | Do not create new automation around undocumented aliases |
| `headroom_retrieve` input | Exact schema: `{"hash": "..."}` only | Remove any legacy `query` property; extra properties are rejected |
| `tool_execution` middleware | Default lane | None |
| `llm_request` middleware | Opt-in and default-off | Enable only for a bounded canary with attribution checks |
| Marker recovery | Exact hash-only retrieval | Do not recompress markers or recovered source |
| Effective configuration | Override → environment → YAML → bounded default | Move callers to canonical names; inspect status warnings |
| Legacy configuration names | Read-only migration shims with warnings | Replace them with canonical names; no new dependency |
| `middleware.py` imports | 97 legacy top-level symbols remain importable | Patch/test the owning focused module, not the facade internals |
| Headroom runtime | `headroom-ai[proxy]==0.31.0` in the reproducible core | Keep an explicit version pin and isolated runtime smoke |

## Verification

Run from a clean temporary `HERMES_HOME`:

```bash
bash scripts/test-clean-hermes-install.sh --local
python3 scripts/test-headroom-runtime-smoke.py --spec 'headroom-ai[proxy]==0.31.0'
```

Verify that exact/final/diff/protected results remain byte-identical, live markers retrieve exactly, runtime failures return the original eligible result, and no provider/model route changes.

## Rollback

1. Disable `llm_request` first if it was explicitly enabled.
2. Disable automatic compression while leaving retrieval available when exact recovery is still needed.
3. Reinstall the preserved v0.3.x checkout or wheel.
4. Remove only the failed temporary install or canary resources.
5. Do not restart or reconfigure the shared Headroom runtime unless that runtime was separately changed.

The public retrieval schema intentionally does not accept `query`; rollback is the compatibility path for callers that cannot migrate to hash-only retrieval.