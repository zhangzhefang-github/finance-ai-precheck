"""Offline evaluation-v1 CLI: immutable records, real precheck runs, human evidence.

Run via python -m prototype.evaluation_experiment --workspace PATH ...
No business database, cloud SDK client, or configured online model is opened here.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any
import uuid

from .document_ingestion import Repository, safe_message, utc_now
from .expense_precheck import AmountPolicy, ExpensePrecheckService, INVOICE_RULESET_VERSION, BASELINE_VERSION
from .evaluation_fixtures import FixtureModel, SnapshotClaimProvider, load_facts, validate_model, validate_payload

SCHEMA = "evaluation-v1"
ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = "ExpensePrecheckService.run"
STAGES = {"RESOLVE_CONFIG", "PREPARE_DATABASE", "LOAD_FACTS", "PRECHECK", "CAPTURE_RESULT"}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise ValueError("invalid record ID")
    return value


def nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value


def read_json(path: Path) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique,
                       parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f"invalid number: {x}")))
    if not isinstance(value, dict):
        raise ValueError("record must be a JSON object")
    return value


def read_record(path: Path) -> dict:
    value = read_json(path)
    checksum = value.get("record_sha256")
    if checksum != digest({k: v for k, v in value.items() if k != "record_sha256"}):
        raise ValueError(f"record integrity mismatch: {path.name}")
    if value.get("schema_version") != SCHEMA:
        raise ValueError("unsupported record schema")
    return value


def publish(path: Path, value: dict) -> dict:
    """Publish a complete file without replacing an existing record (even on a race)."""
    result = {**deepcopy(value), "schema_version": SCHEMA}
    result.pop("record_sha256", None)
    result["record_sha256"] = digest(result)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, path)  # Atomic, exclusive publication; never silently overwrites.
    finally:
        os.unlink(temp)
    return result


def capture_sources() -> dict:
    paths = ["prototype/__init__.py", "prototype/expense_precheck.py", "prototype/document_ingestion.py",
             "prototype/evaluation_experiment.py", "prototype/evaluation_fixtures.py", "requirements.txt"]
    paths += [str(p.relative_to(ROOT)) for p in sorted((ROOT / "prototype/migrations").glob("*.sql"))]
    files = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in paths}
    return {"files": files, "source_sha256": digest(files)}


def git_identity() -> dict:
    def command(*args):
        try:
            result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=5)
            return result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    commit = command("rev-parse", "--verify", "HEAD")
    status = command("status", "--porcelain", "--", "prototype", "requirements.txt")
    return {"git_commit": commit, "git_state": "NO_HEAD" if not commit else ("DIRTY" if status else "CLEAN")}


def resolve_config(config: dict, payload: dict, sources: dict) -> tuple[dict, AmountPolicy]:
    if set(config) != {"amount_policy", "model", "fault"}:
        raise ValueError("execution config requires amount_policy, model, fault only")
    policy = AmountPolicy.from_config(config["amount_policy"])
    if policy.is_mock and (not payload["claim"] or payload["claim"].get("data_classification") != "MOCK"):
        raise ValueError("MOCK policy requires a MOCK claim")
    model = validate_model(config["model"])
    if config["fault"] not in {"none", "provider_error"}:
        raise ValueError("unknown offline fault adapter")
    resolved = {
        "rule": policy.descriptor(), "model": model,
        "model_identity": {"provider": FixtureModel.provider, "model_id": FixtureModel.model_id} if model["mode"] == "fake" else None,
        "prompt": {"id": None, "status": "not_applicable_offline"},
        "implementation": sources["source_sha256"],
        "environment": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version, "platform": platform.platform(), "requirements_sha256": sources["files"]["requirements.txt"]},
        "harness": {"entrypoint": ENTRYPOINT, "fault": config["fault"]},
        "labels": {"invoice_ruleset": INVOICE_RULESET_VERSION, "baseline": BASELINE_VERSION},
    }
    return resolved, policy


def report_checks(report: dict | None) -> dict:
    checks = (report or {}).get("deterministic_checks", [])
    values = {x["check_id"]: x for x in checks}
    if len(values) != len(checks):
        raise ValueError("duplicate report check IDs")
    return values


def check_report(report: dict, payload: dict, resolved: dict) -> None:
    if report.get("claim_id") != payload["claim_id"] or report.get("claim_snapshot") != payload["claim"]:
        raise ValueError("report input does not match the frozen input")
    invoice = payload["invoice_extraction"] if payload["claim"] and payload["claim"].get("invoice_extraction_id") else None
    snapshot = report.get("invoice_snapshot")
    if invoice is None:
        if snapshot is not None:
            raise ValueError("unexpected invoice snapshot")
    else:
        expected_fields = {k: invoice["fields"].get(k) for k in ("invoice_code", "invoice_number", "issue_date", "service_name", "total_amount")}
        checks = [{k: c[k] for k in ("rule_id", "result", "reason", "values", "evidence")}
                  for c in payload["invoice_checks"] if c["ruleset_version"] == resolved["labels"]["invoice_ruleset"]]
        if not snapshot or snapshot.get("fields") != expected_fields or snapshot.get("invoice_checks") != checks:
            raise ValueError("report invoice facts do not match frozen input")
        expected_sources = {k: invoice["field_sources"].get(k, []) for k in ("service_name", "total_amount")}
        if snapshot.get("field_sources") != expected_sources:
            raise ValueError("report evidence differs from frozen input")
        for key in ("schema_version", "extractor_version", "internal_consistency_result"):
            if snapshot.get(key) != invoice[key]:
                raise ValueError("report extraction metadata differs from frozen input")
    actual = deepcopy(report_checks(report)["PRECHECK-AMOUNT-001"]["values"]["amount_policy"])
    if not isinstance(actual.pop("evaluated"), bool) or actual != resolved["rule"]:
        raise ValueError("actual amount policy differs from execution binding")


def capture_report(directory: Path, payload: dict, resolved: dict | None) -> dict | None:
    path = directory / "execution.sqlite3"
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='precheck_runs'").fetchone()
        if not exists:
            return None
        rows = db.execute("SELECT report_json FROM precheck_runs").fetchall()
    if not rows:
        return None
    if len(rows) != 1 or resolved is None:
        raise ValueError("cannot identify a unique report and execution binding")
    report = json.loads(rows[0][0])
    check_report(report, payload, resolved)
    return report


def outcome_record(status: str, report: dict | None, failure: dict | None, integrity: str, *, recovered: bool = False) -> dict:
    policy = report_checks(report).get("PRECHECK-AMOUNT-001", {}).get("values", {}).get("amount_policy")
    return {
        "execution_status": status, "ended_at": None if recovered else utc_now(),
        "precheck_run_id": report.get("precheck_run_id") if report else None,
        "report_snapshot": report, "report_sha256": digest(report) if report else None,
        "failure": failure, "implementation_integrity": integrity,
        "capture_method": "RECOVERED" if recovered else "DIRECT",
        "observed_execution": {
            "amount_evaluated": policy.get("evaluated") if policy else None,
            "model_invoked": report["model"]["invoked"] if report else None,
            "semantic_route": report["semantic_judgment"]["route"] if report else None,
            "technical_reasons": report.get("technical_reasons") if report else None,
        },
    }


class EvaluationStore:
    """Single-writer local experiment artifacts. Existing business DBs are never accepted."""

    def __init__(self, workspace: str | Path):
        self.root = Path(workspace).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, collection: str, item_id: str) -> Path:
        return self.root / collection / (identifier(item_id) + ".json")

    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / identifier(run_id)

    def import_case(self, value: dict) -> dict:
        expected = {"schema_version", "case_id", "title", "description", "data_classification", "declared_scope", "created_at", "scope_sha256"}
        if set(value) - expected or value.get("schema_version") != SCHEMA or value.get("data_classification") != "MOCK":
            raise ValueError("invalid MOCK Case structure")
        for field in ("case_id", "title", "description", "created_at"):
            nonempty(value.get(field), field)
        scope = value.get("declared_scope")
        if not isinstance(scope, dict) or set(scope) != {"included", "excluded", "expectations"}:
            raise ValueError("scope requires included, excluded and expectations")
        for entries in scope.values():
            if not isinstance(entries, list) or not all(isinstance(x, str) and x.strip() for x in entries):
                raise ValueError("scope entries must be text lists")
        if not scope["included"]:
            raise ValueError("scope must declare included capabilities")
        checksum = digest(scope)
        if value.get("scope_sha256", checksum) != checksum:
            raise ValueError("scope fingerprint mismatch")
        return publish(self.path("cases", value["case_id"]), {**value, "scope_sha256": checksum})

    def import_revision(self, value: dict) -> dict:
        expected = {"schema_version", "revision_id", "case_id", "parent_revision_id", "change_note", "created_at", "payload", "input_sha256"}
        if set(value) - expected or value.get("schema_version") != SCHEMA:
            raise ValueError("invalid revision structure")
        case_id = identifier(value.get("case_id"))
        read_record(self.path("cases", case_id))
        for field in ("revision_id", "created_at", "change_note"):
            nonempty(value.get(field), field)
        siblings = [read_record(p) for p in (self.root / "revisions").glob("*.json")]
        siblings = [r for r in siblings if r["case_id"] == case_id]
        parent = value.get("parent_revision_id")
        if parent:
            previous = read_record(self.path("revisions", parent))
            if previous["case_id"] != case_id or any(r.get("parent_revision_id") == parent for r in siblings):
                raise ValueError("revision must extend the same Case's single chain")
        elif siblings:
            raise ValueError("Case already has an initial revision")
        payload = validate_payload(value.get("payload"))
        checksum = digest(payload)
        if value.get("input_sha256", checksum) != checksum:
            raise ValueError("input fingerprint mismatch")
        return publish(self.path("revisions", value["revision_id"]), {**value, "parent_revision_id": parent, "payload": payload, "input_sha256": checksum})

    def run(self, case_id: str, revision_id: str, config: dict, *, run_id: str | None = None,
            parent_run_id: str | None = None, run_reason: str = "INITIAL") -> dict:
        case = read_record(self.path("cases", case_id))
        revision = read_record(self.path("revisions", revision_id))
        payload = validate_payload(revision["payload"])
        if revision["case_id"] != case_id or digest(payload) != revision["input_sha256"]:
            raise ValueError("revision does not match Case/input fingerprint")
        if digest(case["declared_scope"]) != case["scope_sha256"]:
            raise ValueError("scope fingerprint mismatch")
        if run_reason not in {"INITIAL", "INPUT_REVISION", "RULE_CHANGE", "REPEAT", "OTHER"}:
            raise ValueError("invalid run reason")
        if parent_run_id:
            parent = self.load_run(parent_run_id)
            if parent["start"]["case_id"] != case_id:
                raise ValueError("parent Run belongs to another Case")
            if run_reason == "INPUT_REVISION" and revision["parent_revision_id"] != parent["start"]["revision_id"]:
                raise ValueError("input revision Run must follow the parent revision")
        elif run_reason != "INITIAL":
            raise ValueError("non-initial Run requires a parent Run")
        canonical(config)  # Reject non-JSON configuration before creating artifacts.
        run_id = identifier(run_id or uuid.uuid4().hex)
        directory = self.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=False)
        sources = capture_sources()
        publish(directory / "input.json", {"payload": payload, "input_sha256": revision["input_sha256"]})
        publish(directory / "source-manifest.json", sources)
        publish(directory / "start.json", {
            "run_id": run_id, "case_id": case_id, "revision_id": revision_id,
            "revision_snapshot": revision, "case_snapshot": case,
            "input_sha256": revision["input_sha256"], "scope_sha256": case["scope_sha256"],
            "parent_run_id": parent_run_id, "run_reason": run_reason,
            "entrypoint": ENTRYPOINT, "started_at": utc_now(), "requested": config,
            "source_sha256": sources["source_sha256"], **git_identity(),
        })
        stage, resolved, report, returned = "RESOLVE_CONFIG", None, None, None
        status, failure = "COMPLETED", None
        try:
            if sources["source_sha256"] != LOADED_SOURCE_SHA256:
                raise RuntimeError("source files changed after module import; start a fresh process")
            resolved, policy = resolve_config(config, payload, sources)
            publish(directory / "resolved.json", {"execution": resolved})
            stage = "PREPARE_DATABASE"
            repository = Repository(directory / "execution.sqlite3")
            stage = "LOAD_FACTS"
            load_facts(repository, payload, revision["input_sha256"])
            stage = "PRECHECK"
            model = FixtureModel(resolved["model"]) if resolved["model"]["mode"] == "fake" else None
            service = ExpensePrecheckService(repository, SnapshotClaimProvider(payload, config["fault"]), model, amount_policy=policy)
            returned = service.run(payload["claim_id"])
            stage = "CAPTURE_RESULT"
            report = capture_report(directory, payload, resolved)
            if report is None or report != returned:
                raise ValueError("returned and persisted reports differ")
        except Exception as exc:
            status = "FAILED"
            failure = {"stage": stage, "code": getattr(exc, "code", "EVALUATION_EXECUTION_FAILED"), "exception_type": type(exc).__name__, "message": safe_message(exc)}
            try:
                report = capture_report(directory, payload, resolved)
            except Exception as capture_error:
                failure["capture_error"] = safe_message(capture_error)
                if isinstance(returned, dict):
                    failure["known_precheck_run_id"] = returned.get("precheck_run_id")
        try:
            integrity = "UNCHANGED" if capture_sources()["source_sha256"] == sources["source_sha256"] else "CHANGED_DURING_RUN"
            if sources["source_sha256"] != LOADED_SOURCE_SHA256:
                integrity = "UNKNOWN"
        except OSError:
            integrity = "UNKNOWN"
        outcome = outcome_record(status, report, failure, integrity)
        if report is None and failure and failure.get("known_precheck_run_id"):
            outcome["precheck_run_id"] = failure["known_precheck_run_id"]
        publish(directory / "outcome.json", outcome)
        return self.load_run(run_id)

    def load_run(self, run_id: str) -> dict:
        directory = self.run_dir(run_id)
        start = read_record(directory / "start.json")
        inp = read_record(directory / "input.json")
        source = read_record(directory / "source-manifest.json")
        if start["run_id"] != run_id or digest(inp["payload"]) != start["input_sha256"] or inp["input_sha256"] != start["input_sha256"]:
            raise ValueError("run input integrity mismatch")
        if source["source_sha256"] != digest(source["files"]) or source["source_sha256"] != start["source_sha256"]:
            raise ValueError("source manifest mismatch")
        if start["revision_snapshot"]["payload"] != inp["payload"] or digest(start["case_snapshot"]["declared_scope"]) != start["scope_sha256"]:
            raise ValueError("snapshot integrity mismatch")
        resolved_path, outcome_path = directory / "resolved.json", directory / "outcome.json"
        resolved = read_record(resolved_path)["execution"] if resolved_path.exists() else None
        if resolved and resolved["implementation"] != start["source_sha256"]:
            raise ValueError("execution source mismatch")
        outcome = read_record(outcome_path) if outcome_path.exists() else None
        if outcome:
            report = outcome["report_snapshot"]
            if (digest(report) if report else None) != outcome["report_sha256"]:
                raise ValueError("report fingerprint mismatch")
            if report:
                if not resolved or outcome["precheck_run_id"] != report["precheck_run_id"]:
                    raise ValueError("report binding missing")
                check_report(report, inp["payload"], resolved)
            elif outcome["execution_status"] == "COMPLETED":
                raise ValueError("completed Run has no report")
        return {"start": start, "input": inp["payload"], "resolved": resolved, "outcome": outcome,
                "execution_status": outcome["execution_status"] if outcome else "UNFINISHED"}

    def recover(self, run_id: str) -> dict:
        """Explicitly finalize a stopped process's records; never reruns business logic."""
        run = self.load_run(run_id)
        if run["outcome"] is not None:
            raise ValueError("Run already finalized")
        report = capture_report(self.run_dir(run_id), run["input"], run["resolved"])
        # No end-of-run source observation survived; do not invent a clean execution.
        failure = None if report else {"stage": None, "code": "RUN_INTERRUPTED", "exception_type": None, "message": "explicit recovery of an unfinished Run with no report"}
        publish(self.run_dir(run_id) / "outcome.json", outcome_record("COMPLETED" if report else "FAILED", report, failure, "UNKNOWN", recovered=True))
        return self.load_run(run_id)

    def evaluate(self, run_id: str, value: dict) -> dict:
        run = self.load_run(run_id)
        allowed = {"schema_version", "evaluation_id", "run_id", "evaluator_id", "target", "verdict", "scope_relation", "finding_kind", "expected", "observed", "reason", "basis", "evidence_refs", "supersedes_evaluation_id"}
        if set(value) - allowed or value.get("schema_version", SCHEMA) != SCHEMA or value.get("run_id", run_id) != run_id:
            raise ValueError("invalid evaluation structure or Run reference")
        verdict, scope = value.get("verdict"), value.get("scope_relation")
        if verdict not in {"CORRECT", "INCORRECT", "UNDETERMINED", "NOT_COVERED"}:
            raise ValueError("invalid verdict")
        if scope not in {"IN_SCOPE", "OUT_OF_SCOPE", "UNDETERMINED"}:
            raise ValueError("invalid scope relation")
        if verdict in {"CORRECT", "INCORRECT"} and scope != "IN_SCOPE":
            raise ValueError("correctness judgments require an in-scope target")
        if verdict == "NOT_COVERED" and scope != "OUT_OF_SCOPE":
            raise ValueError("NOT_COVERED requires OUT_OF_SCOPE")
        if verdict == "INCORRECT" and value.get("expected") is None:
            raise ValueError("incorrect judgment requires an expected result")
        if value.get("finding_kind") not in {None, "MISSED_ISSUE", "FALSE_ALARM", "WRONG_VALUE", "WRONG_REASON", "EXECUTION_FAILURE"}:
            raise ValueError("unknown finding kind")
        for key in ("reason", "evaluator_id"):
            nonempty(value.get(key), key)
        basis = value.get("basis")
        if not isinstance(basis, dict) or set(basis) != {"kind", "ref", "quote"}:
            raise ValueError("basis requires kind, ref, quote")
        if basis["kind"] not in {"CASE_MOCK_EXPECTATION", "MOCK_POLICY", "IMPLEMENTATION_CONTRACT", "UNCONFIRMED_OPINION"}:
            raise ValueError("invalid basis kind")
        nonempty(basis["ref"], "basis reference")
        nonempty(basis["quote"], "basis quote")
        if basis["kind"] == "UNCONFIRMED_OPINION" and verdict in {"CORRECT", "INCORRECT"}:
            raise ValueError("unconfirmed opinion cannot establish correctness")
        if basis["kind"] == "CASE_MOCK_EXPECTATION":
            expected_ref = run["start"]["case_id"] + "/declared_scope"
            texts = [text for entries in run["start"]["case_snapshot"]["declared_scope"].values() for text in entries]
            if basis["ref"] != expected_ref or not any(basis["quote"] in text for text in texts):
                raise ValueError("Case basis must cite the frozen scope text")
        if basis["kind"] == "MOCK_POLICY":
            rule = (run["resolved"] or {}).get("rule")
            if not rule or basis["ref"] != rule["policy_id"] or rule["data_classification"] != "MOCK":
                raise ValueError("Mock policy basis does not match execution")
        target = value.get("target")
        observed = resolve_target(run, target)
        if "observed" in value and not matches_observation(value["observed"], observed):
            raise ValueError("provided observation differs from the immutable Run")
        refs = value.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            raise ValueError("at least one resolvable evidence reference is required")
        for ref in refs:
            resolve_reference(run, ref)
        previous_id = value.get("supersedes_evaluation_id")
        if previous_id:
            previous = read_record(self.path("evaluations", previous_id))
            if (previous["run_id"], previous["evaluator_id"], previous["target"]) != (run_id, value["evaluator_id"], target):
                raise ValueError("superseding requires the same Run, author and target")
            if any(r.get("supersedes_evaluation_id") == previous_id for r in self.evaluations(run_id)):
                raise ValueError("evaluation already superseded")
        result = {**deepcopy(value), "evaluation_id": value.get("evaluation_id") or uuid.uuid4().hex,
                  "run_id": run_id, "created_at": utc_now(), "observed": observed,
                  "supersedes_evaluation_id": previous_id}
        return publish(self.path("evaluations", result["evaluation_id"]), result)

    def evaluations(self, run_id: str) -> list[dict]:
        result = [read_record(p) for p in (self.root / "evaluations").glob("*.json")]
        return sorted([r for r in result if r["run_id"] == run_id], key=lambda r: (r["created_at"], r["evaluation_id"]))

    def show(self, run_id: str) -> dict:
        run = self.load_run(run_id)
        evaluations = self.evaluations(run_id)
        superseded = {e["supersedes_evaluation_id"] for e in evaluations if e.get("supersedes_evaluation_id")}
        judgments: dict[str, set] = {}
        for item in evaluations:
            if item["evaluation_id"] not in superseded:
                judgments.setdefault(canonical(item["target"]), set()).add((item["verdict"], canonical(item.get("expected"))))
        run["human_evaluations"] = evaluations
        run["disputed_targets"] = [json.loads(target) for target, votes in judgments.items() if len(votes) > 1]
        return run

    def compare(self, base_run_id: str, candidate_run_id: str, *, comparison_id: str | None = None) -> dict:
        # Read the start records first: an invalid case reference is not a comparison.
        starts = [read_record(self.run_dir(x) / "start.json") for x in (base_run_id, candidate_run_id)]
        if starts[0]["case_id"] != starts[1]["case_id"]:
            raise ValueError("comparisons require the same Case")
        limitations, runs = [], []
        for run_id in (base_run_id, candidate_run_id):
            try:
                runs.append(self.load_run(run_id))
            except (ValueError, OSError, KeyError) as exc:
                runs.append(None)
                limitations.append(f"{run_id}: integrity or artifact error: {safe_message(exc)}")
        dimensions, differences = [], {}
        comparable = all(
            r and r["outcome"] and r["resolved"]
            and r["outcome"]["implementation_integrity"] == "UNCHANGED"
            and (r["outcome"].get("failure") or {}).get("stage") != "CAPTURE_RESULT"
            and not (r["outcome"].get("failure") or {}).get("capture_error")
            for r in runs
        )
        if all(runs):
            left, right = runs
            a, b = left["resolved"] or {}, right["resolved"] or {}
            values = {
                "INPUT": (left["start"]["input_sha256"], right["start"]["input_sha256"]),
                "RULE": (a.get("rule"), b.get("rule")),
                "MODEL": ((a.get("model"), a.get("model_identity"), a.get("prompt")), (b.get("model"), b.get("model_identity"), b.get("prompt"))),
                "IMPLEMENTATION": (left["start"]["source_sha256"], right["start"]["source_sha256"]),
                "ENVIRONMENT": (a.get("environment"), b.get("environment")),
                "HARNESS": (a.get("harness"), b.get("harness")),
                "SCOPE": (left["start"]["scope_sha256"], right["start"]["scope_sha256"]),
            }
            dimensions = [name for name, pair in values.items() if pair[0] != pair[1]]
            differences = compare_results(left, right)
            if "MODEL" in dimensions and not any((r["outcome"] or {}).get("observed_execution", {}).get("model_invoked") for r in runs):
                limitations.append("Model configuration changed, but neither Run invoked a model; do not attribute the result to model execution.")
            reason = right["start"]["run_reason"]
            if reason in {"INPUT_REVISION", "RULE_CHANGE", "REPEAT"}:
                intended = {"INPUT_REVISION": ["INPUT"], "RULE_CHANGE": ["RULE"], "REPEAT": []}[reason]
                if dimensions != intended:
                    limitations.append(f"Declared intent {reason} differs from observed dimensions {dimensions}.")
        if not comparable:
            classification = "INCOMPLETE"
            limitations.append("Missing terminal/binding artifacts, capture failure, or unknown/changed implementation integrity prevents controlled attribution.")
        elif dimensions == ["INPUT"]:
            classification = "CONTROLLED_INPUT_CHANGE"
        elif dimensions == ["RULE"]:
            classification = "CONTROLLED_RULE_CHANGE"
        elif not dimensions:
            classification = "SAME_CONDITIONS_REPEAT"
            if differences.get("results_changed"):
                limitations.append("Results differ under recorded identical conditions; investigate instability or unrecorded factors.")
        else:
            classification = "MIXED_CHANGE"
            limitations.append("Changed conditions do not isolate input or rule effects; no single-cause claim is made.")
        comparison_id = comparison_id or uuid.uuid4().hex
        result = {"comparison_id": comparison_id, "case_id": starts[0]["case_id"],
                  "base_run_id": base_run_id, "candidate_run_id": candidate_run_id, "created_at": utc_now(),
                  "comparator_version": "report-comparison-v1",
                  "comparator_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  "changed_dimensions": dimensions, "comparability": classification,
                  "differences": differences, "limitations": limitations}
        return publish(self.path("comparisons", comparison_id), result)


