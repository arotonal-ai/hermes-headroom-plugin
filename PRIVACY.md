# Privacy Posture

Hermes Headroom Plugin is designed as a local/context-reduction control surface for Hermes.

## Defaults

- No external telemetry is enabled by this plugin.
- No global/default provider routing is enabled by installation.
- No owner sessions, memories, profile state, project evidence, or protected context are packaged.
- Plugin install does not require API keys.

## What may be reduced later

Only eligible bulky intermediate/diagnostic traces should be compressed, and only when the Headroom proxy/runtime is explicitly available and healthy.

Examples of eligible classes:

- raw logs;
- worker traces;
- browser/debug traces;
- large diagnostic intermediates;
- retained, retrievable source material.

## What must remain exact or blocked

- final packets and final deliverables;
- patches/diffs/write-critical context;
- manifests, hashes, claim ledgers;
- secrets, tokens, credentials, private keys;
- memory/profile/system/developer instructions;
- protected or contaminated context.

## Local proxy note

`/headroom status` and `/headroom smoke` talk to a configured/local Headroom proxy endpoint such as `http://127.0.0.1:28787` or `HEADROOM_PROXY_URL`. If no proxy is running, the plugin should degrade to a partial runtime state rather than crashing.
