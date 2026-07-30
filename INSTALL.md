# Install Hermes Headroom Plugin

This guide is the shortest safe path for a fresh Hermes instance. It separates the **Hermes plugin** from the **Headroom proxy/backend runtime** so operators do not confuse “plugin loaded” with “compression runtime running”. The runtime is optional only for degraded install/status operation; it is required for real compression, retrieval, smoke tests, and middleware/wrapper compression.

## 0. Prerequisites

On the target machine:

```bash
hermes --version
git --version
python --version  # or python3 --version
```

If `hermes` is missing, install/fix Hermes Agent first: <https://hermes-agent.nousresearch.com/docs/getting-started/installation>.

| Requirement | Needed for | Note |
|---|---|---|
| Hermes Agent | plugin install/load | must be on `PATH` |
| Git | `hermes plugins install owner/repo` | required by native plugin install |
| Python | helper scripts and separate proxy venv | use a Python supported by Hermes and upstream Headroom |
| `headroom-ai[proxy]` | full compression/retrieval runtime | required for `RUNTIME_FULL`; not required only for plugin load/status |
| API keys | not needed | do not paste secrets into install commands or issues |

Native Git and wheel installs share the same packaged runtime manager. Git uses `scripts/headroom-runtime.py`; wheels expose `headroom-runtime`. Both remain explicit and separate from plugin registration.

## 1. Install the Hermes plugin

Run on the owner/target Hermes instance:

```bash
hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
hermes plugins list --enabled --user --plain
hermes gateway restart
```

For a CLI-only or active chat session, start a fresh session with `/new` instead of restarting the gateway.

Verify inside Hermes:

```text
/headroom status
/headroom setup   # read-only setup guidance; does not install/start runtime
/llm-monitor status
```

Expected: the commands exist, `/llm-monitor status` reports `ON · mode=metadata`, and Headroom returns proxy/status guidance. The embedded monitor keeps strict local metadata counters only; it does not retain request/response bodies, call another model, or send external telemetry. Use `/llm-monitor off` for a persistent opt-out. When an enabled standalone `llm-monitor` already exists, that plugin remains authoritative and the embedded copy stays inactive to avoid duplicate hooks.

The optional final-answer Headroom marker remains off by default; when explicitly enabled it reports `visible_marker=on:[HR✓]` only if proxy readiness is healthy. If no proxy is running, it may report unavailable; that is `RUNTIME_PARTIAL`, not a failed plugin install.

## 2. Install Headroom runtime for real compression

Native Git install:

```bash
PLUGIN_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve"
python3 "$PLUGIN_DIR/scripts/headroom-runtime.py" setup
```

Windows PowerShell:

```powershell
$PluginDir = if ($env:HERMES_HOME) { "$env:HERMES_HOME\plugins\headroom_retrieve" } else { "$HOME\.hermes\plugins\headroom_retrieve" }
py -3 "$PluginDir\scripts\headroom-runtime.py" setup
```

Wheel install:

```bash
headroom-runtime setup
```

Use `setup --dry-run --json` for a no-write plan. The manager creates `${HERMES_HOME:-$HOME/.hermes}/runtimes/headroom/venv-0.33.0`, installs official `headroom-ai[proxy]==0.33.0` plus `litellm==1.94.0rc3`, and uses upstream manifests/native supervisors with `provider_mode=manual`, `targets=[]`, and `mutations=[]`. It does not invoke direct upstream apply; a prior 0.32.1 canary wrote persistent shell blocks even with manual providers and no targets. It verifies upstream status, `/readyz`, and real plugin compress → retrieve smoke before reporting `RUNTIME_FULL_DURABLE`.

Verify and roll back with the same launcher/entry point:

```bash
headroom-runtime status --json
headroom-runtime doctor --json
headroom-runtime reconcile --dry-run --json # native Windows plan; no writes
headroom-runtime reconcile --probe-port 18787 --dry-run --json # inspect a known non-default listener
headroom-runtime reconcile --apply --json  # explicit manager-owned migration
headroom-runtime uninstall --json
```

`reconcile --dry-run` performs no lock/log/state/manifest/supervisor write and makes no application-level listener request. It reports manager deployment identity separately from listener PID/executable binding derived from Windows OS tables; `/readyz` and upstream status are `not_probed_read_only`. An unproven listener binding is never treated as adoption authority. A manager-identified deployment with legacy environment-mutation history returns `REINSTALL_REQUIRED`, preserves those records for symmetric rollback, and reports `mutation_authority.eligible=false`. Do not use `--apply` for that case; follow the explicit target-host-gated upgrade and rollback procedure in [docs/runtime-manager.md](docs/runtime-manager.md).

