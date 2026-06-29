# Security Policy

## Supported status

This repository is in stable public-plugin hardening. Supported releases start at `v0.1.0`; inspect GitHub Releases and `pyproject.toml` for the current package version.

## Reporting vulnerabilities

Open a private report through GitHub Security Advisories if available, or contact the repository owner directly. Do **not** paste live secrets, tokens, cookies, private logs, memory/profile files, or protected context into public issues.

## Secret handling

- No API key or token is required to install this plugin.
- `.env`, logs, caches, build outputs, bytecode, and virtualenvs are ignored.
- Do not commit secrets or owner-local Hermes state.
- Treat any accidental credential as compromised; rotate it outside this repo and remove it from git history before publishing.

## Safe-by-default scope

The first-install path must not:

- copy a user's `~/.hermes` state from another machine;
- change global/default provider routing;
- enable external telemetry;
- compress secrets, profile/memory/system/developer instructions, patches, diffs, manifests, hashes, claim ledgers, final packets, or protected content.

## Verification expected before release/push

```bash
scripts/audit-repo-readiness.sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m py_compile $(find src tests -name '*.py' | sort)
bash -n scripts/*.sh
```

If `gitleaks` is available:

```bash
gitleaks detect --source . --redact --verbose --exit-code 7
```
