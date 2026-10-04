PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

ALTER TABLE parse_runs RENAME TO parse_runs_legacy;
DROP INDEX IF EXISTS idx_parse_runs_document_config;
DROP INDEX IF EXISTS uq_parse_runs_one_active_config;

CREATE TABLE parse_runs (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    parser TEXT NOT NULL DEFAULT 'mineru',
    config_json TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    task_id TEXT UNIQUE,
    status TEXT NOT NULL CHECK (
        status IN (
            'queued', 'submitted', 'submission_unknown', 'running',
            'result_pending', 'succeeded', 'failed'
        )
    ),
    result_zip_key TEXT,
    markdown_key TEXT,
    content_list_key TEXT,
    error_code TEXT,
    error_message TEXT,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    submission_attempts INTEGER NOT NULL DEFAULT 0 CHECK (submission_attempts >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (document_id, parser, config_fingerprint, attempt)
);

INSERT INTO parse_runs (
    id, document_id, parser, config_json, config_fingerprint, task_id, status,
    result_zip_key, markdown_key, content_list_key, error_code, error_message,
    attempt, submission_attempts, created_at, updated_at, completed_at
)
SELECT
    id, document_id, parser, config_json, config_fingerprint, task_id, status,
    result_zip_key, markdown_key, content_list_key, error_code, error_message,
    attempt, submission_attempts, created_at, updated_at, completed_at
FROM parse_runs_legacy;

DROP TABLE parse_runs_legacy;

CREATE INDEX idx_parse_runs_document_config
    ON parse_runs(document_id, parser, config_fingerprint, created_at DESC);

CREATE UNIQUE INDEX uq_parse_runs_one_active_config
    ON parse_runs(document_id, parser, config_fingerprint)
    WHERE status IN ('queued', 'submitted', 'running', 'result_pending');

COMMIT;
PRAGMA foreign_keys = ON;
