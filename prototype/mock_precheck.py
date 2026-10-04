#!/usr/bin/env python3
"""Evaluate MOCK lodging expense requests against explicitly versioned MOCK rules."""

from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


CONTRACT_VERSION = "MOCK-CONTRACT-v0.1"
MOCK = "MOCK"
SCENARIO = "MOCK_LODGING"
MONEY_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.[0-9]{2}$")

CONCLUSION_LABELS = {
    "MOCK_PASS": "MOCK：满足模拟规则",
    "MOCK_MISSING_EVIDENCE": "MOCK：缺材料／人工补证",
    "MOCK_REVIEW_CONFLICT": "MOCK：材料冲突／人工复核",
    "MOCK_RULE_PENDING_REVIEW": "MOCK：规则待确认／人工复核",
}


class ContractError(ValueError):
    """Raised when a request or MOCK rule catalog violates the contract."""


def _require_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{path} must be an object")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{path} must be an array")
    return value


def _require_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{path} must be a non-empty string")
    return value


def _require_mock(value: Any, path: str) -> None:
    if value != MOCK:
        raise ContractError(f'{path} must equal "MOCK"')


def _parse_date(value: Any, path: str) -> date:
    raw = _require_string(value, path)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ContractError(f"{path} must be an ISO date (YYYY-MM-DD)") from exc
    if parsed.isoformat() != raw:
        raise ContractError(f"{path} must use canonical YYYY-MM-DD format")
    return parsed


def _parse_money(value: Any, path: str) -> Decimal:
    if not isinstance(value, str) or not MONEY_PATTERN.fullmatch(value):
        raise ContractError(f"{path} must be a non-negative two-decimal string")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ContractError(f"{path} is not a valid decimal amount") from exc


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    return _require_dict(value, str(path))


def validate_rule_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    """Validate the catalog and every rule; invalid rules are never ignored."""

    catalog = deepcopy(_require_dict(catalog, "rule_catalog"))
    _require_mock(catalog.get("data_classification"), "rule_catalog.data_classification")
    _require_string(catalog.get("catalog_id"), "rule_catalog.catalog_id")
    _require_string(catalog.get("catalog_version"), "rule_catalog.catalog_version")
    rules = _require_list(catalog.get("rules"), "rule_catalog.rules")

    seen_ids: set[tuple[str, str]] = set()
    for index, raw_rule in enumerate(rules):
        path = f"rule_catalog.rules[{index}]"
        rule = _require_dict(raw_rule, path)
        _require_mock(rule.get("data_classification"), f"{path}.data_classification")
        rule_id = _require_string(rule.get("rule_id"), f"{path}.rule_id")
        rule_version = _require_string(rule.get("rule_version"), f"{path}.rule_version")
        identity = (rule_id, rule_version)
        if identity in seen_ids:
            raise ContractError(f"duplicate rule identity: {rule_id}@{rule_version}")
        seen_ids.add(identity)

        _require_string(rule.get("scenario"), f"{path}.scenario")
        _require_string(rule.get("scope_key"), f"{path}.scope_key")
        effective_from = _parse_date(rule.get("effective_from"), f"{path}.effective_from")
        effective_to = _parse_date(rule.get("effective_to"), f"{path}.effective_to")
        if effective_to < effective_from:
            raise ContractError(f"{path}.effective_to cannot be before effective_from")

        rule_type = _require_string(rule.get("rule_type"), f"{path}.rule_type")
        if rule_type == "REQUIRED_SUMMARIES":
            required_types = _require_list(
                rule.get("required_summary_types"), f"{path}.required_summary_types"
            )
            if not required_types:
                raise ContractError(f"{path}.required_summary_types cannot be empty")
            for item_index, summary_type in enumerate(required_types):
                _require_string(summary_type, f"{path}.required_summary_types[{item_index}]")
        elif rule_type == "CONSISTENCY":
            checks = _require_list(rule.get("checks"), f"{path}.checks")
            allowed_checks = {"AMOUNT_AND_CURRENCY", "INVOICE_STAY_DATES"}
            if not checks or any(check not in allowed_checks for check in checks):
                raise ContractError(f"{path}.checks contains an unsupported check")
        else:
            raise ContractError(f"{path}.rule_type is unsupported: {rule_type}")

    return catalog


