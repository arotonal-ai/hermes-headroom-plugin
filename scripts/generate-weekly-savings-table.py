#!/usr/bin/env python3
"""Generate evidence-backed weekly Headroom savings tables.

Input is JSONL with objects such as:
{"timestamp":"2026-06-29T12:00:00Z","lane":"debug","data_class":"raw_log","tokens_before":120000,"tokens_after":18000,"retrieval_verified":true,"fail_closed":false}

No input produces a placeholder table; the script never invents metrics.
"""
from __future__ import annotations

import argparse
import glob
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

HEADER = """# Weekly Headroom savings\n\nPublished savings must be generated from retained JSONL evidence. Do not hand-enter estimates as measured results.\n\n"""


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def monday(dt: datetime) -> str:
    start = (dt - timedelta(days=dt.weekday())).date()
    return start.isoformat()


def iter_records(patterns: list[str]):
    for pattern in patterns:
        for name in sorted(glob.glob(pattern)):
            path = Path(name)
            if not path.exists() or path.is_dir():
                continue
            for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"Invalid JSONL {path}:{line_no}: {exc}") from exc
                row["_source"] = f"{path}:{line_no}"
                yield row


def as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def pct(saved: int, before: int) -> str:
    if before <= 0:
        return "—"
    return f"{(saved / before) * 100:.1f}%"


def format_int(value: int) -> str:
    return f"{value:,}" if value else "—"


def render(patterns: list[str]) -> str:
    weekly: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"runs": 0, "before": 0, "after": 0, "saved": 0, "retrieval": 0, "fail_closed": 0})
    classes: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"samples": 0, "before": 0, "after": 0, "saved": 0, "retrieval": 0})
    sources = set()

    for row in iter_records(patterns):
        dt = parse_timestamp(row.get("timestamp") or row.get("time") or row.get("created_at"))
        if dt is None:
            continue
        week = monday(dt)
        lane = str(row.get("lane") or "unspecified")
        data_class = str(row.get("data_class") or "unspecified")
        before = as_int(row.get("tokens_before"))
        after = as_int(row.get("tokens_after"))
        saved = as_int(row.get("tokens_saved")) or max(before - after, 0)
        key = (week, lane)
        weekly[key]["runs"] += 1
        weekly[key]["before"] += before
        weekly[key]["after"] += after
        weekly[key]["saved"] += saved
        weekly[key]["retrieval"] += 1 if row.get("retrieval_verified") is True else 0
        weekly[key]["fail_closed"] += 1 if row.get("fail_closed") is True else 0

        ckey = (week, data_class)
        classes[ckey]["samples"] += 1
        classes[ckey]["before"] += before
        classes[ckey]["after"] += after
        classes[ckey]["saved"] += saved
        classes[ckey]["retrieval"] += 1 if row.get("retrieval_verified") is True else 0
        sources.add(str(row.get("_source")))

    out = [HEADER]
    if not weekly:
        out.append("> Latest measured week: no published metrics yet. Add JSONL evidence under `docs/metrics/data/` and regenerate this file.\n\n")
        out.append("| Week starting Monday | Lane | Runs | Tokens before | Tokens after | Tokens saved | Savings % | Exact retrieval checks | Fail-closed events | Notes |\n")
        out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---|\n")
        out.append("| — | — | — | — | — | — | — | — | — | pending real data |\n\n")
        out.append("## By data class\n\n")
        out.append("| Week starting Monday | Data class | Policy | Samples | Avg before | Avg after | Saved | Verification |\n")
        out.append("|---|---|---|---:|---:|---:|---:|---|\n")
        out.append("| — | — | — | — | — | — | — | pending real data |\n")
        return "".join(out)

    latest_week = sorted({week for week, _ in weekly})[-1]
    total_before = sum(v["before"] for (week, _), v in weekly.items() if week == latest_week)
    total_saved = sum(v["saved"] for (week, _), v in weekly.items() if week == latest_week)
    out.append(f"> Latest measured week: `{latest_week}`; saved `{format_int(total_saved)}` of `{format_int(total_before)}` eligible intermediate tokens ({pct(total_saved, total_before)}).\n\n")
    out.append("| Week starting Monday | Lane | Runs | Tokens before | Tokens after | Tokens saved | Savings % | Exact retrieval checks | Fail-closed events | Notes |\n")
    out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---|\n")
    for (week, lane), v in sorted(weekly.items()):
        out.append(f"| {week} | `{lane}` | {v['runs']} | {format_int(v['before'])} | {format_int(v['after'])} | {format_int(v['saved'])} | {pct(v['saved'], v['before'])} | {v['retrieval']} | {v['fail_closed']} | evidence-backed |\n")

    out.append("\n## By data class\n\n")
    out.append("| Week starting Monday | Data class | Policy | Samples | Avg before | Avg after | Saved | Verification |\n")
    out.append("|---|---|---|---:|---:|---:|---:|---|\n")
    exact_classes = {"final_packet", "patch_diff", "manifest_hashes", "claim_ledger", "secret_or_sensitive", "memory_profile_instruction"}
    for (week, data_class), v in sorted(classes.items()):
        samples = max(v["samples"], 1)
        policy = "exact/blocked" if data_class in exact_classes else "compressible candidate"
        verification = f"{v['retrieval']} retrieval checks" if v["retrieval"] else "source retained"
        out.append(f"| {week} | `{data_class}` | {policy} | {v['samples']} | {format_int(v['before']//samples)} | {format_int(v['after']//samples)} | {format_int(v['saved'])} | {verification} |\n")
    out.append("\n## Evidence sources\n\n")
    for source in sorted(sources):
        out.append(f"- `{source}`\n")
    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate weekly Headroom savings Markdown from JSONL evidence.")
    parser.add_argument("--input", nargs="+", default=["docs/metrics/data/*.jsonl"], help="JSONL glob(s)")
    parser.add_argument("--write", help="write Markdown to this path instead of stdout")
    args = parser.parse_args(argv)
    text = render(args.input)
    if args.write:
        path = Path(args.write)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
