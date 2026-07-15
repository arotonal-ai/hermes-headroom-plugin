# Portable Tool Core

This document is the canonical install/runtime contract for the portable Headroom integration.

## Stable architecture

```text
Hermes tool result
  -> plugin admission + redaction policy
  -> loopback Headroom /v1/compress
  -> compressed intermediate + CCR marker
  -> model

CCR marker
  -> headroom_retrieve
  -> loopback Headroom /v1/retrieve
  -> exact cached source while the marker is live
```

The primary model/provider route stays direct. Provider-proxy routing is experimental and must not be enabled by this installer.

## Reproducible defaults

| Setting | Default |
|---|---|
| Plugin | `hermes-headroom-plugin==0.3.17` |
| Headroom runtime | `headroom-ai[proxy]==0.31.0` |
| Runtime venv | `~/.cache/hermes-headroom-venv-0.31.0` |
| Bind | `127.0.0.1:28787` |
| CCR backend | `memory` |
| CCR TTL | `1800` seconds |
| Report retention | `14` days |
| Report soft size threshold | `268435456` bytes |
| Report prune interval | `3600` seconds |
| Visible final marker | off |
| First-turn availability hint | off |
| `llm-monitor` companion | not installed unless explicitly requested |

Environment variables or `context_reduction` configuration may override report retention. Runtime overrides must be explicit installer arguments or environment variables and recorded in deployment evidence.

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

1. Restore the previous user-unit file.
2. Run `systemctl --user daemon-reload` and restart only `hermes-context-reduction.service`.
3. Switch the plugin repo to the previously recorded commit.
4. If SQLite recovery is required, reinstall with `--ccr-backend sqlite`; accept renewed disk-persistence risk explicitly.

No Hermes model/provider route or global provider configuration should need rollback because the portable core does not mutate them.
