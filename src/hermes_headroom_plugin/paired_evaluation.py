"""Deterministic paired exact-vs-reduced evaluation with task-level net accounting.

The evaluator consumes retained paired evidence. It does not call a model, mutate
Hermes state, or claim provider/billing truth from synthetic inputs. Reports are
content-free: hashes, lengths, invariant outcomes, ledger totals and authority
coverage only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .net_ledger import build_net_ledger

PAIRED_SCHEMA = "headroom.paired_evaluation.v1"
CASE_SCHEMA = "headroom.paired_case.v1"
REQUIRED_COHORTS = (
    "code",
    "json",
    "logs",
    "tables",
    "documents",
    "browsing",
    "instructions",
    "adversarial",
)
REAL_PROVIDER_EVIDENCE = {"provider_reported_disposable", "provider_reported_operational"}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _literal_refs(values: list[str]) -> list[dict[str, Any]]:
    return [{"index": index, "sha256": _sha256(value)} for index, value in enumerate(values)]


def _wilson_interval(successes: int, total: int, *, z: float = 1.959963984540054) -> dict[str, float | None]:
    if total <= 0:
        return {"lower": None, "upper": None}
    p = successes / total
    denom = 1 + (z * z / total)
    center = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt((p * (1 - p) / total) + (z * z / (4 * total * total))) / denom
    return {"lower": round(max(0.0, center - margin), 6), "upper": round(min(1.0, center + margin), 6)}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL {path}:{line_no}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"paired case must be an object: {path}:{line_no}")
        cases.append(value)
    return cases


def _score_case(case: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    case_id = str(case.get("id") or "").strip()
    cohort = str(case.get("cohort") or "").strip().lower()
    exact = case.get("exact_text")
    variant = case.get("variant_text")
    if not case_id or not cohort or not isinstance(exact, str) or not isinstance(variant, str):
        raise ValueError("every case requires id, cohort, exact_text and variant_text")

    evidence_class = str(case.get("evidence_class") or "synthetic_contract")
    policy_outcome = str(case.get("policy_outcome") or "")
    lifecycle_age = str(case.get("lifecycle_age") or "hot")
    admitted = bool(case.get("admitted_reduction"))
    exact_required = bool(case.get("exact_required"))
    critical = bool(case.get("critical", True))
    required = [str(item) for item in case.get("required_literals") or []]
    forbidden = [str(item) for item in case.get("forbidden_literals") or []]

    corpus_errors: list[str] = []
    if case.get("schema") != CASE_SCHEMA:
        corpus_errors.append("unsupported_case_schema")
    if admitted and exact_required:
        corpus_errors.append("admitted_reduction_conflicts_with_exact_required")
    if policy_outcome == "always_exact" and not exact_required:
        corpus_errors.append("always_exact_case_must_require_exact")
    if policy_outcome == "hot_exact_then_cold_compact" and lifecycle_age == "hot" and not exact_required:
        corpus_errors.append("hot_case_must_require_exact")
    if admitted and len(variant) >= len(exact):
        corpus_errors.append("admitted_variant_not_smaller")

    missing_from_exact = [item for item in required if item not in exact]
    missing_from_variant = [item for item in required if item not in variant]
    forbidden_in_variant = [item for item in forbidden if item in variant]
    exact_bytes_equal = exact.encode("utf-8") == variant.encode("utf-8")
    outcome_parity = _canonical(case.get("baseline_outcome")) == _canonical(case.get("variant_outcome"))

    checks = {
        "corpus_valid": not corpus_errors and not missing_from_exact,
        "required_literals_preserved": not missing_from_variant,
        "forbidden_literals_absent": not forbidden_in_variant,
        "outcome_parity": outcome_parity,
        "exact_bytes_equal_when_required": exact_bytes_equal if exact_required else True,
    }
    task_success = all(checks.values())
    baseline_latency = max(0, _as_int((case.get("latency_ms") or {}).get("baseline")))
    variant_latency = max(0, _as_int((case.get("latency_ms") or {}).get("variant")))
    logical_source_id = _sha256(f"{case_id}\0{_sha256(exact)}")
    marker = _sha256(exact)[:12]

    events: list[dict[str, Any]] = []
    if admitted:
        events.append(
            {
                "type": "headroom_tool_result",
                "event_id": f"source:{case_id}",
                "action": "compressed",
                "logical_source_id": logical_source_id,
                "source_sha256": _sha256(exact),
                "marker": marker,
                "original_chars": len(exact),
                "model_facing_chars_before": len(exact),
                "model_facing_chars_after": len(variant),
                "task_id": case_id,
                "turn_id": case_id,
                "compression_latency_ms": max(0, variant_latency - baseline_latency),
            }
        )
        retrieval_chars = max(0, _as_int(case.get("retrieval_reintroduced_chars")))
        if retrieval_chars:
            events.append(
                {
                    "type": "headroom_retrieval",
                    "event_id": f"retrieval:{case_id}",
                    "dedupe_key": f"retrieval:{case_id}",
                    "logical_source_id": logical_source_id,
                    "marker": marker,
                    "task_id": case_id,
                    "turn_id": case_id,
                    "tool_call_id": f"retrieve:{case_id}",
                    "model_facing_chars": retrieval_chars,
                }
            )
        retry_tokens = max(0, _as_int(case.get("retry_input_tokens")))
        extra_tokens = max(0, _as_int(case.get("extra_call_input_tokens")))
        correction_tokens = max(0, _as_int(case.get("quality_correction_input_tokens")))
        if retry_tokens or extra_tokens or correction_tokens:
            events.append(
                {
                    "type": "headroom_retry",
                    "event_id": f"overhead:{case_id}",
                    "logical_source_id": logical_source_id,
                    "task_id": case_id,
                    "turn_id": case_id,
                    "retry_input_tokens": retry_tokens,
                    "extra_call_input_tokens": extra_tokens,
                    "quality_correction_input_tokens": correction_tokens,
                }
            )
        provider = case.get("provider_usage")
        if isinstance(provider, dict):
            request_id = str(provider.get("api_request_id") or f"provider:{case_id}")
            events.append(
                {
                    "type": "provider_usage",
                    "event_id": f"provider:{case_id}",
                    "logical_source_id": logical_source_id,
                    "task_id": case_id,
                    "turn_id": case_id,
                    "api_request_id": request_id,
                    "provider": provider.get("provider"),
                    "model": provider.get("model"),
                    "prompt_tokens": provider.get("prompt_tokens"),
                    "input_tokens": provider.get("input_tokens"),
                    "cache_read_tokens": provider.get("cache_read_tokens"),
                    "cache_write_tokens": provider.get("cache_write_tokens"),
                    "output_tokens": provider.get("output_tokens"),
                    "total_tokens": provider.get("total_tokens"),
                    "billing_authority": provider.get("billing_authority") or "none",
                    "evidence_class": evidence_class,
                }
            )
    events.append(
        {
            "type": "headroom_task_result",
            "event_id": f"task:{case_id}",
            "task_id": case_id,
            "turn_id": case_id,
            "success": task_success,
        }
    )

    result = {
        "id": case_id,
        "cohort": cohort,
        "evidence_class": evidence_class,
        "critical": critical,
        "policy_outcome": policy_outcome,
        "lifecycle_age": lifecycle_age,
        "admitted_reduction": admitted,
        "exact_required": exact_required,
        "task_success": task_success,
        "checks": checks,
        "failures": {
            "corpus_errors": corpus_errors,
            "missing_from_exact": _literal_refs(missing_from_exact),
            "missing_from_variant": _literal_refs(missing_from_variant),
            "forbidden_in_variant": _literal_refs(forbidden_in_variant),
        },
        "exact": {"chars": len(exact), "bytes": len(exact.encode("utf-8")), "sha256": _sha256(exact)},
        "variant": {"chars": len(variant), "bytes": len(variant.encode("utf-8")), "sha256": _sha256(variant)},
        "latency_ms": {
            "baseline": baseline_latency,
            "variant": variant_latency,
            "delta": variant_latency - baseline_latency,
        },
    }
    return result, events


def evaluate_cases(
    cases: Iterable[dict[str, Any]],
    *,
    required_cohorts: Iterable[str] = REQUIRED_COHORTS,
    max_latency_regression_ms: int = 250,
) -> dict[str, Any]:
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    provider_evidence_classes: set[str] = set()
    cache_fields_seen = False
    billing_evidence_seen = False

    for case in cases:
        result, case_events = _score_case(case)
        results.append(result)
        events.extend(case_events)
        for event in case_events:
            if event.get("type") == "provider_usage":
                provider_evidence_classes.add(str(event.get("evidence_class") or "unknown"))
                cache_fields_seen = cache_fields_seen or event.get("cache_read_tokens") is not None or event.get("cache_write_tokens") is not None
                billing_evidence_seen = billing_evidence_seen or (
                    str(event.get("evidence_class") or "") in REAL_PROVIDER_EVIDENCE
                    and str(event.get("billing_authority") or "none") not in {"", "none", "synthetic", "unavailable"}
                )

    ledger = build_net_ledger(events)
    rows_by_source = {str(row.get("source_id")): row for row in ledger.get("rows") or []}
    source_by_case = {
        item["id"]: _sha256(f"{item['id']}\0{item['exact']['sha256']}")
        for item in results
        if item.get("admitted_reduction")
    }
    for item in results:
        source_id = source_by_case.get(item["id"])
        item["net_ledger"] = rows_by_source.get(source_id) if source_id else None

    required = {str(item).lower() for item in required_cohorts}
    observed = {item["cohort"] for item in results}
    missing_cohorts = sorted(required - observed)
    successes = sum(1 for item in results if item["task_success"])
    critical_failures = [item["id"] for item in results if item["critical"] and not item["task_success"]]
    exact_failures = [item["id"] for item in results if item["exact_required"] and not item["checks"]["exact_bytes_equal_when_required"]]
    latency_failures = [
        item["id"]
        for item in results
        if item["latency_ms"]["delta"] > max(0, int(max_latency_regression_ms))
    ]

    cohort_net: dict[str, int] = defaultdict(int)
    admitted_cohorts: set[str] = set()
    for item in results:
        row = item.get("net_ledger")
        if item.get("admitted_reduction"):
            admitted_cohorts.add(item["cohort"])
            cohort_net[item["cohort"]] += _as_int((row or {}).get("net_est_tokens_saved"))
    non_positive_net_cohorts = sorted(cohort for cohort in admitted_cohorts if cohort_net.get(cohort, 0) <= 0)

    provider_real = bool(provider_evidence_classes & REAL_PROVIDER_EVIDENCE)
    if provider_real:
        provider_status = "OBSERVED_REAL"
    elif provider_evidence_classes:
        provider_status = "SYNTHETIC_ONLY"
    else:
        provider_status = "MISSING"
    cache_status = "OBSERVED_REAL" if provider_real and cache_fields_seen else "SYNTHETIC_ONLY" if cache_fields_seen else "MISSING"
    billing_status = "OBSERVED_REAL" if billing_evidence_seen else "MISSING"

    gate_failures: list[str] = []
    if not results:
        gate_failures.append("no_cases")
    if missing_cohorts:
        gate_failures.append("missing_required_cohorts")
    if critical_failures:
        gate_failures.append("critical_task_or_invariant_failure")
    if exact_failures:
        gate_failures.append("exact_contract_failure")
    if non_positive_net_cohorts:
        gate_failures.append("non_positive_net_for_admitted_cohort")
    if latency_failures:
        gate_failures.append("latency_regression")

    gate = "PASS" if not gate_failures else "FAIL"
    synthetic_only = not provider_real
    decision = (
        "PASS_SYNTHETIC_CONTRACT__REAL_PROVIDER_CACHE_EVIDENCE_MISSING"
        if gate == "PASS" and synthetic_only
        else "PASS_PAIRED_EVALUATION"
        if gate == "PASS"
        else "FAIL_PAIRED_EVALUATION"
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "schema": PAIRED_SCHEMA,
        "gate": gate,
        "decision": decision,
        "promotion_eligible": False,
        "cases": results,
        "cohorts": {
            "required": sorted(required),
            "observed": sorted(observed),
            "missing": missing_cohorts,
            "admitted": sorted(admitted_cohorts),
            "net_est_tokens_saved": dict(sorted(cohort_net.items())),
            "non_positive_net": non_positive_net_cohorts,
        },
        "task_quality": {
            "successes": successes,
            "total": len(results),
            "success_rate": round(successes / len(results), 6) if results else 0.0,
            "wilson_95": _wilson_interval(successes, len(results)),
            "critical_failures": critical_failures,
            "exact_failures": exact_failures,
        },
        "latency": {
            "max_allowed_regression_ms": max(0, int(max_latency_regression_ms)),
            "failures": latency_failures,
            "mean_delta_ms": round(sum(item["latency_ms"]["delta"] for item in results) / len(results), 3) if results else 0.0,
        },
        "net_ledger": ledger,
        "authority_coverage": {
            "paired_content": sorted({item["evidence_class"] for item in results}),
            "provider_usage": provider_status,
            "provider_cache": cache_status,
            "billing": billing_status,
            "provider_evidence_classes": sorted(provider_evidence_classes),
        },
        "gate_failures": gate_failures,
        "limits": [
            "Deterministic paired evidence is not a model-quality or billing canary.",
            "Synthetic provider/cache fields validate accounting semantics only and never satisfy real provider authority.",
            "Report content is limited to hashes, sizes, checks and authority-labelled metrics.",
            "A PASS never authorizes production, provider routing, paid calls, Headroom 0.33, or A3 promotion.",
        ],
        "next_gate": "DISPOSABLE_REAL_PROVIDER_CACHE_TASK_EVIDENCE" if gate == "PASS" and synthetic_only else "HEADROOM_033_ISOLATED_COMPATIBILITY_CANARY" if gate == "PASS" else "FIX_PAIRED_EVALUATION_FAILURES",
        "elapsed_ms": elapsed_ms,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score retained exact-vs-reduced Headroom paired evidence.")
    parser.add_argument("--input", required=True, help="JSONL paired-case evidence")
    parser.add_argument("--output", help="write content-free JSON report")
    parser.add_argument("--max-latency-regression-ms", type=int, default=250)
    args = parser.parse_args(argv)
    try:
        report = evaluate_cases(
            load_jsonl(Path(args.input).expanduser()),
            max_latency_regression_ms=args.max_latency_regression_ms,
        )
    except (OSError, ValueError) as exc:
        print(f"paired evaluation input error: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if report["gate"] == "PASS" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
