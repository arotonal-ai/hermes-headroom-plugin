# Ports and native supervisor names

This document separates **portable defaults** from **instance-specific overrides**. The Hermes plugin does not open a listener: it is a client of the managed Headroom proxy. A healthy listener on the wrong endpoint is not evidence for the current Hermes instance.

## Canonical portable defaults

| Layer | Portable default | Authority |
|---|---|---|
| Plugin/runtime proxy URL | `http://127.0.0.1:8787` | `context_reduction.proxy_url` resolution and `runtime_manager.DEFAULT_PORT` |
| Managed profile | `hermes-plugin` | `runtime_manager.DEFAULT_PROFILE` |
| Runtime root | `${HERMES_HOME:-$HOME/.hermes}/runtimes/headroom/` | runtime manager |
| Linux user unit | `headroom-hermes-plugin.service` | upstream/native supervisor for profile `hermes-plugin` |
| macOS LaunchAgent label | `com.headroom.hermes-plugin` | upstream/native supervisor for profile `hermes-plugin` |
| Windows scheduled tasks | `headroom-hermes-plugin-startup` and `headroom-hermes-plugin-health` | managed native task contract |

`8787` is a default, not a globally reserved port. For concurrent Hermes instances under the same OS user, each runtime must use a distinct profile, runtime root, and free loopback port. Configure the plugin and manager with the same endpoint:

```bash
headroom-runtime setup --profile hermes-plugin-lab --port 18787 --json
export HEADROOM_PROXY_URL=http://127.0.0.1:18787
```

Prefer canonical Hermes YAML over a persistent environment override:

```yaml
context_reduction:
  proxy_url: http://127.0.0.1:18787
```

Start a fresh Hermes session or reload only the affected gateway after changing plugin configuration. Do not change model/provider routing to use the Headroom runtime manager; provider routing is a separate capability and gate.

## Active-instance authority

Do not infer the active endpoint from examples or old reports. Reconcile these read-only sources in order:

1. `hermes config get context_reduction.proxy_url`
2. `headroom-runtime status --json`
3. `headroom-runtime doctor --json`
4. native supervisor status for the exact profile/service
5. loopback listener ownership

The config endpoint and manager state must agree. `doctor` must report `RUNTIME_FULL_DURABLE`; another user's or profile's ready listener does not count.

## Legacy names and ports

| Legacy surface | Current status |
|---|---|
| `127.0.0.1:28787` | Retired integration-specific default from pre-v0.4 work. It may appear in historical changelog entries and inert test fixtures; do not use it for new installs. |
| `hermes-context-reduction.service` | Compatibility service name used by the old `scripts/install-production-runtime.py` path. New installs use `headroom-runtime` and the profile-derived native supervisor. |
| `scripts/install-production-runtime.py` | v0.4 compatibility/optional companion path only; not the clean-install production path. |
| Any other loopback port, including `28789` | Instance-specific override only. It has no portable meaning unless current Hermes config and manager state both select it. |

## Clean-instance acceptance

A clean install may use `8787` only if it is free. Otherwise select a distinct free loopback port and bind every check to that exact endpoint. PASS requires:

- plugin enabled in the target `HERMES_HOME`;
- plugin config and manager state agree on the endpoint;
- exact profile-derived supervisor is active;
- `headroom-runtime doctor --json` returns `RUNTIME_FULL_DURABLE`;
- compress → retrieve sentinel recovery passes;
- global/default model-provider routing remains unchanged.
