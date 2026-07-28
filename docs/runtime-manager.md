# Portable runtime manager

`headroom-runtime` is the explicit lifecycle command shipped with `hermes-headroom-plugin>=0.5.0`.

It installs the **official** `headroom-ai` distribution into an isolated venv, reuses Headroom's native manifest and supervisor implementation without activating provider/shell mutations, and verifies the Hermes `readyz -> compress -> retrieve` contract. Plugin registration never runs it.

## Decision: wrap upstream lifecycle, not `headroom deploy` or direct `install apply`

The v0.5 design was rechecked against the official PyPI `headroom-ai==0.32.1` sdist (`sha256:329dda3328f0fb45ec7128353f7fc9108f08e9676c9dc1873b4841c5c00c94bd`). `headroom/cli/install.py` is byte-identical (`sha256:ae59cdbc74de060b0d79eda1cf4725318615800487c72a51de6a79a18378843f`) to the previously reviewed upstream `v0.32.0` source at commit `438138832db97c4712d3c08197797f6bb64e68d9`; the other wrapped lifecycle functions are AST-equivalent.

| Upstream surface | Finding | Plugin decision |
|---|---|---|
| `headroom deploy` | Turnkey UX, but defaults to `providers=auto`; it may discover and mutate Codex, Claude, OpenClaw, or other provider configuration. Its Docker default also referenced a stale image namespace in 0.32.0. | Do not call it. |
| `headroom install apply` | `--providers manual` with no target yields an empty provider target set, but a real 0.32.0 user-scope canary still wrote persistent `HEADROOM_*` blocks to `.bashrc`, `.zshrc`, and `.profile`. | Do not call it directly. |
| Pinned 0.32.1 lifecycle APIs | `_build_deployment_manifest`, `install_supervisor`, `save_manifest`, and `_start_deployment` preserve upstream manifests and native supervisors without calling mutation activation. | Wrap narrowly; require `provider_mode=manual`, `targets=[]`, and `mutations=[]`, and fail closed if the pinned contract changes. |
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
- accepts only package-name/version specs (no URL, path, marker, or credentials) and installs `headroom-ai[proxy]==0.32.1` plus `litellm==1.91.3` from official PyPI with pip isolated mode;
- builds an upstream manifest with `provider_mode=manual`, **no provider targets**, and **no provider/shell mutations**;
- disables telemetry and code-aware optional dependencies;
- uses the memory CCR backend with a 1,800-second TTL by default;
- records no secrets in manager state;
- rejects filesystem, home, Hermes-home, shared-temp, shallow, non-directory, and non-empty unowned runtime roots;
- makes the runtime root private (`0700` best-effort), requires a private marker, and deletes only validated manager-owned top-level entries;
- on the certified Linux/macOS path, anchors recursive deletion to an open runtime-root descriptor and uses Python's symlink-attack-resistant `rmtree`; any unexpected entry, path swap, or deletion error fails closed;
- reports `RUNTIME_FULL_DURABLE` only after the saved upstream manifest matches the complete manager-owned identity/environment/proxy-argument contract, upstream status parses unambiguously as the expected profile/preset/runtime/supervisor/scope/port with `Status: running` and `Healthy: yes`, the expected native supervisor contract is exact, readiness succeeds, and compress → retrieve recovers the sentinel. On Windows this includes enabled startup/health tasks, exact actions/triggers, and the managed launcher hash.

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

A successful `doctor` returns exit code `0` and decision `RUNTIME_FULL_DURABLE`. Exit code `0` from upstream `install status` is necessary but insufficient: its lifecycle and identity fields are parsed and cross-checked before the durable decision is emitted.

## Platform lifecycle

| Platform | Default upstream preset | Scope |
|---|---|---|
| Linux | `persistent-service` | user systemd service |
| macOS | `persistent-service` | user LaunchAgent |
| Windows | `persistent-task` | user Task Scheduler |

