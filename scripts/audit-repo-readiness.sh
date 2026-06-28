#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

required=(
  README.md
  INSTALL.md
  AGENTS.md
  SECURITY.md
  PRIVACY.md
  ACKNOWLEDGEMENTS.md
  docs/AGENT-INSTALL.md
  plugin.yaml
  __init__.py
  pyproject.toml
  src/hermes_headroom_plugin/__init__.py
  scripts/install-hermes-plugin.sh
  scripts/verify-hermes-install.sh
  scripts/test-clean-hermes-install.sh
)

for f in "${required[@]}"; do
  [[ -f "$f" ]] || fail "missing required file $f"
done
pass "required files present"

python3 - <<'PY'
from pathlib import Path
import ast, re, sys
root = Path('.')
py_files = [Path('__init__.py')] + sorted(Path('src').rglob('*.py')) + sorted(Path('tests').rglob('*.py'))
for p in py_files:
    ast.parse(p.read_text(encoding='utf-8'), filename=str(p))
print(f"PASS: python syntax ok ({len(py_files)} files)")
required_text = {
    'README.md': ['hermes plugins install arotonal-ai/hermes-headroom-plugin --enable', '/headroom status', 'INSTALL_PASS', 'RUNTIME_PARTIAL', 'RUNTIME_FULL', 'chopratejas/headroom'],
    'INSTALL.md': ['Acceptance matrix', 'No API keys are required', 'scripts/test-clean-hermes-install.sh --local'],
    'AGENTS.md': ['Do not copy another machine', 'Acceptance states', 'headroom_retrieve', 'upstream Headroom'],
    'docs/AGENT-INSTALL.md': ['PASS if', 'PARTIAL if', 'FAIL if'],
    'ACKNOWLEDGEMENTS.md': ['chopratejas/headroom', 'headroom-ai', 'Hermes Agent integration layer'],
}
for rel, needles in required_text.items():
    text = Path(rel).read_text(encoding='utf-8')
    missing = [n for n in needles if n not in text]
    if missing:
        raise SystemExit(f"FAIL: {rel} missing required text: {missing}")
print('PASS: required documentation text present')
missing_links = []
for rel in ['README.md', 'INSTALL.md', 'AGENTS.md', 'SECURITY.md', 'PRIVACY.md', 'ACKNOWLEDGEMENTS.md', 'docs/AGENT-INSTALL.md']:
    path = Path(rel)
    text = path.read_text(encoding='utf-8')
    for target in re.findall(r'\[[^\]]+\]\(([^)]+)\)', text):
        if target.startswith(('http://','https://','#','mailto:')):
            continue
        target_path = (path.parent / target.split('#', 1)[0]).resolve()
        if target and not target_path.exists():
            missing_links.append((rel, target))
if missing_links:
    raise SystemExit(f"FAIL: markdown links missing: {missing_links}")
print('PASS: markdown local links ok')
PY

bash -n scripts/*.sh
pass "shell syntax ok"

python3 - <<'PY'
from pathlib import Path
import os, re, sys
skip = {'.git','__pycache__','.pytest_cache','.mypy_cache','.venv','build','dist'}
patterns = [
    re.compile(r'gho_[A-Za-z0-9_]{20,}'),
    re.compile(r'github_pat_[A-Za-z0-9_]{20,}'),
    re.compile(r'sk-[A-Za-z0-9]{20,}'),
    re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC |DSA |)PRIVATE KEY-----'),
]
hits = []
count = 0
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in skip and not d.endswith('.egg-info')]
    for name in files:
        p = Path(root) / name
        if p.suffix in {'.pyc','.pyo'}:
            continue
        count += 1
        try:
            text = p.read_text(errors='ignore')
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for pat in patterns:
                if pat.search(line):
                    hits.append((str(p), i, pat.pattern))
if hits:
    print('FAIL: potential secrets found:')
    for hit in hits:
        print(hit)
    sys.exit(1)
print(f'PASS: basic secret pattern scan ok ({count} files)')
PY

if command -v git >/dev/null 2>&1; then
  git ls-remote https://github.com/arotonal-ai/hermes-headroom-plugin.git HEAD >/dev/null
  pass "remote GitHub HEAD reachable"
fi

pass "repo readiness audit complete"
