PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS precheck_runs (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL,
    extraction_id TEXT REFERENCES invoice_extractions(id),
    input_fingerprint TEXT NOT NULL,
    claim_snapshot_json TEXT NOT NULL,
    invoice_snapshot_json TEXT NOT NULL,
    deterministic_checks_json TEXT NOT NULL,
    semantic_judgment_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    technical_reasons_json TEXT NOT NULL,
    ruleset_version TEXT NOT NULL,
    semantic_baseline_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model_provider TEXT,
    model_id TEXT,
    model_gateway TEXT,
    model_invoked INTEGER NOT NULL CHECK (model_invoked IN (0, 1)),
    final_status TEXT NOT NULL CHECK (
        final_status IN ('PASS', 'REVIEW', 'MISSING_EVIDENCE')
    ),
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_precheck_runs_claim
    ON precheck_runs(claim_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_precheck_runs_extraction
    ON precheck_runs(extraction_id, created_at DESC);
