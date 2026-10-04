from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prototype.document_ingestion import Repository, utc_now
from prototype.expense_precheck import (
    AgictoChatCompletionsSemanticModel,
    ExpensePrecheckService,
    ModelTechnicalError,
    SemanticModelResult,
)


class FakeClaimProvider:
    def __init__(self, claim: dict | None):
        self.claim = claim
        self.calls = 0

    def get_claim(self, claim_id: str) -> dict | None:
        self.calls += 1
        if self.claim is None:
            return None
        value = dict(self.claim)
        value["claim_id"] = claim_id
        return value


class FakeModel:
    provider = "fake-model"
    model_id = "fake-v1"
    gateway = "https://fake.invalid/v1/chat/completions"

    def __init__(self, result: dict | None = None, error: ModelTechnicalError | None = None):
        self.result = result or {
            "decision": "SUPPORTED",
            "reason": "the stated trip is consistent with passenger transport",
            "claim_quote": "前往客户办公地点",
            "invoice_quote": "客运服务",
        }
        self.error = error
        self.calls = 0

    def judge(self, *, purpose: str, category: str, service_name: str) -> SemanticModelResult:
        del purpose, category, service_name
        self.calls += 1
        if self.error:
            raise self.error
        return SemanticModelResult(dict(self.result), request_id="fake-request", http_status=200)


class BombProvider:
    def get_claim(self, claim_id: str) -> dict | None:  # pragma: no cover - must not run
        raise AssertionError(f"show unexpectedly read claim {claim_id}")


class FakeHttpResponse:
    status = 200

    def __init__(self, value: dict):
        self.value = json.dumps(value).encode()

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        self.assert_limit = limit
        return self.value


class ExpensePrecheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temp.name) / "metadata.sqlite3")
        self.extraction_id = "synthetic-extraction"
        now = utc_now()
        with self.repository.connect() as connection:
            connection.execute(
                """INSERT INTO documents (
                    id, sha256, original_filename, media_type, size_bytes, cos_object_key,
                    storage_status, created_at, updated_at
                ) VALUES ('doc', ?, 'synthetic.pdf', 'application/pdf', 10,
                          'raw/synthetic.pdf', 'ready', ?, ?)""",
                ("a" * 64, now, now),
            )
            connection.execute(
                """INSERT INTO parse_runs (
                    id, document_id, parser, config_json, config_fingerprint, task_id,
                    status, content_list_key, attempt, created_at, updated_at, completed_at
                ) VALUES ('parse', 'doc', 'mineru', '{}', ?, 'task', 'succeeded',
                          'parsed/content_list.json', 1, ?, ?, ?)""",
                ("b" * 64, now, now, now),
            )
            sources = {
                "service_name": [{"object_key": "parsed/content_list.json", "block_index": 8, "page": 0, "bbox": [0, 0, 10, 10], "raw_text": "*运输服务*客运服务费"}],
                "total_amount": [{"object_key": "parsed/content_list.json", "block_index": 8, "page": 0, "bbox": [0, 0, 10, 10], "raw_text": "（小写）￥181.73"}],
            }
            connection.execute(
                """INSERT INTO invoice_extractions (
                    id, parse_run_id, input_object_key, input_sha256, schema_version,
                    extractor_version, status, internal_consistency_result,
                    invoice_code, invoice_number, issue_date, service_name,
                    net_amount, tax_amount, total_amount, tax_rate,
                    field_sources_json, extraction_issues_json, created_at
                ) VALUES (?, 'parse', 'parsed/content_list.json', ?, 'invoice-v1',
                          'synthetic-v1', 'succeeded', 'PASS', '031002100000',
                          '12345678', '2024-01-02', '*运输服务*客运服务费',
                          '176.44', '5.29', '181.73', '3%', ?, '[]', ?)""",
                (self.extraction_id, "c" * 64, json.dumps(sources, ensure_ascii=False), now),
            )
            for index, rule_id in enumerate((
                "INV-AMOUNT-FORMAT-001", "INV-AMOUNT-SUM-001", "INV-DATE-001",
                "INV-EXTRACTION-001", "INV-REQUIRED-001", "INV-TAX-RATE-001",
            )):
                connection.execute(
                    """INSERT INTO invoice_checks (
                        id, extraction_id, rule_id, ruleset_version, result,
                        values_json, evidence_json, reason, created_at
                    ) VALUES (?, ?, ?, 'invoice-consistency-v1', 'PASS', '{}', '{}', 'synthetic pass', ?)""",
                    (f"check-{index}", self.extraction_id, rule_id, now),
                )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def claim(self, **changes: object) -> dict:
        value = {
            "claim_id": "MOCK-CLAIM-1",
            "claim_amount": "181.73",
            "currency": "CNY",
            "expense_category": "TRANSPORT",
            "purpose": "市内打车拜访客户",
            "invoice_extraction_id": self.extraction_id,
        }
        value.update(changes)
        return value

    def test_equal_amount_and_clear_rule_baseline_pass_without_model(self) -> None:
        model = FakeModel()
        result = ExpensePrecheckService(
            self.repository, FakeClaimProvider(self.claim()), model
        ).run("MOCK-CLAIM-1")

        self.assertEqual("PASS", result["final_status"])
        self.assertEqual("baseline", result["semantic_judgment"]["route"])
        self.assertFalse(result["model"]["invoked"])
        self.assertEqual(0, model.calls)

    def test_amount_difference_is_review_not_rejection(self) -> None:
        result = ExpensePrecheckService(
            self.repository, FakeClaimProvider(self.claim(claim_amount="180.00")), FakeModel()
        ).run("MOCK-CLAIM-1")
        checks = {item["check_id"]: item for item in result["deterministic_checks"]}
        self.assertEqual("REVIEW", result["final_status"])
        self.assertEqual("REVIEW", checks["PRECHECK-AMOUNT-001"]["result"])
        self.assertEqual("-1.73", checks["PRECHECK-AMOUNT-001"]["values"]["difference"])
        self.assertIn("no reimbursement policy", checks["PRECHECK-AMOUNT-001"]["reason"])

    def test_missing_claim_is_persisted_as_missing_evidence(self) -> None:
        result = ExpensePrecheckService(
            self.repository, FakeClaimProvider(None), FakeModel()
        ).run("MOCK-MISSING")
        self.assertEqual("MISSING_EVIDENCE", result["final_status"])
        self.assertIsNone(result["claim_snapshot"])
        with self.repository.connect() as connection:
            stored = connection.execute("SELECT final_status FROM precheck_runs").fetchone()
        self.assertEqual("MISSING_EVIDENCE", stored["final_status"])

    def test_rule_baseline_can_contradict_without_model(self) -> None:
        model = FakeModel()
        result = ExpensePrecheckService(
            self.repository,
            FakeClaimProvider(self.claim(expense_category="MEALS", purpose="客户聚餐")),
            model,
        ).run("MOCK-CLAIM-1")
        self.assertEqual("REVIEW", result["final_status"])
        self.assertEqual("CONTRADICTED", result["semantic_judgment"]["decision"])
        self.assertEqual("baseline", result["semantic_judgment"]["route"])
        self.assertEqual(0, model.calls)

    def test_unclear_baseline_calls_model_once_and_can_add_supported_result(self) -> None:
        model = FakeModel()
        provider = FakeClaimProvider(self.claim(
            expense_category="BUSINESS_VISIT",
            purpose="前往客户办公地点进行项目沟通产生的市内往返费用",
        ))
        result = ExpensePrecheckService(self.repository, provider, model).run("MOCK-CLAIM-1")
        self.assertEqual("PASS", result["final_status"])
        self.assertEqual("model", result["semantic_judgment"]["route"])
        self.assertTrue(result["model"]["invoked"])
        self.assertEqual(1, model.calls)

    def test_model_uncertain_stays_review(self) -> None:
        model = FakeModel({
            "decision": "UNCERTAIN", "reason": "insufficient link",
            "claim_quote": "", "invoice_quote": "",
        })
        result = ExpensePrecheckService(
            self.repository,
            FakeClaimProvider(self.claim(expense_category="OTHER", purpose="项目相关费用")),
            model,
        ).run("MOCK-CLAIM-1")
        self.assertEqual("REVIEW", result["final_status"])
        self.assertEqual("UNCERTAIN", result["semantic_judgment"]["decision"])

    def test_model_output_missing_field_is_strictly_rejected(self) -> None:
        model = FakeModel({
            "decision": "UNCERTAIN", "reason": "insufficient evidence", "claim_quote": "",
        })
        result = ExpensePrecheckService(
            self.repository,
            FakeClaimProvider(self.claim(expense_category="OTHER", purpose="项目相关费用")),
            model,
        ).run("MOCK-CLAIM-1")
        self.assertEqual("REVIEW", result["final_status"])
        technical = result["technical_reasons"][0]
        self.assertEqual("MODEL_OUTPUT_INVALID", technical["code"])
        self.assertIn("invoice_quote", technical["message"])
        self.assertIn("returned fields", technical["message"])

    def test_invalid_model_evidence_is_review_with_technical_reason(self) -> None:
        model = FakeModel({
            "decision": "SUPPORTED", "reason": "unsupported quote test",
            "claim_quote": "不存在的申请原文", "invoice_quote": "客运服务",
        })
        result = ExpensePrecheckService(
            self.repository,
            FakeClaimProvider(self.claim(expense_category="OTHER", purpose="项目相关费用")),
            model,
        ).run("MOCK-CLAIM-1")
        self.assertEqual("REVIEW", result["final_status"])
        self.assertEqual("MODEL_EVIDENCE_INVALID", result["technical_reasons"][0]["code"])

    def test_model_output_with_extra_fields_is_strictly_rejected(self) -> None:
        model = FakeModel({
            "decision": "UNCERTAIN", "reason": "insufficient evidence",
            "claim_quote": "", "invoice_quote": "", "unexpected": "not allowed",
        })
        result = ExpensePrecheckService(
            self.repository,
            FakeClaimProvider(self.claim(expense_category="OTHER", purpose="项目相关费用")),
            model,
        ).run("MOCK-CLAIM-1")
        self.assertEqual("REVIEW", result["final_status"])
        self.assertEqual("MODEL_OUTPUT_INVALID", result["technical_reasons"][0]["code"])

    def test_model_timeout_is_review_and_report_is_saved(self) -> None:
        model = FakeModel(error=ModelTechnicalError("MODEL_REQUEST_FAILED", "response timeout"))
        result = ExpensePrecheckService(
            self.repository,
            FakeClaimProvider(self.claim(expense_category="OTHER", purpose="项目相关费用")),
            model,
        ).run("MOCK-CLAIM-1")
        self.assertEqual("REVIEW", result["final_status"])
        self.assertEqual("MODEL_REQUEST_FAILED", result["technical_reasons"][0]["code"])
        self.assertIn("timeout", result["technical_reasons"][0]["message"])
        self.assertEqual(result, ExpensePrecheckService(self.repository, BombProvider()).show(
            precheck_run_id=result["precheck_run_id"]
        ))

    def test_every_run_creates_immutable_report_even_for_same_input(self) -> None:
        service = ExpensePrecheckService(self.repository, FakeClaimProvider(self.claim()), FakeModel())
        first = service.run("MOCK-CLAIM-1")
        second = service.run("MOCK-CLAIM-1")
        self.assertNotEqual(first["precheck_run_id"], second["precheck_run_id"])
        self.assertEqual(first["input_fingerprint"], second["input_fingerprint"])
        with self.repository.connect() as connection:
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM precheck_runs").fetchone()[0])

    def test_invoice_check_failure_maps_to_review(self) -> None:
        with self.repository.connect() as connection:
            connection.execute("UPDATE invoice_checks SET result = 'FAIL' WHERE id = 'check-1'")
        result = ExpensePrecheckService(
            self.repository, FakeClaimProvider(self.claim()), FakeModel()
        ).run("MOCK-CLAIM-1")
        checks = {item["check_id"]: item for item in result["deterministic_checks"]}
        self.assertEqual("REVIEW", result["final_status"])
        self.assertEqual("REVIEW", checks["PRECHECK-INVOICE-001"]["result"])

    def test_agicto_adapter_uses_chat_completions_and_only_semantic_fields(self) -> None:
        response = FakeHttpResponse({
            "id": "agicto-request-1",
            "choices": [{"message": {"content": json.dumps({
                "decision": "SUPPORTED",
                "reason": "semantic match",
                "claim_quote": "前往客户办公地点",
                "invoice_quote": "客运服务",
            }, ensure_ascii=False)}}],
        })
        model = AgictoChatCompletionsSemanticModel(api_key="never-print-this-key")
        with patch(
            "prototype.expense_precheck.urllib.request.urlopen", return_value=response
        ) as opener:
            result = model.judge(
                purpose="前往客户办公地点进行项目沟通产生的市内往返费用",
                category="BUSINESS_VISIT",
                service_name="运输服务*客运服务费",
            )

        request = opener.call_args.args[0]
        body = json.loads(request.data)
        user_payload = json.loads(body["messages"][1]["content"])
        self.assertEqual("https://api.agicto.cn/v1/chat/completions", request.full_url)
        self.assertEqual("gpt-6-luna", body["model"])
        self.assertEqual({"model", "messages", "response_format"}, set(body))
        self.assertIn("JSON", body["messages"][0]["content"])
        for field_name in ("decision", "reason", "claim_quote", "invoice_quote"):
            self.assertIn(field_name, body["messages"][0]["content"])
        self.assertIn(
            '{"decision":"UNCERTAIN","reason":"Evidence is insufficient.",'
            '"claim_quote":"","invoice_quote":""}',
            body["messages"][0]["content"],
        )
        self.assertEqual(
            {"claim": {"expense_category": "BUSINESS_VISIT", "purpose": "前往客户办公地点进行项目沟通产生的市内往返费用"},
             "invoice": {"service_name": "运输服务*客运服务费"}},
            user_payload,
        )
        self.assertEqual("SUPPORTED", result.judgment["decision"])
        self.assertEqual("agicto-request-1", result.request_id)
        self.assertEqual(200, result.http_status)
        self.assertEqual(1, opener.call_count)
        self.assertEqual(120, opener.call_args.kwargs["timeout"])
        self.assertNotIn("never-print-this-key", repr(model))


if __name__ == "__main__":
    unittest.main()
