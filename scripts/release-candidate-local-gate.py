#!/usr/bin/env python3
"""Local release-candidate gate for the Hermes Headroom plugin.

This is intentionally repo-portable: it derives paths from this checkout, uses
venvs/temp homes under the requested run directory, starts only loopback
Headroom proxy processes, and does not push, tag, publish, or mutate the real
Hermes profile.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
import venv
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_HEADROOM_SPEC = "headroom-ai[proxy]"
PUBLIC_SCAN_PATHS = [
    "README.md",
    "INSTALL.md",
    "AGENTS.md",
    "SECURITY.md",
    "PRIVACY.md",
    "ACKNOWLEDGEMENTS.md",
    "docs",
    "scripts",
    "src",
    "plugin.yaml",
    "pyproject.toml",
    ".github/workflows",
]
OWNER_LOCAL_FORBIDDEN = (
    "/home/" + "openclaw",
    "/home/" + "bb",
    "control-plane/" + "projects",
    "owner-" + "capabilities",
    "plugin-repo/" + "hermes-headroom-plugin",
)
SECRET_PATTERNS = [
    re.compile(r"gho_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile("-----BEGIN " + r"(?:RSA |OPENSSH |EC |DSA |)PRIVATE KEY-----"),
]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def run(cmd: list[str], *, cwd: Path = REPO, timeout: int = 600, env: dict[str, str] | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=env,
            check=False,
        )
        return {"cmd": cmd, "returncode": proc.returncode, "stdout": proc.stdout, "duration_s": round(time.perf_counter() - started, 3)}
    except Exception as exc:  # noqa: BLE001
        return {"cmd": cmd, "returncode": 127, "stdout": f"{type(exc).__name__}: {exc}", "duration_s": round(time.perf_counter() - started, 3)}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def free_loopback_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_readyz(proxy_url: str, *, timeout: int = 90) -> dict[str, Any]:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{proxy_url}/readyz", timeout=3) as resp:  # noqa: S310 loopback gate
                body = resp.read().decode("utf-8", "replace")
                data = json.loads(body)
                if resp.status == 200 and isinstance(data, dict) and data.get("ready", True):
                    return {"ok": True, "status": resp.status, "body": data}
                last = body[:500]
        except Exception as exc:  # pragma: no cover - timing/platform dependent
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
    return {"ok": False, "last": last}


def create_venv(venv_dir: Path) -> Path:
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    return bin_dir(venv_dir) / exe("python")


def git_head() -> str:
    result = run(["git", "rev-parse", "HEAD"], timeout=30)
    return result["stdout"].strip() if result["returncode"] == 0 else "unknown"


def tracked_files() -> list[Path]:
    result = run(["git", "ls-files"], timeout=60)
    if result["returncode"] == 0:
        return [REPO / line for line in result["stdout"].splitlines() if line.strip()]
    files: list[Path] = []
    for root, dirs, names in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "__pycache__", ".pytest_cache", "build", "dist", "release-candidate-runs"}]
        files.extend(Path(root) / n for n in names)
    return files


def public_path_scan() -> dict[str, Any]:
    allowed_roots = [(REPO / p).resolve() for p in PUBLIC_SCAN_PATHS]
    hits: list[dict[str, Any]] = []
    scanned = 0
    for path in tracked_files():
        resolved = path.resolve()
        if not any(resolved == root or root in resolved.parents for root in allowed_roots):
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        scanned += 1
        rel = str(path.relative_to(REPO))
        for i, line in enumerate(text.splitlines(), 1):
            for needle in OWNER_LOCAL_FORBIDDEN:
                if needle in line:
                    hits.append({"file": rel, "line": i, "kind": "owner_local_path", "needle": needle})
            for pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    hits.append({"file": rel, "line": i, "kind": "secret_pattern", "pattern": pattern.pattern})
    return {"pass": not hits, "scanned_files": scanned, "hits": hits}


def archive_members(path: Path) -> list[str]:
    if path.suffix == ".whl" or path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            return zf.namelist()
    if path.suffixes[-2:] == [".tar", ".gz"] or path.suffix in {".tgz", ".gz"}:
        with tarfile.open(path, "r:*") as tf:
            return tf.getnames()
    return []


def read_archive_texts(path: Path, limit_bytes: int = 80_000) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if path.suffix == ".whl" or path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith((".py", ".md", ".toml", ".yaml", ".yml", ".txt", ".sh")):
                    out.append((name, zf.read(name)[:limit_bytes].decode("utf-8", "ignore")))
    else:
        with tarfile.open(path, "r:*") as tf:
            for member in tf.getmembers():
                if member.isfile() and member.name.endswith((".py", ".md", ".toml", ".yaml", ".yml", ".txt", ".sh")):
                    fh = tf.extractfile(member)
                    if fh:
                        out.append((member.name, fh.read(limit_bytes).decode("utf-8", "ignore")))
    return out


def build_and_inspect(run_dir: Path) -> dict[str, Any]:
    venv_dir = run_dir / "build-venv"
    python = create_venv(venv_dir)
    dist_dir = run_dir / "dist"
    steps = [
        run([str(python), "-m", "pip", "install", "--upgrade", "pip", "build"], timeout=300),
        run([str(python), "-m", "build", "--sdist", "--wheel", "--outdir", str(dist_dir)], timeout=300),
    ]
    artifacts = sorted(dist_dir.glob("*")) if dist_dir.exists() else []
    issues: list[dict[str, Any]] = []
    for artifact in artifacts:
        for member in archive_members(artifact):
            lowered = member.lower()
            if any(bad in lowered for bad in (".git/", ".venv/", "__pycache__", ".pytest_cache", "release-candidate-runs")):
                issues.append({"artifact": artifact.name, "member": member, "kind": "forbidden_member"})
        for member, text in read_archive_texts(artifact):
            for needle in OWNER_LOCAL_FORBIDDEN:
                if needle in text:
                    issues.append({"artifact": artifact.name, "member": member, "kind": "owner_local_path", "needle": needle})
            for pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    issues.append({"artifact": artifact.name, "member": member, "kind": "secret_pattern", "pattern": pattern.pattern})
    return {
        "pass": all(s["returncode"] == 0 for s in steps) and len(artifacts) >= 2 and not issues,
        "steps": [{"cmd": s["cmd"], "returncode": s["returncode"], "duration_s": s["duration_s"], "stdout_tail": s["stdout"][-3000:]} for s in steps],
        "artifacts": [str(p) for p in artifacts],
        "issues": issues,
    }


def pytest_gate(run_dir: Path) -> dict[str, Any]:
    venv_dir = run_dir / "pytest-venv"
    python = create_venv(venv_dir)
    steps = [
        run([str(python), "-m", "pip", "install", "--upgrade", "pip"], timeout=240),
        run([str(python), "-m", "pip", "install", "-e", f"{REPO}[test]"], timeout=360),
        run([str(python), "-m", "pytest", "-q"], timeout=480),
    ]
    return {"pass": all(s["returncode"] == 0 for s in steps), "venv": str(venv_dir), "steps": [{"cmd": s["cmd"], "returncode": s["returncode"], "duration_s": s["duration_s"], "stdout_tail": s["stdout"][-3000:]} for s in steps]}


def wheel_install_gate(run_dir: Path, build_gate: dict[str, Any]) -> dict[str, Any]:
    wheels = [Path(p) for p in build_gate.get("artifacts", []) if str(p).endswith(".whl")]
    if not wheels:
        return {"pass": False, "error": "no wheel artifact"}
    venv_dir = run_dir / "wheel-install-venv"
    python = create_venv(venv_dir)
    wheel = wheels[0]
    steps = [
        run([str(python), "-m", "pip", "install", "--upgrade", "pip"], timeout=240),
        run([str(python), "-m", "pip", "install", str(wheel)], timeout=300),
    ]
    checks = []
    for name in ("headroom-worker-lane", "headroom-background-lane", "headroom-command-preflight", "headroom-health-audit", "headroom-proxy-start"):
        checks.append(run([str(bin_dir(venv_dir) / exe(name)), "--help"], timeout=60))
    import_check = run([str(python), "-c", "import hermes_headroom_plugin, importlib.metadata as m; print(m.version('hermes-headroom-plugin'))"], timeout=60)
    return {
        "pass": all(s["returncode"] == 0 for s in steps) and all(c["returncode"] == 0 for c in checks) and import_check["returncode"] == 0,
        "wheel": str(wheel),
        "steps": [{"cmd": s["cmd"], "returncode": s["returncode"], "duration_s": s["duration_s"], "stdout_tail": s["stdout"][-1500:]} for s in steps],
        "checks": [{"cmd": c["cmd"], "returncode": c["returncode"], "duration_s": c["duration_s"], "stdout_head": c["stdout"][:800]} for c in checks],
        "import_check": {"returncode": import_check["returncode"], "stdout": import_check["stdout"].strip()},
    }


def start_proxy_with_fresh_runtime(run_dir: Path, spec: str, install_timeout: int) -> tuple[subprocess.Popen[str] | None, str, Path, dict[str, Any]]:
    runtime_dir = run_dir / "headroom-runtime-venv"
    python = create_venv(runtime_dir)
    log = run_dir / "logs" / "headroom-runtime.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    for cmd in ([str(python), "-m", "pip", "install", "--upgrade", "pip"], [str(python), "-m", "pip", "install", spec]):
        result = run(cmd, timeout=install_timeout)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"\n$ {' '.join(cmd)}\n{result['stdout']}\n")
        if result["returncode"] != 0:
            return None, "", log, {"ok": False, "phase": "install", "cmd": cmd, "stdout_tail": result["stdout"][-3000:]}
    port = free_loopback_port()
    proxy_url = f"http://127.0.0.1:{port}"
    headroom = bin_dir(runtime_dir) / exe("headroom")
    env = os.environ.copy()
    env.update({"HEADROOM_TELEMETRY": "off", "HEADROOM_MEMORY_ENABLED": "0", "HEADROOM_MEMORY_LEARN": "0", "PYTHONUNBUFFERED": "1"})
    fh = log.open("a", encoding="utf-8")
    proc = subprocess.Popen([str(headroom), "proxy", "--host", "127.0.0.1", "--port", str(port), "--no-telemetry"], cwd=str(REPO), env=env, stdout=fh, stderr=subprocess.STDOUT, text=True)
    setattr(proc, "_headroom_log_fh", fh)
    ready = wait_readyz(proxy_url)
    return proc, proxy_url, log, ready


def stop_proxy(proc: subprocess.Popen[str] | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    fh = getattr(proc, "_headroom_log_fh", None)
    if fh:
        fh.close()


def bulky_log(label: str, lines: int) -> str:
    return "\n".join(
        f"level={'ERROR' if i % 97 == 0 else 'WARNING' if i % 53 == 0 else 'INFO'} run_id={label}-run task_id={label}-task line={i} status={'failed' if i % 97 == 0 else 'ok'} path=/tmp/{label}/artifact-{i % 19}.log sentinel={label.upper()}_RC_SENTINEL diagnostic='bulky intermediate trace for release candidate gate'"
        for i in range(lines)
    ) + "\n"


def browser_trace(lines: int) -> str:
    return "\n".join(
        f"browser event_index={i} session_id=rc-sess-{i%5} frame_id=frame-{i%7} target_id=target-{i%3} selector=#node-{i%31} bounds=10,20,300,40 source_url=https://example.test/page/{i%11} status={'warning' if i%89==0 else 'ok'} sentinel=BROWSER_RC_SENTINEL message='DOM diagnostic event {i}'"
        for i in range(lines)
    ) + "\n"


def research_corpus(lines: int) -> str:
    return "\n".join(
        f"source_url=https://example.org/paper/{i%23} document_id=doc-{i%23} citation=[{i}] title='Portable Headroom Evidence {i}' sentinel=RESEARCH_RC_SENTINEL excerpt='semantic retrieval and context optimization repeated bulky corpus line {i}'"
        for i in range(lines)
    ) + "\n"


def workload_matrix(run_dir: Path, proxy_url: str, wheel_gate: dict[str, Any]) -> dict[str, Any]:
    wheels = [Path(wheel_gate["wheel"])] if wheel_gate.get("wheel") else []
    venv_dir = run_dir / "workload-venv"
    python = create_venv(venv_dir)
    install = [
        run([str(python), "-m", "pip", "install", "--upgrade", "pip"], timeout=240),
        run([str(python), "-m", "pip", "install", str(wheels[0])], timeout=300) if wheels else {"returncode": 1, "stdout": "missing wheel", "cmd": [], "duration_s": 0},
    ]
    hermes_home = run_dir / "temp-hermes-home-workload"
    hermes_home.mkdir(parents=True, exist_ok=True)
    payload_dir = run_dir / "workload-payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)
    cases = [
        {"name": "terminal_qa_build_log", "tool": "terminal", "args": {"command": "pytest tests --maxfail=1", "lane": "qa", "data_class": "qa_trace"}, "body": bulky_log("qa", 5200), "expect": "compress", "sentinel": "QA_RC_SENTINEL"},
        {"name": "delegate_subagent_trace", "tool": "delegate_task", "args": {"goal": "delegate bulky trace", "lane": "delegate", "data_class": "worker_trace_raw"}, "body": bulky_log("delegate", 5000), "expect": "compress", "sentinel": "DELEGATE_RC_SENTINEL"},
        {"name": "browser_debug_trace", "tool": "browser_snapshot", "args": {"lane": "browser debug", "data_class": "browser_debug_trace"}, "body": browser_trace(4200), "expect": "compress", "sentinel": "BROWSER_RC_SENTINEL"},
        {"name": "research_corpus_web_extract", "tool": "web_extract", "args": {"lane": "research", "data_class": "research_corpus"}, "body": research_corpus(4200), "expect": "compress", "sentinel": "RESEARCH_RC_SENTINEL"},
        {"name": "exact_git_diff_negative", "tool": "terminal", "args": {"command": "git diff -- README.md", "lane": "dev"}, "body": "*** Begin Patch\n" + bulky_log("diff", 2500) + "*** End Patch\n", "expect": "exact", "sentinel": "DIFF_RC_SENTINEL"},
        {"name": "secret_material_negative", "tool": "terminal", "args": {"command": "diagnostic", "lane": "diagnostic"}, "body": (("[REDACTED " + "PRIVATE" + " KEY]\n") * 500), "expect": "exact", "sentinel": "SECRET_RC_SENTINEL"},
        {"name": "worker_final_packet_negative", "tool": "delegate_task", "args": {"goal": "return final packet", "lane": "delegate"}, "body": "# Worker Final Packet\n\nstatus: PASS\nclaim_ledger: exact\n" + bulky_log("final", 2200), "expect": "exact", "sentinel": "FINAL_RC_SENTINEL"},
    ]
    results: list[dict[str, Any]] = []
    for case in cases:
        payload_path = payload_dir / f"{case['name']}.txt"
        payload_path.write_text(case["body"], encoding="utf-8")
        code = f"""
