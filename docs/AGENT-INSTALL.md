# Agent install brief

Use this when another Hermes/AI agent is given only this repository URL and asked to install it in a Hermes instance.

## Goal

Install and enable the Hermes Headroom plugin without exposing secrets, copying owner-local state, or changing global/default provider routing.

## Commands

```bash
hermes --version
git --version
hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
hermes plugins list --enabled --user --plain
hermes gateway restart || true
```

If operating inside an active Hermes chat instead of gateway shell, start a fresh session with `/new` after install.

## Verify

In Hermes:

```text
/headroom status
```

If a proxy is running:

```text
/headroom smoke
```

## Acceptance

PASS if:

- `hermes plugins list --enabled --user --plain` includes `headroom_retrieve`;
- `/headroom status` responds after restart/new session;
- no secrets are requested or printed;
- global/default provider routing is unchanged.

PARTIAL if:

- install succeeds but `/headroom smoke` fails because no Headroom proxy is running.

FULL if:

- install succeeds and `/headroom smoke` returns PASS with sentinel retrieval.

FAIL if:

- plugin is not listed as enabled;
- `/headroom` command is unavailable after a fresh session/restart;
- install required copying owner-local `~/.hermes` state.

## Analyze without installing

```bash
git clone https://github.com/arotonal-ai/hermes-headroom-plugin.git
cd hermes-headroom-plugin
scripts/audit-repo-readiness.sh
```

## Temp-home test when allowed

```bash
scripts/test-clean-hermes-install.sh --local
```

## Rollback

```bash
hermes plugins disable headroom_retrieve
hermes plugins remove headroom_retrieve
hermes gateway restart || true
```
