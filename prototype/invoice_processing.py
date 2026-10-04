#!/usr/bin/env python3
"""Extract and deterministically check invoices from persisted MinerU artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from typing import Any

try:  # Support both `python -m prototype...` and direct script execution.
    from .document_ingestion import (
        CosObjectStorage,
        IngestionError,
        ObjectStorage,
        Repository,
        Settings,
        safe_message,
        utc_now,
    )
except ImportError:  # pragma: no cover - exercised by documented CLI usage
    from document_ingestion import (  # type: ignore[no-redef]
        CosObjectStorage,
        IngestionError,
        ObjectStorage,
        Repository,
        Settings,
        safe_message,
        utc_now,
    )


SCHEMA_VERSION = "invoice-v1"
EXTRACTOR_VERSION = "mineru-content-list-v1"
RULESET_VERSION = "invoice-consistency-v1"
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
MAX_BLOCKS = 10_000
MAX_BLOCK_TEXT_CHARS = 2_000_000
MONEY_QUANTUM = Decimal("0.01")
FIELDS = (
    "invoice_type",
    "invoice_code",
    "invoice_number",
    "issue_date",
    "buyer_name",
    "buyer_tax_id",
    "seller_name",
    "seller_tax_id",
    "service_name",
    "net_amount",
    "tax_amount",
    "total_amount",
    "tax_rate",
)
REQUIRED_FIELDS = (
    "invoice_code",
    "invoice_number",
    "issue_date",
    "buyer_name",
    "buyer_tax_id",
    "seller_name",
    "seller_tax_id",
    "net_amount",
    "tax_amount",
    "total_amount",
)


@dataclass(frozen=True)
class Candidate:
    value: str
    raw_text: str
    evidence: dict[str, Any]


@dataclass
class Extraction:
    fields: dict[str, str | None]
    sources: dict[str, list[dict[str, Any]]]
    issues: list[dict[str, Any]]


class TableParser(HTMLParser):
    """Small dependency-free HTML table reader preserving row/cell boundaries."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() == "tr":
            self._row = []
        elif tag.lower() in {"td", "th"} and self._row is not None:
            self._cell = []
        elif tag.lower() == "br" and self._cell is not None:
            self._cell.append("\n")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"td", "th"} and self._cell is not None:
            assert self._row is not None
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag.lower() == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def compact(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("：", ":")


def bounded_raw(value: str) -> str:
    return value.replace("\x00", "")[:1000]


def money(value: str) -> str:
    try:
        amount = Decimal(value.replace(",", "").replace("￥", "").replace("¥", ""))
    except InvalidOperation as exc:
        raise ValueError("invalid money") from exc
    return str(amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP))


def rate(value: str) -> str:
    number = Decimal(value.rstrip("%"))
    normalized = format(number.normalize(), "f")
    return f"{normalized}%"


def evidence_for(
    object_key: str,
    block_index: int,
    block: dict[str, Any],
    source_kind: str,
    raw_text: str,
    *,
    row: int | None = None,
    cell: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "object_key": object_key,
        "block_index": block_index,
        "page": block.get("page_idx"),
        "bbox": block.get("bbox"),
        "source_kind": source_kind,
        "raw_text": bounded_raw(raw_text),
    }
    if row is not None:
        result["table_row"] = row
    if cell is not None:
        result["table_cell"] = cell
    return result


def add_candidate(
    candidates: dict[str, list[Candidate]],
    field: str,
    value: str,
    raw_text: str,
    evidence: dict[str, Any],
) -> None:
    cleaned = value.strip().strip(":：*（）()")
    if cleaned:
        candidates[field].append(Candidate(cleaned, bounded_raw(raw_text), evidence))


def labeled_value(text: str, label: str, following_labels: tuple[str, ...]) -> str | None:
    source = compact(text)
    escaped_following = "|".join(re.escape(compact(item)) for item in following_labels)
    suffix = rf"(?={escaped_following}|$)" if escaped_following else "$"
    match = re.search(rf"{re.escape(compact(label))}:?(.*?){suffix}", source)
    return match.group(1).strip() if match else None


