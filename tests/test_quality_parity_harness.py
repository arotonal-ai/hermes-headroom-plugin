"""Deterministic e2e quality-parity harness for Headroom reduction.

These tests compare material decisions from exact synthetic contexts vs the
Headroom-reduced replacement. They intentionally avoid external LLM calls: a
small deterministic consumer extracts the same action-critical fields a model
would need for the next decision.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_headroom_plugin import middleware


def _large_payload(fact_line: str, *, min_chars: int = 130_000) -> str:
    lines = [fact_line.rstrip() + "\n"]
    i = 0
    while sum(len(line) for line in lines) < min_chars:
        lines.append(f"filler line={i} path=/tmp/headroom-quality/{i}\n")
        i += 1
    return "".join(lines)


def _compressed_response() -> dict:
    return {
        "ok": True,
        "tokens_before": 40000,
        "tokens_after": 300,
        "tokens_saved": 39700,
        "compression_ratio": 0.0075,
        "messages": [
            {
                "role": "tool",
                "name": "worker_trace",
                "content": "<<ccr:qualityparity001,base64,4KB>>",
            }
        ],
    }


def _field(text: str, label: str) -> str | None:
    # Line-local: do not let YAML-like section labels such as ``status:``
    # capture the dash from the next ``- status=...`` item.
    match = re.search(rf"(?im)(?:^|[ \t-]){re.escape(label)}[ \t]*[:=][ \t]*['\"]?([^\s\"',;}}\]]+)", text)
    return match.group(1) if match else None


def _first_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s\"'<>),;]+", text)
    return match.group(0) if match else None


def _has(text: str, needle: str) -> bool:
    return needle.lower() in text.lower()


def _debug_decision(text: str) -> dict[str, object]:
    return {
        "status": _field(text, "status"),
        "exit_code": _field(text, "exit_code"),
        "root_error": "AssertionError" if _has(text, "AssertionError") else None,
        "action": "investigate_test_failure" if _has(text, "AssertionError") else "continue",
    }


def _research_decision(text: str) -> dict[str, object]:
    return {
        "citation_url": _first_url(text),
        "document_id": _field(text, "document_id"),
        "degraded": _field(text, "degraded"),
        "can_make_cited_claim": bool(_first_url(text)) and _field(text, "degraded") == "false",
    }


def _interaction_decision(text: str) -> dict[str, object]:
    return {
        "url": _first_url(text),
        "frame_id": _field(text, "frame_id"),
        "selector": _field(text, "selector"),
        "bounds": _field(text, "bounds"),
        "action": "click" if _field(text, "selector") and _field(text, "bounds") else "inspect_more",
    }


def _orchestration_decision(text: str) -> dict[str, object]:
    return {
        "task_id": _field(text, "task_id"),
        "run_id": _field(text, "run_id"),
        "status": _field(text, "status"),
        "acceptance": _field(text, "acceptance"),
        "action": "continue_worker" if _field(text, "status") == "in_progress" else "inspect_state",
    }


class QualityParityHarnessTest(unittest.TestCase):
    def _reduced(self, *, tool_name: str, args: dict, result: str) -> str:
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.middleware.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=_compressed_response()
        ):
            out = middleware.on_tool_execution(
                tool_name=tool_name,
                args=args,
                next_call=lambda current_args: result,
            )
        self.assertIsInstance(out, str)
        self.assertIn("Headroom auto-compressed tool result", out)
        self.assertIn("[Headroom compressed intermediate]", out)
        self.assertIn("exact_header:", out)
        return out

    def assertDecisionParity(self, consumer, exact: str, reduced: str) -> None:
        self.assertEqual(consumer(reduced), consumer(exact))

    def test_debug_quality_parity_preserves_root_failure_decision(self):
        exact = _large_payload(
            "pytest status=FAIL exit_code=1 error=AssertionError changed_file=src/example.py failed=1 passed=21"
        )
        reduced = self._reduced(
            tool_name="terminal",
            args={"command": "pytest tests/test_example.py"},
            result=exact,
        )
        self.assertIn("classification: qa_trace", reduced)
        self.assertDecisionParity(_debug_decision, exact, reduced)

    def test_research_quality_parity_preserves_citation_and_degraded_decision(self):
        exact = _large_payload(
            "research answer citation url=https://source.example/a title=Source-A document_id=doc-123 degraded=false"
        )
        reduced = self._reduced(
            tool_name="x_search",
            args={"data_class": "research_corpus"},
            result=exact,
        )
        self.assertIn("classification: research_corpus", reduced)
        self.assertDecisionParity(_research_decision, exact, reduced)

    def test_interaction_quality_parity_preserves_action_target_decision(self):
        exact = _large_payload(
            "CDP method=DOM.getDocument url=https://example.test/app frame_id=FRAME-1 selector=#submit bounds=10x20x100x32 status=ok"
        )
        reduced = self._reduced(
            tool_name="browser_cdp",
            args={"method": "DOM.getDocument"},
            result=exact,
        )
        self.assertIn("classification: interaction_state", reduced)
        self.assertDecisionParity(_interaction_decision, exact, reduced)

    def test_orchestration_quality_parity_preserves_next_worker_action(self):
        exact = _large_payload(
            "kanban task_id=TASK-1 run_id=7 status=in_progress acceptance=preserve-exact-header assignee=worker"
        )
        reduced = self._reduced(
            tool_name="kanban_show",
            args={"data_class": "orchestration_fanin"},
            result=exact,
        )
        self.assertIn("classification: orchestration_fanin", reduced)
        self.assertDecisionParity(_orchestration_decision, exact, reduced)

    def test_protected_context_has_no_reduced_quality_path(self):
        protected = _large_payload(
            "diagnostic status=FAIL authorization=Bearer SYNTHETICPROTECTEDTOKEN1234567890 error=Traceback"
        )
        with tempfile.TemporaryDirectory() as td, patch(
            "hermes_headroom_plugin.middleware.hermes_home", return_value=Path(td)
        ), patch("hermes_headroom_plugin.middleware.readyz", return_value={"ok": True}), patch(
            "hermes_headroom_plugin.middleware.compress_messages", return_value=_compressed_response()
        ) as compress:
            out = middleware.on_tool_execution(
                tool_name="terminal",
                args={"command": "journalctl --user"},
                next_call=lambda current_args: protected,
            )
            reports_dir = Path(td) / "control-plane" / "headroom" / "reports"
            sidecars = list(reports_dir.glob("*")) if reports_dir.exists() else []

        self.assertEqual(out, protected)
        compress.assert_not_called()
        self.assertEqual(sidecars, [])


if __name__ == "__main__":
    unittest.main()