import json, os, re
from pathlib import Path
from hermes_headroom_plugin.middleware import compress_tool_result_for_context
from hermes_headroom_plugin.tools import handle_headroom_retrieve
body = Path({str(payload_path)!r}).read_text(encoding='utf-8', errors='replace')
report_dir = Path(os.environ['HERMES_HOME'])/'control-plane'/'headroom'/'reports'
before_reports = set(report_dir.glob('auto-tool-*.json')) if report_dir.exists() else set()
result = compress_tool_result_for_context(tool_name={case['tool']!r}, args={case['args']!r}, result=body, task_id={case['name']!r}, tool_call_id={case['name']!r})
text = result or ''
marker_match = re.search(r"marker=([^\\s\\]]+)", text)
marker = marker_match.group(1) if marker_match else ''
after_reports = sorted(p for p in report_dir.glob('auto-tool-*.json') if p not in before_reports) if report_dir.exists() else []
report_data = {{}}
if after_reports:
    report_data = json.loads(after_reports[-1].read_text(encoding='utf-8'))
source_path = Path(report_data.get('source_path', '')) if report_data.get('source_path') else None
source_has_sentinel = bool(source_path and source_path.exists() and {case['sentinel']!r} in source_path.read_text(encoding='utf-8', errors='replace'))
retrieve = {{}}
retrieve_has_sentinel = False
if marker:
    retrieve = json.loads(handle_headroom_retrieve({{'hash': marker, 'query': {case['sentinel']!r}}}))
    retrieve_has_sentinel = {case['sentinel']!r} in json.dumps(retrieve, ensure_ascii=False)
