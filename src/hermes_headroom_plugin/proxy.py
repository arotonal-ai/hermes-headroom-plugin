"""Headroom proxy endpoint resolution and small HTTP helpers."""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import yaml
except Exception:  # pragma: no cover - package can still report env/default status.
    yaml = None

DEFAULT_PROXY_URL = "http://127.0.0.1:28787"
DEFAULT_SERVICE = "hermes-context-reduction.service"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Headroom proxy helper for Hermes plugin installations.")
    parser.add_argument("action", nargs="?", choices=["status"], default="status")
    args = parser.parse_args(argv)
    if args.action == "status":
        print(json.dumps(readyz(), ensure_ascii=False, indent=2))
        return 0
    return 2
