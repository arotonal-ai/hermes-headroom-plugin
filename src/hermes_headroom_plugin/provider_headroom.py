"""Headroom HTTP adapter for the provider-neutral reduction contract."""
from __future__ import annotations

from typing import Any, Mapping

from .contracts import (
    CompressionResult,
    ProviderHealth,
    ReductionContext,
    RetrievalResult,
    normalize_ccr_hash,
)
from .proxy import compress_messages, readyz, retrieve, retrieve_stats


class HeadroomReductionProvider:
    """Translate Headroom 0.31 HTTP responses into typed provider outcomes."""

    name = "headroom"

    def __init__(self, *, proxy_url: str | None = None, default_model: str = "gpt-5.5") -> None:
        self.proxy_url = proxy_url
        self.default_model = default_model

    def ready(self) -> ProviderHealth:
        result = readyz(proxy_url=self.proxy_url)
        is_ready = bool(result.get("ok") or result.get("ready") or result.get("success")) and not result.get("error")
        return ProviderHealth(
            ready=is_ready,
            provider=self.name,
            status=_status_code(result),
            detail=str(result.get("error") or result.get("detail") or result.get("body") or ""),
            endpoint=str(result.get("proxy_url") or self.proxy_url or ""),
        )

    def compress(self, payload: Any, context: ReductionContext | None = None) -> CompressionResult:
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            return CompressionResult(ok=False, error="payload must be a list of message mappings", provider=self.name)
        model = context.model if context else ""
        kwargs: dict[str, Any] = {}
        if model:
            kwargs["model"] = model
        if self.proxy_url is not None:
            kwargs["proxy_url"] = self.proxy_url
        result = compress_messages(payload, **kwargs)
        ok = bool(result.get("ok") or result.get("success")) and not result.get("error")
        markers = result.get("markers") if isinstance(result.get("markers"), list) else []
        marker = str(markers[0]) if markers else ""
        value = result.get("messages") if ok else None
        metrics = {
            key: result[key]
            for key in ("tokens_before", "tokens_after", "tokens_saved", "compression_ratio")
            if key in result
        }
        return CompressionResult(
            ok=ok,
            value=value,
            marker=marker,
            error=str(result.get("error") or ""),
            provider=self.name,
            metrics=metrics,
        )

    def retrieve(self, hash_key: str) -> RetrievalResult:
        normalized = normalize_ccr_hash(hash_key)
        if not normalized:
            return RetrievalResult(success=False, hash="", error="Missing or invalid hash", provider=self.name)
        result = retrieve(normalized, proxy_url=self.proxy_url)
        nested = result.get("result")
        payload = nested if isinstance(nested, Mapping) else result
        candidates = (payload, result) if payload is not result else (result,)
        content = next(
            (
                candidate[key]
                for candidate in candidates
                for key in ("content", "original_content")
                if isinstance(candidate.get(key), str)
            ),
            None,
        )
        has_exact_content = isinstance(content, str)
        status = _status_code(payload) or _status_code(result)
        error = str(result.get("error") or payload.get("error") or "")
        provider_success = bool(result.get("success"))
        if payload is not result and "success" in payload:
            provider_success = provider_success and bool(payload.get("success"))
        response_hash = str(payload.get("hash") or result.get("hash") or "")
        hash_mismatch = bool(response_hash) and normalize_ccr_hash(response_hash) != normalized
        if hash_mismatch:
            error = "Headroom retrieve response hash did not match the requested hash"
        if bool(result.get("success")) and not has_exact_content and not error:
            error = "Headroom retrieve response omitted exact content"
        success = provider_success and has_exact_content and not error and not hash_mismatch
        missing = status in {404, 410} or "expired" in error.lower() or "not found" in error.lower()
        return RetrievalResult(
            success=success,
            hash=normalized,
            content=content if success else None,
            error=error,
            provider=self.name,
            expired_or_missing=missing,
            exact=True,
        )

    def stats(self) -> Mapping[str, Any]:
        return dict(retrieve_stats(proxy_url=self.proxy_url))


def _status_code(result: Mapping[str, Any]) -> int | None:
    value = result.get("status")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