def pointer(root: Any, path: str) -> Any:
    if not isinstance(path, str) or (path and not path.startswith("/")):
        raise ValueError("invalid JSON Pointer")
    value = root
    if path:
        for token in path[1:].split("/"):
            if re.search(r"~(?![01])", token):
                raise ValueError("invalid JSON Pointer escape")
            key = token.replace("~1", "/").replace("~0", "~")
            try:
                if isinstance(value, list):
                    if not re.fullmatch(r"0|[1-9][0-9]*", key):
                        raise ValueError("invalid array index")
                    value = value[int(key)]
                elif isinstance(value, dict):
                    value = value[key]
                else:
                    raise ValueError("pointer traverses a scalar")
            except (KeyError, IndexError) as exc:
                raise ValueError("evidence pointer does not exist") from exc
    return deepcopy(value)


def resolve_reference(run: dict, ref: Any) -> Any:
    if not isinstance(ref, dict) or set(ref) not in ({"root", "pointer"}, {"root", "check_id"}):
        raise ValueError("reference requires a root and either pointer or check_id")
    report = (run["outcome"] or {}).get("report_snapshot")
    roots = {"input": run["input"], "report": report, "case": run["start"]["case_snapshot"],
             "execution": run["outcome"] or {"execution_status": "UNFINISHED", "failure": None}}
    if ref["root"] not in roots or roots[ref["root"]] is None:
        raise ValueError("evidence root is unavailable")
    if "check_id" in ref:
        if ref["root"] != "report" or ref["check_id"] not in report_checks(report):
            raise ValueError("check reference does not exist")
        return deepcopy(report_checks(report)[ref["check_id"]])
    return pointer(roots[ref["root"]], ref["pointer"])


