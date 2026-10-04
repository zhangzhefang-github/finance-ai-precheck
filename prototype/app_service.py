"""Shared application services for the local Streamlit demo and Python callers."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .document_ingestion import IngestionError, Repository, utc_now
from .expense_precheck import (
    AgictoChatCompletionsSemanticModel,
    CURRENCY_PATTERN,
    ExpensePrecheckService,
    MONEY_PATTERN,
    SemanticModel,
)
from .precheck_report_export import connect_read_only, load_report_bundle


FEEDBACK_LABELS = {
    "CONFIRMED_ISSUE": "确认问题",
    "NEEDS_MORE_EVIDENCE": "需补材料",
    "PRECHECK_INCORRECT": "原预审判断有误",
}
ANOMALY_LABELS = {
    "PRECHECK-CLAIM-001": "申请信息不完整",
    "PRECHECK-EVIDENCE-001": "申请材料缺失",
    "PRECHECK-INVOICE-001": "票面检查异常",
    "PRECHECK-AMOUNT-001": "金额差异",
    "PRECHECK-CURRENCY-001": "币种问题",
}
CLAIM_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def manual_review_status(final_status: str, feedback_count: int) -> str:
    """Describe the next human action without changing the persisted AI result."""

    if feedback_count:
        return "已留反馈"
    return {
        "PASS": "无需复核",
        "REVIEW": "未处理",
        "MISSING_EVIDENCE": "待补材料",
    }.get(final_status, "未处理")


class DictClaimProvider:
    """Expose one independently authored page claim through the ClaimProvider contract."""

    def __init__(self, claim: dict[str, Any]):
        self.claim = dict(claim)

    def get_claim(self, claim_id: str) -> dict[str, Any] | None:
        if claim_id != self.claim.get("claim_id"):
            return None
        return dict(self.claim)


def validate_claim(claim: dict[str, Any]) -> dict[str, Any]:
    """Validate identity/linkage while preserving absent facts for precheck evaluation."""

    fields = (
        "claim_id", "claim_amount", "currency", "expense_category", "purpose",
        "invoice_extraction_id",
    )
    required_identity = ("claim_id", "invoice_extraction_id")
    missing = [
        name for name in required_identity
        if not isinstance(claim.get(name), str) or not claim[name].strip()
    ]
    if missing:
        raise IngestionError("CLAIM_INVALID", f"missing claim fields: {', '.join(missing)}")
    invalid_types = [
        name for name in fields
        if name in claim and claim[name] is not None and not isinstance(claim[name], str)
    ]
    if invalid_types:
        raise IngestionError(
            "CLAIM_INVALID", f"claim fields must be strings: {', '.join(invalid_types)}"
        )
    normalized = {
        name: claim.get(name, "").strip()
        if isinstance(claim.get(name, ""), str) else ""
        for name in fields
    }
    if not CLAIM_ID_PATTERN.fullmatch(normalized["claim_id"]):
        raise IngestionError("CLAIM_INVALID", "claim_id contains unsupported characters or is too long")
    if normalized["claim_amount"] and not MONEY_PATTERN.fullmatch(normalized["claim_amount"]):
        raise IngestionError("CLAIM_INVALID", "claim_amount must be a non-negative two-decimal string")
    if normalized["currency"] and not CURRENCY_PATTERN.fullmatch(normalized["currency"]):
        raise IngestionError("CLAIM_INVALID", "currency must be a three-letter uppercase code")
    if len(normalized["expense_category"]) > 100 or len(normalized["purpose"]) > 2000:
        raise IngestionError("CLAIM_INVALID", "expense category or purpose is too long")
    normalized["data_classification"] = "MOCK"
    return normalized


def report_anomaly_types(report: dict[str, Any]) -> list[str]:
    """Map persisted non-PASS checks to reviewer-facing labels without re-evaluation."""

    labels: list[str] = []
    semantic_check_failed = False
    for item in report.get("deterministic_checks", []):
        if not isinstance(item, dict) or item.get("result") == "PASS":
            continue
        if item.get("check_id") == "PRECHECK-SEMANTIC-001":
            semantic_check_failed = True
            continue
        label = ANOMALY_LABELS.get(str(item.get("check_id")), "其他预审异常")
        if label not in labels:
            labels.append(label)
    if semantic_check_failed:
        semantic = report.get("semantic_judgment") or {}
        route = semantic.get("route")
        decision = semantic.get("decision")
        if route in {"model_error", "model_unavailable"}:
            labels.append("系统未能完成语义判断")
        elif decision == "CONTRADICTED":
            labels.append("申请事由与票面不匹配")
        else:
            labels.append("申请事由与票面关系无法确定")
    return labels


class DemoQueryService:
    """Strictly read-only queries used while browsing the demo."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def list_reports(
        self, *, status: str | None = None, search: str = "", limit: int = 200
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("final_status = ?")
            values.append(status)
        if search.strip():
            clauses.append("(claim_id LIKE ? OR id LIKE ?)")
            token = f"%{search.strip()}%"
            values.extend((token, token))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with connect_read_only(self.db_path) as connection:
            feedback_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_feedback'"
            ).fetchone() is not None
            feedback_columns = (
                """, (SELECT COUNT(*) FROM review_feedback f WHERE f.precheck_run_id = precheck_runs.id)
                         AS feedback_count,
                       (SELECT outcome FROM review_feedback f WHERE f.precheck_run_id = precheck_runs.id
                        ORDER BY f.created_at DESC, f.id DESC LIMIT 1) AS latest_feedback_outcome"""
                if feedback_exists else
                ", 0 AS feedback_count, NULL AS latest_feedback_outcome"
            )
            rows = connection.execute(
                f"""SELECT id, claim_id, extraction_id, final_status, model_invoked,
                           model_provider, model_id, created_at, report_json
                           {feedback_columns}
                    FROM precheck_runs {where}
                    ORDER BY created_at DESC, rowid DESC LIMIT ?""",
                (*values, max(1, min(limit, 500))),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row_value in rows:
            row = dict(row_value)
            try:
                report = json.loads(row.pop("report_json"))
            except (TypeError, json.JSONDecodeError) as exc:
                raise IngestionError("PRECHECK_REPORT_INVALID", "stored report_json is invalid") from exc
            checks = {
                item.get("check_id"): item
                for item in report.get("deterministic_checks", [])
                if isinstance(item, dict)
            }
            amount = checks.get("PRECHECK-AMOUNT-001", {}).get("values", {})
            semantic = report.get("semantic_judgment", {})
            feedback_count = int(row.get("feedback_count") or 0)
            manual_status = manual_review_status(row.get("final_status"), feedback_count)
            anomalies = report_anomaly_types(report)
            row.update({
                "claim_amount": amount.get("claim_amount"),
                "invoice_total_amount": amount.get("invoice_total_amount"),
                "amount_difference": amount.get("difference"),
                "semantic_route": semantic.get("route", "unrecorded"),
                "manual_status": manual_status,
                "anomaly_types": anomalies,
                "anomaly_summary": "、".join(anomalies) if anomalies else "已配置检查未见异常",
            })
            result.append(row)
        return result

    def report_bundle(self, precheck_run_id: str) -> dict[str, Any]:
        bundle = load_report_bundle(self.db_path, precheck_run_id)
        with connect_read_only(self.db_path) as connection:
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_feedback'"
            ).fetchone()
            feedback = [] if table_exists is None else [dict(row) for row in connection.execute(
                """SELECT id, outcome, note, operator_id, created_at
                   FROM review_feedback WHERE precheck_run_id = ?
                   ORDER BY created_at, id""",
                (precheck_run_id,),
            ).fetchall()]
        bundle["review_feedback"] = feedback
        return bundle

    def list_extractions(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with connect_read_only(self.db_path) as connection:
            rows = connection.execute(
                """SELECT e.id AS extraction_id, e.parse_run_id, e.created_at,
                          e.internal_consistency_result, e.invoice_type, e.invoice_number,
                          e.issue_date, e.service_name, e.total_amount,
                          r.status AS parse_status, r.document_id,
                          d.original_filename, d.media_type
                   FROM invoice_extractions e
                   JOIN parse_runs r ON r.id = e.parse_run_id
                   JOIN documents d ON d.id = r.document_id
                   WHERE e.status = 'succeeded' AND r.status = 'succeeded'
                   ORDER BY e.created_at DESC, e.rowid DESC LIMIT ?""",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def local_parse_status(self, parse_run_id: str) -> dict[str, Any]:
        with connect_read_only(self.db_path) as connection:
            row = connection.execute(
                """SELECT r.*, d.original_filename, d.media_type
                   FROM parse_runs r JOIN documents d ON d.id = r.document_id
                   WHERE r.id = ?""",
                (parse_run_id,),
            ).fetchone()
        if row is None:
            raise IngestionError("PARSE_RUN_NOT_FOUND", "parse run not found")
        return dict(row)

    def latest_parse_status(self, document_id: str) -> dict[str, Any] | None:
        """Return the latest persisted attempt for recovery after a submit exception."""

        with connect_read_only(self.db_path) as connection:
            row = connection.execute(
                """SELECT r.*, d.original_filename, d.media_type
                   FROM parse_runs r JOIN documents d ON d.id = r.document_id
                   WHERE r.document_id = ?
                   ORDER BY r.attempt DESC, r.created_at DESC, r.rowid DESC LIMIT 1""",
                (document_id,),
            ).fetchone()
        return dict(row) if row is not None else None


class DemoCommandService:
    """Explicit write operations; construction applies migrations, browsing never does."""

    def __init__(self, db_path: str | Path):
        self.repository = Repository(db_path, migrate=True)

    def run_precheck(
        self,
        claim: dict[str, Any],
        *,
        semantic_model: SemanticModel | None = None,
    ) -> dict[str, Any]:
        normalized = validate_claim(claim)
        with self.repository.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM invoice_extractions WHERE id = ? AND status = 'succeeded'",
                (normalized["invoice_extraction_id"],),
            ).fetchone()
        if exists is None:
            raise IngestionError("EXTRACTION_NOT_FOUND", "selected successful extraction was not found")
        service = ExpensePrecheckService(
            self.repository, DictClaimProvider(normalized), semantic_model
        )
        return service.run(normalized["claim_id"])

    def configured_model(self) -> AgictoChatCompletionsSemanticModel | None:
        return AgictoChatCompletionsSemanticModel.from_env()

    def add_review_feedback(
        self, precheck_run_id: str, *, outcome: str, note: str, operator_id: str
    ) -> dict[str, Any]:
        if outcome not in FEEDBACK_LABELS:
            raise IngestionError("FEEDBACK_INVALID", "unsupported review feedback outcome")
        note, operator_id = note.strip(), operator_id.strip()
        if not note or len(note) > 2000:
            raise IngestionError("FEEDBACK_INVALID", "note is required and must not exceed 2000 characters")
        if not operator_id or len(operator_id) > 100:
            raise IngestionError("FEEDBACK_INVALID", "operator_id is required and must not exceed 100 characters")
        feedback = {
            "id": uuid.uuid4().hex,
            "precheck_run_id": precheck_run_id,
            "outcome": outcome,
            "note": note,
            "operator_id": operator_id,
            "created_at": utc_now(),
        }
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            report = connection.execute(
                "SELECT final_status FROM precheck_runs WHERE id = ?", (precheck_run_id,)
            ).fetchone()
            if report is None:
                raise IngestionError("PRECHECK_RUN_NOT_FOUND", "precheck report not found")
            connection.execute(
                """INSERT INTO review_feedback (
                       id, precheck_run_id, outcome, note, operator_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                tuple(feedback.values()),
            )
        return feedback
