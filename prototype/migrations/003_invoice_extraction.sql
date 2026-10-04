PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS invoice_extractions (
    id TEXT PRIMARY KEY,
    parse_run_id TEXT NOT NULL REFERENCES parse_runs(id),
    input_object_key TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('succeeded')),
    internal_consistency_result TEXT NOT NULL
        CHECK (internal_consistency_result IN ('PASS', 'FAIL', 'REVIEW')),
    invoice_type TEXT,
    invoice_code TEXT,
    invoice_number TEXT,
    issue_date TEXT,
    buyer_name TEXT,
    buyer_tax_id TEXT,
    seller_name TEXT,
    seller_tax_id TEXT,
    service_name TEXT,
    net_amount TEXT,
    tax_amount TEXT,
    total_amount TEXT,
    tax_rate TEXT,
    field_sources_json TEXT NOT NULL,
    extraction_issues_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (parse_run_id, input_sha256, schema_version, extractor_version)
);

CREATE INDEX IF NOT EXISTS idx_invoice_extractions_parse_run
    ON invoice_extractions(parse_run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS invoice_checks (
    id TEXT PRIMARY KEY,
    extraction_id TEXT NOT NULL REFERENCES invoice_extractions(id) ON DELETE CASCADE,
    rule_id TEXT NOT NULL,
    ruleset_version TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('PASS', 'FAIL', 'REVIEW')),
    values_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (extraction_id, rule_id, ruleset_version)
);

CREATE INDEX IF NOT EXISTS idx_invoice_checks_extraction
    ON invoice_checks(extraction_id, ruleset_version, rule_id);