def resolve_target(run: dict, target: Any) -> Any:
    if not isinstance(target, dict):
        raise ValueError("evaluation target required")
    kind = target.get("kind")
    if kind == "DECISION" and set(target) == {"kind"}:
        return {"final_status": resolve_reference(run, {"root": "report", "pointer": "/final_status"})}
    if kind == "CHECK" and set(target) == {"kind", "check_id"}:
        return resolve_reference(run, {"root": "report", "check_id": target["check_id"]})
    if kind == "FIELD" and set(target) == {"kind", "root", "pointer"} and target["root"] in {"input", "report"}:
        return resolve_reference(run, {"root": target["root"], "pointer": target["pointer"]})
    if kind == "EXECUTION" and set(target) in ({"kind"}, {"kind", "stage"}):
        if target.get("stage") is not None and target["stage"] not in STAGES:
            raise ValueError("unknown execution stage")
        outcome = run["outcome"] or {}
        actual_stage = (outcome.get("failure") or {}).get("stage")
        if target.get("stage") and target["stage"] != actual_stage:
            if not (target["stage"] == "PRECHECK" and outcome.get("report_snapshot")):
                raise ValueError("execution stage was not observed")
        return {"execution_status": run["execution_status"], "failure": outcome.get("failure"),
                "observed_execution": outcome.get("observed_execution")}
    raise ValueError("invalid evaluation target")


