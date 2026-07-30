"""Profile-isolated, bounded local exact-source manifests.

The store is default-off for new writes. Existing exact entries remain readable so
disabling retention never strands a marker before its recorded expiry. Blobs are
content-addressed; provider CCR hashes are only aliases to a verified SHA-256.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import EffectiveConfig, hermes_home, resolve_effective_config
from .contracts import normalize_ccr_hash
from .policy import _contains_protected_control, _redact_text

LOCAL_EXACT_SCHEMA = "headroom.local_exact.v1"
EXACT = "exact"
REDACTED = "redacted"
EXPIRED = "expired"
MISSING = "missing"
CORRUPT = "corrupt"
DISABLED = "disabled"
NOT_ALLOWED = "not_allowed"
ERROR = "error"
_WINDOWS_MODE_BITS = os.name == "nt"
ALLOWED_DATA_CLASSES = frozenset(
    {
        "browser_debug_trace",
        "diagnostic_intermediate",
        "diagnostic_trace",
        "interaction_state",
        "qa_trace",
        "raw_log",
        "research_corpus",
        "research_corpus_raw",
        "source_readback",
        "worker_trace_raw",
    }
)


@dataclass(frozen=True)
class LocalSourceResult:
    state: str
    hash: str
    content: str | None = None
    sha256: str = ""
    byte_length: int = 0
    expires_at: str = ""
    source: str = "headroom_local_exact_manifest"
    manifest_path: str = ""
    error: str = ""

    @property
    def exact(self) -> bool:
        return self.state == EXACT and isinstance(self.content, str)

    def as_dict(self, *, include_content: bool = True, include_internal: bool = False) -> dict[str, Any]:
        payload = asdict(self)
        payload["success"] = self.exact
        payload["exact"] = self.exact
        if not include_content or self.content is None:
            payload.pop("content", None)
        if not include_internal:
            payload.pop("manifest_path", None)
        return payload


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _scope_id(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode("utf-8", errors="replace")).hexdigest()


def _root(home: Path | None = None, *, create: bool = False) -> Path:
    root = (home or hermes_home()) / "control-plane" / "headroom" / "exact-sources"
    if create:
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
    return root


def _private_file(path: Path) -> bool:
    """Reject links everywhere and group/other-readable files on POSIX.

    Windows ``st_mode`` exposes DOS compatibility bits rather than the NT ACL;
    files under the active profile inherit that profile's user-scoped ACL.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return False
        if _WINDOWS_MODE_BITS:
            return True
        return not bool(path.stat().st_mode & 0o077)
    except OSError:
        return False


def _manifest_path(root: Path, digest: str) -> Path:
    return root / f"{digest}.manifest.json"


def _blob_path(root: Path, digest: str) -> Path:
    return root / f"{digest}.payload"


def _alias_path(root: Path, alias: str) -> Path:
    return root / f"{alias}.alias.json"


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _atomic_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not _private_file(path):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return dict(value) if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _delete_blob(root: Path, digest: str) -> None:
    try:
        path = _blob_path(root, digest)
        if path.exists() and not path.is_symlink():
            path.unlink()
    except OSError:
        pass


