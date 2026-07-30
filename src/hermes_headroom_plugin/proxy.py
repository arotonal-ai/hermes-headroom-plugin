"""Headroom proxy endpoint resolution and small HTTP helpers."""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from .config import (
    DEFAULT_PROXY_URL,
    hermes_home,
    load_context_reduction_config,
    resolve_effective_config,
)
from .contracts import normalize_ccr_hash

SMOKE_SENTINEL = "SYNTHETIC_SENTINEL_HEADROOM_PLUGIN"
_MARKER_RE = re.compile(r"<<ccr:([^,>]+)")


class ProxyConfigurationError(ValueError):
    """Raised when proxy configuration is unsafe or invalid."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def is_loopback_proxy_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().lower()
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def remote_proxy_allowed(config: dict[str, Any] | None = None) -> bool:
    """Resolve the opt-in from canonical config when no explicit mapping is supplied."""
    return resolve_effective_config(raw_config=config).allow_remote_proxy


def validate_proxy_url(proxy_url: str, config: dict[str, Any] | None = None) -> str:
    proxy_url = str(proxy_url or "").strip().rstrip("/")
    parsed = urlparse(proxy_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProxyConfigurationError(f"invalid HEADROOM proxy URL: {proxy_url!r}")
    if not is_loopback_proxy_url(proxy_url) and not remote_proxy_allowed(config):
        raise ProxyConfigurationError(
            "remote Headroom proxy URL blocked by default; use loopback or set "
            "HEADROOM_ALLOW_REMOTE_PROXY=1 / context_reduction.allow_remote_proxy: true "
            "only for a controlled, trusted endpoint"
        )
    return proxy_url


def resolve_proxy_url(config: dict[str, Any] | None = None) -> str:
    """Resolve and validate the Headroom proxy URL without owner-local paths."""
    raw = config if isinstance(config, dict) else load_context_reduction_config()
    effective = resolve_effective_config(raw_config=raw)
    return validate_proxy_url(effective.proxy_url, raw)


def http_json(url: str, payload: dict[str, Any] | None = None, timeout: int = 15) -> tuple[int | None, dict[str, Any] | None, str]:
    try:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 loopback/default endpoint
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body), body[:500]
            except Exception:
                return resp.status, None, body[:500]
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        return exc.code, None, body
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def readyz(proxy_url: str | None = None) -> dict[str, Any]:
    try:
        proxy_url = validate_proxy_url(proxy_url) if proxy_url else resolve_proxy_url()
    except ProxyConfigurationError as exc:
        return {"ok": False, "status": None, "proxy_url": proxy_url, "body": str(exc), "error": "proxy_configuration_blocked"}
    proxy_url = proxy_url.rstrip("/")
    status, data, body = http_json(f"{proxy_url}/readyz", timeout=5)
    ok = status == 200 and isinstance(data, dict) and bool(data.get("ready", True))
    return {"ok": ok, "status": status, "proxy_url": proxy_url, "body": data or body}


def retrieve(hash_key: str, proxy_url: str | None = None) -> dict[str, Any]:
    """Retrieve the complete exact retained payload for one CCR hash."""
    hash_key = normalize_ccr_hash(hash_key)
    if not hash_key:
        return {"success": False, "error": "missing or invalid Headroom hash", "proxy_url": proxy_url}
    try:
        proxy_url = validate_proxy_url(proxy_url) if proxy_url else resolve_proxy_url()
    except ProxyConfigurationError as exc:
        return {"success": False, "error": f"proxy configuration blocked: {exc}", "proxy_url": proxy_url}
    proxy_url = proxy_url.rstrip("/")
    status, data, body = http_json(f"{proxy_url}/v1/retrieve", {"hash": hash_key}, timeout=30)
    if status != 200 or not isinstance(data, dict):
        return {"success": False, "error": f"headroom retrieve failed status={status} body={body}", "proxy_url": proxy_url}
    data.setdefault("success", True)
    data.setdefault("proxy_url", proxy_url)
    return data


def retrieve_stats(proxy_url: str | None = None) -> dict[str, Any]:
    """Read Headroom CCR retrieval/store stats without exposing admin/debug APIs."""
    try:
        proxy_url = validate_proxy_url(proxy_url) if proxy_url else resolve_proxy_url()
    except ProxyConfigurationError as exc:
        return {"success": False, "error": f"proxy configuration blocked: {exc}", "proxy_url": proxy_url}
    proxy_url = proxy_url.rstrip("/")
    status, data, body = http_json(f"{proxy_url}/v1/retrieve/stats", timeout=10)
    if status != 200 or not isinstance(data, dict):
        return {"success": False, "error": f"headroom retrieve stats failed status={status} body={body}", "proxy_url": proxy_url}
    return {"success": True, "proxy_url": proxy_url, **data}


def synthetic_messages(sentinel: str = SMOKE_SENTINEL) -> list[dict[str, Any]]:
    rows = []
    for i in range(220):
        rows.append({
            "session_id": f"synthetic-{i}",
            "title": "Synthetic Headroom Plugin Smoke",
            "messages": [{
                "role": "assistant",
                "content": "synthetic filler " * 80 + (sentinel if i == 137 else ""),
            }],
            "bookend_start": [],
            "bookend_end": [],
        })
    content = json.dumps({"results": rows}, ensure_ascii=False)
    return [
        {"role": "system", "content": "Compression smoke."},
        {"role": "user", "content": "Find the synthetic sentinel."},
        {"role": "tool", "tool_call_id": "synthetic", "name": "session_search", "content": content},
    ]


def _result_text(retrieved: dict[str, Any]) -> str:
    result = retrieved.get("result") if isinstance(retrieved.get("result"), dict) else retrieved
    text = str(result.get("original_content") or "") if isinstance(result, dict) else ""
    if isinstance(result, dict) and isinstance(result.get("results"), list):
        text += json.dumps(result.get("results"), ensure_ascii=False)
    return text


def compress_messages(messages: list[dict[str, Any]], model: str = "gpt-5.5", proxy_url: str | None = None) -> dict[str, Any]:
    try:
        proxy_url = validate_proxy_url(proxy_url) if proxy_url else resolve_proxy_url()
    except ProxyConfigurationError as exc:
        return {"ok": False, "error": f"proxy configuration blocked: {exc}", "proxy_url": proxy_url}
    proxy_url = proxy_url.rstrip("/")
    status, data, body = http_json(f"{proxy_url}/v1/compress", {"model": model, "messages": messages}, timeout=60)
    if status != 200 or not isinstance(data, dict):
        return {"ok": False, "error": f"compress failed status={status} body={body}", "proxy_url": proxy_url}
    data.setdefault("proxy_url", proxy_url)
    data.setdefault("ok", True)
    return data


def smoke(proxy_url: str | None = None, *, require_marker: bool = True) -> dict[str, Any]:
    try:
        proxy_url = validate_proxy_url(proxy_url) if proxy_url else resolve_proxy_url()
    except ProxyConfigurationError as exc:
        return {"ok": False, "phase": "config", "proxy_url": proxy_url, "error": f"proxy configuration blocked: {exc}"}
    proxy_url = proxy_url.rstrip("/")
    health = readyz(proxy_url)
    if not health.get("ok"):
        return {"ok": False, "phase": "readyz", "proxy_url": proxy_url, "readyz": health, "error": "proxy not ready"}

    sentinel = f"{SMOKE_SENTINEL}_{uuid.uuid4().hex}"
    compressed = compress_messages(synthetic_messages(sentinel), proxy_url=proxy_url)
    if not compressed.get("ok"):
        return {"ok": False, "phase": "compress", **compressed}

    text = json.dumps(compressed.get("messages"), ensure_ascii=False)
    markers = _MARKER_RE.findall(text)
    if not markers:
        return {
            "ok": not require_marker,
            "phase": "compress",
            "proxy_url": proxy_url,
            "error": "no CCR marker produced",
            "tokens_before": compressed.get("tokens_before"),
            "tokens_after": compressed.get("tokens_after"),
            "tokens_saved": compressed.get("tokens_saved"),
        }

    unique_markers = list(dict.fromkeys(marker.split()[0] for marker in markers))
    if len(unique_markers) != 1:
        return {
            "ok": False,
            "phase": "marker_integrity",
            "proxy_url": proxy_url,
            "error": "compression produced an ambiguous multipart marker set",
            "marker_count": len(unique_markers),
            "markers": unique_markers,
            "tokens_before": compressed.get("tokens_before"),
            "tokens_after": compressed.get("tokens_after"),
            "tokens_saved": compressed.get("tokens_saved"),
        }

    marker = unique_markers[0]
    retrieved = retrieve(marker, proxy_url=proxy_url)
    sentinel_found = sentinel in _result_text(retrieved)
    result = retrieved.get("result") if isinstance(retrieved.get("result"), dict) else retrieved
    retrieve_count = result.get("count") if isinstance(result, dict) else None
    ok = bool(retrieved.get("success", "error" not in retrieved)) and (sentinel_found or int(retrieve_count or 0) >= 1)
    return {
        "ok": ok,
        "phase": "retrieve" if ok else "retrieve_failed",
        "ts": utc_now(),
        "marker": marker,
        "tokens_before": compressed.get("tokens_before"),
        "tokens_after": compressed.get("tokens_after"),
        "tokens_saved": compressed.get("tokens_saved"),
        "retrieve_count": retrieve_count,
        "sentinel_found": sentinel_found,
        "proxy_url": proxy_url,
        "retrieve_success": retrieved.get("success"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Headroom proxy helper for Hermes plugin installations.")
    parser.add_argument("action", nargs="?", choices=["status", "smoke"], default="status")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = readyz() if args.action == "status" else smoke()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    elif args.action == "status":
        print(f"Headroom proxy status {'PASS' if result.get('ok') else 'FAIL'} · proxy={result.get('proxy_url')} status={result.get('status')}")
    else:
        print(f"Headroom smoke {'PASS' if result.get('ok') else 'FAIL'} · phase={result.get('phase')} proxy={result.get('proxy_url')} marker={result.get('marker', '-')}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
