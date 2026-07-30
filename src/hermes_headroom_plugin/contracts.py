"""Provider-neutral contracts for Hermes context reduction.

This module deliberately has no Headroom or Hermes runtime dependency. Provider
adapters implement these contracts; middleware consumes their typed outcomes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

_HASH_VALUE = r"[A-Za-z0-9_.:-]{6,128}"
_RAW_HASH_RE = re.compile(rf"^{_HASH_VALUE}$")
_CCR_MARKER_RE = re.compile(rf"<<ccr:({_HASH_VALUE})(?:,[^>]*)?>>", re.IGNORECASE)
_HASH_MARKER_RE = re.compile(rf"\b(?:hash|marker)\s*=\s*['\"]?({_HASH_VALUE})", re.IGNORECASE)
_CCR_PREFIX_RE = re.compile(rf"^ccr:({_HASH_VALUE})$", re.IGNORECASE)


def normalize_ccr_hash(raw: Any) -> str:
    """Return a validated CCR hash from a raw hash or known marker form.

    Arbitrary prose is rejected. Accepted forms are a bare 6-128 character
    hash, ``ccr:<hash>``, ``<<ccr:<hash>,...>>``, and text containing an
    explicit ``hash=<hash>``/``marker=<hash>`` attribute.
    """
    text = str(raw or "").strip().strip("`\"'")
    if not text:
        return ""
    for pattern in (_CCR_PREFIX_RE, _CCR_MARKER_RE, _HASH_MARKER_RE):
        match = pattern.search(text)
        if match and _RAW_HASH_RE.fullmatch(match.group(1)):
            return match.group(1)
    if _RAW_HASH_RE.fullmatch(text):
        return text
    return ""


@dataclass(frozen=True)
class ProviderHealth:
    ready: bool
    provider: str
    status: int | None = None
    detail: str = ""
    endpoint: str = ""


@dataclass(frozen=True)
class ReductionContext:
    model: str = ""
    tool_name: str = ""
    data_class: str = ""
    exact_required: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CompressionResult:
    ok: bool
    value: Any = None
    marker: str = ""
    markers: tuple[str, ...] = ()
    error: str = ""
    provider: str = ""
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def value_or(self, original: Any) -> Any:
        """Copy-on-write fail-open boundary used by middleware adapters."""
        return self.value if self.ok else original


@dataclass(frozen=True)
class RetrievalResult:
    success: bool
    hash: str
    content: Any = None
    error: str = ""
    provider: str = ""
    expired_or_missing: bool = False
    exact: bool = True


@runtime_checkable
class ReductionProvider(Protocol):
    """Minimal provider contract. Retrieval is exact and hash-only."""

    name: str

    def ready(self) -> ProviderHealth: ...

    def compress(self, payload: Any, context: ReductionContext | None = None) -> CompressionResult: ...

    def retrieve(self, hash_key: str) -> RetrievalResult: ...

    def stats(self) -> Mapping[str, Any]: ...
