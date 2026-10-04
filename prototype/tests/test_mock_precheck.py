from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path

from prototype.mock_precheck import ContractError, evaluate, load_json, validate_rule_catalog


ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = ROOT / "prototype" / "rules" / "mock-rules.json"
EXAMPLES = ROOT / "prototype" / "examples"


class MockPrecheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_json(RULES_PATH)

    def request(self, case: str) -> dict:
        return load_json(EXAMPLES / f"mock-case-{case}.json")

    def test_four_examples_return_expected_conclusions(self) -> None:
        expected = {
            "m1": "MOCK_PASS",
            "m2": "MOCK_MISSING_EVIDENCE",
            "m3": "MOCK_REVIEW_CONFLICT",
            "m4": "MOCK_RULE_PENDING_REVIEW",
        }
        for case, conclusion in expected.items():
            with self.subTest(case=case):
                result = evaluate(self.request(case), self.catalog)
                self.assertEqual(conclusion, result["conclusion"]["code"])

        pass_result = evaluate(self.request("m1"), self.catalog)
        evidence_paths = {item["field_path"] for item in pass_result["evidence_sources"]}
        self.assertTrue(
            {"expense.currency", "invoice.currency", "payment.currency"}.issubset(evidence_paths)
        )

    def test_money_requires_two_decimal_string(self) -> None:
        request = self.request("m1")
        request["expense"]["claim_amount"] = 1280.00
        with self.assertRaisesRegex(ContractError, "two-decimal string"):
            evaluate(request, self.catalog)

        request = self.request("m1")
        request["expense"]["claim_amount"] = "1280.0"
        with self.assertRaisesRegex(ContractError, "two-decimal string"):
            evaluate(request, self.catalog)

    def test_effective_date_boundaries_are_inclusive(self) -> None:
        for boundary in ("2030-01-01", "2030-12-31"):
            request = self.request("m1")
            request["rule_lookup"]["effective_on"] = boundary
            result = evaluate(request, self.catalog)
            self.assertEqual("MOCK_PASS", result["conclusion"]["code"])

    def test_outside_effective_date_has_no_rule(self) -> None:
        request = self.request("m1")
        request["rule_lookup"]["effective_on"] = "2029-12-31"
        result = evaluate(request, self.catalog)
        self.assertEqual("MOCK_RULE_PENDING_REVIEW", result["conclusion"]["code"])
        self.assertEqual([], result["rule_snapshot"]["matched_rules"])

    def test_every_rule_must_be_explicitly_mock(self) -> None:
        catalog = deepcopy(self.catalog)
        del catalog["rules"][0]["data_classification"]
        with self.assertRaisesRegex(ContractError, "must equal"):
            validate_rule_catalog(catalog)

        catalog = deepcopy(self.catalog)
        catalog["rules"][1]["data_classification"] = "REAL"
        with self.assertRaisesRegex(ContractError, "must equal"):
            validate_rule_catalog(catalog)

    def test_missing_rule_never_generates_a_replacement(self) -> None:
        result = evaluate(self.request("m4"), self.catalog)
        self.assertEqual("MOCK_RULE_PENDING_REVIEW", result["conclusion"]["code"])
        self.assertEqual("MOCK：规则待确认／人工复核", result["conclusion"]["label"])
        self.assertEqual([], result["rule_snapshot"]["matched_rules"])
        self.assertIsNone(result["reasons"][0]["rule_id"])
        self.assertIsNone(result["reasons"][0]["rule_version"])
        self.assertEqual("CONFIRM_APPLICABLE_RULE", result["manual_handling"]["requested_action"])
        self.assertEqual(
            {"rule_lookup.scope_key", "rule_lookup.effective_on"},
            {item["field_path"] for item in result["evidence_sources"]},
        )

        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("MOCK-LODGE-R001", serialized)
        self.assertNotIn("MOCK-LODGE-R002", serialized)
        self.assertNotIn("generated_rule", serialized.lower())

    def test_empty_rule_catalog_never_generates_a_replacement(self) -> None:
        catalog = deepcopy(self.catalog)
        catalog["rules"] = []
        result = evaluate(self.request("m1"), catalog)
        self.assertEqual("MOCK_RULE_PENDING_REVIEW", result["conclusion"]["code"])
        self.assertEqual([], result["rule_snapshot"]["matched_rules"])
        self.assertIsNone(result["reasons"][0]["rule_id"])
        self.assertIsNone(result["reasons"][0]["rule_version"])


if __name__ == "__main__":
    unittest.main()
