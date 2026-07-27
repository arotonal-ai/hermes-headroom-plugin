from __future__ import annotations

import os
from pathlib import Path


COLLECTION_HOME = os.environ.get("HOME", "")
COLLECTION_HERMES_HOME = os.environ.get("HERMES_HOME", "")
COLLECTION_ISOLATED = os.environ.get("HERMES_HEADROOM_PYTEST_ISOLATED")


def test_pytest_collection_occurs_inside_isolated_home() -> None:
    assert COLLECTION_ISOLATED == "1"
    assert Path(COLLECTION_HERMES_HOME).parent == Path(COLLECTION_HOME)
    assert Path(COLLECTION_HOME).name.startswith("hermes-headroom-pytest-home-")