def _expire_manifest(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    digest = str(manifest.get("sha256") or "")
    _delete_blob(root, digest)
    manifest = dict(manifest)
    manifest["state"] = EXPIRED
    manifest["expired_at"] = _utc_iso(time.time())
    manifest["blob"] = ""
    try:
        _atomic_json(_manifest_path(root, digest), manifest)
    except OSError:
        pass
    return manifest


def _prune(root: Path, cfg: EffectiveConfig, *, now: float) -> None:
    manifests: list[tuple[float, Path, dict[str, Any]]] = []
    total_bytes = 0
    for path in root.glob("*.manifest.json"):
        manifest = _read_json(path)
        if not manifest:
            continue
        created = float(manifest.get("created_epoch") or 0.0)
        expires = float(manifest.get("expires_epoch") or 0.0)
        if manifest.get("state") == EXACT and expires and expires <= now:
            manifest = _expire_manifest(root, manifest)
        if manifest.get("state") == EXACT:
            total_bytes += int(manifest.get("byte_length") or 0)
        manifests.append((created, path, manifest))

    manifests.sort(key=lambda item: item[0])
    exact_count = sum(1 for _, _, item in manifests if item.get("state") == EXACT)
    for _, _, manifest in manifests:
        if exact_count <= cfg.local_exact_max_entries and total_bytes <= cfg.local_exact_max_bytes:
            break
        if manifest.get("state") != EXACT:
            continue
        total_bytes -= int(manifest.get("byte_length") or 0)
        exact_count -= 1
        _expire_manifest(root, manifest)

    # Bound tombstones too; aliases to removed tombstones simply become missing.
    all_manifests = sorted(root.glob("*.manifest.json"), key=lambda path: path.stat().st_mtime)
    tombstone_limit = max(cfg.local_exact_max_entries * 2, 32)
    for path in all_manifests[:-tombstone_limit]:
        manifest = _read_json(path)
        if manifest and manifest.get("state") != EXACT:
            try:
                path.unlink()
            except OSError:
                pass

    aliases = sorted(root.glob("*.alias.json"), key=lambda path: path.stat().st_mtime)
    alias_limit = max(cfg.local_exact_max_entries * 2, 32)
    for path in aliases[:-alias_limit]:
        try:
            if not path.is_symlink():
                path.unlink()
        except OSError:
            pass


def retain_local_source(
    alias: str,
    content: str,
    *,
    data_class: str,
    tool_name: str,
    args: Mapping[str, Any] | None = None,
    home: Path | None = None,
    config: EffectiveConfig | None = None,
    now: float | None = None,
) -> LocalSourceResult:
    """Retain one allowlisted exact payload and map a CCR alias to its digest."""
    normalized_alias = normalize_ccr_hash(alias)
    if not normalized_alias or not isinstance(content, str):
        return LocalSourceResult(NOT_ALLOWED, normalized_alias, error="invalid alias or non-text content")
    cfg = config or resolve_effective_config(home=home)
    if not cfg.local_exact_enabled:
        return LocalSourceResult(DISABLED, normalized_alias)
    if str(data_class or "") not in ALLOWED_DATA_CLASSES:
        return LocalSourceResult(NOT_ALLOWED, normalized_alias, error="data class is not allowlisted")

    observed = time.time() if now is None else float(now)
    payload = content.encode("utf-8", errors="strict")
    digest = hashlib.sha256(payload).hexdigest()
    if len(payload) > cfg.local_exact_max_bytes:
        return LocalSourceResult(NOT_ALLOWED, normalized_alias, sha256=digest, byte_length=len(payload), error="payload exceeds local exact quota")
    root: Path | None = None
    manifest_existed = False
    try:
        root = _root(home, create=True)
        manifest_existed = _manifest_path(root, digest).exists()
        expires_epoch = observed + cfg.local_exact_ttl_seconds
        protected = _contains_protected_control(tool_name, dict(args or {}), content) or _redact_text(content) != content
        state = REDACTED if protected else EXACT
        manifest: dict[str, Any] = {
            "schema": LOCAL_EXACT_SCHEMA,
            "state": state,
            "sha256": digest,
            "byte_length": len(payload),
            "data_class": str(data_class),
            "tool_name": str(tool_name),
            "created_epoch": observed,
            "created_at": _utc_iso(observed),
            "expires_epoch": expires_epoch,
            "expires_at": _utc_iso(expires_epoch),
            "profile_scope_sha256": _scope_id(root),
            "blob": f"{digest}.payload" if state == EXACT else "",
        }
        if state == EXACT:
            _atomic_bytes(_blob_path(root, digest), payload)
        _atomic_json(_manifest_path(root, digest), manifest)
        _atomic_json(
            _alias_path(root, normalized_alias),
            {
                "schema": LOCAL_EXACT_SCHEMA,
                "alias": normalized_alias,
                "sha256": digest,
                "state": state,
                "profile_scope_sha256": _scope_id(root),
            },
        )
        _prune(root, cfg, now=observed)
    except (OSError, ValueError) as exc:
        if root is not None:
            try:
                alias_path = _alias_path(root, normalized_alias)
                if alias_path.exists() and not alias_path.is_symlink():
                    alias_path.unlink()
                if not manifest_existed:
                    _delete_blob(root, digest)
                    manifest_path = _manifest_path(root, digest)
                    if manifest_path.exists() and not manifest_path.is_symlink():
                        manifest_path.unlink()
            except OSError:
                pass
        return LocalSourceResult(ERROR, normalized_alias, sha256=digest, byte_length=len(payload), error=str(exc))
    if state != EXACT:
        return LocalSourceResult(REDACTED, normalized_alias, sha256=digest, byte_length=len(payload), expires_at=manifest["expires_at"], manifest_path=str(_manifest_path(root, digest)))
    return LocalSourceResult(EXACT, normalized_alias, content=content, sha256=digest, byte_length=len(payload), expires_at=manifest["expires_at"], manifest_path=str(_manifest_path(root, digest)))


def retrieve_local_source_result(hash_key: str, *, home: Path | None = None, now: float | None = None) -> LocalSourceResult:
    normalized = normalize_ccr_hash(hash_key)
    if not normalized:
        return LocalSourceResult(MISSING, "", error="missing or invalid hash")
    root = _root(home)
    if not root.exists():
        return LocalSourceResult(MISSING, normalized)

    digest = normalized
    alias = _read_json(_alias_path(root, normalized))
    if alias:
        if alias.get("profile_scope_sha256") != _scope_id(root):
            return LocalSourceResult(CORRUPT, normalized, error="profile scope mismatch")
        digest = str(alias.get("sha256") or "")
    manifest_path = _manifest_path(root, digest)
    manifest = _read_json(manifest_path)
    if not manifest:
        return LocalSourceResult(MISSING, normalized)
    if manifest.get("schema") != LOCAL_EXACT_SCHEMA or manifest.get("profile_scope_sha256") != _scope_id(root):
        return LocalSourceResult(CORRUPT, normalized, manifest_path=str(manifest_path), error="invalid manifest contract")

    state = str(manifest.get("state") or MISSING)
    byte_length = int(manifest.get("byte_length") or 0)
    expires_at = str(manifest.get("expires_at") or "")
    observed = time.time() if now is None else float(now)
    if state == EXPIRED or float(manifest.get("expires_epoch") or 0.0) <= observed:
        if state != EXPIRED:
            _expire_manifest(root, manifest)
        return LocalSourceResult(EXPIRED, normalized, sha256=digest, byte_length=byte_length, expires_at=expires_at, manifest_path=str(manifest_path))
    if state == REDACTED:
        return LocalSourceResult(REDACTED, normalized, sha256=digest, byte_length=byte_length, expires_at=expires_at, manifest_path=str(manifest_path))
    if state != EXACT:
        return LocalSourceResult(CORRUPT, normalized, sha256=digest, byte_length=byte_length, expires_at=expires_at, manifest_path=str(manifest_path), error=f"unknown manifest state: {state}")

    blob = _blob_path(root, digest)
    try:
        if not _private_file(blob):
            raise OSError("blob permissions are not 0600")
        payload = blob.read_bytes()
    except OSError as exc:
        return LocalSourceResult(CORRUPT, normalized, sha256=digest, byte_length=byte_length, expires_at=expires_at, manifest_path=str(manifest_path), error=str(exc))
    if len(payload) != byte_length or hashlib.sha256(payload).hexdigest() != digest:
        return LocalSourceResult(CORRUPT, normalized, sha256=digest, byte_length=byte_length, expires_at=expires_at, manifest_path=str(manifest_path), error="blob checksum or length mismatch")
    try:
        content = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        return LocalSourceResult(CORRUPT, normalized, sha256=digest, byte_length=byte_length, expires_at=expires_at, manifest_path=str(manifest_path), error=str(exc))
    return LocalSourceResult(EXACT, normalized, content=content, sha256=digest, byte_length=byte_length, expires_at=expires_at, manifest_path=str(manifest_path))


def retrieve_local_source(hash_key: str) -> str | None:
    """Backward-compatible exact-content hook used by ``headroom_retrieve``."""
    result = retrieve_local_source_result(hash_key)
    return result.content if result.exact else None


def local_exact_status(*, home: Path | None = None, config: EffectiveConfig | None = None) -> dict[str, Any]:
    """Return bounded metadata-only posture for the current profile store."""
    cfg = config or resolve_effective_config(home=home)
    root = _root(home)
    states: dict[str, int] = {}
    exact_bytes = 0
    corrupt = 0
    manifests = list(root.glob("*.manifest.json")) if root.exists() else []
    for path in manifests:
        manifest = _read_json(path)
        if not manifest:
            corrupt += 1
            continue
        state = str(manifest.get("state") or MISSING)
        states[state] = states.get(state, 0) + 1
        if state == EXACT:
            exact_bytes += int(manifest.get("byte_length") or 0)
    aliases = len(list(root.glob("*.alias.json"))) if root.exists() else 0
    return {
        "enabled": cfg.local_exact_enabled,
        "entries": len(manifests),
        "aliases": aliases,
        "exact_bytes": exact_bytes,
        "states": states,
        "corrupt": corrupt,
        "ttl_seconds": cfg.local_exact_ttl_seconds,
        "max_entries": cfg.local_exact_max_entries,
        "max_bytes": cfg.local_exact_max_bytes,
        "permissions": "0700_dir_0600_files",
        "profile_isolated": True,
    }
