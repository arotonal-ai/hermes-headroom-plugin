# Portable Tool Core

This document is the canonical install/runtime contract for the portable Headroom integration.

## Stable architecture

```text
Hermes tool result
  -> plugin admission + redaction policy
  -> loopback Headroom /v1/compress
  -> compressed intermediate + CCR marker
  -> model

Hermes common LLM request boundary (explicit opt-in)
  -> protocol adapter selects tool-result text only
  -> same admission + redaction policy
  -> loopback Headroom /v1/compress
  -> original provider/model transport remains direct

CCR marker
  -> headroom_retrieve
  -> loopback Headroom /v1/retrieve
  -> complete exact cached source while the marker is live
```

Retrieval is hash-only. The plugin sends exactly `{"hash": "<ccr-hash>"}` and does not expose a provider-side focus parameter. Any future bounded slicing must be a separate deterministic capability and must not weaken the exact-retrieval contract.

The primary model/provider route stays direct. Provider-proxy routing is experimental and must not be enabled by this installer. `context_reduction.llm_request_middleware` is a separate opt-in safety net for eligible legacy/bypassed tool results at Hermes's native post-build/pre-transport middleware boundary; it does not rewrite routing, auth, headers, tools, tool arguments, system/user prompts, signatures, images, or streaming controls.

## Runtime module boundaries

The v0.4 middleware implementation follows one-way dependencies and keeps the
legacy import path as a compatibility facade:

```text
Hermes registration
  -> middleware.py compatibility facade
  -> middleware_tool.py / middleware_request.py
  -> reduction.py + policy.py + observability.py
  -> provider_headroom.py (current ReductionProvider implementation)
  -> contracts.py + proxy.py loopback HTTP transport
```

| Module | Primary authority |
|---|---|
| `config.py` | typed effective configuration and legacy-alias resolution |
| `policy.py` | exact/compressible/blocked admission and redaction rules |
| `observability.py` | local reports, event attribution, platform context and bounded retention |
| `reduction.py` | common reduction orchestration and compatibility output construction; v0.4 composes the current Headroom adapter directly |
| `middleware_tool.py` | Hermes `tool_execution` adapter and fail-open boundary |
| `middleware_request.py` | opt-in, copy-on-write protocol request adapters and logical-source cache |
| `contracts.py` | provider-neutral compression, retrieval and health contracts |
| `provider_headroom.py` | Headroom 0.31 response adapter |
| `proxy.py` | direct loopback HTTP transport only |
| `middleware.py` | import/call compatibility; not a dependency-injection or monkeypatch boundary |

Implementation modules must not import `middleware.py`; dependencies flow toward
policy/contracts/transport. Existing callers may continue importing the hooks and
legacy helpers from `middleware.py`, but tests and extensions that patch internals
must patch the module that owns the dependency.

The contracts are provider-neutral, but runtime provider selection is not yet
provider-neutral: `reduction.py` composes `HeadroomReductionProvider` directly.
That is an explicit v0.4 limit, not evidence of a second supported reducer.
Do not rename the product or claim provider-neutral runtime selection until a
second provider and an injection/selection gate are implemented and tested.

## Reproducible defaults

| Setting | Default |
|---|---|
| Plugin | `hermes-headroom-plugin==0.4.1` |
| LLM request middleware | off; explicit `mode: tool_results` opt-in |
| Headroom runtime | `headroom-ai[proxy]==0.31.0` |
| LiteLLM transitive runtime | `litellm==1.91.3` (portable wheel constraint) |
| Runtime venv | `~/.cache/hermes-headroom-venv-0.31.0` |
| Bind | `127.0.0.1:8787` |
| CCR backend | `memory` |
| CCR TTL | `1800` seconds |
| Report retention | `14` days |
| Report soft size threshold | `268435456` bytes |
| Report prune interval | `3600` seconds |
| Visible final marker | off |
| First-turn availability hint | off |
| `llm-monitor` companion | not installed unless explicitly requested |

Core plugin configuration resolves once into a typed effective contract with this precedence: explicit function override, environment, `context_reduction` YAML, then portable defaults. Legacy `host`/`port`, `auto_compress`, `auto_terminal`, `compression_mode`, and `events_max_bytes` aliases are accepted only at that resolver boundary and are reported by `/headroom status`; `HEADROOM_HOST`/`HEADROOM_PORT` remain deprecated endpoint shims. Environment variables or `context_reduction` configuration may override report retention. Runtime overrides must be explicit installer arguments or environment variables and recorded in deployment evidence. The complete mapping is in the README configuration matrix.

