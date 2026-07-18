# Portable runtime manager

`headroom-runtime` is the explicit lifecycle command shipped with `hermes-headroom-plugin>=0.5.0`.

It installs the **official** `headroom-ai` distribution into an isolated venv, reuses Headroom's native manifest and supervisor implementation without activating provider/shell mutations, and verifies the Hermes `readyz -> compress -> retrieve` contract. Plugin registration never runs it.

## Decision: wrap upstream lifecycle, not `headroom deploy` or direct `install apply`

The v0.5 design was checked against upstream Headroom `v0.32.0` at commit `438138832db97c4712d3c08197797f6bb64e68d9`.

| Upstream surface | Finding | Plugin decision |
|---|---|---|
| `headroom deploy` | Turnkey UX, but defaults to `providers=auto`; it may discover and mutate Codex, Claude, OpenClaw, or other provider configuration. Its Docker default also referenced a stale image namespace in 0.32.0. | Do not call it. |
| `headroom install apply` | `--providers manual` with no target yields an empty provider target set, but a real 0.32.0 user-scope canary still wrote persistent `HEADROOM_*` blocks to `.bashrc`, `.zshrc`, and `.profile`. | Do not call it directly. |
| Pinned 0.32.0 lifecycle APIs | `_build_deployment_manifest`, `install_supervisor`, `save_manifest`, and `_start_deployment` preserve upstream manifests and native supervisors without calling mutation activation. | Wrap narrowly; require `provider_mode=manual`, `targets=[]`, and `mutations=[]`, and fail closed if the pinned contract changes. |
| `headroom install status/remove` | Owns manifest and supervisor lifecycle across systemd, launchd, Windows Service/Task Scheduler, and crontab adapters. | Delegate status and rollback; do not create a second supervisor implementation. |
| Headroom proxy | Owns `/readyz`, `/v1/compress`, `/v1/retrieve`, and CCR storage. | Verify through the plugin's real smoke. |

## Safety contract

The manager:

- is explicit: `setup` must be invoked by a human or agent;
- supports `setup --dry-run` with no writes or downloads;
- accepts only loopback `127.0.0.1`;
- rejects any existing TCP listener before lifecycle writes unless it is the fully verified managed deployment;
- rejects any upstream manifest or user-global supervisor whose profile has no matching manager-owned state;
- never replaces an existing deployment manifest implicitly; unhealthy deployments require explicit uninstall before repair;
- serializes each setup/uninstall transaction with a lock outside the removable runtime root;
- rejects corrupt manager state or any state path that points outside the selected runtime root;
- requires `uninstall` before changing an existing managed profile, port, preset, or package spec;
- strips inherited `HEADROOM_*` variables before adding the manager-controlled runtime environment;
- refuses a ready port when no matching manager state exists;
- accepts only package-name/version specs (no URL, path, marker, or credentials) and installs `headroom-ai[proxy]==0.32.0` plus `litellm==1.91.3` from official PyPI with pip isolated mode;
- builds an upstream manifest with `provider_mode=manual`, **no provider targets**, and **no provider/shell mutations**;
- disables telemetry and code-aware optional dependencies;
- uses the memory CCR backend with a 1,800-second TTL by default;
- records no secrets in manager state;
- rejects filesystem, home, Hermes-home, shared-temp, shallow, non-directory, and non-empty unowned runtime roots;
- requires a private marker and deletes only the validated manager-owned top-level entries; any unexpected path blocks purge;
- reports `RUNTIME_FULL_DURABLE` only after the saved upstream manifest matches the complete manager-owned identity/environment/proxy-argument contract, upstream status passes, readiness succeeds, and compress → retrieve recovers the sentinel.

It does **not** change Hermes model/provider routing, write persistent shell environment blocks, install API keys, enable a paid provider, or run from `register()`.

## Native Git install

```bash
PLUGIN_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve"
python3 "$PLUGIN_DIR/scripts/headroom-runtime.py" setup
```