def parse_content_list(raw: bytes) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IngestionError("CONTENT_LIST_INVALID", "content_list.json is not valid UTF-8 JSON") from exc
    if not isinstance(value, list):
        raise IngestionError("CONTENT_LIST_INVALID", "content_list.json root must be a list")
    if len(value) > MAX_BLOCKS:
        raise IngestionError("CONTENT_LIST_TOO_LARGE", "content_list.json contains too many blocks")
    for index, block in enumerate(value):
        if not isinstance(block, dict):
            raise IngestionError("CONTENT_LIST_INVALID", f"block {index} must be an object")
        for field in ("text", "table_body"):
            content = block.get(field)
            if content is not None and not isinstance(content, str):
                raise IngestionError("CONTENT_LIST_INVALID", f"block {index} {field} must be text")
            if isinstance(content, str) and len(content) > MAX_BLOCK_TEXT_CHARS:
                raise IngestionError("CONTENT_LIST_TOO_LARGE", f"block {index} is too large")
    return value


def _text_candidates(
    blocks: list[dict[str, Any]], object_key: str, candidates: dict[str, list[Candidate]]
) -> None:
    patterns: tuple[tuple[str, re.Pattern[str], Any], ...] = (
        ("invoice_code", re.compile(r"发\s*票\s*代\s*码\s*[:：]?\s*([0-9]{10,12})"), str),
        ("invoice_number", re.compile(r"发\s*票\s*号\s*码\s*[:：]?\s*([0-9]{8,20})"), str),
        (
            "issue_date",
            re.compile(r"开\s*票\s*日\s*期\s*[:：]?\s*(\d{4})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?"),
            lambda match: f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}",
        ),
    )
    invoice_types = ("增值税电子普通发票", "增值税普通发票", "增值税专用发票", "电子发票")
    for index, block in enumerate(blocks):
        text = block.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        ev = evidence_for(object_key, index, block, "text_block", text)
        normalized_text = compact(text)
        for invoice_type in invoice_types:
            if invoice_type in normalized_text:
                add_candidate(candidates, "invoice_type", invoice_type, text, ev)
                break
        for field, pattern, converter in patterns:
            for match in pattern.finditer(text):
                value = converter(match) if field == "issue_date" else converter(match.group(1))
                add_candidate(candidates, field, value, match.group(0), ev)


def _party_candidates(
    cells: list[str], party: str, block: dict[str, Any], block_index: int, object_key: str,
    row_index: int, candidates: dict[str, list[Candidate]], issues: list[dict[str, Any]],
) -> None:
    label_cell = compact(cells[0]) if cells else ""
    expected = "购买方" if party == "buyer" else "销售方"
    if expected not in label_cell:
        return
    content_cells = [(index, value) for index, value in enumerate(cells[1:], 1) if value.strip()]
    if not content_cells:
        issues.append({"code": "PARTY_CELL_MISSING", "field": party, "reason": f"{expected} row has no value cell"})
        return
    cell_index, source = content_cells[0]
    ev = evidence_for(object_key, block_index, block, "table_cell", source, row=row_index, cell=cell_index)
    name = labeled_value(source, "名称", ("纳税人识别号", "地址、电话", "开户行及账号"))
    tax_id = labeled_value(source, "纳税人识别号", ("地址、电话", "开户行及账号"))
    if name:
        add_candidate(candidates, f"{party}_name", name, source, ev)
    else:
        issues.append({"code": "LABELED_VALUE_MISSING", "field": f"{party}_name", "reason": f"{expected}名称 label/value boundary was not reliable", "evidence": ev})
    if tax_id and re.fullmatch(r"[0-9A-Z]{15,20}", tax_id.upper()):
        add_candidate(candidates, f"{party}_tax_id", tax_id.upper(), source, ev)
    else:
        issues.append({"code": "LABELED_VALUE_MISSING", "field": f"{party}_tax_id", "reason": f"{expected}税号 was missing or malformed", "evidence": ev})


def _unique_numeric_values(text: str, kind: str) -> list[str]:
    if kind == "rate":
        found = [rate(item) for item in re.findall(r"\d+(?:\.\d+)?%", text)]
    else:
        found = [money(item) for item in re.findall(r"[￥¥]?[-+]?\d[\d,]*\.\d{1,2}", text)]
    return list(dict.fromkeys(found))