def validate_request(request: dict[str, Any]) -> dict[str, Any]:
    request = deepcopy(_require_dict(request, "request"))
    if request.get("contract_version") != CONTRACT_VERSION:
        raise ContractError(f"request.contract_version must equal {CONTRACT_VERSION}")
    _require_mock(request.get("data_classification"), "request.data_classification")
    case_id = _require_string(request.get("case_id"), "request.case_id")
    if not case_id.startswith("MOCK-CASE-"):
        raise ContractError("request.case_id must start with MOCK-CASE-")

    expense = _require_dict(request.get("expense"), "request.expense")
    if expense.get("expense_type") != SCENARIO:
        raise ContractError(f"request.expense.expense_type must equal {SCENARIO}")
    _parse_money(expense.get("claim_amount"), "request.expense.claim_amount")
    _require_string(expense.get("currency"), "request.expense.currency")
    stay_start = _parse_date(expense.get("stay_start"), "request.expense.stay_start")
    stay_end = _parse_date(expense.get("stay_end"), "request.expense.stay_end")
    if stay_end < stay_start:
        raise ContractError("request.expense.stay_end cannot be before stay_start")
    nights = expense.get("nights")
    if isinstance(nights, bool) or not isinstance(nights, int) or nights <= 0:
        raise ContractError("request.expense.nights must be a positive integer")

    summaries = _require_list(request.get("attachment_summaries"), "request.attachment_summaries")
    seen_summary_ids: set[str] = set()
    for index, raw_summary in enumerate(summaries):
        path = f"request.attachment_summaries[{index}]"
        summary = _require_dict(raw_summary, path)
        summary_id = _require_string(summary.get("summary_id"), f"{path}.summary_id")
        if not summary_id.startswith("MOCK-SUMMARY-"):
            raise ContractError(f"{path}.summary_id must start with MOCK-SUMMARY-")
        if summary_id in seen_summary_ids:
            raise ContractError(f"duplicate summary_id: {summary_id}")
        seen_summary_ids.add(summary_id)
        summary_type = _require_string(summary.get("summary_type"), f"{path}.summary_type")
        if summary_type not in {"MOCK_HOTEL_INVOICE_SUMMARY", "MOCK_PAYMENT_SUMMARY"}:
            raise ContractError(f"{path}.summary_type is unsupported")
        if summary.get("source_kind") != "MOCK_GENERATED_SUMMARY":
            raise ContractError(f"{path}.source_kind must equal MOCK_GENERATED_SUMMARY")
        _parse_money(summary.get("amount"), f"{path}.amount")
        _require_string(summary.get("currency"), f"{path}.currency")
        for field in ("stay_start", "stay_end"):
            if summary.get(field) is not None:
                _parse_date(summary.get(field), f"{path}.{field}")
        _require_string(summary.get("summary_text"), f"{path}.summary_text")

    rule_lookup = _require_dict(request.get("rule_lookup"), "request.rule_lookup")
    if rule_lookup.get("scenario") != SCENARIO:
        raise ContractError(f"request.rule_lookup.scenario must equal {SCENARIO}")
    _require_string(rule_lookup.get("scope_key"), "request.rule_lookup.scope_key")
    _parse_date(rule_lookup.get("effective_on"), "request.rule_lookup.effective_on")
    return request