The old `scripts/install-production-runtime.py` remains for v0.4 compatibility and optional companion operations only. New runtime installs should use the manager.

For a clean-instance canary, assert that `127.0.0.1:8787` is free or allocate a distinct loopback port with `--port`. Never count another instance's ready listener as clean-install evidence.

Then verify inside Hermes: `/headroom smoke`; use `/headroom cache` for read-only runtime-owned CCR store posture.

## 3. Acceptance matrix

| State | Meaning | Evidence |
|---|---|---|
| `INSTALL_PASS` | Plugin installed and Hermes can load it | `hermes plugins list --enabled --user --plain` includes `headroom_retrieve`; `/headroom status` and `/headroom setup` respond after restart/new session |
| `RUNTIME_PARTIAL` | Plugin commands load, proxy unavailable | `/headroom status` reports unavailable or `/headroom smoke` fails at `readyz`; no compression/retrieval/middleware compression is active |
| `RUNTIME_FULL` | Plugin, dependency, and proxy work in the current process/session | dependency smoke passes and `/headroom smoke` returns PASS with sentinel retrieval; `/headroom cache` can read runtime-owned CCR store stats |
| `RUNTIME_FULL_DURABLE` | Native user lifecycle is installed and healthy | `headroom-runtime doctor --json` returns exit 0 with upstream status, readyz, and sentinel recovery PASS |
| `FAIL` | Plugin not usable | plugin not enabled, `/headroom` unavailable after restart/new session, or install required copying owner-local `~/.hermes` state |

## 4. Optional validation helpers

Use these from a repo checkout when you want evidence without mutating real environments. The full-suite command requires `uv` only as a development tool; `--isolated --no-project` keeps dependency resolution and lock state outside the checkout.

Analyze without installing:

```bash
git clone https://github.com/arotonal-ai/hermes-headroom-plugin.git
cd hermes-headroom-plugin
python scripts/run-isolated-unit-tests.py
scripts/audit-repo-readiness.sh
```

The runner bootstraps pinned pytest/PyYAML through an ephemeral `uv --isolated --no-project` environment when the selected interpreter lacks pytest or can import a live Hermes host. It does not install test dependencies into the Hermes production/runtime venv.

Validate upstream Headroom dependency in a temporary Python venv:

```bash
python scripts/test-headroom-dependency-install.py
python scripts/test-headroom-runtime-smoke.py  # starts real loopback proxy + plugin smoke
# Unix wrappers:
scripts/test-headroom-dependency-install.sh
```

Validate plugin install in a temporary Hermes home:

```bash
scripts/test-clean-hermes-install.sh --local
```

Compatibility: the production portable lane uses `headroom-ai[proxy]==0.33.0` plus `litellm==1.94.0rc3`, fail-fast Python `>=3.11,<3.15` validation, isolated Python subprocess environments, and blocking native-Windows Python 3.14 CI. Headroom 0.31.0 remains a plugin-compatibility/rollback lane, not the manager default. Use `--spec` / `HEADROOM_AI_SPEC` or `--litellm-spec` / `HEADROOM_LITELLM_SPEC` only for an explicit rollback, target-host canary, or incident override. A non-default LiteLLM spec does not silently bypass the Python gate; Python outside the certified range additionally requires `--allow-unsupported-python` and remains canary-only. See the [compatibility contract](docs/compatibility.md) for platform evidence and host-level durability limits.

## 5. Scoped on-demand mode and cache
Portable plugin operation should favor compression and savings by default. For this repo's own heavy iterative improvement loops, disable middleware auto-compression without stopping the runtime: set `HEADROOM_AUTO_COMPRESSION=0` for the development process, or `context_reduction.auto_compression: false` in Hermes config plus fresh session/gateway restart. Status/smoke/cache/retrieve still work; eligible tool outputs return exact unless an explicit wrapper/runtime path compresses them. Re-enable automatic compression for normal portable operation.

Cache boundary:

The plugin has no independent CCR cache. The runtime/proxy owns CCR TTL, limits, backend, and eviction; `/headroom cache` is read-only and reports runtime plus local-fallback posture without paths or content.

The optional profile-local exact fallback retains only allowlisted, non-sensitive compressed intermediates. It is content-addressed, TTL/quota bounded, profile-isolated, and default-off for new writes; files use `0700`/`0600` on POSIX and the active user's inherited profile ACL on Windows. Existing entries remain readable until expiry, while protected payloads create only `redacted` tombstones.

Config: `context_reduction.local_exact_store: {enabled: false, ttl_seconds: 1800, max_entries: 256, max_bytes: 67108864}`.

Expired runtime entries remain unretrievable without an exact manifest, so canonical, instruction, final-answer, edit/recovery, memory, and hot source-readback material stays exact. Hermes exposes no purge/admin cache mutation command.

