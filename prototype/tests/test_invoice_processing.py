from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from prototype.document_ingestion import IngestionError, Repository, utc_now
from prototype.invoice_processing import InvoiceProcessingService


BUYER_ID = "91110000TESTBUYER1"
SELLER_ID = "91110000TESTSELLER"


def synthetic_blocks(
    *, total: str = "181.73", include_number: bool = True,
    second_number: str | None = None, changed_table: bool = False,
) -> list[dict]:
    blocks: list[dict] = [
        {"type": "text", "text": "增值税电子普通发票", "page_idx": 0, "bbox": [1, 1, 10, 10]},
        {"type": "text", "text": "发票代码：031002100000", "page_idx": 0, "bbox": [1, 11, 10, 20]},
        {"type": "text", "text": "开票日期：2024年01月02日", "page_idx": 0, "bbox": [1, 21, 10, 30]},
    ]
    if include_number:
        blocks.append({"type": "text", "text": "发票号码：12345678", "page_idx": 0, "bbox": [1, 31, 10, 40]})
    if second_number:
        blocks.append({"type": "text", "text": f"发票号码：{second_number}", "page_idx": 0, "bbox": [1, 41, 10, 50]})
    if changed_table:
        detail = """
        <tr><td>货物或应税劳务、服务名称</td><td>规格型号</td><td>单位</td><td>数量</td><td>单价</td><td>金额</td><td>税率</td><td>税额</td></tr>
        <tr><td>*测试服务*脱敏服务费</td><td>无</td><td>次</td><td>1</td><td>176.44</td><td>176.44</td><td>3%</td><td>5.29</td></tr>
        <tr><td>合计</td><td></td><td></td><td></td><td></td><td>176.44</td><td></td><td>5.29</td></tr>
        """
    else:
        detail = """
        <tr><td>货物或应税劳务、服务名称*测试服务*脱敏服务费合计</td><td>规格型号无</td><td>单位次</td><td>数量1</td><td>单价176.44</td><td>金额176.44176.44</td><td>税率3%</td><td>税额5.295.29</td><td></td></tr>
        """
    table = f"""
    <table>
      <tr><td>购 买 方</td><td>名 称：测试购买方有限公司 纳税人识别号：{BUYER_ID} 地址、电话：脱敏 开户行及账号：脱敏</td><td>密码区</td><td></td></tr>
      {detail}
      <tr><td>价税合计（大写）</td><td>（小写）￥{total}</td><td></td></tr>
      <tr><td>销 售 方</td><td>名 称：测试销售方有限公司 纳税人识别号：{SELLER_ID} 地址、电话：脱敏 开户行及账号：脱敏</td><td>备注</td><td></td></tr>
    </table>
    """
    blocks.append({"type": "table", "table_body": table, "page_idx": 0, "bbox": [0, 100, 100, 500]})
    return blocks


class FakeStorage:
    def __init__(self, value: bytes | None = None) -> None:
        self.value = value
        self.reads = 0

    def get_bytes(self, key: str, max_bytes: int) -> bytes:
        self.reads += 1
        if self.value is None:
            raise IngestionError("COS_READ_FAILED", "synthetic COS failure")
        if len(self.value) > max_bytes:
            raise IngestionError("ARTIFACT_TOO_LARGE", "synthetic artifact too large")
        return self.value


class InvoiceProcessingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temp.name) / "metadata.sqlite3")
        self.run_id = "synthetic-run"
        now = utc_now()
        with self.repository.connect() as connection:
            connection.execute(
                """INSERT INTO documents (
                    id, sha256, original_filename, media_type, size_bytes, cos_object_key,
                    storage_status, created_at, updated_at
                ) VALUES ('synthetic-document', ?, 'synthetic.pdf', 'application/pdf', 10,
                          'raw/synthetic.pdf', 'ready', ?, ?)""",
                ("a" * 64, now, now),
            )
            connection.execute(
                """INSERT INTO parse_runs (
                    id, document_id, parser, config_json, config_fingerprint, task_id,
                    status, content_list_key, attempt, submission_attempts,
                    created_at, updated_at, completed_at
                ) VALUES (?, 'synthetic-document', 'mineru', '{}', ?, 'synthetic-task',
                          'succeeded', 'parsed/synthetic/content_list.json', 1, 1, ?, ?, ?)""",
                (self.run_id, "b" * 64, now, now, now),
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def service(self, blocks: list[dict] | None = None) -> InvoiceProcessingService:
        value = json.dumps(blocks if blocks is not None else synthetic_blocks(), ensure_ascii=False).encode()
        return InvoiceProcessingService(self.repository, FakeStorage(value))

    def test_normal_squeezed_row_deduplicates_repeated_amounts(self) -> None:
        result = self.service().process(self.run_id)

        self.assertEqual("PASS", result["internal_consistency_result"])
        self.assertEqual("176.44", result["fields"]["net_amount"])
        self.assertEqual("5.29", result["fields"]["tax_amount"])
        self.assertEqual("181.73", result["fields"]["total_amount"])
        self.assertEqual("3%", result["fields"]["tax_rate"])
        self.assertEqual(BUYER_ID, result["fields"]["buyer_tax_id"])
        self.assertEqual(SELLER_ID, result["fields"]["seller_tax_id"])
        self.assertEqual(0, result["field_sources"]["net_amount"][0]["page"])
        self.assertEqual([0, 100, 100, 500], result["field_sources"]["buyer_name"][0]["bbox"])

    def test_reliable_amount_mismatch_is_fail(self) -> None:
        result = self.service(synthetic_blocks(total="181.74")).process(self.run_id)
        checks = {item["rule_id"]: item for item in result["checks"]}
        self.assertEqual("FAIL", checks["INV-AMOUNT-SUM-001"]["result"])
        self.assertEqual("FAIL", result["internal_consistency_result"])

    def test_missing_required_field_is_review_not_fail(self) -> None:
        result = self.service(synthetic_blocks(include_number=False)).process(self.run_id)
        self.assertIsNone(result["fields"]["invoice_number"])
        self.assertEqual("REVIEW", result["internal_consistency_result"])
        self.assertNotIn("FAIL", {item["result"] for item in result["checks"]})

    def test_distinct_candidates_are_ambiguous_and_left_empty(self) -> None:
        result = self.service(synthetic_blocks(second_number="87654321")).process(self.run_id)
        self.assertIsNone(result["fields"]["invoice_number"])
        self.assertEqual("REVIEW", result["internal_consistency_result"])
        self.assertTrue(any(
            item["code"] == "AMBIGUOUS_CANDIDATES" and item["field"] == "invoice_number"
            for item in result["issues"]
        ))

    def test_optional_field_ambiguity_also_requires_review(self) -> None:
        blocks = synthetic_blocks()
        second_table = dict(blocks[-1])
        second_table["table_body"] = second_table["table_body"].replace("脱敏服务费", "另一测试服务")
        blocks.append(second_table)

        result = self.service(blocks).process(self.run_id)

        self.assertIsNone(result["fields"]["service_name"])
        self.assertEqual("REVIEW", result["internal_consistency_result"])
        checks = {item["rule_id"]: item for item in result["checks"]}
        self.assertEqual("REVIEW", checks["INV-EXTRACTION-001"]["result"])

    def test_changed_table_structure_degrades_to_review_without_guessing(self) -> None:
        result = self.service(synthetic_blocks(changed_table=True)).process(self.run_id)
        self.assertEqual("REVIEW", result["internal_consistency_result"])
        self.assertIsNone(result["fields"]["net_amount"])
        self.assertIsNone(result["fields"]["tax_amount"])
        self.assertNotIn("FAIL", {item["result"] for item in result["checks"]})

    def test_repeated_run_reuses_same_extraction(self) -> None:
        service = self.service()
        first = service.process(self.run_id)
        second = service.process(self.run_id)
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["extraction_id"], second["extraction_id"])
        with self.repository.connect() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM invoice_extractions").fetchone()[0])

    def test_extractor_version_change_creates_traceable_record(self) -> None:
        service = self.service()
        first = service.process(self.run_id)
        second = service.process(self.run_id, extractor_version="synthetic-v2")
        self.assertNotEqual(first["extraction_id"], second["extraction_id"])
        with self.repository.connect() as connection:
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM invoice_extractions").fetchone()[0])

    def test_cos_read_failure_writes_no_extraction(self) -> None:
        service = InvoiceProcessingService(self.repository, FakeStorage(None))
        with self.assertRaisesRegex(IngestionError, "synthetic COS failure"):
            service.process(self.run_id)
        with self.repository.connect() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM invoice_extractions").fetchone()[0])

    def test_non_succeeded_parse_run_is_rejected_before_cos_read(self) -> None:
        with self.repository.connect() as connection:
            connection.execute("UPDATE parse_runs SET status = 'failed' WHERE id = ?", (self.run_id,))
        storage = FakeStorage(json.dumps(synthetic_blocks()).encode())
        with self.assertRaisesRegex(IngestionError, "only a succeeded"):
            InvoiceProcessingService(self.repository, storage).process(self.run_id)
        self.assertEqual(0, storage.reads)


if __name__ == "__main__":
    unittest.main()
