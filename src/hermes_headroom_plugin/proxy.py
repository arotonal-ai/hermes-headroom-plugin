"""Headroom proxy endpoint resolution and small HTTP helpers."""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import yaml
except Exception:  # pragma: no cover - package can still report env/default status.
    yaml = None

DEFAULT_PROXY_URL = "http://127.0.0.1:28787"
DEFAULT_SERVICE = "hermes-context-reduction.service"
SMOKE_SENTINEL = "SYNTHETIC_SENTINEL_HEADROOM_PLUGIN_P0"
_MARKER_RE = re.compile(r"<<ccr:([^,>]+)")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def load_context_reduction_config(home: Path | None = None) -> dict[str, Any]:
    home = home or hermes_home()
    path = home / "config.yaml"
    if yaml is None or not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    cr = data.get("context_reduction") if isinstance(data, dict) else {}
    return cr if isinstance(cr, dict) else {}


def resolve_proxy_url(config: dict[str, Any] | None = None) -> str:
    """Resolve the Headroom proxy URL without requiring owner-local paths."""
    config = config or load_context_reduction_config()
    cfg_url = str(config.get("proxy_url") or DEFAULT_PROXY_URL).strip().rstrip("/")
    parsed = urlparse(cfg_url)
    cfg_host = str(config.get("host") or parsed.hostname or "127.0.0.1").strip() or "127.0.0.1"
    cfg_port = int(config.get("port") or parsed.port or 28787)

    host = os.environ.get("HEADROOM_HOST")
    port = os.environ.get("HEADROOM_PORT")
    if host or port:
        return f"http://{(host or cfg_host).strip()}:{int(port or cfg_port)}"
    explicit = os.environ.get("HEADROOM_PROXY_URL")
    if explicit:
        return explicit.strip().rstrip("/")
    return f"http://{cfg_host}:{cfg_port}"


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
    proxy_url = (proxy_url or resolve_proxy_url()).rstrip("/")
    status, data, body = http_json(f"{proxy_url}/readyz", timeout=5)
    ok = status == 200 and isinstance(data, dict) and bool(data.get("ready", True))
    return {"ok": ok, "status": status, "proxy_url": proxy_url, "body": data or body}


def retrieve(hash_key: str, query: str = "", proxy_url: str | None = None) -> dict[str, Any]:
    proxy_url = (proxy_url or resolve_proxy_url()).rstrip("/")
    payload = {"hash": hash_key}
    if query:
        payload["query"] = query
    status, data, body = http_json(f"{proxy_url}/v1/retrieve", payload, timeout=30)
    if status != 200 or not isinstance(data, dict):
        return {"success": False, "error": f"headroom retrieve failed status={status} body={body}", "proxy_url": proxy_url}
    data.setdefault("success", True)
    data.setdefault("proxy_url", proxy_url)
    return data


def synthetic_messages() -> list[dict[str, Any]]:
    rows = []
    for i in range(220):
        rows.append({
            "session_id": f"synthetic-{i}",
            "title": "Synthetic Headroom Plugin Smoke",
            "messages": [{
                "role": "assistant",
                "content": "synthetic filler " * 80 + (SMOKE_SENTINEL if i == 137 else ""),
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
    proxy_url = (proxy_url or resolve_proxy_url()).rstrip("/")
    status, data, body = http_json(f"{proxy_url}/v1/compress", {"model": model, "messages": messages}, timeout=60)
    if status != 200 or not isinstance(data, dict):
        return {"ok": False, "error": f"compress failed status={status} body={body}", "proxy_url": proxy_url}
    data.setdefault("proxy_url", proxy_url)
    data.setdefault("ok", True)
    return data


def smoke(proxy_url: str | None = None, *, require_marker: bool = True) -> dict[str, Any]:
    proxy_url = (proxy_url or resolve_proxy_url()).rstrip("/")
    health = readyz(proxy_url)
    if not health.get("ok"):
        return {"ok": False, "phase": "readyz", "proxy_url": proxy_url, "readyz": health, "error": "proxy not ready"}

    compressed = compress_messages(synthetic_messages(), proxy_url=proxy_url)
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

    marker = markers[0].split()[0]
    retrieved = retrieve(marker, query=SMOKE_SENTINEL, proxy_url=proxy_url)
    sentinel_found = SMOKE_SENTINEL in _result_text(retrieved)
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