def matches_observation(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and matches_observation(v, actual[k]) for k, v in expected.items())
    return type(expected) is type(actual) and expected == actual


def input_changes(left: Any, right: Any, path: str = "") -> list[dict]:
    """JSON paths for frozen input only; absence and null stay distinguishable."""
    if isinstance(left, dict) and isinstance(right, dict):
        changes = []
        for key in sorted(set(left) | set(right)):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in left or key not in right:
                changes.append({"path": child, "before_present": key in left, "after_present": key in right,
                                "before": left.get(key), "after": right.get(key)})
            else:
                changes.extend(input_changes(left[key], right[key], child))
        return changes
    if type(left) is not type(right) or left != right:
        return [{"path": path, "before_present": True, "after_present": True, "before": left, "after": right}]
    return []


def compare_results(left: dict, right: dict) -> dict:
    a, b = left["outcome"] or {}, right["outcome"] or {}
    reports = [a.get("report_snapshot"), b.get("report_snapshot")]
    result: dict = {"input_changes": input_changes(left["input"], right["input"]), "checks": [], "report_comparison_available": all(reports)}
    views = []
    for run, outcome, report in zip((left, right), (a, b), reports):
        view = {"execution_status": run["execution_status"], "failure": outcome.get("failure"),
                "final_status": report.get("final_status") if report else None,
                "semantic": None, "technical_reasons": report.get("technical_reasons") if report else None}
        if report:
            semantic = report["semantic_judgment"]
            view["semantic"] = {k: v for k, v in semantic.items() if k not in {"model_request_id"}}
        views.append(view)
    result["summary"] = {"before": views[0], "after": views[1]}
    if all(reports):
        old, new = map(report_checks, reports)
        for check_id in sorted(set(old) | set(new)):
            if old.get(check_id) != new.get(check_id):
                result["checks"].append({"check_id": check_id, "before": old.get(check_id), "after": new.get(check_id),
                                         "before_present": check_id in old, "after_present": check_id in new})
    # If a whole report is unavailable, don't invent per-check PASS/missing results.
    result["results_changed"] = views[0] != views[1] or bool(result["checks"])
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path, help="local evaluation artifacts directory (not a business DB)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("import-case", "import-revision"):
        commands.add_parser(name).add_argument("file", type=Path)
    run = commands.add_parser("run")
    run.add_argument("--case", required=True)
    run.add_argument("--revision", required=True)
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--id")
    run.add_argument("--parent")
    run.add_argument("--reason", default="INITIAL")
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("run_id")
    evaluate.add_argument("--file", required=True, type=Path)
    compare = commands.add_parser("compare")
    compare.add_argument("base")
    compare.add_argument("candidate")
    compare.add_argument("--id")
    compare.add_argument("--summary", action="store_true")
    show = commands.add_parser("show")
    show.add_argument("run_id")
    show.add_argument("--summary", action="store_true")
    recover = commands.add_parser("recover")
    recover.add_argument("run_id")
    recover.add_argument("--process-stopped", action="store_true", required=True, help="operator has verified the original process is stopped")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store = EvaluationStore(args.workspace)
        if args.command == "import-case":
            result = store.import_case(read_json(args.file))
        elif args.command == "import-revision":
            result = store.import_revision(read_json(args.file))
        elif args.command == "run":
            result = store.run(args.case, args.revision, read_json(args.config), run_id=args.id, parent_run_id=args.parent, run_reason=args.reason)
        elif args.command == "evaluate":
            result = store.evaluate(args.run_id, read_json(args.file))
        elif args.command == "compare":
            result = store.compare(args.base, args.candidate, comparison_id=args.id)
        elif args.command == "recover":
            result = store.recover(args.run_id)
        else:
            result = store.show(args.run_id)
        if getattr(args, "summary", False):
            if args.command == "compare":
                print(f"{result['base_run_id']} -> {result['candidate_run_id']}: {result['comparability']}")
                print("Changed dimensions: " + (", ".join(result["changed_dimensions"]) or "none"))
                summary = result["differences"].get("summary")
                if summary:
                    print(f"Final status: {summary['before']['final_status']} -> {summary['after']['final_status']}")
                print("Changed checks: " + (", ".join(c["check_id"] for c in result["differences"].get("checks", [])) or "none / unavailable"))
                for limitation in result["limitations"]:
                    print("Limit: " + limitation)
                print("Saved comparison: " + result["comparison_id"])
            else:
                report = (result["outcome"] or {}).get("report_snapshot") or {}
                print(f"{args.run_id}: {result['execution_status']}; final_status={report.get('final_status')}")
                print(f"Human evaluations: {len(result['human_evaluations'])}; disputed targets: {len(result['disputed_targets'])}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 2 if result.get("execution_status") == "FAILED" else 0
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": safe_message(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


LOADED_SOURCE_SHA256 = capture_sources()["source_sha256"]


if __name__ == "__main__":
    raise SystemExit(main())
