"""Optional real-proxy retrieval smoke for quality-parity promotion evidence.

This test is skipped when no local Headroom proxy is ready. When it runs, it
exercises the real /v1/compress -> marker -> /v1/retrieve path for one
exact-header payload. It is intentionally small and local-loopback only.
"""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from hermes_headroom_plugin import middleware
from hermes_headroom_plugin.proxy import retrieve


def _large_orchestration_payload(sentinel: str, *, min_chars: int = 130_000) -> str:
    first = (
        f"kanban task_id={sentinel} run_id=11 status=in_progress "
        "acceptance=real-retrieval-smoke assignee=worker\n"
    )
    lines = [first]
    i = 0
    while sum(len(line) for line in lines) < min_chars:
        lines.append(f"history line={i} for {sentinel} path=/tmp/headroom-real-smoke/{i}\n")
        i += 1
    return "".join(lines)


class RealProxyQualitySmokeTest(unittest.TestCase):
    def test_real_proxy_marker_retrieves_exact_header_payload_sentinel(self):
        health = middleware.readyz()
        if not health.get("ok"):
            self.skipTest(f"Headroom proxy unavailable: {health}")

        sentinel = f"TASK-REAL-{uuid.uuid4().hex}"
        payload = _large_orchestration_payload(sentinel)
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.middleware.hermes_home", return_value=Path(td)
        ):
            reduced = middleware.on_tool_execution(
                tool_name="kanban_show",
                args={"data_class": "orchestration_fanin"},
                next_call=lambda current_args: payload,
            )

        self.assertIsInstance(reduced, str)
        self.assertIn("Headroom auto-compressed tool result", reduced)
        self.assertIn("classification: orchestration_fanin", reduced)
        self.assertIn(sentinel, reduced)

        markers = middleware._extract_markers([{"content": reduced}])
        self.assertTrue(markers, reduced[:500])

        retrieved = retrieve(markers[0], query=sentinel)
        self.assertTrue(retrieved.get("success", "error" not in retrieved), retrieved)
        self.assertIn(sentinel, str(retrieved))


if __name__ == "__main__":
    unittest.main()
