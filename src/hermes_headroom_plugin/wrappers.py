"""Console-script placeholders for P1 wrapper migration."""
from __future__ import annotations

import sys


def _not_migrated(name: str) -> int:
    print(f"{name}: wrapper migration pending P1; use owner-local wrapper until migrated", file=sys.stderr)
    return 2


def worker_main() -> int:
    return _not_migrated("headroom-worker-lane")


def background_main() -> int:
    return _not_migrated("headroom-background-lane")


def preflight_main() -> int:
    return _not_migrated("headroom-command-preflight")
