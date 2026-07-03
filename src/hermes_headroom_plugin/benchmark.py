"""Bounded Headroom adoption benchmark.

This module intentionally benchmarks the *loop/reporting layer* separately from
portable compression-first runtime behavior. It never mutates Hermes config or
runtime state.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

from .middleware import auto_compression_enabled
from .proxy import compress_messages, readyz, retrieve, resolve_proxy_url, utc_now

_MARKER_RE = re.compile(r"<<ccr:([^,>]+)")
DECISIONS = {"ADOPT_LOOP", "COMPRESSION_ONLY", "DISABLE_LOOP_REPORTING"}


@dataclass(frozen=True)
class BenchmarkConfig:
    samples: int = 3
    min_net_saved_chars: int = 20_000
    max_overhead_ratio: float = 0.15
    min_savings_ratio: float = 0.30
    model: str = "gpt-5.5"
    proxy_url: str | None = None


def _synthetic_payload(sample: int, sentinel: str) -> str:
    rows: list[dict[str, Any]] = []
    for idx in range(160 + sample * 40):
        rows.append(
            {
                "session_id": f"adoption-benchmark-{sample}-{idx}",
                "title": "Synthetic Headroom Adoption Benchmark",
                "messages": [
                    {
                        "role": "assistant",
                        "content": (
                            "synthetic benchmark diagnostic trace line; repeated noisy build/test output; "
                            "source retained by CCR store; exact final claims must verify from retrieval. " * 5
                        )
                        + (sentinel if idx == 37 else ""),
                    }
                ],
                "bookend_start": [],
                "bookend_end": [],
            }
        )
    return json.dumps({"results": rows}, ensure_ascii=False)


def synthetic_messages(sample: int, sentinel: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "Headroom adoption benchmark. Compress only bulky intermediate diagnostics."},
        {"role": "user", "content": "Find the synthetic adoption benchmark sentinel."},
        {"role": "tool", "name": "session_search", "tool_call_id": f"bench-{sample}", "content": _synthetic_payload(sample, sentinel)},
    ]


def _json_len(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _result_text(retrieved: dict[str, Any]) -> str:
    result = retrieved.get("result") if isinstance(retrieved.get("result"), dict) else retrieved
    if isinstance(result, dict):
        text = str(result.get("original_content") or "")
        if isinstance(result.get("results"), list):
            text += json.dumps(result.get("results"), ensure_ascii=False)
        return text
    return str(retrieved)


def _extract_marker(compressed: dict[str, Any]) -> str | None:
    text = json.dumps(compressed.get("messages"), ensure_ascii=False)
    match = _MARKER_RE.search(text)
    return match.group(1).split()[0] if match else None


def _decision(*, runtime_ok: bool, quality_ok: bool, net_saved_chars: int, savings_ratio: float, overhead_ratio: float, config: BenchmarkConfig) -> str:
    if not runtime_ok or not quality_ok:
        return "DISABLE_LOOP_REPORTING"
    if net_saved_chars >= config.min_net_saved_chars and savings_ratio >= config.min_savings_ratio and overhead_ratio <= config.max_overhead_ratio:
        return "ADOPT_LOOP"
    if net_saved_chars > 0 and quality_ok:
        return "COMPRESSION_ONLY"
    return "DISABLE_LOOP_REPORTING"


def run_benchmark(config: BenchmarkConfig | None = None) -> dict[str, Any]:
    cfg = config or BenchmarkConfig()
    started = time.perf_counter()
    try:
        proxy_url = cfg.proxy_url or resolve_proxy_url()
    except Exception as exc:
        return {
            "schema": "headroom-adoption-benchmark/v1",
            "ts": utc_now(),
            "decision": "DISABLE_LOOP_REPORTING",
            "status": "RUNTIME_PARTIAL",
            "runtime_ok": False,
            "quality": "fail",
            "error": f"proxy configuration failed: {type(exc).__name__}: {exc}",
            "next": "Fix proxy configuration or keep plugin installed without adopting the loop/reporting layer.",
        }

    health = readyz(proxy_url)
    runtime_ok = bool(health.get("ok"))
    samples: list[dict[str, Any]] = []
    chars_before = 0
    chars_after = 0
    token_saved_values: list[int] = []
    quality_hits = 0
    errors: list[str] = []

    if runtime_ok:
        for sample in range(max(1, cfg.samples)):
            sentinel = f"HEADROOM_ADOPTION_BENCHMARK_SENTINEL_{sample}_{int(started * 1000)}"
            messages = synthetic_messages(sample, sentinel)
            before = _json_len(messages)
            compressed = compress_messages(messages, model=cfg.model, proxy_url=proxy_url)
            if not compressed.get("ok"):
                errors.append(str(compressed.get("error") or "compress failed"))
                samples.append({"sample": sample, "ok": False, "chars_before": before, "error": errors[-1]})
                chars_before += before
                continue
            marker = _extract_marker(compressed)
            after = _json_len(compressed.get("messages"))
            retrieved_ok = False
            if marker:
                retrieved = retrieve(marker, query=sentinel, proxy_url=proxy_url)
                retrieved_ok = bool(retrieved.get("success", "error" not in retrieved)) and sentinel in _result_text(retrieved)
            if retrieved_ok:
                quality_hits += 1
            tokens_saved = int(compressed.get("tokens_saved") or 0)
            token_saved_values.append(tokens_saved)
            chars_before += before
            chars_after += after
            samples.append(
                {
                    "sample": sample,
                    "ok": True,
                    "chars_before": before,
                    "chars_after": after,
                    "chars_saved": max(0, before - after),
                    "tokens_before": compressed.get("tokens_before"),
                    "tokens_after": compressed.get("tokens_after"),
                    "tokens_saved": tokens_saved,
                    "marker_present": bool(marker),
                    "retrieval_quality": "pass" if retrieved_ok else "fail",
                }
            )
    else:
        errors.append("proxy not ready")

    elapsed_s = round(time.perf_counter() - started, 3)
    # Approximate the loop/reporting overhead as the final report size plus a
    # small fixed control header. This is intentionally conservative and local;
    # the compression path itself is measured through runtime token/char deltas.
    preliminary = {
        "schema": "headroom-adoption-benchmark/v1",
        "samples": samples,
        "errors": errors,
        "elapsed_s": elapsed_s,
    }
    overhead_chars = _json_len(preliminary) + 600
    saved_chars = max(0, chars_before - chars_after)
    net_saved_chars = saved_chars - overhead_chars
    savings_ratio = round(saved_chars / chars_before, 4) if chars_before else 0.0
    overhead_ratio = round(overhead_chars / max(saved_chars, 1), 4)
    quality_ok = runtime_ok and quality_hits == max(1, cfg.samples)
    decision = _decision(
        runtime_ok=runtime_ok,
        quality_ok=quality_ok,
        net_saved_chars=net_saved_chars,
        savings_ratio=savings_ratio,
        overhead_ratio=overhead_ratio,
        config=cfg,
    )
    next_by_decision = {
        "ADOPT_LOOP": "Adopt bounded loop reporting for this instance; savings exceed overhead and retrieval quality passed.",
        "COMPRESSION_ONLY": "Keep plugin/runtime compression active, but do not promote recurring loop reports yet.",
        "DISABLE_LOOP_REPORTING": "Do not adopt loop/reporting for this instance; fix runtime/quality or keep compression-only behavior.",
    }
    return {
        "schema": "headroom-adoption-benchmark/v1",
        "ts": utc_now(),
        "decision": decision,
        "status": "RUNTIME_FULL" if runtime_ok else "RUNTIME_PARTIAL",
        "runtime_ok": runtime_ok,
        "auto_compression": "on" if auto_compression_enabled() else "manual",
        "proxy_url": proxy_url,
        "quality": "pass" if quality_ok else "fail",
        "samples": samples,
        "metrics": {
            "chars_before": chars_before,
            "chars_after": chars_after,
            "chars_saved": saved_chars,
            "overhead_chars": overhead_chars,
            "net_saved_chars": net_saved_chars,
            "savings_ratio": savings_ratio,
            "overhead_ratio": overhead_ratio,
            "tokens_saved_total": sum(token_saved_values),
            "elapsed_s": elapsed_s,
        },
        "thresholds": {
            "min_net_saved_chars": cfg.min_net_saved_chars,
            "min_savings_ratio": cfg.min_savings_ratio,
            "max_overhead_ratio": cfg.max_overhead_ratio,
        },
        "errors": errors,
        "next": next_by_decision[decision],
    }


def _format_text(report: dict[str, Any]) -> str:
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    return " · ".join(
        [
            f"Headroom adoption benchmark {report.get('decision')}",
            f"status={report.get('status')}",
            f"auto={report.get('auto_compression')}",
            f"quality={report.get('quality')}",
            f"net_saved_chars={metrics.get('net_saved_chars')}",
            f"saved_chars={metrics.get('chars_saved')}",
            f"overhead_chars={metrics.get('overhead_chars')}",
            f"overhead_ratio={metrics.get('overhead_ratio')}",
            f"next={report.get('next')}",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a bounded Headroom loop-adoption benchmark.")
    parser.add_argument("--samples", type=int, default=3, help="Synthetic samples to compress/retrieve (default: 3).")
    parser.add_argument("--min-net-saved-chars", type=int, default=20_000)
    parser.add_argument("--max-overhead-ratio", type=float, default=0.15)
    parser.add_argument("--min-savings-ratio", type=float, default=0.30)
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--proxy-url", default=None)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument("--output", help="Optional path to write the JSON report.")
    args = parser.parse_args(argv)
    report = run_benchmark(
        BenchmarkConfig(
            samples=args.samples,
            min_net_saved_chars=args.min_net_saved_chars,
            max_overhead_ratio=args.max_overhead_ratio,
            min_savings_ratio=args.min_savings_ratio,
            model=args.model,
            proxy_url=args.proxy_url,
        )
    )
    if args.output:
        from pathlib import Path

        path = Path(args.output).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True) if args.format == "json" else _format_text(report))
    return 0 if report.get("decision") in DECISIONS else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