def _table_candidates(
    blocks: list[dict[str, Any]], object_key: str, candidates: dict[str, list[Candidate]],
    issues: list[dict[str, Any]],
) -> None:
    for block_index, block in enumerate(blocks):
        html = block.get("table_body")
        if not isinstance(html, str) or not html.strip():
            continue
        parser = TableParser()
        try:
            parser.feed(html)
            parser.close()
        except Exception as exc:
            issues.append({"code": "TABLE_HTML_INVALID", "field": None, "reason": safe_message(exc)})
            continue
        if not parser.rows:
            issues.append({"code": "TABLE_EMPTY", "field": None, "reason": "table block has no rows"})
            continue
        for row_index, cells in enumerate(parser.rows):
            if not cells:
                continue
            _party_candidates(cells, "buyer", block, block_index, object_key, row_index, candidates, issues)
            _party_candidates(cells, "seller", block, block_index, object_key, row_index, candidates, issues)
            compact_row = compact("".join(cells))

            # The detail and total may share one physical row. Labels within each
            # cell, rather than a dedicated summary row, determine field meaning.
            if "合计" in compact_row:
                for cell_index, cell in enumerate(cells):
                    normalized = compact(cell)
                    field_kind: tuple[str, str] | None = None
                    if "金额" in normalized:
                        field_kind = ("net_amount", "money")
                    elif "税额" in normalized:
                        field_kind = ("tax_amount", "money")
                    elif "税率" in normalized:
                        field_kind = ("tax_rate", "rate")
                    if field_kind:
                        values = _unique_numeric_values(normalized, field_kind[1])
                        ev = evidence_for(object_key, block_index, block, "table_cell", cell, row=row_index, cell=cell_index)
                        for value in values:
                            add_candidate(candidates, field_kind[0], value, cell, ev)

                service_cell = cells[0]
                service_text = compact(service_cell)
                service_label = "货物或应税劳务、服务名称"
                if service_label in service_text:
                    value = service_text.split(service_label, 1)[1].split("合计", 1)[0].strip("*:：")
                    ev = evidence_for(object_key, block_index, block, "table_cell", service_cell, row=row_index, cell=0)
                    add_candidate(candidates, "service_name", value, service_cell, ev)

            if "价税合计" in compact_row and "小写" in compact_row:
                row_values: list[tuple[str, int, str]] = []
                for cell_index, cell in enumerate(cells):
                    if "小写" not in compact(cell) and not row_values:
                        continue
                    for value in _unique_numeric_values(compact(cell), "money"):
                        row_values.append((value, cell_index, cell))
                for value, cell_index, cell in row_values:
                    ev = evidence_for(object_key, block_index, block, "table_cell", cell, row=row_index, cell=cell_index)
                    add_candidate(candidates, "total_amount", value, cell, ev)


def extract_invoice(blocks: list[dict[str, Any]], object_key: str) -> Extraction:
    candidates = {field: [] for field in FIELDS}
    issues: list[dict[str, Any]] = []
    _text_candidates(blocks, object_key, candidates)
    _table_candidates(blocks, object_key, candidates, issues)

    fields: dict[str, str | None] = {}
    sources: dict[str, list[dict[str, Any]]] = {}
    for field, values in candidates.items():
        by_value: dict[str, list[Candidate]] = {}
        for candidate in values:
            by_value.setdefault(candidate.value, []).append(candidate)
        if len(by_value) == 1:
            value, matching = next(iter(by_value.items()))
            fields[field] = value
            sources[field] = [item.evidence for item in matching]
        elif len(by_value) > 1:
            fields[field] = None
            sources[field] = []
            issues.append({
                "code": "AMBIGUOUS_CANDIDATES",
                "field": field,
                "reason": "multiple distinct candidates were found; field was left empty",
                "candidates": [
                    {"value": value, "evidence": [item.evidence for item in matching]}
                    for value, matching in by_value.items()
                ],
            })
        else:
            fields[field] = None
            sources[field] = []
    return Extraction(fields, sources, issues)


def _check(
    rule_id: str, result: str, values: dict[str, Any], evidence: dict[str, Any], reason: str
) -> dict[str, Any]:
    return {"rule_id": rule_id, "result": result, "values": values, "evidence": evidence, "reason": reason}


