"""Offline facts and adapters for evaluation-v1; never reads COS or model credentials."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from .document_ingestion import IngestionError, Repository
from .expense_precheck import ModelTechnicalError, SemanticModelResult

INVOICE_FIELDS = (
    "invoice_type", "invoice_code", "invoice_number", "issue_date", "buyer_name",
    "buyer_tax_id", "seller_name", "seller_tax_id", "service_name", "net_amount",
    "tax_amount", "total_amount", "tax_rate",
)
EXTRACTION_KEYS = {"fields", "field_sources", "issues", "schema_version", "extractor_version", "internal_consistency_result"}
CHECK_KEYS = {"rule_id", "ruleset_version", "result", "values", "evidence", "reason"}


def validate_payload(payload: Any) -> dict:
    """Validate structural integrity, while intentionally allowing missing business facts."""
    value = deepcopy(payload)
    if not isinstance(value, dict) or set(value) != {"claim_id", "claim", "invoice_extraction", "invoice_checks"}:
        raise ValueError("payload must contain claim_id, claim, invoice_extraction, invoice_checks")
    if not isinstance(value["claim_id"], str) or not value["claim_id"]:
        raise ValueError("claim_id must be nonempty text")
    claim = value["claim"]
    if claim is not None:
        if not isinstance(claim, dict) or claim.get("data_classification") != "MOCK":
            raise ValueError("claim must be null or explicitly MOCK")
        allowed = {"claim_id", "claim_amount", "currency", "expense_category", "purpose", "invoice_extraction_id", "data_classification"}
        if set(claim) - allowed or any(v is not None and not isinstance(v, str) for v in claim.values()):
            raise ValueError("claim fields must be text or null; unknown fields are not accepted")
        if claim.get("claim_id") != value["claim_id"]:
            raise ValueError("claim_id does not match payload")
        if claim.get("invoice_extraction_id") not in (None, "eval-extraction"):
            raise ValueError("V1 only accepts the fixed eval-extraction reference or null")
    invoice = value["invoice_extraction"]
    if invoice is not None:
        if not isinstance(invoice, dict) or set(invoice) != EXTRACTION_KEYS:
            raise ValueError("invalid extraction structure")
        if not isinstance(invoice["fields"], dict) or set(invoice["fields"]) - set(INVOICE_FIELDS):
            raise ValueError("invalid invoice fields")
        if any(v is not None and not isinstance(v, str) for v in invoice["fields"].values()):
            raise ValueError("invoice fields must be text or null")
        if invoice["internal_consistency_result"] not in {"PASS", "FAIL", "REVIEW"}:
            raise ValueError("invalid internal consistency result")
        for key in ("schema_version", "extractor_version"):
            if not isinstance(invoice[key], str) or not invoice[key]:
                raise ValueError(f"{key} must be text")
        if not isinstance(invoice["issues"], list) or not isinstance(invoice["field_sources"], dict):
            raise ValueError("invalid extraction evidence")
        for field, entries in invoice["field_sources"].items():
            if field not in INVOICE_FIELDS or not isinstance(entries, list) or not all(isinstance(x, dict) for x in entries):
                raise ValueError("invalid field source list")
    checks = value["invoice_checks"]
    if not isinstance(checks, list) or (invoice is None and checks):
        raise ValueError("checks require an extraction")
    seen = set()
    for check in checks:
        if not isinstance(check, dict) or set(check) != CHECK_KEYS:
            raise ValueError("invalid invoice check")
        for field in ("rule_id", "ruleset_version", "reason"):
            if not isinstance(check[field], str) or not check[field]:
                raise ValueError(f"{field} must be nonempty text")
        if check["rule_id"] in seen:
            raise ValueError("invoice rule IDs must be unique")
        seen.add(check["rule_id"])
        if check["result"] not in {"PASS", "FAIL", "REVIEW"}:
            raise ValueError("invalid check result")
        if not isinstance(check["values"], dict) or not isinstance(check["evidence"], dict):
            raise ValueError("check values/evidence must be objects")
    value["invoice_checks"] = sorted(checks, key=lambda x: x["rule_id"])
    return value


def load_facts(repository: Repository, payload: dict, input_sha256: str) -> None:
    """Only call with a newly created evaluation database. All facts are synthetic."""
    invoice = payload["invoice_extraction"]
    if invoice is None:
        return
    stamp = "2000-01-01T00:00:00+00:00"
    dump = lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True, allow_nan=False)
    with repository.connect() as db:
        db.execute("""INSERT INTO documents
            (id,sha256,original_filename,media_type,size_bytes,cos_object_key,storage_status,created_at,updated_at)
            VALUES ('eval-document',?,'MOCK.pdf','application/pdf',1,'mock/raw','ready',?,?)""",
            (input_sha256, stamp, stamp))
        db.execute("""INSERT INTO parse_runs
            (id,document_id,parser,config_json,config_fingerprint,status,content_list_key,attempt,created_at,updated_at)
            VALUES ('eval-parse','eval-document','mock-fixture','{}',?,'succeeded','mock/content_list.json',1,?,?)""",
            (input_sha256, stamp, stamp))
        fields = invoice["fields"]
        columns = ["id", "parse_run_id", "input_object_key", "input_sha256", "schema_version", "extractor_version", "status", "internal_consistency_result", *INVOICE_FIELDS, "field_sources_json", "extraction_issues_json", "created_at"]
        values = ["eval-extraction", "eval-parse", "mock/content_list.json", input_sha256, invoice["schema_version"], invoice["extractor_version"], "succeeded", invoice["internal_consistency_result"], *[fields.get(f) for f in INVOICE_FIELDS], dump(invoice["field_sources"]), dump(invoice["issues"]), stamp]
        db.execute(f"INSERT INTO invoice_extractions ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)
        for index, check in enumerate(payload["invoice_checks"]):
            db.execute("""INSERT INTO invoice_checks
                (id,extraction_id,rule_id,ruleset_version,result,values_json,evidence_json,reason,created_at)
                VALUES (?,'eval-extraction',?,?,?,?,?,?,?)""",
                (f"eval-check-{index}", check["rule_id"], check["ruleset_version"], check["result"], dump(check["values"]), dump(check["evidence"]), check["reason"], stamp))


class SnapshotClaimProvider:
    def __init__(self, payload: dict, fault: str):
        self.payload = deepcopy(payload)
        self.fault = fault

    def get_claim(self, claim_id: str) -> dict | None:
        if self.fault == "provider_error":
            raise IngestionError("MOCK_PROVIDER_FAILURE", "injected offline claim provider failure")
        if claim_id != self.payload["claim_id"]:
            raise ValueError("unexpected claim identity")
        return deepcopy(self.payload["claim"])


class FixtureModel:
    provider = "evaluation-fake"
    model_id = "fixed-response-v1"
    gateway = "offline://evaluation"

    def __init__(self, config: dict):
        self.config = deepcopy(config)

    def judge(self, *, purpose: str, category: str, service_name: str) -> SemanticModelResult:
        if self.config.get("error"):
            raise ModelTechnicalError(self.config["error"]["code"], self.config["error"]["message"])
        return SemanticModelResult(deepcopy(self.config["judgment"]), request_id="offline-fixed-response")


def validate_model(config: Any) -> dict:
    if config == {"mode": "disabled"}:
        return deepcopy(config)
    if not isinstance(config, dict) or config.get("mode") != "fake":
        raise ValueError("model mode must be disabled or fake; online models are not supported")
    if set(config) == {"mode", "error"}:
        error = config["error"]
        if not isinstance(error, dict) or set(error) != {"code", "message"} or not all(isinstance(v, str) and v for v in error.values()):
            raise ValueError("fake error requires code and message")
    elif set(config) == {"mode", "judgment"}:
        if not isinstance(config["judgment"], dict):
            raise ValueError("fake judgment must be an object")
        # Deliberately allow invalid judgment fields for output-validation experiments.
    else:
        raise ValueError("fake model accepts exactly one judgment or error")
    return deepcopy(config)