Windows PowerShell:

```powershell
$PluginDir = if ($env:HERMES_HOME) { "$env:HERMES_HOME\plugins\headroom_retrieve" } else { "$HOME\.hermes\plugins\headroom_retrieve" }
py -3 "$PluginDir\scripts\headroom-runtime.py" setup
```

## Wheel install

The base wheel is sufficient: `setup` creates the isolated official runtime itself.

```bash
python3 -m pip install hermes-headroom-plugin
headroom-runtime setup
```

The optional `[proxy]` extra remains useful for developers who intentionally want the runtime packages in the same environment, but it is not required by the managed path.

## Human and agent flow

Inspect without mutation:

```bash
headroom-runtime setup --dry-run --json
```

Install and verify:

```bash
headroom-runtime setup --json
headroom-runtime status --json
headroom-runtime doctor --json
```

Use a non-default loopback port:

```bash
headroom-runtime setup --port 18787 --json
```

A successful `doctor` returns exit code `0` and decision `RUNTIME_FULL_DURABLE`.

## Platform lifecycle

| Platform | Default upstream preset | Scope |
|---|---|---|
| Linux | `persistent-service` | user systemd service |
| macOS | `persistent-service` | user LaunchAgent |
| Windows | `persistent-task` | user Task Scheduler |

The host must provide the corresponding native supervisor. Use `--preset persistent-task` as an explicit Linux/macOS fallback only when task scheduling is the intended lifecycle.

The default profile is `hermes-plugin` (native supervisor name `headroom-hermes-plugin`). Concurrent runtime roots on one user account must use distinct `--profile` values and distinct ports because native supervisor names are user-global; the manager will not adopt or replace another root's deployment.

## State and evidence

Default root:

```text
${HERMES_HOME:-$HOME/.hermes}/runtimes/headroom/
```

Key files:

| File | Role |
|---|---|
| `.hermes-headroom-runtime-manager` | purge-safety marker |
| `manager-state.json` | non-secret manager state and exact specs |
| `install.log` | private pip/safe upstream lifecycle evidence |
| `manager.log` | private status/remove evidence |
| `venv-0.32.0/` | isolated official runtime |
| `workspace/` | upstream deployment manifest/workspace |

Upstream's deployment manifest remains the supervisor authority. The manager state only records how the plugin-owned runtime was created and located.

## Rollback

```bash
headroom-runtime uninstall --json
```

`uninstall` delegates to `headroom install remove`, waits for both the listener and native supervisor to disappear, validates every top-level runtime-root entry, and then deletes only the known manager-owned venv, workspace, state, marker, and logs. If upstream remove fails, the listener or supervisor remains present, or an unexpected path is found, it returns `UNINSTALL_PARTIAL` and preserves the root for recovery.

Preserve the venv while removing deployment state:

```bash
headroom-runtime uninstall --keep-runtime --json
```

If state, the purge marker, or the saved complete manager-owned manifest contract is missing or invalid, upstream remove and normal deletion are blocked. One narrow recovery is allowed after setup fails before writing a manifest: a `RUNTIME_PARTIAL` root may be removed without invoking upstream only when both listener and supervisor are absent and its entries match the managed-root deletion contract. Remove supervisor artifacts manually only after verifying the upstream profile and target paths.

## Known limits

- The manager requires Python 3.11+ and network access to official PyPI during first setup.
- A host without a usable native user supervisor cannot claim durable lifecycle from dry-run or unit tests alone.
- Memory CCR markers intentionally do not survive runtime restart.
- WSL2 and Termux require target-host evidence; they are not implied by Linux CI.
- Provider-proxy routing remains outside this manager and outside the portable-core claim.
- Headroom 0.32.0 does not expose a public no-mutations apply flag. The manager therefore uses a narrow set of pinned private lifecycle APIs; version drift must fail closed in tests/canaries rather than silently falling back to direct `install apply`.
