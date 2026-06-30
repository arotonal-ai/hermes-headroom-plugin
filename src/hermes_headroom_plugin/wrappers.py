"""Portable CLI wrappers for bulky intermediate command output.

These wrappers are product-safe because they do not mutate Hermes provider routing,
model config, plugins, memory, or gateway state. They run explicit operator
commands, retain exact raw sidecars, keep worker final packets exact, and only
ask the loopback/controlled Headroom proxy to compress eligible bulky diagnostic
traces.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .proxy import compress_messages, hermes_home, readyz, resolve_proxy_url

DEFAULT_MIN_CHARS = 40_000
DEFAULT_ALWAYS_CHARS = 120_000
DEFAULT_MAX_COMPRESS_CHARS = 250_000

OUTPUT_HINTS = (
    "ERROR", "WARNING", "Traceback", "Exception", "failed", "failure",
    "[INFO]", "[WARN]", "[ERROR]", "FAILED",
)
COMMAND_HINTS = (
    "pytest", "pnpm", "npm", "yarn", "vitest", "jest", "cargo", "go test",
    "make", "build", "test", "docker logs", "kubectl logs", "journalctl",
    "systemctl status", "browser", "ocr", "crawl", "scrape",
)
EXACT_COMMAND_HINTS = (
    "git diff", "git show", "git status", "sha256sum", "md5sum",
    "openssl dgst", "hermes config", "hermes model", "hermes auth",
    "py_compile",
)
REDACT_PATTERNS = [
    (r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}", r"\1[REDACTED]"),
    (r"(?i)((?:api[_-]?key|token|secret|password|authorization|client_secret)[^\n]{0,40}[:=]\s*)[^\s'\"]{8,}", r"\1[REDACTED]"),
    (r"(?i)((?:OPENAI|ANTHROPIC|GEMINI|GOOGLE|GITHUB|CLOUDFLARE|TELEGRAM|SLACK|DISCORD)[A-Z0-9_]*(?:KEY|TOKEN)\s*=\s*)\S+", r"\1[REDACTED]"),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def default_out_root() -> Path:
    return Path(os.environ.get("HEADROOM_RUNS_DIR") or hermes_home() / "headroom-runs" / "worker-lane-wrapper").expanduser()


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def redact(text: str) -> str:
    out = text
    for pattern, repl in REDACT_PATTERNS:
        out = re.sub(pattern, repl, out)
    return out


def safe_lane_name(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", raw or "worker")[:80].strip("-") or "worker"


def shell_join(parts: list[str]) -> str:
    return " ".join(shlex.quote(str(p)) for p in parts)


def is_loglike(command: list[str], output: str, min_chars: int, always_chars: int) -> bool:
    if len(output) >= always_chars:
        return True
    if len(output) < min_chars:
        return False
    command_text = shell_join(command).lower()
    if any(hint in command_text for hint in COMMAND_HINTS):
        return True
    hint_hits = sum(1 for hint in OUTPUT_HINTS if hint in output)
    return hint_hits >= 2 or (output.count("\n") >= 300 and hint_hits >= 1)


def extract_markers(messages: Any) -> list[str]:
    text = json.dumps(messages, ensure_ascii=False)
    return [m.split()[0] for m in re.findall(r"<<ccr:([^,>]+)", text)]


def compression_terms(query: str) -> list[str]:
    common = {"event", "line", "lines", "error", "errors", "trace", "worker", "final", "status", "query", "focus"}
    terms = []
    for term in re.findall(r"[A-Za-z0-9_.:-]{4,}", query.lower()):
        if term not in common:
            terms.append(term)
    return list(dict.fromkeys(terms))[:12]


def bound_compression_input(raw: str, query: str, max_chars: int) -> tuple[str, dict[str, Any]]:
    if max_chars <= 0 or len(raw) <= max_chars:
        return raw, {"bounded": False, "input_chars": len(raw), "original_chars": len(raw)}
    reserve = min(4_000, max_chars // 10)
    head_budget = max_chars // 4
    tail_budget = max_chars // 4
    middle_budget = max(0, max_chars - head_budget - tail_budget - reserve)
    terms = compression_terms(query)
    match_lines: list[str] = []
    numeric_terms = [term for term in terms if any(ch.isdigit() for ch in term)]
    if terms and middle_budget > 0:
        used = 0
        for line in raw.splitlines():
            low = line.lower()
            if numeric_terms:
                matched = all(term in low for term in numeric_terms)
            else:
                matched = any(term in low for term in terms)
            if matched:
                match_lines.append(line)
                used += len(line) + 1
            if used >= middle_budget:
                break
    matches = "\n".join(match_lines)
    if len(matches) > middle_budget:
        matches = matches[:middle_budget]
    bounded = (
        "===== HEADROOM BOUNDED COMPRESSION INPUT =====\n"
        f"original_chars={len(raw)}\n"
        f"bounded_chars_limit={max_chars}\n"
        "exact_raw_sidecar_retained=true\n"
        "compression_input_role=triage_not_source_of_truth\n"
        f"query_terms={terms}\n"
        "===== TRACE HEAD =====\n"
        f"{raw[:head_budget]}\n"
        "===== QUERY MATCHING LINES =====\n"
        f"{matches}\n"
        "===== TRACE TAIL =====\n"
        f"{raw[-tail_budget:]}\n"
    )
    if len(bounded) > max_chars:
        bounded = bounded[:max_chars]
    return bounded, {
        "bounded": True,
        "original_chars": len(raw),
        "input_chars": len(bounded),
        "max_compress_chars": max_chars,
        "query_terms": terms,
        "matching_lines_included": len(match_lines),
    }


def compact_health(proxy_url: str | None = None) -> dict[str, Any]:
    health = readyz(proxy_url)
    body = health.get("body") if isinstance(health.get("body"), dict) else {}
    return {
        "ok": bool(health.get("ok")),
        "status": health.get("status"),
        "proxy_url": health.get("proxy_url") or proxy_url,
        "ready": body.get("ready") if isinstance(body, dict) else None,
        "version": body.get("version") if isinstance(body, dict) else None,
        "error": health.get("error") or (health.get("body") if not health.get("ok") else None),
    }


def compress_trace(proxy_url: str, lane: str, query: str, raw: str, out_dir: Path, max_chars: int) -> dict[str, Any]:
    compression_input, bounding = bound_compression_input(raw, query, max_chars)
    messages = [
        {"role": "system", "content": f"Worker lane raw trace compression: {lane}. Preserve operational facts, paths, errors, checks, and final status indicators."},
        {"role": "user", "content": f"Compress this raw worker/subagent trace for parent fan-in. Query focus: {query}"},
        {"role": "tool", "tool_call_id": safe_lane_name(lane), "name": "worker_trace", "content": compression_input},
    ]
    started = time.perf_counter()
    result: dict[str, Any] = {
        "attempted": True,
        "status": "error",
        "estimated_tokens_before": estimate_tokens(raw),
        "compression_input": bounding,
    }
    data = compress_messages(messages, proxy_url=proxy_url)
    if not data.get("ok"):
        result.update({"error": data.get("error") or data, "duration_s": round(time.perf_counter() - started, 3)})
        return result
    data["markers"] = extract_markers(data.get("messages"))
    compressed_path = out_dir / "worker-raw-trace.compressed.json"
    compressed_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    before = int(data.get("tokens_before") or result["estimated_tokens_before"])
    after = int(data.get("tokens_after") or 0)
    saved = int(data.get("tokens_saved") or max(0, before - after))
    status = "ok" if saved > 0 or data["markers"] else "no_material_savings"
    result.update({
        "status": status,
        "reason": None if status == "ok" else "tokens_saved<=0 and marker_count=0; use exact sidecar/final packet",
        "compressed_path": str(compressed_path),
        "marker": data["markers"][0] if data["markers"] else None,
        "marker_count": len(data["markers"]),
        "tokens_before": before,
        "tokens_after": after,
        "tokens_saved": saved,
        "savings_percent": round((saved / before) * 100, 2) if before else 0.0,
        "duration_s": round(time.perf_counter() - started, 3),
    })
    return result


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _parse_command(command: list[str]) -> list[str]:
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("Usage: headroom-worker-lane --lane <name> -- <worker command>")
    return command


def run_worker(args: argparse.Namespace) -> int:
    command = _parse_command(list(args.command))
    lane = safe_lane_name(args.lane)
    proxy_url = args.proxy_url or resolve_proxy_url()
    out_dir = Path(args.out_root).expanduser().resolve() / f"{stamp()}-{lane}"
    out_dir.mkdir(parents=True, exist_ok=True)

    final_packet = out_dir / "worker-final-packet.md"
    raw_stdout = out_dir / "worker-stdout.raw.txt"
    raw_stderr = out_dir / "worker-stderr.raw.txt"
    raw_trace = out_dir / "worker-raw-trace.redacted.txt"

    env = os.environ.copy()
    env["HEADROOM_WORKER_RUN_DIR"] = str(out_dir)
    env["HEADROOM_WORKER_FINAL_PACKET"] = str(final_packet)

    started = time.perf_counter()
    proc = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=args.timeout, check=False, env=env)
    duration = round(time.perf_counter() - started, 3)

    raw_stdout.write_text(proc.stdout, encoding="utf-8", errors="ignore")
    raw_stderr.write_text(proc.stderr, encoding="utf-8", errors="ignore")
    redacted = redact(f"===== COMMAND =====\n{shell_join(command)}\n===== STDOUT =====\n{proc.stdout}\n===== STDERR =====\n{proc.stderr}\n")
    raw_trace.write_text(redacted, encoding="utf-8")

    if not (final_packet.exists() and final_packet.stat().st_size > 0):
        final_packet.write_text(
            "# Worker Final Packet\n\n"
            f"status: {'PASS' if proc.returncode == 0 else 'FAIL'}\n"
            f"role: {lane}\n"
            f"result: command exited with rc={proc.returncode}\n\n"
            "## Evidence pointers\n\n"
            f"- raw_stdout: `{raw_stdout}`\n"
            f"- raw_stderr: `{raw_stderr}`\n"
            f"- raw_trace_source: `{raw_trace}`\n\n"
            "## Contract\n\n- Exact fallback final packet; inspect sidecars for richer evidence.\n",
            encoding="utf-8",
        )

    health = compact_health(proxy_url)
    eligible = is_loglike(command, redacted, args.min_chars, args.always_chars)
    compression: dict[str, Any] = {
        "attempted": False,
        "eligible": eligible,
        "reason": "disabled" if args.no_compress else ("not loglike/large enough" if not eligible else "proxy unhealthy" if not health.get("ok") else "eligible"),
        "estimated_tokens_before": estimate_tokens(redacted),
    }
    if not args.no_compress and eligible and health.get("ok"):
        compression = compress_trace(proxy_url, lane, args.query, redacted, out_dir, args.max_compress_chars)
        compression["eligible"] = eligible

    base_status = "PASS" if proc.returncode == 0 else "FAIL"
    wrapper_status = "PARTIAL" if base_status == "PASS" and eligible and compression.get("status") == "error" else base_status
    report = {
        "run_id": out_dir.name.split("-", 1)[0],
        "ts": utc_now(),
        "mode": "headroom_worker_lane",
        "wrapper_status": wrapper_status,
        "worker_returncode": proc.returncode,
        "lane": lane,
        "command": command,
        "duration_s": duration,
        "out_dir": str(out_dir),
        "worker_final_packet_path": str(final_packet),
        "worker_final_packet_exact": True,
        "raw_stdout_path": str(raw_stdout),
        "raw_stderr_path": str(raw_stderr),
        "raw_trace_source_path": str(raw_trace),
        "raw_trace_chars": len(redacted),
        "headroom_proxy": {"url": proxy_url, "health": health},
        "compression": compression,
        "contract": {
            "raw_trace_sidecar_retained": True,
            "raw_trace_may_be_compressed": True,
            "final_packet_compressed": False,
            "runtime_config_mutated": False,
        },
    }
    report_path = out_dir / "worker-lane-wrapper-report.json"
    write_json(report_path, report)
    fanin_path = out_dir / "PARENT_FANIN_PACKET.md"
    fanin_path.write_text(
        "# Parent Fan-in Packet\n\n"
        f"status: **{wrapper_status}**  \n"
        f"lane: `{lane}`  \n"
        f"worker_returncode: `{proc.returncode}`  \n"
        f"worker_final_packet: `{final_packet}`  \n"
        f"raw_trace_source: `{raw_trace}`  \n"
        f"compressed_trace: `{compression.get('compressed_path') or '-'}`  \n"
        f"report: `{report_path}`  \n\n"
        "## Contract\n\n- Read `worker-final-packet.md` first; it is exact and uncompressed.\n- Use compressed raw trace only for triage.\n- Verify important claims from exact sources.\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": wrapper_status,
        "run_dir": str(out_dir),
        "fanin_packet": str(fanin_path),
        "worker_final_packet": str(final_packet),
        "report": str(report_path),
        "raw_trace_chars": len(redacted),
        "compression": compression,
    }, ensure_ascii=False, indent=2))
    return 0 if wrapper_status in {"PASS", "PARTIAL"} else 1


def classify_command(command: list[str], expected_chars: int | None, threshold: int, always: int, force: bool, no_health: bool) -> dict[str, Any]:
    command = _parse_command(command)
    command_text = shell_join(command)
    lowered = command_text.lower()
    wrap_reasons: list[str] = []
    exact_reasons: list[str] = []
    if force:
        wrap_reasons.append("operator forced wrapper")
    if expected_chars is not None:
        if expected_chars >= always:
            wrap_reasons.append(f"expected_chars >= always threshold ({expected_chars} >= {always})")
        elif expected_chars >= threshold:
            wrap_reasons.append(f"expected_chars >= soft threshold ({expected_chars} >= {threshold})")
        else:
            exact_reasons.append(f"expected_chars below threshold ({expected_chars} < {threshold})")
    for hint in sorted(COMMAND_HINTS, key=len, reverse=True):
        if hint in lowered:
            wrap_reasons.append(f"command hint suggests bulky intermediate output: {hint}")
            break
    for hint in sorted(EXACT_COMMAND_HINTS, key=len, reverse=True):
        if hint in lowered:
            exact_reasons.append(f"command hint is usually exact/edit-critical or compact: {hint}")
            break
    if exact_reasons and not force and not (expected_chars is not None and expected_chars >= always):
        decision, confidence = "direct", "medium"
    elif wrap_reasons:
        decision, confidence = "wrap", "high" if force or (expected_chars is not None and expected_chars >= threshold) else "medium"
    else:
        decision, confidence = "direct", "low"
        exact_reasons.append("no bulky-output hint and no expected_chars over threshold")
    proxy_url = resolve_proxy_url()
    health = {"skipped": True} if no_health else compact_health(proxy_url)
    if decision == "wrap" and not no_health and not health.get("ok"):
        wrap_reasons.append("proxy health check failed; wrapper can still retain raw sidecar but compression may be skipped")
        confidence = "medium"
    return {
        "ts": utc_now(),
        "decision": decision,
        "confidence": confidence,
        "command": command,
        "command_shell": command_text,
        "expected_chars": expected_chars,
        "threshold_chars": threshold,
        "always_chars": always,
        "reasons": wrap_reasons,
        "exact_reasons": exact_reasons,
        "proxy_url": proxy_url,
        "proxy_health": health,
        "contract": {
            "wrapper_only_for_intermediate_bulk": True,
            "final_packets_diffs_manifests_claim_ledgers_remain_exact": True,
            "runtime_config_mutated": False,
        },
    }


def worker_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a bounded worker command with exact sidecars and optional Headroom compression.")
    parser.add_argument("--lane", default="worker")
    parser.add_argument("--query", default="errors failures warnings root cause verification final status")
    parser.add_argument("--out-root", default=str(default_out_root()))
    parser.add_argument("--proxy-url", default=None)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--min-chars", type=int, default=DEFAULT_MIN_CHARS)
    parser.add_argument("--always-chars", type=int, default=DEFAULT_ALWAYS_CHARS)
    parser.add_argument("--max-compress-chars", type=int, default=DEFAULT_MAX_COMPRESS_CHARS)
    parser.add_argument("--no-compress", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return run_worker(parser.parse_args(argv))


def background_main(argv: list[str] | None = None) -> int:
    return worker_main(argv)


def preflight_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify a command and optionally run it through headroom-worker-lane.")
    parser.add_argument("--lane", default="")
    parser.add_argument("--query", default="errors failures warnings root cause verification final status")
    parser.add_argument("--expected-chars", type=int, default=None)
    parser.add_argument("--threshold", type=int, default=DEFAULT_MIN_CHARS)
    parser.add_argument("--always", type=int, default=DEFAULT_ALWAYS_CHARS)
    parser.add_argument("--wrapper-min-chars", type=int, default=DEFAULT_MIN_CHARS)
    parser.add_argument("--wrapper-always-chars", type=int, default=DEFAULT_MIN_CHARS)
    parser.add_argument("--wrapper-max-compress-chars", type=int, default=DEFAULT_MAX_COMPRESS_CHARS)
    parser.add_argument("--force-wrap", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-health", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = _parse_command(list(args.command))
    lane = safe_lane_name(args.lane or (Path(command[0]).name if command else "command"))
    info = classify_command(command, args.expected_chars, args.threshold, args.always, args.force_wrap, args.no_health)
    wrapped = ["headroom-worker-lane", "--lane", lane, "--query", args.query, "--min-chars", str(args.wrapper_min_chars), "--always-chars", str(args.wrapper_always_chars), "--max-compress-chars", str(args.wrapper_max_compress_chars), "--", *command]
    info["lane"] = lane
    info["recommended_command"] = wrapped if info["decision"] == "wrap" else command
    info["recommended_command_shell"] = shell_join(info["recommended_command"])
    if args.json or args.run:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print(f"decision: {info['decision']} ({info['confidence']})")
        print(f"lane: {lane}")
        print("reasons:")
        for reason in info["reasons"] or info["exact_reasons"]:
            print(f"- {reason}")
        print("recommended_command:")
        print(info["recommended_command_shell"])
    sys.stdout.flush()
    if not args.run:
        return 0
    if info["decision"] == "wrap":
        return worker_main(["--lane", lane, "--query", args.query, "--min-chars", str(args.wrapper_min_chars), "--always-chars", str(args.wrapper_always_chars), "--max-compress-chars", str(args.wrapper_max_compress_chars), "--", *command])
    return subprocess.run(command, check=False).returncode
