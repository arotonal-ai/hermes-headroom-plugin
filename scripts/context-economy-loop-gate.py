#!/usr/bin/env python3
"""Portable Context Economy Loop gate for the Hermes Headroom plugin.

The gate is intentionally instance-neutral: it uses a temporary Hermes home,
loopback-only runtime smoke, repository docs/skill scans, and a synthetic local
context-pressure store. It does not mutate the real Hermes profile, publish,
push, or add slash-command surface.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
FORBIDDEN_PORTABLE_PATTERNS = [
    re.compile(r"/home/[A-Za-z0-9._-]+"),
    re.compile(r"/Users/[A-Za-z0-9._-]+"),
    re.compile(r"\b20\d{6}T\d{6}Z\b"),
    re.compile(r"\.hermes"),
    re.compile(r"telegram", re.I),
    re.compile(r"incident", re.I),
]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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
        return {
            "cmd": cmd,
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-6000:],
            "stdout_chars": len(proc.stdout),
            "duration_s": round(time.perf_counter() - started, 3),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "cmd": cmd,
            "returncode": 127,
            "stdout_tail": f"{type(exc).__name__}: {exc}",
            "stdout_chars": 0,
            "duration_s": round(time.perf_counter() - started, 3),
        }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def scan_portable_docs() -> dict[str, Any]:
    targets = [
        REPO / "docs" / "context-economy-loop.md",
        REPO / "README.md",
        REPO / "src" / "hermes_headroom_plugin" / "skills" / "headroom-token-cost-evaluation" / "SKILL.md",
    ]
    # Strict contamination scan applies to the new portable loop doc. README/skill
    # may legitimately include public install commands or warnings such as
    # "do not copy ~/.hermes".
    strict_targets = {REPO / "docs" / "context-economy-loop.md"}
    hits: list[dict[str, Any]] = []
    required_needles = [
        "observe -> classify -> act -> verify -> learn",
        "context-economy-loop.md",
        "Portable Context Economy Loop",
        "not an autonomous meta-agent",
        "HEADROOM_AUTO_COMPRESSION=0",
        "Efficiency test for a fresh Hermes instance",
        "measure exact context chars/tokens avoided or compressed minus loop overhead",
    ]
    combined = ""
    for path in targets:
        text = path.read_text(encoding="utf-8")
        combined += "\n" + text
        if path in strict_targets:
            for line_no, line in enumerate(text.splitlines(), start=1):
                for pat in FORBIDDEN_PORTABLE_PATTERNS:
                    if pat.search(line):
                        hits.append({"path": str(path.relative_to(REPO)), "line": line_no, "pattern": pat.pattern, "text": line[:240]})
    missing = [needle for needle in required_needles if needle not in combined]
    return {"ok": not hits and not missing, "hits": hits, "missing": missing, "targets": [str(p.relative_to(REPO)) for p in targets]}


def command_surface_check() -> dict[str, Any]:
    commands_py = REPO / "src" / "hermes_headroom_plugin" / "commands.py"
    text = commands_py.read_text(encoding="utf-8")
    forbidden = [
        "exact-reads",
        "exact-read-lint",
        "schema-pressure",
        "retrieval-advice",
        "cross-lane-advice",
        "below-min",
        "below-min-canary",
        "next-action",
    ]
    usage_match = re.search(r'^USAGE = "([^"]+)"', text, re.M)
    usage = usage_match.group(1) if usage_match else ""
    hits = [item for item in forbidden if item in usage]
    return {
        "ok": bool(usage) and not hits,
        "usage": usage,
        "forbidden_hits_in_usage": hits,
    }


def classify_event(event: dict[str, Any]) -> dict[str, Any]:
    tool = event.get("tool") or event.get("tool_name") or "unknown"
    chars = int(event.get("chars") or 0)
    request = event.get("request") or {}
    limit = request.get("limit")
    if event.get("sensitive"):
        return {"classification": "blocked", "reason": "sensitive_or_protected"}
    if tool == "read_file" and (limit is None or int(limit) > 120 or chars >= 20_000):
        return {
            "classification": "broad_read",
            "reason": "read_file_missing_or_large_limit_or_large_output",
            "recommended_pattern": "search/index first; read bounded slices <=120 lines; cite pointer instead of re-reading broad source",
        }
    if tool == "read_file":
        return {"classification": "bounded_read", "reason": "bounded_source_truth_read"}
    if event.get("final") or event.get("exact_authority"):
        return {"classification": "exact", "reason": "final_or_authority"}
    if chars >= 20_000 and event.get("intermediate", True):
        return {"classification": "compressible_intermediate", "reason": "bulky_intermediate_with_retained_source"}
    return {"classification": "exact", "reason": "small_or_unsure_keep_exact"}


def synthetic_store(run_dir: Path) -> Path:
    events = [
        {
            "id": "evt-001",
            "source": "local-temp-store",
            "tool": "read_file",
            "chars": 48000,
            "request": {"path": "docs/example-status.md", "offset": 1, "limit": 240},
            "exact_authority": True,
            "pointer": "temp-store:event=evt-001",
        },
        {
            "id": "evt-002",
            "source": "local-temp-store",
            "tool": "read_file",
            "chars": 9000,
            "request": {"path": "docs/example-status.md", "offset": 121, "limit": 80},
            "exact_authority": True,
            "pointer": "temp-store:event=evt-002",
        },
        {
            "id": "evt-003",
            "source": "local-temp-store",
            "tool": "terminal",
            "chars": 72000,
            "intermediate": True,
            "retained_source": "sidecars/terminal-build.log",
            "tokens_saved": 41000,
            "pointer": "temp-store:event=evt-003",
        },
        {
            "id": "evt-004",
            "source": "local-temp-store",
            "tool": "patch",
            "chars": 28000,
            "final": True,
            "pointer": "temp-store:event=evt-004",
        },
    ]
    path = run_dir / "synthetic-local-store.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in events), encoding="utf-8")
    return path


def analyze_context_pressure(store_path: Path, run_dir: Path) -> dict[str, Any]:
    events = [json.loads(line) for line in store_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    enriched = []
    source_truth_exact_chars = 0
    for event in events:
        cls = classify_event(event)
        item = {**event, **cls}
        enriched.append(item)
        if item["classification"] in {"broad_read", "bounded_read", "exact"}:
            source_truth_exact_chars += int(item.get("chars") or 0)
    offenders = sorted((e for e in enriched if e["classification"] == "broad_read"), key=lambda e: int(e.get("chars") or 0), reverse=True)
    compressible = sorted((e for e in enriched if e["classification"] == "compressible_intermediate"), key=lambda e: int(e.get("chars") or 0), reverse=True)
    if offenders:
        leader = offenders[0]
        context_status = "FAIL"
        next_intervention = {
            "action": "tighten_context_intake",
            "rule": leader["recommended_pattern"],
            "target": {"tool": leader.get("tool"), "pointer": leader.get("pointer"), "request": leader.get("request")},
        }
    elif compressible:
        leader = compressible[0]
        context_status = "WARN"
        next_intervention = {
            "action": "compress_intermediate",
            "rule": "compress bulky intermediate with retained exact sidecar and retrieval smoke",
            "target": {"tool": leader.get("tool"), "pointer": leader.get("pointer")},
        }
    else:
        leader = {}
        context_status = "PASS"
        next_intervention = {"action": "keep_current_loop", "rule": "continue exact authority plus compact receipts"}
    report = {
        "status": "PASS_WITH_CONTEXT_ECONOMY_FAIL" if context_status == "FAIL" else "PASS",
        "runtime": {"proxy_ready": "covered_by_runtime_smoke_gate", "smoke_passed": "covered_by_runtime_smoke_gate"},
        "context_economy": {
            "status": context_status,
            "reason": "broad_exact_intake_detected" if context_status == "FAIL" else "no_broad_exact_intake_leader",
            "source_truth_exact_chars": source_truth_exact_chars,
            "leader": {"tool_or_lane": leader.get("tool"), "chars": leader.get("chars"), "pointer": leader.get("pointer")} if leader else {},
        },
        "top_offenders": offenders[:5],
        "compressible_intermediates": compressible[:5],
        "next_intervention": next_intervention,
    }
    write_json(run_dir / "context-economy-synthetic-report.json", report)
    md_lines = [
        "# Context Economy Synthetic Report",
        "",
        f"status: `{report['status']}`",
        f"context_economy: `{context_status}`",
        "",
        "## Next intervention",
        "",
        "```yaml",
        json.dumps(next_intervention, indent=2, sort_keys=True),
        "```",
        "",
        "## Top offenders",
        "",
    ]
    for offender in offenders[:5]:
        md_lines.append(f"- `{offender['pointer']}` `{offender['tool']}` chars={offender['chars']} class={offender['classification']} request={offender.get('request')}")
    (run_dir / "CONTEXT_ECONOMY_SYNTHETIC_REPORT.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Run portable Context Economy Loop gate.")
    ap.add_argument("--out-root", default=str(REPO / "context-economy-loop-gate-runs"))
    ap.add_argument("--skip-runtime-smoke", action="store_true")
    ap.add_argument("--keep", action="store_true", help="keep temp outputs from subprocess helpers when supported")
    args = ap.parse_args()

    run_dir = Path(args.out_root).resolve() / f"{utc_stamp()}-context-economy-loop-gate"
    run_dir.mkdir(parents=True, exist_ok=True)

    docs = scan_portable_docs()
    command_surface = command_surface_check()
    if shutil.which("hermes"):
        clean_install = run(["bash", "scripts/test-clean-hermes-install.sh", "--local"] + (["--keep"] if args.keep else []), timeout=300)
        clean_install_pass = clean_install.get("returncode") == 0
    else:
        clean_install = {
            "cmd": ["bash", "scripts/test-clean-hermes-install.sh", "--local"],
            "returncode": 0,
            "stdout_tail": "SKIP: hermes CLI not available in this runner; package portability is covered by release-candidate wheel/entrypoint gates.",
            "stdout_chars": 0,
            "duration_s": 0,
            "skipped": True,
            "skip_reason": "hermes_cli_not_available",
        }
        clean_install_pass = True
    runtime_smoke: dict[str, Any]
    if args.skip_runtime_smoke:
        runtime_smoke = {"skipped": True, "returncode": 0, "reason": "--skip-runtime-smoke"}
    else:
        port = free_loopback_port()
        runtime_smoke = run([sys.executable, "scripts/test-headroom-runtime-smoke.py", "--port", str(port)], timeout=900)
        runtime_smoke["port"] = port

    store = synthetic_store(run_dir)
    context_report = analyze_context_pressure(store, run_dir)

    checks = {
        "portable_docs_scan": docs.get("ok") is True,
        "clean_temp_hermes_install": clean_install_pass,
        "runtime_smoke": runtime_smoke.get("returncode") == 0,
        "synthetic_context_report_next_intervention": bool(context_report.get("next_intervention")),
        "stable_slash_command_surface": command_surface.get("ok") is True,
    }
    status = "CONTEXT_ECONOMY_LOOP_GATE_PASS" if all(checks.values()) else "CONTEXT_ECONOMY_LOOP_GATE_FAIL"
    result = {
        "status": status,
        "repo": str(REPO),
        "run_dir": str(run_dir),
        "checks": checks,
        "docs_scan": docs,
        "clean_install": clean_install,
        "command_surface": command_surface,
        "runtime_smoke": runtime_smoke,
        "context_report": context_report,
        "artifacts": {
            "synthetic_store": str(store),
            "synthetic_report_json": str(run_dir / "context-economy-synthetic-report.json"),
            "synthetic_report_md": str(run_dir / "CONTEXT_ECONOMY_SYNTHETIC_REPORT.md"),
        },
    }
    write_json(run_dir / "CONTEXT_ECONOMY_LOOP_GATE_RESULT.json", result)
    md = [
        "# Context Economy Loop Gate",
        "",
        f"status: `{status}`",
        "",
        "## Checks",
        "",
    ]
    for k, v in checks.items():
        md.append(f"- {'PASS' if v else 'FAIL'} `{k}`")
    md.extend([
        "",
        "## Next intervention from synthetic adapter",
        "",
        "```yaml",
        json.dumps(context_report.get("next_intervention"), indent=2, sort_keys=True),
        "```",
        "",
        "## Artifacts",
        "",
        f"- `{run_dir / 'CONTEXT_ECONOMY_LOOP_GATE_RESULT.json'}`",
        f"- `{run_dir / 'CONTEXT_ECONOMY_SYNTHETIC_REPORT.md'}`",
    ])
    (run_dir / "CONTEXT_ECONOMY_LOOP_GATE_REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if status.endswith("PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