## 6. Proxy endpoint configuration

Default plugin/runtime target:

```text
http://127.0.0.1:8787
```

The `[HR✓]` / `[HR!]` final-answer marker is disabled by default. Enable it with `context_reduction.visible_status_marker: true` or `HEADROOM_VISIBLE_STATUS_MARKER=1`. It reports runtime readiness only, not per-message compression.

This integration uses the portable loopback default `8787` and passes it explicitly for reproducibility. Start production runtime with `headroom-runtime setup` or the native Git launcher. For concurrent same-host instances, allocate a distinct free loopback port and set `context_reduction.proxy_url` (preferred) or `HEADROOM_PROXY_URL` to that exact endpoint; a ready proxy owned by another instance is not clean-install evidence. `28787` is a retired pre-v0.4 integration default, and any port such as `28789` is only an instance-specific override. See [ports and native supervisor names](docs/ports-and-services.md).

To point Hermes at another local/controlled endpoint:

```bash
export HEADROOM_PROXY_URL="http://127.0.0.1:8787"
```

Or set Hermes config:

```yaml
context_reduction:
  proxy_url: http://127.0.0.1:8787
```

Restart/fresh-session before rechecking `/headroom status` and `/headroom setup`.

**Remote proxy guardrail:** loopback (`127.0.0.1` / `localhost`) is allowed by default. Non-loopback `HEADROOM_PROXY_URL` is blocked unless you explicitly set `HEADROOM_ALLOW_REMOTE_PROXY=1` or `context_reduction.allow_remote_proxy: true`; use that only for controlled, trusted endpoints.

## 7. Update

```bash
hermes plugins update headroom_retrieve
hermes gateway restart
```

## 8. Disable / remove / rollback

Disable but keep files:

```bash
hermes plugins disable headroom_retrieve
hermes gateway restart
```

Remove plugin files:

```bash
hermes plugins remove headroom_retrieve
hermes gateway restart
```

If installed from a local checkout with `--local`, remove the plugin directory/symlink manually only after confirming it is the intended target:

```bash
rm -rf "${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve"
```

Run `headroom-runtime uninstall --json` before removing plugin files. On `UNINSTALL_PARTIAL`, preserve manager state/logs and inspect upstream lifecycle; do not delete supervisor artifacts blindly. Restore the previous plugin release and re-run load/status plus compress → retrieve smoke when runtime capability is retained. See `docs/portable-core.md` for the canonical rollback contract.

## 9. Metrics and savings table
Savings tables are generated from JSONL evidence, grouped by Monday. If no evidence exists, the table stays as placeholders rather than estimated numbers.

```bash
python scripts/generate-weekly-savings-table.py --input docs/metrics/data/*.jsonl --write docs/metrics/weekly-savings.md
```

See [docs/metrics/weekly-savings.md](docs/metrics/weekly-savings.md).

## 10. Troubleshooting
### `hermes plugins install` works but `/headroom` is unknown

Start a fresh session or restart the gateway:

```bash
hermes gateway restart
```

### GitHub page works but install fails

Check network/Git access from the target machine:

```bash
git ls-remote https://github.com/arotonal-ai/hermes-headroom-plugin.git HEAD
```

### Dependency install fails

This is a backend/runtime issue, not a Hermes plugin install issue. On Windows, prefer a Hermes-compatible Python 3.11/3.12 venv for `RUNTIME_FULL`; if using a newer global Python, verify native imports before trusting proxy startup. Capture the environment and rerun with kept temp files:

```bash
python --version
python -m pip --version
python scripts/test-headroom-dependency-install.py --keep
```

- <https://github.com/headroomlabs-ai/headroom>
- <https://headroom-docs.vercel.app/docs>
- <https://pypi.org/project/headroom-ai/>

### Smoke fails at `readyz`

The plugin is installed; the proxy is not reachable. Start/configure the proxy, or set:

```bash
export HEADROOM_PROXY_URL="http://host:port"
```

### Is systemd required?

No. The bundled systemd template is Linux-only and optional.

### Are worker/background wrappers included?

Yes. `headroom-worker-lane`, `headroom-background-lane`, and `headroom-command-preflight` are packaged production wrappers for explicit operator commands. They retain exact stdout/stderr sidecars and exact `worker-final-packet.md`, then compress only eligible bulky intermediate traces through the configured loopback Headroom proxy. Oversized traces are bounded before proxy compression with deterministic head + query-matching lines + tail input (`--max-compress-chars`, default 250k), and the exact full raw sidecar remains the source of truth. They do not mutate Hermes provider/model routing. Natural `hr-*` smart-route aliases are not packaged behavior.
