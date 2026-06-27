"""Health audit for the installable Headroom plugin."""
from __future__ import annotations

import argparse
import json

from .policy import classify_data
from .proxy import readyz, resolve_proxy_url, smoke


def audit(include_smoke: bool = False) -> dict:
    health = readyz()
    policy_checks = {
        "secret_blocked": classify_data(data_class="secret_or_sensitive") == "blocked",
        "final_packet_exact": classify_data(data_class="final_packet") == "exact",
        "raw_log_compressible": classify_data(data_class="raw_log") == "compressible",
        "read_file_exact": classify_data(tool="read_file") == "exact",
    }
    result = {
        "ok": bool(health.get("ok")) and all(policy_checks.values()),
        "proxy_url": resolve_proxy_url(),
        "readyz": health,
        "policy_checks": policy_checks,
    }
    if include_smoke:
        smoke_result = smoke()
        result["smoke"] = smoke_result
        result["ok"] = result["ok"] and bool(smoke_result.get("ok"))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--smoke", action="store_true", help="also run compress→retrieve sentinel smoke")
    args = parser.parse_args(argv)
    result = audit(include_smoke=args.smoke)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Headroom health audit {'PASS' if result['ok'] else 'FAIL'} · proxy={result['proxy_url']} ready={result['readyz'].get('ok')}")
    return 0 if result["ok"] else 1