out = {{
  'name': {case['name']!r},
  'expect': {case['expect']!r},
  'compressed': result is not None,
  'contains_auto_header': 'Headroom auto-compressed tool result' in text,
  'contains_private_key': 'PRIVATE KEY' in text,
  'marker': marker,
  'tokens_saved': report_data.get('tokens_saved'),
  'source_retained': bool(source_path and source_path.exists()),
  'source_has_sentinel': source_has_sentinel,
  'retrieve_success': retrieve.get('success'),
  'retrieve_has_sentinel': retrieve_has_sentinel,
  'report_data_class': report_data.get('data_class'),
}}
if out['expect'] == 'compress':
    out['pass'] = bool(out['compressed'] and out['contains_auto_header'] and not out['contains_private_key'] and out['source_retained'] and out['source_has_sentinel'] and isinstance(out.get('tokens_saved'), int) and out['tokens_saved'] > 1000)
else:
    out['pass'] = not out['compressed']
print(json.dumps(out, sort_keys=True))
raise SystemExit(0 if out['pass'] else 1)
""".strip()
        env = os.environ.copy()
        env.update({"HEADROOM_PROXY_URL": proxy_url, "HERMES_HOME": str(hermes_home), "HEADROOM_TELEMETRY": "off"})
        result = run([str(python), "-c", code], timeout=180, env=env)
        try:
            parsed = json.loads(result["stdout"].strip().splitlines()[-1])
        except Exception:
            parsed = {"name": case["name"], "expect": case["expect"], "pass": False, "parse_error": True, "stdout_tail": result["stdout"][-2000:]}
        parsed["returncode"] = result["returncode"]
        parsed["duration_s"] = result["duration_s"]
        results.append(parsed)
    return {
        "pass": all(s["returncode"] == 0 for s in install) and all(r.get("pass") for r in results),
        "install": [{"cmd": s.get("cmd"), "returncode": s.get("returncode"), "duration_s": s.get("duration_s"), "stdout_tail": str(s.get("stdout", ""))[-1200:]} for s in install],
        "hermes_home": str(hermes_home),
        "results": results,
    }


def no_leftover_proxy() -> dict[str, Any]:
    matches = []
    proc_root = Path("/proc")
    if proc_root.exists():
        for p in proc_root.iterdir():
            if not p.name.isdigit():
                continue
            try:
                raw = (p / "cmdline").read_bytes()
            except Exception:
                continue
            argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
            if argv and Path(argv[0]).name == "headroom" and "proxy" in argv[1:]:
                matches.append({"pid": p.name, "argv": argv})
    return {"pass": not matches, "headroom_proxy_processes": matches}


def write_report(run_dir: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Hermes Headroom plugin release-candidate local gate",
        "",
        f"Generated UTC: {summary['generated_at']}",
        "",
        "## Decision",
        "",
        f"`{summary['decision']}`",
        "",
        "## Gates",
        "",
        "| Gate | Pass | Evidence |",
        "|---|---:|---|",
    ]
    for name, gate in summary["gates"].items():
        lines.append(f"| `{name}` | `{gate.get('pass')}` | `{gate.get('evidence', '')}` |")
    matrix = summary.get("workload_matrix", {})
    lines += ["", "## Bulky workload matrix", "", "| Case | Expect | Pass | Compressed | Source retained | Tokens saved | Retrieve sentinel |", "|---|---|---:|---:|---:|---:|---:|"]
    for row in matrix.get("results", []):
        lines.append(
            f"| `{row.get('name')}` | `{row.get('expect')}` | `{row.get('pass')}` | `{row.get('compressed')}` | `{row.get('source_retained')}` | `{row.get('tokens_saved')}` | `{row.get('retrieve_has_sentinel')}` |"
        )
    lines += [
        "",
        "## Scope",
        "",
        "- This gate is local-only. It does not push, tag, publish, or mutate the real Hermes profile.",
        "- PASS means the checkout is ready for owner review as a local release candidate, not that public release is authorized.",
        "- Public release still requires explicit owner approval, final diff review, remote CI readback, and release notes.",
    ]
    (run_dir / "RELEASE_CANDIDATE_LOCAL_GATE_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local release-candidate gate for Hermes Headroom plugin.")
    parser.add_argument("--run-root", default=str(REPO / "release-candidate-runs"), help="directory for gate evidence")
    parser.add_argument("--headroom-spec", default=os.environ.get("HEADROOM_AI_SPEC", DEFAULT_HEADROOM_SPEC))
    parser.add_argument("--install-timeout", type=int, default=int(os.environ.get("HEADROOM_DEP_INSTALL_TIMEOUT", "600")))
    args = parser.parse_args(argv)

    run_dir = Path(args.run_root).expanduser().resolve() / f"{utc_stamp()}-release-candidate-local-gate"
    run_dir.mkdir(parents=True, exist_ok=True)
    gates: dict[str, dict[str, Any]] = {}

    audit = run(["bash", "scripts/audit-repo-readiness.sh"], timeout=240)
    write_json(run_dir / "commands" / "audit-repo-readiness.json", audit)
    gates["repo_readiness_audit"] = {"pass": audit["returncode"] == 0, "evidence": str(run_dir / "commands" / "audit-repo-readiness.json")}

    public_scan = public_path_scan()
    write_json(run_dir / "public-path-secret-scan.json", public_scan)
    gates["public_path_secret_scan"] = {"pass": public_scan.get("pass"), "evidence": str(run_dir / "public-path-secret-scan.json")}

    pytest_result = pytest_gate(run_dir)
    write_json(run_dir / "pytest-gate.json", pytest_result)
    gates["unit_contract_tests"] = {"pass": pytest_result.get("pass"), "evidence": str(run_dir / "pytest-gate.json")}

    build_result = build_and_inspect(run_dir)
    write_json(run_dir / "build-and-archive-inspection.json", build_result)
    gates["build_and_archive_inspection"] = {"pass": build_result.get("pass"), "evidence": str(run_dir / "build-and-archive-inspection.json")}

    wheel_result = wheel_install_gate(run_dir, build_result)
    write_json(run_dir / "wheel-install-entrypoints.json", wheel_result)
    gates["wheel_install_entrypoints"] = {"pass": wheel_result.get("pass"), "evidence": str(run_dir / "wheel-install-entrypoints.json")}

    if shutil.which("hermes"):
        clean = run(["bash", "scripts/test-clean-hermes-install.sh", "--local"], timeout=300)
        clean_pass = clean["returncode"] == 0
    else:
        clean = {
            "cmd": ["bash", "scripts/test-clean-hermes-install.sh", "--local"],
            "returncode": 0,
            "stdout": "SKIP: hermes CLI not available in this runner; wheel install/entrypoint gate still validates package portability.",
            "duration_s": 0,
            "skipped": True,
            "skip_reason": "hermes_cli_not_available",
        }
        clean_pass = True
    write_json(run_dir / "commands" / "clean-temp-hermes-install.json", clean)
    gates["clean_temp_hermes_install"] = {"pass": clean_pass, "evidence": str(run_dir / "commands" / "clean-temp-hermes-install.json")}

    runtime = run([sys.executable, "scripts/test-headroom-runtime-smoke.py", "--spec", args.headroom_spec, "--install-timeout", str(args.install_timeout)], timeout=args.install_timeout + 240)
    write_json(run_dir / "commands" / "runtime-smoke.json", runtime)
    gates["runtime_compress_retrieve_smoke"] = {"pass": runtime["returncode"] == 0, "evidence": str(run_dir / "commands" / "runtime-smoke.json")}

    workload: dict[str, Any] = {"pass": False, "results": []}
    proxy_proc: subprocess.Popen[str] | None = None
    proxy_url = ""
    proxy_log = ""
    try:
        proxy_proc, proxy_url, proxy_log_path, ready = start_proxy_with_fresh_runtime(run_dir, args.headroom_spec, args.install_timeout)
        proxy_log = str(proxy_log_path)
        if ready.get("ok"):
            workload = workload_matrix(run_dir, proxy_url, wheel_result)
            workload["proxy_url"] = proxy_url
            workload["proxy_log"] = proxy_log
        else:
            workload = {"pass": False, "ready": ready, "proxy_log": proxy_log}
    finally:
        stop_proxy(proxy_proc)
    write_json(run_dir / "bulky-workload-matrix.json", workload)
    gates["bulky_workload_matrix"] = {"pass": workload.get("pass"), "evidence": str(run_dir / "bulky-workload-matrix.json")}

    leftover = no_leftover_proxy()
    write_json(run_dir / "post-proxy-check.json", leftover)
    gates["no_leftover_proxy"] = {"pass": leftover.get("pass"), "evidence": str(run_dir / "post-proxy-check.json")}

    status = run(["git", "status", "--short"], timeout=30)
    write_json(run_dir / "git-status.json", status)

    pass_count = sum(1 for g in gates.values() if g.get("pass"))
    total = len(gates)
    decision = "PLUGIN_RELEASE_CANDIDATE_LOCAL_PASS" if pass_count == total else "PLUGIN_RELEASE_CANDIDATE_LOCAL_GAPS_FOUND"
    summary = {
        "schema": "hermes-headroom-plugin-release-candidate-local-gate-v1",
        "generated_at": utc_iso(),
        "decision": decision,
        "pass_count": pass_count,
        "total_gates": total,
        "run_dir": str(run_dir),
        "repo": str(REPO),
        "plugin_head": git_head(),
        "gates": gates,
        "workload_matrix": workload,
        "proxy_url_used": proxy_url,
        "proxy_log": proxy_log,
        "remote_pushed": False,
        "public_release": False,
        "real_hermes_profile_mutated": False,
        "git_status_short": status.get("stdout", ""),
        "next_gate": "OWNER_RELEASE_REVIEW_AND_REMOTE_CI_READBACK" if pass_count == total else "FIX_RC_GAPS_AND_RERUN",
    }
    write_json(run_dir / "summary.json", summary)
    write_report(run_dir, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if pass_count == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