Port `8787` is the canonical portable/default-port contract. A canary that claims to verify this default must first prove the port is unoccupied and then bind the resulting proxy process to that run. Concurrent same-host canaries are a separate contract: they must lease distinct free loopback ports and pass matching explicit endpoint overrides. Reusing a ready proxy owned by another user or instance is not isolation evidence.

## Attribution invariant

- `tool_execution` is the primary compression and savings-attribution surface.
- `llm_request` is off by default. When enabled, it only transforms eligible tool-result text that did not already carry a Headroom marker from `tool_execution`.
- A marker produced by `tool_execution` is recognized as already compressed at the request boundary, so it is neither recompressed nor emitted as a second new-savings event.
- Repeated request-boundary transforms use a logical-source fingerprint scoped by session, tool call, protocol family, tool name, and source digest. Plugin cache-reuse events set `new_savings_event=false`; downstream `llm-monitor` marker correlations independently set `counts_as_new_savings=false`.
- A bounded five-minute cross-surface negative-outcome cache suppresses repeated provider work when an unchanged logical source already produced `compression_not_useful`. The first skipped event and exact sidecar remain authoritative; cache hits emit no duplicate report/event. Provider errors and runtime-unavailable outcomes are never negative-cached, so fail-open recovery remains retryable.
- Savings totals must count only rows explicitly marked as new savings; retained correlations, experimental aggregates, and legacy internal-service token counters use separate scopes.

## Storage contract

- The memory CCR backend keeps exact recoverable source only in the Headroom process and loses markers on service restart. This is the privacy-preserving portable default.
- `--ccr-backend sqlite` is an explicit restart-survival tradeoff. Treat its database and WAL as sensitive local state; logical TTL is not secure physical erasure.
- Middleware artifacts under `$HERMES_HOME/control-plane/headroom/reports` are disposable, redacted observability data, not canonical evidence.
- Pruning is opportunistic before report writes, fail-open, and bounded by age then total bytes. The byte threshold is soft: one write may exceed it until the next configured prune cycle. It does not create a timer or watcher.
- A report group is protected when its name starts with `PINNED-` or it has a sibling `.keep`/`.retain` marker.
- Evidence that must survive retention should be promoted to a project run/closeout outside the reports directory.

## Install

Linux durable runtime:

```bash
python scripts/install-production-runtime.py --systemd-user
```

The installer writes a `0600` user unit, reloads systemd, enables and restarts the service, waits for `/readyz`, and runs real compress -> retrieve smoke. Claim `RUNTIME_FULL_DURABLE` only when the service is enabled and active and the smoke sentinel is recovered.

Optional companion:

```bash
python scripts/install-production-runtime.py --companion-only
# or install it together with the runtime:
python scripts/install-production-runtime.py --with-llm-monitor-companion
```

Explicit SQLite tradeoff:

```bash
python scripts/install-production-runtime.py --systemd-user --ccr-backend sqlite
```

## Verification

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src tests scripts
bash -n scripts/*.sh
bash scripts/audit-repo-readiness.sh
python scripts/test-headroom-runtime-smoke.py --spec 'headroom-ai[proxy]==0.31.0'
```

On Linux durable deployments additionally verify:

```bash
systemctl --user is-enabled hermes-context-reduction.service
systemctl --user is-active hermes-context-reduction.service
systemctl --user show hermes-context-reduction.service -p ExecStart -p Environment
```

## Rollback

1. Disable or remove `headroom_retrieve` with Hermes, then reload only the affected Hermes session/gateway when required.
2. If a durable runtime was installed, stop and disable `hermes-context-reduction.service`; restore or remove only its recorded user-unit path, then run `systemctl --user daemon-reload`.
3. Switch the plugin repo to the previously recorded commit or restore the recorded plugin snapshot.
4. Keep or delete the versioned runtime venv only after confirming no other deployment references it. Reinstall the prior pinned runtime spec when rollback requires runtime parity.
5. If SQLite recovery is required, restore the approved database separately or reinstall with `--ccr-backend sqlite`; accept renewed disk-persistence risk explicitly. Memory-backend CCR entries are intentionally not restart-recoverable.
6. Re-run plugin load/status and, if runtime remains enabled, compress → retrieve smoke. Do not call rollback complete from service state alone.

No Hermes model/provider route or global provider configuration should need rollback because the portable core does not mutate them.
