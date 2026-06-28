# Acknowledgements

Hermes Headroom Plugin exists because of the upstream Headroom project and its public work on reversible/context-aware reduction for LLM applications.

## Upstream Headroom

- Original/open-source project: [chopratejas/headroom](https://github.com/chopratejas/headroom)
- Documentation site: [headroomlabs-ai.github.io/headroom](https://headroomlabs-ai.github.io/headroom/)
- Python package: [`headroom-ai` on PyPI](https://pypi.org/project/headroom-ai/)

Thanks to the Headroom maintainers and contributors for publishing the underlying ideas and package surface that make this Hermes integration possible.

## What this repository adds

This repository is a Hermes Agent integration layer. It adds:

- native Hermes plugin installation via `hermes plugins install`;
- `headroom_retrieve` tool registration;
- `/headroom status`, `/headroom smoke`, and `/headroom audit` commands;
- conservative exact/compress/blocked policy defaults;
- agent/human installation docs and clean-home test harnesses.

## Positioning

This plugin should amplify, not obscure, upstream Headroom momentum:

- link back to upstream for the broader Headroom project and backend/proxy evolution;
- keep Hermes-specific installation friction low;
- avoid copying owner-local Hermes state;
- avoid presenting this integration as the canonical Headroom project.