def _matching_rules(request: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
    lookup = request["rule_lookup"]
    effective_on = date.fromisoformat(lookup["effective_on"])
    return [
        rule
        for rule in catalog["rules"]
        if rule["scenario"] == lookup["scenario"]
        and rule["scope_key"] == lookup["scope_key"]
        and date.fromisoformat(rule["effective_from"])
        <= effective_on
        <= date.fromisoformat(rule["effective_to"])
    ]


def _rule_snapshot(
    request: dict[str, Any], catalog: dict[str, Any], matched_rules: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "catalog_id": catalog["catalog_id"],
        "catalog_version": catalog["catalog_version"],
        "lookup_scope_key": request["rule_lookup"]["scope_key"],
        "matched_rules": [
            {
                "rule_id": rule["rule_id"],
                "rule_version": rule["rule_version"],
                "data_classification": rule["data_classification"],
                "effective_from": rule["effective_from"],
                "effective_to": rule["effective_to"],
            }
            for rule in matched_rules
        ],
    }


def _result_base(
    request: dict[str, Any], catalog: dict[str, Any], matched_rules: list[dict[str, Any]], code: str
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "data_classification": MOCK,
        "case_id": request["case_id"],
        "conclusion": {"code": code, "label": CONCLUSION_LABELS[code]},
        "reasons": [],
        "evidence_sources": [],
        "rule_snapshot": _rule_snapshot(request, catalog, matched_rules),
        "manual_handling": {},
    }


def _evidence(
    evidence_id: str,
    source_type: str,
    source_id: str,
    field_path: str,
    observed_value: Any,
) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "source_type": source_type,
        "source_id": source_id,
        "field_path": field_path,
        "observed_value": observed_value,
    }


