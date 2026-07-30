"""Bounded retention for disposable Headroom middleware artifacts.

Canonical evidence must be promoted outside the ambient reports directory. This
module prunes only flat files in that directory, never directories or symlinks.
Failures are reported as metadata and never break tool execution.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_REPORT_MAX_BYTES,
    DEFAULT_REPORT_PRUNE_INTERVAL_SECONDS,
    DEFAULT_REPORT_RETENTION_DAYS,
    resolve_effective_config,
)
_PIN_SUFFIXES = (".keep", ".retain")
_LOCK = threading.Lock()
_LAST_PRUNE_MONOTONIC: dict[str, float] = {}


def retention_settings(config: dict[str, Any] | None = None) -> dict[str, int]:
    cfg = resolve_effective_config(raw_config=config if isinstance(config, dict) else None)
    return {
        "retention_days": cfg.report_retention_days,
        "max_bytes": cfg.report_max_bytes,
        "interval_seconds": cfg.report_prune_interval_seconds,
    }


def _artifact_group(path: Path) -> str:
    name = path.name
    for suffix in (".redacted.log", ".exact.log", ".compressed.json", ".json", ".log", ".md", ".txt", ".keep", ".retain"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _inventory(report_dir: Path) -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = defaultdict(list)
    try:
        entries = list(report_dir.iterdir())
    except OSError:
        return {}
    for path in entries:
        try:
            if path.is_symlink() or not path.is_file() or path.name.startswith("."):
                continue
        except OSError:
            continue
        groups[_artifact_group(path)].append(path)
    return dict(groups)


def _group_metadata(paths: list[Path]) -> tuple[int, float, bool]:
    total = 0
    newest_mtime = 0.0
    pinned = False
    for path in paths:
        if path.name.startswith("PINNED-") or path.name.endswith(_PIN_SUFFIXES):
            pinned = True
        try:
            stat_result = path.stat()
        except OSError:
            continue
        total += int(stat_result.st_size)
        newest_mtime = max(newest_mtime, float(stat_result.st_mtime))
    return total, newest_mtime, pinned


def prune_report_artifacts(
    report_dir: Path,
    *,
    retention_days: int = DEFAULT_REPORT_RETENTION_DAYS,
    max_bytes: int = DEFAULT_REPORT_MAX_BYTES,
    now: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Prune disposable report groups by age, then by total-size pressure."""
    report_dir = Path(report_dir)
    groups = _inventory(report_dir)
    metadata = {key: _group_metadata(paths) for key, paths in groups.items()}
    before_files = sum(len(paths) for paths in groups.values())
    before_bytes = sum(item[0] for item in metadata.values())
    cutoff = (time.time() if now is None else float(now)) - max(0, retention_days) * 86400

    selected: set[str] = set()
    if retention_days > 0:
        for key, (_size, newest_mtime, pinned) in metadata.items():
            if not pinned and newest_mtime and newest_mtime < cutoff:
                selected.add(key)

    remaining_bytes = before_bytes - sum(metadata[key][0] for key in selected)
    if max_bytes > 0 and remaining_bytes > max_bytes:
        candidates = sorted(
            (
                (metadata[key][1], key)
                for key in groups
                if key not in selected and not metadata[key][2]
            ),
            key=lambda item: (item[0], item[1]),
        )
        for _mtime, key in candidates:
            if remaining_bytes <= max_bytes:
                break
            selected.add(key)
            remaining_bytes -= metadata[key][0]

    deleted_files = 0
    deleted_bytes = 0
    errors = 0
    for key in sorted(selected):
        for path in groups[key]:
            size = 0
            try:
                size = int(path.stat().st_size)
                if not dry_run:
                    path.unlink()
                deleted_files += 1
                deleted_bytes += size
            except OSError:
                errors += 1

    return {
        "report_dir": str(report_dir),
        "dry_run": dry_run,
        "retention_days": retention_days,
        "max_bytes": max_bytes,
        "before_files": before_files,
        "before_bytes": before_bytes,
        "selected_groups": len(selected),
        "deleted_files": deleted_files,
        "deleted_bytes": deleted_bytes,
        "after_files": before_files - deleted_files if not dry_run else before_files,
        "after_bytes": before_bytes - deleted_bytes if not dry_run else before_bytes,
        "errors": errors,
    }


def maybe_prune_reports(
    report_dir: Path,
    *,
    config: dict[str, Any] | None = None,
    force: bool = False,
    dry_run: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Run bounded pruning at most once per configured interval per process/path."""
    settings = retention_settings(config)
    key = str(Path(report_dir).expanduser().resolve())
    monotonic_now = time.monotonic()
    with _LOCK:
        previous = _LAST_PRUNE_MONOTONIC.get(key)
        if not force and previous is not None and monotonic_now - previous < max(0, settings["interval_seconds"]):
            return {"report_dir": key, "skipped": True, "reason": "interval"}
        _LAST_PRUNE_MONOTONIC[key] = monotonic_now
    try:
        return prune_report_artifacts(
            Path(report_dir),
            retention_days=settings["retention_days"],
            max_bytes=settings["max_bytes"],
            now=now,
            dry_run=dry_run,
        )
    except Exception as exc:
        return {
            "report_dir": key,
            "skipped": False,
            "errors": 1,
            "error": f"{type(exc).__name__}: {exc}",
        }
