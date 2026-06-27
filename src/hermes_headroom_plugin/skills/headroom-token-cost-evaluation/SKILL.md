---
name: headroom-token-cost-evaluation
description: Use when operating the installable Headroom plugin: retrieval, safe compression admission, health checks, and exact-source verification.
version: 0.1.0
author: Hermes Headroom contributors
license: MIT
metadata:
  hermes:
    tags: [headroom, context-reduction, plugin]
---

# Headroom plugin operations

## Contract

Use Headroom only for eligible bulky intermediate/diagnostic material with retained exact sources. Keep final, edit-critical, sensitive, profile/memory/system/developer, patches, manifests, hashes, claim ledgers, and final packets exact or blocked.

## Quick checks

```bash
headroom-health-audit --json
headroom-proxy-start status
```

## Verification

- Retrieval marker resolves before trusting compressed summaries.
- Exact sidecar exists for important claims.
- Proxy-down failure falls back to exact output.
