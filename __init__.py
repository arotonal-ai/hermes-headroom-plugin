"""Directory-plugin shim for local development.

When this repository is symlinked/cloned under ~/.hermes/plugins/headroom_retrieve,
Hermes imports this file. Add src/ to sys.path and delegate to the packaged
entry point implementation.
"""
from pathlib import Path
import sys

_src = Path(__file__).resolve().parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from hermes_headroom_plugin import register  # noqa: E402,F401
