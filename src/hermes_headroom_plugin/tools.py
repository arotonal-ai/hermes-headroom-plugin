"""Tool handlers."""
from __future__ import annotations

import json

from .contracts import normalize_ccr_hash
from .proxy import retrieve


def handle_headroom_retrieve(args: dict, **kwargs) -> str:
    del kwargs
    hash_key = normalize_ccr_hash(args.get("hash"))
    if not hash_key:
        return json.dumps({"success": False, "error": "missing or invalid Headroom hash"}, ensure_ascii=False)
    result = retrieve(hash_key)
    return json.dumps(result, ensure_ascii=False)
