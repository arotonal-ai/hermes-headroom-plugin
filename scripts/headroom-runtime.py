#!/usr/bin/env python3
"""Native-Git launcher for the packaged Headroom runtime manager."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hermes_headroom_plugin.runtime_manager import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