def run_checks(extraction: Extraction) -> list[dict[str, Any]]:
    fields, sources = extraction.fields, extraction.sources
    missing = [field for field in REQUIRED_FIELDS if not fields.get(field)]
    checks = [
        _check(
            "INV-EXTRACTION-001",
            "REVIEW" if extraction.issues else "PASS",
            {"issue_count": len(extraction.issues)},
            {"issues": extraction.issues},
            "extraction has unresolved issues" if extraction.issues else "extraction has no unresolved issues",
        ),
        _check(
            "INV-REQUIRED-001",
            "REVIEW" if missing else "PASS",
            {"required_fields": list(REQUIRED_FIELDS), "missing_fields": missing},
            {field: sources.get(field, []) for field in REQUIRED_FIELDS},
            "required fields are missing or ambiguous" if missing else "all required fields are present",
        )
    ]

    issue_date = fields.get("issue_date")
    date_ok = False
    if issue_date:
        try:
            datetime.strptime(issue_date, "%Y-%m-%d")
            date_ok = True
        except ValueError:
            date_ok = False
    checks.append(_check(
        "INV-DATE-001", "PASS" if date_ok else "REVIEW", {"issue_date": issue_date},
        {"issue_date": sources.get("issue_date", [])},
        "date uses YYYY-MM-DD" if date_ok else "date is missing, ambiguous, or invalid",
    ))

    amount_fields = ("net_amount", "tax_amount", "total_amount")
    parsed_amounts: dict[str, Decimal] = {}
    for field in amount_fields:
        try:
            value = fields.get(field)
            if value is not None and re.fullmatch(r"-?\d+\.\d{2}", value):
                parsed_amounts[field] = Decimal(value)
        except InvalidOperation:
            pass
    amounts_ok = len(parsed_amounts) == len(amount_fields)
    checks.append(_check(
        "INV-AMOUNT-FORMAT-001", "PASS" if amounts_ok else "REVIEW",
        {field: fields.get(field) for field in amount_fields},
        {field: sources.get(field, []) for field in amount_fields},
        "all monetary fields use two decimal places" if amounts_ok else "one or more monetary fields are missing, ambiguous, or invalid",
    ))

    if amounts_ok:
        expected = (parsed_amounts["net_amount"] + parsed_amounts["tax_amount"]).quantize(MONEY_QUANTUM)
        actual = parsed_amounts["total_amount"].quantize(MONEY_QUANTUM)
        sum_result = "PASS" if expected == actual else "FAIL"
        sum_reason = "net amount plus tax equals total" if sum_result == "PASS" else "reliable monetary fields do not add up"
        sum_values = {**{field: fields[field] for field in amount_fields}, "expected_total": str(expected)}
    else:
        sum_result, sum_reason = "REVIEW", "cannot evaluate amount equality without three reliable monetary fields"
        sum_values = {field: fields.get(field) for field in amount_fields}
    checks.append(_check(
        "INV-AMOUNT-SUM-001", sum_result, sum_values,
        {field: sources.get(field, []) for field in amount_fields}, sum_reason,
    ))

    tax_rate = fields.get("tax_rate")
    if amounts_ok and tax_rate and re.fullmatch(r"\d+(?:\.\d+)?%", tax_rate):
        expected_tax = (
            parsed_amounts["net_amount"] * Decimal(tax_rate[:-1]) / Decimal("100")
        ).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        actual_tax = parsed_amounts["tax_amount"].quantize(MONEY_QUANTUM)
        tax_result = "PASS" if expected_tax == actual_tax else "FAIL"
        tax_reason = "tax matches net amount times rate using ROUND_HALF_UP to cents" if tax_result == "PASS" else "reliable tax fields are inconsistent after ROUND_HALF_UP to cents"
        tax_values = {"net_amount": fields["net_amount"], "tax_rate": tax_rate, "tax_amount": fields["tax_amount"], "expected_tax": str(expected_tax)}
    else:
        tax_result, tax_reason = "REVIEW", "cannot evaluate tax relation without reliable net amount, tax amount, and tax rate"
        tax_values = {"net_amount": fields.get("net_amount"), "tax_rate": tax_rate, "tax_amount": fields.get("tax_amount")}
    checks.append(_check(
        "INV-TAX-RATE-001", tax_result, tax_values,
        {field: sources.get(field, []) for field in ("net_amount", "tax_rate", "tax_amount")}, tax_reason,
    ))
    return checks


def aggregate_result(checks: list[dict[str, Any]]) -> str:
    results = {item["result"] for item in checks}
    if "FAIL" in results:
        return "FAIL"
    if "REVIEW" in results:
        return "REVIEW"
    return "PASS"


