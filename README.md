# Hermes Headroom Plugin

Starter repository for converting the owner-local Headroom / Context Reduction Layer into an installable Hermes plugin.

## Status

This repo is intentionally **P0/P1 scaffolding**, not a full migration of the owner control plane. It is safe to iterate locally and is designed to support both:

1. pip/entry-point installation via `hermes_agent.plugins`;
2. direct directory-plugin testing via the top-level `plugin.yaml` + `__init__.py` shim.

## Architecture stance

```text
Headroom = bounded context-reduction for eligible intermediates
not = global/default provider proxy routing
```

Safe defaults:

- plugin disabled until explicitly enabled in Hermes;
- telemetry off by default;
- no global/default provider routing;
- final/edit-critical/sensitive content remains exact or blocked;
- retrieval tool name stays `headroom_retrieve` for compatibility.

## P0 target

- `headroom_retrieve` tool;
- `/headroom status|smoke|audit` command family;
- proxy endpoint resolver;
- policy module for exact/compress/blocked classes;
- tests that run without a live Hermes process.

## Local verification

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
python -m py_compile $(find src -name '*.py')
```

## Hermes install sketch

Pip/editable path:

```bash
pip install -e .
hermes plugins enable headroom_retrieve
# start a fresh session or restart gateway so plugin discovery reruns
```

Directory-plugin path for local development:

```bash
ln -s "$PWD" ~/.hermes/plugins/headroom_retrieve
hermes plugins enable headroom_retrieve
```

## Non-goals in this repo stage

- No default/global provider proxy route.
- No owner profile/session/memory/project evidence copied into package payload.
- No external telemetry.
- No compression of patches, diffs, manifests, hashes, claim ledgers, final packets, secrets, memory/profile/system/developer instructions, or protected content.
