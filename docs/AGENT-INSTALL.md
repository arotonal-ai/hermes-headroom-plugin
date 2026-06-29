# Agent install brief

Use this when another Hermes/AI agent is given only this repository URL and asked to install it in a Hermes instance.

## Goal

Install and enable the Hermes Headroom plugin without exposing secrets, copying owner-local state, or changing global/default provider routing.

## Platform note

Linux is the primary tested path. macOS/WSL are expected when Hermes, git, and Python are available. Native Windows should use native Hermes commands and Python helper scripts; Bash helpers require Git Bash/WSL.

## Commands

Plugin install on the target Hermes instance:

```bash
hermes --version
git --version
hermes plugins install arotonal-ai/hermes-headroom-plugin --enable
hermes plugins list --enabled --user --plain
hermes gateway restart || true
```

If operating inside an active Hermes chat instead of gateway shell, start a fresh session with `/new` after install.

Optional full Headroom runtime on Unix/macOS/WSL:

```bash
python3 -m venv ~/.cache/hermes-headroom-venv
~/.cache/hermes-headroom-venv/bin/python -m pip install --upgrade pip
~/.cache/hermes-headroom-venv/bin/python -m pip install 'headroom-ai[proxy]>=0.26,<0.27'
~/.cache/hermes-headroom-venv/bin/headroom proxy --host 127.0.0.1 --port 28787
```

Optional full Headroom runtime on Windows PowerShell:

```powershell
py -m venv $env:USERPROFILE\.cache\hermes-headroom-venv
& $env:USERPROFILE\.cache\hermes-headroom-venv\Scripts\python.exe -m pip install --upgrade pip
& $env:USERPROFILE\.cache\hermes-headroom-venv\Scripts\python.exe -m pip install 'headroom-ai[proxy]>=0.26,<0.27'
& $env:USERPROFILE\.cache\hermes-headroom-venv\Scripts\headroom.exe proxy --host 127.0.0.1 --port 28787
```

## Verify

In Hermes:

```text
/headroom status
```

If full runtime/proxy validation is requested, verify the upstream Headroom dependency without touching the real environment:

```bash
python scripts/test-headroom-dependency-install.py
# Unix wrapper:
scripts/test-headroom-dependency-install.sh
# or, after native Hermes install:
"${HERMES_HOME:-$HOME/.hermes}/plugins/headroom_retrieve/scripts/test-headroom-dependency-install.sh"
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

- `scripts/test-headroom-dependency-install.sh` or the Python equivalent passes for `headroom-ai[proxy]>=0.26,<0.27`;
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

## Metrics

Weekly savings tables must be generated from retained JSONL evidence:

```bash
python scripts/generate-weekly-savings-table.py --input docs/metrics/data/*.jsonl --write docs/metrics/weekly-savings.md
```

## Rollback

```bash
hermes plugins disable headroom_retrieve
hermes plugins remove headroom_retrieve
hermes gateway restart || true
```
