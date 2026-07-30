from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from hermes_headroom_plugin import paired_evaluation


FIXTURE = Path(__file__).parent / "fixtures" / "phase2-paired-corpus.jsonl"


class PairedEvaluationTest(unittest.TestCase):
    def test_representative_synthetic_contract_passes_without_promotion_claim(self):
        report = paired_evaluation.evaluate_cases(paired_evaluation.load_jsonl(FIXTURE))

        self.assertEqual(report["gate"], "PASS")
        self.assertEqual(report["decision"], "PASS_SYNTHETIC_CONTRACT__REAL_PROVIDER_CACHE_EVIDENCE_MISSING")
        self.assertFalse(report["promotion_eligible"])
        self.assertEqual(report["cohorts"]["missing"], [])
        self.assertEqual(report["task_quality"]["successes"], 8)
        self.assertEqual(report["task_quality"]["total"], 8)
        self.assertEqual(report["task_quality"]["success_rate"], 1.0)
        self.assertEqual(report["task_quality"]["critical_failures"], [])
        self.assertGreater(report["task_quality"]["wilson_95"]["lower"], 0.6)
        self.assertEqual(report["authority_coverage"]["provider_usage"], "SYNTHETIC_ONLY")
        self.assertEqual(report["authority_coverage"]["provider_cache"], "SYNTHETIC_ONLY")
        self.assertEqual(report["authority_coverage"]["billing"], "MISSING")
        self.assertEqual(report["next_gate"], "DISPOSABLE_REAL_PROVIDER_CACHE_TASK_EVIDENCE")
        self.assertEqual(report["cohorts"]["non_positive_net"], [])
        self.assertTrue(all(value > 0 for value in report["cohorts"]["net_est_tokens_saved"].values()))

        ledger_summary = report["net_ledger"]["summary"]
        self.assertEqual(ledger_summary["provider_prompt_or_input_tokens"], 120)
        self.assertEqual(ledger_summary["provider_cache_read_tokens"], 40)
        self.assertEqual(report["net_ledger"]["provider_requests"][0]["cache_semantics"], "non_additive_component")
        self.assertNotIn("exact_text", json.dumps(report, sort_keys=True))
        self.assertNotIn("variant_text", json.dumps(report, sort_keys=True))

    def test_critical_exact_instruction_drift_fails_closed(self):
        cases = paired_evaluation.load_jsonl(FIXTURE)
        changed = copy.deepcopy(cases)
        instruction = next(case for case in changed if case["cohort"] == "instructions")
        instruction["variant_text"] = instruction["variant_text"].replace("Do not install", "Install")

        report = paired_evaluation.evaluate_cases(changed)

        self.assertEqual(report["gate"], "FAIL")
        self.assertIn("critical_task_or_invariant_failure", report["gate_failures"])
        self.assertIn("exact_contract_failure", report["gate_failures"])
        self.assertIn("instruction-no-promotion", report["task_quality"]["critical_failures"])
        self.assertEqual(report["next_gate"], "FIX_PAIRED_EVALUATION_FAILURES")
        self.assertNotIn("Do not install", json.dumps(report, sort_keys=True))

    def test_synthetic_billing_label_cannot_claim_real_authority(self):
        cases = copy.deepcopy(paired_evaluation.load_jsonl(FIXTURE))
        provider_case = next(case for case in cases if isinstance(case.get("provider_usage"), dict))
        provider_case["provider_usage"]["billing_authority"] = "provider_invoice"

        report = paired_evaluation.evaluate_cases(cases)

        self.assertEqual(report["gate"], "PASS")
        self.assertEqual(report["authority_coverage"]["billing"], "MISSING")

    def test_missing_cohort_and_non_positive_net_fail(self):
        cases = paired_evaluation.load_jsonl(FIXTURE)
        changed = [copy.deepcopy(case) for case in cases if case["cohort"] != "tables"]
        code = next(case for case in changed if case["cohort"] == "code")
        code["retrieval_reintroduced_chars"] = len(code["exact_text"]) * 2

        report = paired_evaluation.evaluate_cases(changed)

        self.assertEqual(report["gate"], "FAIL")
        self.assertIn("missing_required_cohorts", report["gate_failures"])
        self.assertIn("non_positive_net_for_admitted_cohort", report["gate_failures"])
        self.assertEqual(report["cohorts"]["missing"], ["tables"])
        self.assertIn("code", report["cohorts"]["non_positive_net"])

    def test_cli_writes_content_free_report(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "paired-report.json"
            rc = paired_evaluation.main(["--input", str(FIXTURE), "--output", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(report["gate"], "PASS")
        self.assertFalse(report["promotion_eligible"])


if __name__ == "__main__":
    unittest.main()
