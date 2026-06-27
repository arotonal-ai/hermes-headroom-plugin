"""Explicit route helpers; full migration is P3."""
from __future__ import annotations

import argparse
import json
import re

CHEAP_PATTERNS = [r"\b(summary|summarize|resumir|resumen|synthesis|síntesis|triage|audit|review|fan[- ]?in)\b"]
STRONG_PATTERNS = [r"\b(final|patch|diff|edit|secret|token|credential|delete|publish|deploy|protected)\b"]


def classify_prompt(prompt: str) -> dict:
    cheap_hits = [p for p in CHEAP_PATTERNS if re.search(p, prompt, flags=re.I)]
    strong_hits = [p for p in STRONG_PATTERNS if re.search(p, prompt, flags=re.I)]
    if strong_hits:
        route = "default"
        reason = "strong_or_sensitive_markers"
    elif cheap_hits:
        route = "cheap"
        reason = "cheap_eligible_markers"
    else:
        route = "default"
        reason = "ambiguous_fail_closed_to_default"
    return {"route": route, "reason": reason, "cheap_hits": cheap_hits, "strong_hits": strong_hits}


def smart_route_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="*")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = classify_prompt(" ".join(args.prompt))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else f"route={result['route']} reason={result['reason']}")
    return 0


def cost_route_main(argv: list[str] | None = None) -> int:
    del argv
    print("headroom-cost-route: advanced explicit provider route pending P3; default/global routing is unchanged")
    return 0