The host must provide the corresponding native supervisor. Use `--preset persistent-task` as an explicit Linux/macOS fallback only when task scheduling is the intended lifecycle.

Native Windows setup creates a deterministic UTF-16 VBS launcher in the upstream profile root and runs it with `wscript.exe //B //NoLogo`. The launcher waits for the upstream `ensure-headroom.cmd` result while requesting a hidden window; both Task Scheduler actions, their boot/five-minute triggers, enabled state, and the launcher SHA-256 are recorded through upstream `ArtifactRecord.metadata`. The manager queries Task Scheduler XML and fails closed on action, trigger, enabled-state, artifact, or hash drift without returning raw XML.

Manager-owned Windows deployments created before this contract are not rewritten implicitly. Inspect the plan, then opt in explicitly:

```powershell
headroom-runtime reconcile --dry-run --json
headroom-runtime reconcile --apply --json
```

The first command is read-only and returns `MIGRATION_REQUIRED` when applicable. The plan reports manager-owned deployment identity and live-listener binding separately. `--apply` is authorized only for the bounded `windows_task_contract` scope when manager state, purge marker, complete non-supervisor manifest identity, runtime identity, native supervisor-name identity, and the exact managed action of both extant tasks all match. The accepted task action is either the current `wscript.exe` launcher contract or the known legacy direct `ensure-headroom.cmd` path, with no extra action or arguments. Foreign profiles, task actions, and listeners are never adopted.

`reconcile --dry-run --json` is a strict zero-write inventory: it does not acquire the transaction lock, append runtime logs, download packages, rewrite state/manifest files, connect to the listener, or touch the supervisor. The `headroom-reconcile-plan-v1` payload reads manager state, purge marker, runtime executables and pinned state versions, the complete manifest mismatch set, native supervisor identity and exact task-action booleans, and Windows OS socket/process tables independently. It deliberately does **not** call `/readyz` or `headroom install status`, because application-level discovery can append to proxy/access logs. The payload reports both probes as `not_probed_read_only`. `ownership.deployment` records the manager-controlled files and supervisor namespace; `ownership.listener_binding` independently records whether Windows preserved enough process evidence to bind the live listener. A base Python process shared by multiple venvs is never accepted as listener binding merely because `pyvenv.cfg` points to it. Process arguments are discarded before output. Unknown, inaccessible, multiple, non-loopback, or mismatched listener identity remains unproven and cannot authorize listener operations or adoption. A matching task name without an exact managed action cannot authorize task migration either.

When manager state is absent, the planner checks the default loopback port `8787`. Use `--probe-port <port>` to inventory a known non-default listener without scanning unrelated local ports. `--probe-port` is read-only discovery metadata and is rejected with `--apply`.

The read-only decision matrix is:

| Decision | Meaning | Mutation allowed by dry-run? |
|---|---|---:|
| `RECONCILIATION_NOT_REQUIRED` | Complete manager manifest and live supervisor contracts already match. | No |
| `MIGRATION_REQUIRED` | Positive manager deployment identity and both extant tasks have exact current-or-legacy managed actions; only the managed launcher, Scheduled Tasks, and `manifest.artifacts` task contract need migration. Listener binding is reported separately and is not directly mutated by this scope. | No; review the explicit `mutation_authority` before separate `--apply` |
| `REINSTALL_REQUIRED` | Positive manager deployment identity, but the manifest contains legacy environment-mutation history (optionally with the legacy task contract). The records are rollback history, not fields that may be deleted to force adoption. Listener binding may remain unproven. | No |
| `OWNERSHIP_AMBIGUOUS` / `RECONCILE_BLOCKED` | Manager identity is absent, foreign, incomplete, or has additional mismatches. | No |

