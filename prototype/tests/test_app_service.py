from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prototype.app_service import (
    DemoCommandService,
    DemoQueryService,
    report_anomaly_types,
    validate_claim,
)
from prototype.document_ingestion import IngestionError, Repository, utc_now

ROOT = Path(__file__).resolve().parents[2]


class AppServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "demo.sqlite3"
        self.repository = Repository(self.db)
        now = utc_now()
        sources = {
            "service_name": [{
                "object_key": "parsed/demo/content_list.json", "block_index": 8,
                "page": 0, "bbox": [0, 100, 100, 500], "source_kind": "table_cell",
                "table_row": 1, "table_cell": 0, "raw_text": "*运输服务*客运服务费",
            }],
            "total_amount": [{
                "object_key": "parsed/demo/content_list.json", "block_index": 8,
                "page": 0, "bbox": [0, 100, 100, 500], "source_kind": "table_cell",
                "table_row": 2, "table_cell": 1, "raw_text": "（小写）￥181.73",
            }],
        }
        with self.repository.connect() as connection:
            connection.execute(
                """INSERT INTO documents (
                    id, sha256, original_filename, media_type, size_bytes, cos_object_key,
                    storage_status, created_at, updated_at
                ) VALUES ('doc-app', ?, 'invoice.pdf', 'application/pdf', 100,
                          'raw/demo/invoice.pdf', 'ready', ?, ?)""",
                ("a" * 64, now, now),
            )
            connection.execute(
                """INSERT INTO parse_runs (
                    id, document_id, parser, config_json, config_fingerprint, task_id,
                    status, content_list_key, attempt, created_at, updated_at, completed_at
                ) VALUES ('parse-app', 'doc-app', 'mineru', '{}', ?, 'task-app',
                          'succeeded', 'parsed/demo/content_list.json', 1, ?, ?, ?)""",
                ("b" * 64, now, now, now),
            )
            connection.execute(
                """INSERT INTO invoice_extractions (
                    id, parse_run_id, input_object_key, input_sha256, schema_version,
                    extractor_version, status, internal_consistency_result,
                    invoice_code, invoice_number, issue_date, service_name,
                    net_amount, tax_amount, total_amount, tax_rate,
                    field_sources_json, extraction_issues_json, created_at
                ) VALUES ('extract-app', 'parse-app', 'parsed/demo/content_list.json', ?,
                          'invoice-v1', 'synthetic-v1', 'succeeded', 'PASS',
                          '031002100000', '12345678', '2024-01-02',
                          '*运输服务*客运服务费', '176.44', '5.29', '181.73', '3%',
                          ?, '[]', ?)""",
                ("c" * 64, json.dumps(sources, ensure_ascii=False), now),
            )
            for index, rule_id in enumerate((
                "INV-AMOUNT-FORMAT-001", "INV-AMOUNT-SUM-001", "INV-DATE-001",
                "INV-EXTRACTION-001", "INV-REQUIRED-001", "INV-TAX-RATE-001",
            )):
                connection.execute(
                    """INSERT INTO invoice_checks (
                        id, extraction_id, rule_id, ruleset_version, result,
                        values_json, evidence_json, reason, created_at
                    ) VALUES (?, 'extract-app', ?, 'invoice-consistency-v1', 'PASS',
                              '{}', '{}', 'synthetic pass', ?)""",
                    (f"check-app-{index}", rule_id, now),
                )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def claim(self, **changes: object) -> dict:
        value = {
            "claim_id": "MOCK-APP-001",
            "claim_amount": "181.73",
            "currency": "CNY",
            "expense_category": "TRANSPORT",
            "purpose": "市内打车拜访客户",
            "invoice_extraction_id": "extract-app",
        }
        value.update(changes)
        return value

    def digest(self) -> str:
        return hashlib.sha256(self.db.read_bytes()).hexdigest()

    def test_read_only_lists_and_detail_do_not_write_or_access_network(self) -> None:
        report = DemoCommandService(self.db).run_precheck(self.claim())
        before = self.digest()
        query = DemoQueryService(self.db)
        with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            reports = query.list_reports()
            extractions = query.list_extractions()
            bundle = query.report_bundle(report["precheck_run_id"])
            status = query.local_parse_status("parse-app")
            latest = query.latest_parse_status("doc-app")
        self.assertEqual(before, self.digest())
        self.assertEqual(report["precheck_run_id"], reports[0]["id"])
        self.assertEqual("无需复核", reports[0]["manual_status"])
        self.assertEqual([], reports[0]["anomaly_types"])
        self.assertEqual("extract-app", extractions[0]["extraction_id"])
        self.assertEqual("doc-app", bundle["row"]["document_id"])
        self.assertEqual("succeeded", status["status"])
        self.assertEqual("parse-app", latest["id"])
        self.assertIsNone(query.latest_parse_status("absent-document"))

    def test_page_claim_is_independent_and_creates_immutable_report(self) -> None:
        report = DemoCommandService(self.db).run_precheck(self.claim(claim_amount="180.00"))
        self.assertEqual("REVIEW", report["final_status"])
        self.assertEqual("MOCK", report["claim_snapshot"]["data_classification"])
        amount = next(
            item for item in report["deterministic_checks"]
            if item["check_id"] == "PRECHECK-AMOUNT-001"
        )
        self.assertEqual("-1.73", amount["values"]["difference"])

    def test_feedback_accepts_all_report_states_is_append_only_and_does_not_modify_report(self) -> None:
        command = DemoCommandService(self.db)
        report = command.run_precheck(self.claim(claim_amount="180.00"))
        original_json = None
        with self.repository.connect() as connection:
            original_json = connection.execute(
                "SELECT report_json FROM precheck_runs WHERE id = ?",
                (report["precheck_run_id"],),
            ).fetchone()[0]
        feedback = command.add_review_feedback(
            report["precheck_run_id"], outcome="CONFIRMED_ISSUE",
            note="金额差异已人工确认", operator_id="demo-reviewer",
        )
        bundle = DemoQueryService(self.db).report_bundle(report["precheck_run_id"])
        self.assertEqual(feedback["id"], bundle["review_feedback"][0]["id"])
        summary = DemoQueryService(self.db).list_reports(status="REVIEW")[0]
        self.assertEqual("已留反馈", summary["manual_status"])
        self.assertIn("金额差异", summary["anomaly_types"])
        with self.repository.connect() as connection:
            self.assertEqual(original_json, connection.execute(
                "SELECT report_json FROM precheck_runs WHERE id = ?",
                (report["precheck_run_id"],),
            ).fetchone()[0])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE review_feedback SET note = 'overwrite' WHERE id = ?", (feedback["id"],)
                )

        passed = command.run_precheck(self.claim(claim_id="MOCK-APP-PASS"))
        command.add_review_feedback(
            passed["precheck_run_id"], outcome="PRECHECK_INCORRECT",
            note="人工发现 PASS 漏检", operator_id="demo-reviewer",
        )
        pass_summary = next(
            item for item in DemoQueryService(self.db).list_reports(status="PASS")
            if item["id"] == passed["precheck_run_id"]
        )
        self.assertEqual("已留反馈", pass_summary["manual_status"])

        with self.repository.connect() as connection:
            connection.execute(
                "UPDATE invoice_extractions SET total_amount = NULL WHERE id = 'extract-app'"
            )
        missing = command.run_precheck(self.claim(claim_id="MOCK-APP-MISSING"))
        self.assertEqual("MISSING_EVIDENCE", missing["final_status"])
        missing_summary = DemoQueryService(self.db).list_reports(status="MISSING_EVIDENCE")[0]
        self.assertEqual("待补材料", missing_summary["manual_status"])
        command.add_review_feedback(
            missing["precheck_run_id"], outcome="NEEDS_MORE_EVIDENCE",
            note="请补充金额证据", operator_id="demo-reviewer",
        )
        self.assertEqual(
            "已留反馈",
            DemoQueryService(self.db).list_reports(status="MISSING_EVIDENCE")[0]["manual_status"],
        )

    def test_validation_rejects_invalid_claim_before_write(self) -> None:
        with self.assertRaisesRegex(IngestionError, "two-decimal"):
            validate_claim(self.claim(claim_amount="181.7"))
        with self.assertRaisesRegex(IngestionError, "must be strings"):
            validate_claim(self.claim(claim_amount=181.73))
        with self.assertRaisesRegex(IngestionError, "claim_id"):
            validate_claim(self.claim(claim_id="bad id"))
        with self.assertRaisesRegex(IngestionError, "claim_id"):
            validate_claim(self.claim(claim_id=""))
        with self.assertRaisesRegex(IngestionError, "invoice_extraction_id"):
            validate_claim(self.claim(invoice_extraction_id=""))
        with self.repository.connect() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM precheck_runs").fetchone()[0])

    def test_blank_business_fact_is_saved_as_missing_evidence(self) -> None:
        report = DemoCommandService(self.db).run_precheck(
            self.claim(claim_id="MOCK-APP-MISSING-AMOUNT", claim_amount="")
        )
        self.assertEqual("MISSING_EVIDENCE", report["final_status"])
        self.assertEqual("", report["claim_snapshot"]["claim_amount"])
        checks = {item["check_id"]: item for item in report["deterministic_checks"]}
        self.assertEqual("MISSING_EVIDENCE", checks["PRECHECK-CLAIM-001"]["result"])
        self.assertEqual(["claim_amount"], checks["PRECHECK-CLAIM-001"]["values"]["missing_fields"])
        self.assertEqual("MISSING_EVIDENCE", checks["PRECHECK-AMOUNT-001"]["result"])

    def test_semantic_anomalies_distinguish_business_and_system_outcomes(self) -> None:
        semantic_check = [{"check_id": "PRECHECK-SEMANTIC-001", "result": "REVIEW"}]
        contradicted = {
            "deterministic_checks": semantic_check,
            "semantic_judgment": {"decision": "CONTRADICTED", "route": "baseline"},
            "technical_reasons": [],
        }
        uncertain = {
            "deterministic_checks": semantic_check,
            "semantic_judgment": {"decision": "UNCERTAIN", "route": "model"},
            "technical_reasons": [],
        }
        model_failed = {
            "deterministic_checks": semantic_check,
            "semantic_judgment": {"decision": "UNCERTAIN", "route": "model_error"},
            "technical_reasons": [{"code": "MODEL_REQUEST_FAILED"}],
        }

        self.assertEqual(
            ["申请事由与票面不匹配"], report_anomaly_types(contradicted)
        )
        self.assertEqual(
            ["申请事由与票面关系无法确定"], report_anomaly_types(uncertain)
        )
        self.assertEqual(
            ["系统未能完成语义判断"], report_anomaly_types(model_failed)
        )

    def test_golden_bad_and_failure_regression_cases(self) -> None:
        catalog = json.loads((
            ROOT / "prototype" / "examples" / "precheck-regression-cases.json"
        ).read_text(encoding="utf-8"))
        self.assertEqual("MOCK", catalog["data_classification"])
        command = DemoCommandService(self.db)
        for case in catalog["cases"]:
            with self.subTest(case=case["case_id"]):
                claim = dict(case["claim"])
                claim["invoice_extraction_id"] = "extract-app"
                report = command.run_precheck(claim)
                self.assertEqual(case["expected_status"], report["final_status"])
                self.assertEqual(case["expected_route"], report["semantic_judgment"]["route"])
                if "expected_technical_code" in case:
                    self.assertEqual(
                        case["expected_technical_code"], report["technical_reasons"][0]["code"]
                    )


if __name__ == "__main__":
    unittest.main()
