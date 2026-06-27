"""Tool handlers."""
from __future__ import annotations

import json
import re

from .proxy import retrieve

_HASH_RE = re.compile(r"(?:ccr:|hash=)?([A-Za-z0-9_.:-]{6,128})")


def _normalize_hash(raw: str) -> str:
    raw = (raw or "").strip().strip("<>")
    match = _HASH_RE.search(raw)
    return match.group(1) if match else ""


def handle_headroom_retrieve(args: dict, **kwargs) -> str:
    del kwargs
    hash_key = _normalize_hash(str(args.get("hash") or ""))
    query = str(args.get("query") or "").strip()
    if not hash_key:
        return json.dumps({"success": False, "error": "missing or invalid Headroom hash"}, ensure_ascii=False)
    result = retrieve(hash_key, query=query)
    return json.dumps(result, ensure_ascii=False)
