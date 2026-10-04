from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prototype.document_ingestion import Repository, utc_now
from prototype.expense_precheck import (
    ExpensePrecheckService,
    ModelTechnicalError,
    SemanticModelResult,
    main as precheck_main,
)
from prototype.precheck_report_export import (
    connect_read_only,
    export_report,
    load_report_bundle,
)


class ClaimProvider:
    def __init__(self, claim: dict | None):
        self.claim = claim

    def get_claim(self, claim_id: str) -> dict | None:
        if self.claim is None:
            return None
        value = dict(self.claim)
        value["claim_id"] = claim_id
        return value


class FailingModel:
    provider = "fake-provider"
    model_id = "fake-model"
    gateway = "https://gateway.invalid/v1/chat/completions"

    def judge(self, *, purpose: str, category: str, service_name: str) -> SemanticModelResult:
        del purpose, category, service_name
        raise ModelTechnicalError("MODEL_REQUEST_FAILED", "synthetic timeout")


class PrecheckReportExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "metadata.sqlite3"
        self.repository = Repository(self.db)
        now = utc_now()
        sources = {
            "invoice_code": [{
                "object_key": "parsed/demo/content_list.json", "block_index": 4,
                "page": 0, "bbox": [10, 20, 30, 40], "source_kind": "text_block",
                "raw_text": "发票代码：000000000000<script>alert(1)</script>",
            }],
            "service_name": [{
                "object_key": "parsed/demo/content_list.json", "block_index": 8,
                "page": 0, "bbox": [1, 2, 900, 800], "source_kind": "table_cell",
                "table_row": 1, "table_cell": 0, "raw_text": "*运输服务*客运服务费",
            }],
            "total_amount": [{
                "object_key": "parsed/demo/content_list.json", "block_index": 8,
                "page": 0, "bbox": [1, 2, 900, 800], "source_kind": "table_cell",
                "table_row": 2, "table_cell": 1, "raw_text": "（小写）¥181.73",
            }],
        }
        with self.repository.connect() as connection:
            connection.execute(
                """INSERT INTO documents (
                    id, sha256, original_filename, media_type, size_bytes, cos_object_key,
                    storage_status, created_at, updated_at
                ) VALUES ('doc-html', ?, 'invoice.pdf', 'application/pdf', 100,
                          'raw/demo/invoice.pdf', 'ready', ?, ?)""",
                ("a" * 64, now, now),
            )
            connection.execute(
                """INSERT INTO parse_runs (
                    id, document_id, parser, config_json, config_fingerprint, task_id,
                    status, result_zip_key, markdown_key, content_list_key,
                    attempt, created_at, updated_at, completed_at
                ) VALUES ('parse-html', 'doc-html', 'mineru', '{}', ?, 'task-html',
                          'succeeded', 'parsed/demo/result.zip', 'parsed/demo/full.md',
                          'parsed/demo/content_list.json', 1, ?, ?, ?)""",
                ("b" * 64, now, now, now),
            )
            connection.execute(
                """INSERT INTO invoice_extractions (
                    id, parse_run_id, input_object_key, input_sha256, schema_version,
                    extractor_version, status, internal_consistency_result, invoice_type,
                    invoice_code, invoice_number, issue_date, buyer_name, buyer_tax_id,
                    seller_name, seller_tax_id, service_name, net_amount, tax_amount,
                    total_amount, tax_rate, field_sources_json, extraction_issues_json, created_at
                ) VALUES ('extract-html', 'parse-html', 'parsed/demo/content_list.json', ?,
                          'invoice-v1', 'extractor-v1', 'succeeded', 'PASS', '增值税发票',
                          '000000000000', '12345678', '2024-01-02', '脱敏购买方',
                          'BUYER000000000001', '脱敏销售方', 'SELLER0000000001',
                          '运输服务*客运服务费', '176.44', '5.29', '181.73', '3%', ?, '[]', ?)""",
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
                    ) VALUES (?, 'extract-html', ?, 'invoice-consistency-v1', 'PASS',
                              '{}', '{}', 'synthetic pass', ?)""",
                    (f"check-html-{index}", rule_id, now),
                )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def claim(self, **changes: object) -> dict:
        value = {
            "claim_id": "MOCK-HTML-1",
            "data_classification": "MOCK",
            "claim_amount": "181.73",
            "currency": "CNY",
            "expense_category": "TRANSPORT",
            "purpose": "市内打车拜访客户 <script>alert('claim')</script>",
            "invoice_extraction_id": "extract-html",
        }
        value.update(changes)
        return value

    def _file_hash(self) -> str:
        return hashlib.sha256(self.db.read_bytes()).hexdigest()

    def test_export_is_read_only_offline_and_escapes_untrusted_text(self) -> None:
        report = ExpensePrecheckService(
            self.repository, ClaimProvider(self.claim())
        ).run("MOCK-HTML-1")
        before_hash = self._file_hash()
        with self.repository.connect() as connection:
            before_count = connection.execute("SELECT COUNT(*) FROM precheck_runs").fetchone()[0]
        output = self.root / "reports" / "pass.html"

        with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            result = export_report(self.db, report["precheck_run_id"], output)

        self.assertEqual("read-only", result["database_mode"])
        self.assertEqual(0, result["network_requests"])
        self.assertEqual("PASS", result["persisted_v1_status"])
        self.assertEqual("PARTIAL", result["coverage"])
        self.assertEqual("HUMAN_REVIEW_REQUIRED", result["display_recommendation"])
        self.assertEqual(before_hash, self._file_hash())
        with self.repository.connect() as connection:
            self.assertEqual(before_count, connection.execute("SELECT COUNT(*) FROM precheck_runs").fetchone()[0])
        page = output.read_text(encoding="utf-8")
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;alert", page)
        self.assertIn("Mock 报销申请", page)
        self.assertIn("不代表发票真实或报销获批", page)
        self.assertIn("总体：需人工复核", page)
        self.assertIn("已检查能力域：2/8 项通过", page)
        self.assertIn("未检查：6/8 项", page)
        self.assertIn("异常：0 项", page)
        self.assertIn("原始 V1 基础校验状态", page)
        self.assertIn("需要人工复核", page)
        self.assertIn("发票验真与作废状态", page)
        self.assertIn("重复报销 / 重复发票", page)
        self.assertIn("制度标准与预算", page)
        self.assertGreaterEqual(page.count("未检查"), 6)
        self.assertNotIn('<header class="hero status-PASS">', page)
        self.assertIn('<details class="technical">', page)
        self.assertIn('<details class="technical-panel">', page)
        self.assertNotIn('<details class="technical" open>', page)
        self.assertIn("¥181.73", page)
        self.assertIn("¥0.00", page)
        self.assertIn("UTC+8", page)
        self.assertIn("待办复核清单", page)
        self.assertIn("所需材料 / 数据", page)
        self.assertIn("责任方", page)
        self.assertIn("补充材料", page)
        self.assertIn("转人工复核", page)
        self.assertIn("查看原始凭证", page)
        self.assertGreaterEqual(page.count("disabled"), 3)
        self.assertIn("当前只读报告尚未接入工作流", page)
        self.assertIn("BUYE**********001", page)
        self.assertNotIn("BUYER000000000001", page)
        self.assertNotIn("SELLER0000000001", page)
        self.assertIn("块级 bbox（整表范围，非字段精确坐标）", page)
        self.assertNotIn("parsed/demo/content_list.json", page)
        self.assertNotIn("https://gateway.invalid", page)
        self.assertNotIn("COS object key", page)
        self.assertNotIn("?sign=", page)

    def test_read_only_connection_rejects_writes(self) -> None:
        with connect_read_only(self.db) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("INSERT INTO documents (id) VALUES ('forbidden')")

    def test_model_failure_review_is_prominent_and_not_green_pass(self) -> None:
        claim = self.claim(
            expense_category="OTHER",
            purpose="项目协调产生的费用 <img src=x onerror=alert(1)>",
        )
        report = ExpensePrecheckService(
            self.repository, ClaimProvider(claim), FailingModel()
        ).run("MOCK-HTML-REVIEW")
        output = self.root / "review.html"
        export_report(self.db, report["precheck_run_id"], output)
        page = output.read_text(encoding="utf-8")

        self.assertEqual("REVIEW", report["final_status"])
        self.assertIn("总体：需人工复核", page)
        self.assertIn("已检查能力域：1/8 项通过", page)
        self.assertIn("未检查：6/8 项", page)
        self.assertIn("异常：1 项", page)
        self.assertIn("模型技术失败", page)
        self.assertIn("MODEL_REQUEST_FAILED", page)
        self.assertIn("synthetic timeout", page)
        self.assertIn("待复核", page)
        self.assertIn(
            'Mock 申请比对 <span class="status status-pass">已检查范围内通过</span>', page
        )
        self.assertIn(
            '文字语义判断 <span class="status status-review">待复核</span>', page
        )
        self.assertIn("语义判断不可用或未通过校验，需要人工复核", page)
        self.assertIn("当前语义结论不是有效的支持性证据", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;img src=x", page)
        self.assertNotIn('<header class="hero status-PASS">', page)

    def test_missing_lineage_is_displayed_as_unrecorded(self) -> None:
        report = ExpensePrecheckService(
            self.repository, ClaimProvider(None)
        ).run("MOCK-MISSING")
        output = self.root / "missing.html"
        export_report(self.db, report["precheck_run_id"], output)
        page = output.read_text(encoding="utf-8")

        self.assertEqual("MISSING_EVIDENCE", report["final_status"])
        self.assertIn("证据缺失", page)
        self.assertIn("未记录", page)
        self.assertNotIn("原始文档 <span class=\"status status-succeeded\">", page)

    def test_cli_export_does_not_construct_model_or_migrate(self) -> None:
        report = ExpensePrecheckService(
            self.repository, ClaimProvider(self.claim())
        ).run("MOCK-HTML-CLI")
        output = self.root / "cli.html"
        before_hash = self._file_hash()
        with patch(
            "prototype.expense_precheck.AgictoChatCompletionsSemanticModel.from_env",
            side_effect=AssertionError("model configuration must not load"),
        ), patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            exit_code = precheck_main([
                "--db", str(self.db), "export-report",
                "--precheck-run-id", report["precheck_run_id"], "--output", str(output),
            ])
        self.assertEqual(0, exit_code)
        self.assertTrue(output.is_file())
        self.assertEqual(before_hash, self._file_hash())

    def test_bundle_values_match_persisted_report_snapshot(self) -> None:
        report = ExpensePrecheckService(
            self.repository, ClaimProvider(self.claim())
        ).run("MOCK-HTML-MATCH")
        bundle = load_report_bundle(self.db, report["precheck_run_id"])
        self.assertEqual(report, bundle["report"])
        self.assertEqual("parse-html", bundle["row"]["parse_run_id"])
        self.assertEqual("doc-html", bundle["row"]["document_id"])
        self.assertEqual(6, len(bundle["invoice_checks"]))


if __name__ == "__main__":
    unittest.main()