def evaluate(request: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    """Return one of the four contract conclusions for a validated MOCK request."""

    request = validate_request(request)
    catalog = validate_rule_catalog(catalog)
    matched_rules = _matching_rules(request, catalog)

    if not matched_rules:
        result = _result_base(request, catalog, [], "MOCK_RULE_PENDING_REVIEW")
        result["evidence_sources"] = [
            _evidence(
                "MOCK-EVIDENCE-RULE-LOOKUP",
                "RULE_LOOKUP_RESULT",
                catalog["catalog_id"],
                "rule_lookup.scope_key",
                request["rule_lookup"]["scope_key"],
            ),
            _evidence(
                "MOCK-EVIDENCE-EFFECTIVE-ON",
                "RULE_LOOKUP_RESULT",
                catalog["catalog_id"],
                "rule_lookup.effective_on",
                request["rule_lookup"]["effective_on"],
            ),
        ]
        result["reasons"] = [
            {
                "reason_code": "MOCK_NO_APPLICABLE_RULE",
                "message": (
                    f'MOCK：对于 {request["rule_lookup"]["scope_key"]} 未找到适用规则，'
                    "未生成或猜测替代规则。"
                ),
                "evidence_refs": [
                    "MOCK-EVIDENCE-RULE-LOOKUP",
                    "MOCK-EVIDENCE-EFFECTIVE-ON",
                ],
                "rule_id": None,
                "rule_version": None,
            }
        ]
        result["manual_handling"] = {
            "required": True,
            "reason_code": "MOCK_NO_APPLICABLE_RULE",
            "reason": "MOCK：规则不存在，必须由人工确认适用规则后再处理。",
            "requested_action": "CONFIRM_APPLICABLE_RULE",
        }
        return result

    summaries_by_type = {item["summary_type"]: item for item in request["attachment_summaries"]}
    consistency_rules = [rule for rule in matched_rules if rule["rule_type"] == "CONSISTENCY"]
    conflicts: list[dict[str, Any]] = []

    invoice = summaries_by_type.get("MOCK_HOTEL_INVOICE_SUMMARY")
    payment = summaries_by_type.get("MOCK_PAYMENT_SUMMARY")
    expense = request["expense"]

    if consistency_rules:
        checks = {check for rule in consistency_rules for check in rule["checks"]}
        if "AMOUNT_AND_CURRENCY" in checks:
            amount_items: list[tuple[str, str, str, str]] = [
                ("claim", request["case_id"], "expense.claim_amount", expense["claim_amount"])
            ]
            currency_items: list[tuple[str, str, str, str]] = [
                ("claim", request["case_id"], "expense.currency", expense["currency"])
            ]
            for label, summary in (("invoice", invoice), ("payment", payment)):
                if summary is not None:
                    amount_items.append(
                        (label, summary["summary_id"], f"{label}.amount", summary["amount"])
                    )
                    currency_items.append(
                        (label, summary["summary_id"], f"{label}.currency", summary["currency"])
                    )
            if len({_parse_money(item[3], item[2]) for item in amount_items}) > 1:
                conflicts.extend(
                    _evidence(
                        f"MOCK-EVIDENCE-AMOUNT-{label.upper()}",
                        "CLAIM_FIELD" if label == "claim" else "ATTACHMENT_SUMMARY_FIELD",
                        source_id,
                        field_path,
                        value,
                    )
                    for label, source_id, field_path, value in amount_items
                )
            if len({item[3] for item in currency_items}) > 1:
                conflicts.extend(
                    _evidence(
                        f"MOCK-EVIDENCE-CURRENCY-{label.upper()}",
                        "CLAIM_FIELD" if label == "claim" else "ATTACHMENT_SUMMARY_FIELD",
                        source_id,
                        field_path,
                        value,
                    )
                    for label, source_id, field_path, value in currency_items
                )
        if "INVOICE_STAY_DATES" in checks and invoice is not None:
            for field in ("stay_start", "stay_end"):
                if invoice.get(field) != expense[field]:
                    conflicts.extend(
                        [
                            _evidence(
                                f"MOCK-EVIDENCE-{field.upper()}-CLAIM",
                                "CLAIM_FIELD",
                                request["case_id"],
                                f"expense.{field}",
                                expense[field],
                            ),
                            _evidence(
                                f"MOCK-EVIDENCE-{field.upper()}-INVOICE",
                                "ATTACHMENT_SUMMARY_FIELD",
                                invoice["summary_id"],
                                f"invoice.{field}",
                                invoice.get(field),
                            ),
                        ]
                    )

    if conflicts:
        rule = consistency_rules[0]
        result = _result_base(request, catalog, matched_rules, "MOCK_REVIEW_CONFLICT")
        result["evidence_sources"] = conflicts
        result["reasons"] = [
            {
                "reason_code": "MOCK_EVIDENCE_CONFLICT",
                "message": "MOCK：结构化报销信息与附件摘要存在显式冲突。",
                "evidence_refs": [item["evidence_id"] for item in conflicts],
                "rule_id": rule["rule_id"],
                "rule_version": rule["rule_version"],
            }
        ]
        result["manual_handling"] = {
            "required": True,
            "reason_code": "MOCK_EVIDENCE_CONFLICT",
            "reason": "MOCK：材料关键字段不一致，需人工核对。",
            "requested_action": "VERIFY_CONFLICT",
        }
        return result

    required_rules = [rule for rule in matched_rules if rule["rule_type"] == "REQUIRED_SUMMARIES"]
    missing: list[tuple[str, dict[str, Any]]] = []
    for rule in required_rules:
        for summary_type in rule["required_summary_types"]:
            if summary_type not in summaries_by_type:
                missing.append((summary_type, rule))
    if missing:
        result = _result_base(request, catalog, matched_rules, "MOCK_MISSING_EVIDENCE")
        result["evidence_sources"] = [
            _evidence(
                f"MOCK-EVIDENCE-MISSING-{index}",
                "MISSING_EXPECTED_SOURCE",
                summary_type,
                "attachment_summaries[].summary_type",
                None,
            )
            for index, (summary_type, _) in enumerate(missing, start=1)
        ]
        result["reasons"] = [
            {
                "reason_code": "MOCK_REQUIRED_SUMMARY_MISSING",
                "message": f"MOCK：缺少必需附件摘要 {summary_type}。",
                "evidence_refs": [f"MOCK-EVIDENCE-MISSING-{index}"],
                "rule_id": rule["rule_id"],
                "rule_version": rule["rule_version"],
            }
            for index, (summary_type, rule) in enumerate(missing, start=1)
        ]
        result["manual_handling"] = {
            "required": True,
            "reason_code": "MOCK_MISSING_EVIDENCE",
            "reason": "MOCK：必需材料摘要不完整，需人工补证。",
            "requested_action": "REQUEST_EVIDENCE",
        }
        return result

    result = _result_base(request, catalog, matched_rules, "MOCK_PASS")
    pass_evidence = [
        _evidence(
            "MOCK-EVIDENCE-AMOUNT-CLAIM",
            "CLAIM_FIELD",
            request["case_id"],
            "expense.claim_amount",
            expense["claim_amount"],
        ),
        _evidence(
            "MOCK-EVIDENCE-CURRENCY-CLAIM",
            "CLAIM_FIELD",
            request["case_id"],
            "expense.currency",
            expense["currency"],
        ),
        _evidence(
            "MOCK-EVIDENCE-STAY-START-CLAIM",
            "CLAIM_FIELD",
            request["case_id"],
            "expense.stay_start",
            expense["stay_start"],
        ),
        _evidence(
            "MOCK-EVIDENCE-STAY-END-CLAIM",
            "CLAIM_FIELD",
            request["case_id"],
            "expense.stay_end",
            expense["stay_end"],
        ),
    ]
    for label, summary in (("INVOICE", invoice), ("PAYMENT", payment)):
        if summary is not None:
            pass_evidence.append(
                _evidence(
                    f"MOCK-EVIDENCE-AMOUNT-{label}",
                    "ATTACHMENT_SUMMARY_FIELD",
                    summary["summary_id"],
                    f"{label.lower()}.amount",
                    summary["amount"],
                )
            )
            pass_evidence.append(
                _evidence(
                    f"MOCK-EVIDENCE-CURRENCY-{label}",
                    "ATTACHMENT_SUMMARY_FIELD",
                    summary["summary_id"],
                    f"{label.lower()}.currency",
                    summary["currency"],
                )
            )
            if label == "INVOICE":
                for field in ("stay_start", "stay_end"):
                    pass_evidence.append(
                        _evidence(
                            f"MOCK-EVIDENCE-{field.upper()}-{label}",
                            "ATTACHMENT_SUMMARY_FIELD",
                            summary["summary_id"],
                            f"invoice.{field}",
                            summary[field],
                        )
                    )
    result["evidence_sources"] = pass_evidence
    result["reasons"] = [
        {
            "reason_code": "MOCK_REQUIRED_SUMMARIES_PRESENT",
            "message": "MOCK：模拟规则要求的两类附件摘要均已提供。",
            "evidence_refs": [
                "MOCK-EVIDENCE-AMOUNT-INVOICE",
                "MOCK-EVIDENCE-AMOUNT-PAYMENT",
            ],
            "rule_id": required_rules[0]["rule_id"] if required_rules else None,
            "rule_version": required_rules[0]["rule_version"] if required_rules else None,
        },
        {
            "reason_code": "MOCK_FIELDS_CONSISTENT",
            "message": "MOCK：本轮指定的金额、货币与住宿日期字段一致。",
            "evidence_refs": [item["evidence_id"] for item in pass_evidence],
            "rule_id": consistency_rules[0]["rule_id"] if consistency_rules else None,
            "rule_version": consistency_rules[0]["rule_version"] if consistency_rules else None,
        },
    ]
    result["manual_handling"] = {
        "required": False,
        "reason_code": None,
        "reason": "MOCK：本模拟契约无待人工处理项；不代表真实财务审批通过。",
        "requested_action": "NONE",
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, help="Path to a MOCK request JSON file")
    parser.add_argument("--rules", required=True, help="Path to a MOCK rule catalog JSON file")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print the JSON result")
    args = parser.parse_args(argv)

    try:
        request = load_json(args.request)
        catalog = load_json(args.rules)
        result = evaluate(request, catalog)
    except (OSError, json.JSONDecodeError, ContractError) as exc:
        error = {
            "data_classification": "MOCK",
            "error": "MOCK_CONTRACT_ERROR",
            "message": str(exc),
        }
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