class InvoiceProcessingService:
    def __init__(
        self, repository: Repository, storage: ObjectStorage,
        *, max_artifact_bytes: int = MAX_ARTIFACT_BYTES,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.max_artifact_bytes = max_artifact_bytes

    def _load(self, parse_run_id: str) -> tuple[dict[str, Any], bytes, str]:
        run = self.repository.get_parse_run(parse_run_id)
        if run is None:
            raise IngestionError("PARSE_RUN_NOT_FOUND", "parse run not found")
        if run["status"] != "succeeded":
            raise IngestionError("RESULT_NOT_READY", "only a succeeded parse run can be processed")
        key = run.get("content_list_key")
        if not isinstance(key, str) or not key:
            raise IngestionError("CONTENT_LIST_MISSING", "successful parse run has no content_list object key")
        raw = self.storage.get_bytes(key, self.max_artifact_bytes)
        return run, raw, key

    @staticmethod
    def _decoded_extraction(row: dict[str, Any]) -> Extraction:
        return Extraction(
            {field: row.get(field) for field in FIELDS},
            json.loads(row["field_sources_json"]),
            json.loads(row["extraction_issues_json"]),
        )

    @staticmethod
    def _view(row: dict[str, Any], checks: list[dict[str, Any]], reused: bool) -> dict[str, Any]:
        return {
            "extraction_id": row["id"],
            "parse_run_id": row["parse_run_id"],
            "input_object_key": row["input_object_key"],
            "input_sha256": row["input_sha256"],
            "schema_version": row["schema_version"],
            "extractor_version": row["extractor_version"],
            "status": row["status"],
            "internal_consistency_result": aggregate_result(checks),
            "reused": reused,
            "fields": {field: row.get(field) for field in FIELDS},
            "field_sources": json.loads(row["field_sources_json"]),
            "issues": json.loads(row["extraction_issues_json"]),
            "checks": checks,
            "disclaimer": "Results cover extraction and internal consistency only; they do not verify authenticity or approve reimbursement.",
        }

    def process(
        self, parse_run_id: str, *, schema_version: str = SCHEMA_VERSION,
        extractor_version: str = EXTRACTOR_VERSION, ruleset_version: str = RULESET_VERSION,
    ) -> dict[str, Any]:
        _run, raw, object_key = self._load(parse_run_id)
        digest = hashlib.sha256(raw).hexdigest()
        blocks = parse_content_list(raw)
        extracted = extract_invoice(blocks, object_key)
        checks = run_checks(extracted)
        overall = aggregate_result(checks)
        now = utc_now()
        extraction_id = uuid.uuid4().hex
        canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_row = connection.execute(
                """SELECT * FROM invoice_extractions
                   WHERE parse_run_id = ? AND input_sha256 = ?
                     AND schema_version = ? AND extractor_version = ?""",
                (parse_run_id, digest, schema_version, extractor_version),
            ).fetchone()
            reused = existing_row is not None
            if existing_row is None:
                connection.execute(
                    f"""INSERT INTO invoice_extractions (
                        id, parse_run_id, input_object_key, input_sha256, schema_version,
                        extractor_version, status, internal_consistency_result,
                        {', '.join(FIELDS)}, field_sources_json, extraction_issues_json, created_at
                    ) VALUES ({', '.join('?' for _ in range(8 + len(FIELDS) + 3))})""",
                    (
                        extraction_id, parse_run_id, object_key, digest, schema_version,
                        extractor_version, "succeeded", overall,
                        *(extracted.fields[field] for field in FIELDS),
                        canonical(extracted.sources), canonical(extracted.issues), now,
                    ),
                )
                existing_row = connection.execute(
                    "SELECT * FROM invoice_extractions WHERE id = ?", (extraction_id,)
                ).fetchone()
            else:
                extraction_id = existing_row["id"]
                extracted = self._decoded_extraction(dict(existing_row))
                checks = run_checks(extracted)
                overall = aggregate_result(checks)

            for item in checks:
                connection.execute(
                    """INSERT OR IGNORE INTO invoice_checks (
                           id, extraction_id, rule_id, ruleset_version, result,
                           values_json, evidence_json, reason, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        uuid.uuid4().hex, extraction_id, item["rule_id"], ruleset_version,
                        item["result"], canonical(item["values"]), canonical(item["evidence"]),
                        item["reason"], now,
                    ),
                )
            connection.execute(
                "UPDATE invoice_extractions SET internal_consistency_result = ? WHERE id = ?",
                (overall, extraction_id),
            )
            row = dict(connection.execute(
                "SELECT * FROM invoice_extractions WHERE id = ?", (extraction_id,)
            ).fetchone())
            stored_checks = [dict(item) for item in connection.execute(
                """SELECT * FROM invoice_checks
                   WHERE extraction_id = ? AND ruleset_version = ? ORDER BY rule_id""",
                (extraction_id, ruleset_version),
            ).fetchall()]
        check_views = [
            {
                "rule_id": item["rule_id"], "ruleset_version": item["ruleset_version"],
                "result": item["result"], "values": json.loads(item["values_json"]),
                "evidence": json.loads(item["evidence_json"]), "reason": item["reason"],
            }
            for item in stored_checks
        ]
        return self._view(row, check_views, reused)

    def show(
        self, *, parse_run_id: str | None = None, extraction_id: str | None = None,
        ruleset_version: str = RULESET_VERSION,
    ) -> dict[str, Any]:
        if bool(parse_run_id) == bool(extraction_id):
            raise IngestionError("QUERY_INVALID", "provide exactly one of parse_run_id or extraction_id")
        with self.repository.connect() as connection:
            if extraction_id:
                row_value = connection.execute(
                    "SELECT * FROM invoice_extractions WHERE id = ?", (extraction_id,)
                ).fetchone()
            else:
                row_value = connection.execute(
                    """SELECT * FROM invoice_extractions WHERE parse_run_id = ?
                       ORDER BY created_at DESC, rowid DESC LIMIT 1""", (parse_run_id,)
                ).fetchone()
            if row_value is None:
                raise IngestionError("EXTRACTION_NOT_FOUND", "invoice extraction not found")
            row = dict(row_value)
            stored_checks = [dict(item) for item in connection.execute(
                """SELECT * FROM invoice_checks
                   WHERE extraction_id = ? AND ruleset_version = ? ORDER BY rule_id""",
                (row["id"], ruleset_version),
            ).fetchall()]
        if not stored_checks:
            raise IngestionError("CHECKS_NOT_FOUND", "no checks exist for the requested ruleset version")
        checks = [
            {
                "rule_id": item["rule_id"], "ruleset_version": item["ruleset_version"],
                "result": item["result"], "values": json.loads(item["values_json"]),
                "evidence": json.loads(item["evidence_json"]), "reason": item["reason"],
            }
            for item in stored_checks
        ]
        return self._view(row, checks, True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("DOCUMENT_DB_PATH", "var/document_ingestion.sqlite3"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    process = subparsers.add_parser("process", help="extract and check one successful parse run")
    process.add_argument("--parse-run-id", required=True)
    process.add_argument("--schema-version", default=SCHEMA_VERSION)
    process.add_argument("--extractor-version", default=EXTRACTOR_VERSION)
    process.add_argument("--ruleset-version", default=RULESET_VERSION)
    show = subparsers.add_parser("show", help="query a persisted extraction and its checks")
    target = show.add_mutually_exclusive_group(required=True)
    target.add_argument("--parse-run-id")
    target.add_argument("--extraction-id")
    show.add_argument("--ruleset-version", default=RULESET_VERSION)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repository = Repository(args.db)
        if args.command == "process":
            settings = Settings.from_env()
            service = InvoiceProcessingService(repository, CosObjectStorage(settings))
            output = service.process(
                args.parse_run_id, schema_version=args.schema_version,
                extractor_version=args.extractor_version, ruleset_version=args.ruleset_version,
            )
        else:
            # Local query deliberately requires no COS credentials or network access.
            service = InvoiceProcessingService(repository, storage=None)  # type: ignore[arg-type]
            output = service.show(
                parse_run_id=args.parse_run_id, extraction_id=args.extraction_id,
                ruleset_version=args.ruleset_version,
            )
    except (IngestionError, OSError, sqlite3.Error, ValueError) as exc:
        code = exc.code if isinstance(exc, IngestionError) else "INVOICE_PROCESSING_ERROR"
        print(json.dumps({"error": code, "message": safe_message(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