For `REINSTALL_REQUIRED`, this release intentionally stops at a plan: `mutation_authority.eligible` is false, `--apply` refuses legacy mutation history, and an unproven listener binding stays explicit. Preserve manager state, the manifest, both Task Scheduler XML exports, and backups of every mutation target. Before any operation that may stop or replace the listener, establish listener binding independently; if that is impossible, preserve the existing deployment and use a separate clean runtime root, profile, and loopback port. Only after listener binding and an explicit target-host mutation gate may the pinned managed Headroom CLI's upstream `install remove --profile <profile>` path replay the recorded mutations symmetrically; verify that the listener and both tasks are absent; only then run clean `setup`. Do not edit or delete `mutations` manually. If any rollback step is inaccessible or cannot be reversed exactly, stop, restore the target/task backups, and retain the runtime for investigation.

`reconcile --apply` runs the same read-only planner before lock acquisition and repeats it after acquiring the manager transaction lock. It proceeds only when both plans grant the exact `windows_task_contract` scope, resource set, and `managed_task_action_identity` evidence token. A direct apply invocation cannot bypass classification, and any authority change between the two preflights fails closed before `_safe_reconcile` runs.

Apply also requires lossless XML snapshots of both existing managed tasks before
the first mutation. A missing, inaccessible, or transiently unqueryable task is
not inferred to be safely absent: reconciliation fails closed and does not alter
the launcher or either task. Use explicit uninstall/setup for an incomplete task
set after verifying ownership.

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
| `venv-0.32.1/` | isolated official runtime |
| `workspace/` | upstream deployment manifest/workspace |

On Windows, `workspace/deploy/<profile>/ensure-headroom-hidden.vbs` is an upstream-profile artifact governed by the manifest. It is removed with the profile; it is not a separate global launcher.

Upstream's deployment manifest remains the supervisor authority. The manager state only records how the plugin-owned runtime was created and located; current durability is derived from the manifest, parsed live lifecycle semantics, native supervisor evidence, and runtime probes. Existing state files remain readable and require no schema migration.

## Rollback

```bash
headroom-runtime uninstall --json
```

`uninstall` delegates to `headroom install remove`, waits for both the listener and native supervisor to disappear, validates every top-level runtime-root entry, and then deletes only the known manager-owned venv, workspace, state, marker, and logs. It intentionally accepts the complete base identity contract when a Windows launcher/task contract is legacy or drifted so rollback remains possible; provider targets, mutations, foreign identity, or unsafe paths still block upstream mutation. If upstream remove fails, the listener or supervisor remains present, an unexpected path is found, or a deletion race/error occurs, it returns `UNINSTALL_PARTIAL` and preserves the root for recovery.

Preserve the venv while removing deployment state:

```bash
headroom-runtime uninstall --keep-runtime --json
```

If state, the purge marker, or the saved manager-owned base manifest contract is missing or invalid, upstream remove and normal deletion are blocked. One narrow recovery is allowed after setup fails before writing a manifest: a `RUNTIME_PARTIAL` root may be removed without invoking upstream only when both listener and supervisor are absent and its entries match the managed-root deletion contract. Remove supervisor artifacts manually only after verifying the upstream profile and target paths.

## Known limits

- The manager requires Python 3.11+ and network access to official PyPI during first setup.
- A host without a usable native user supervisor cannot claim durable lifecycle from dry-run or unit tests alone.
- Headroom 0.32.1 does not expose JSON for `install status`, so the manager uses a version-pinned parser for its documented labels. Missing or duplicate fields, unknown lifecycle values, and identity mismatches fail closed to `RUNTIME_PARTIAL`; an upstream format change requires an explicit adapter update.
- Memory CCR markers intentionally do not survive runtime restart.
- WSL2 and Termux require target-host evidence; they are not implied by Linux CI.
- Provider-proxy routing remains outside this manager and outside the portable-core claim.
- Headroom 0.32.1 does not expose a public no-mutations apply flag. The manager therefore uses a narrow set of pinned private lifecycle APIs; version drift must fail closed in tests/canaries rather than silently falling back to direct `install apply`.
- Some Windows Defender signatures quarantine the base `ast-grep-cli` dependency on affected target hosts. The manager does not create exclusions or bypass detections; such an installation remains partial and rolls back fail-closed.
