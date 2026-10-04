PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    original_filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
    cos_object_key TEXT NOT NULL UNIQUE,
    storage_status TEXT NOT NULL CHECK (storage_status IN ('uploading', 'ready', 'failed')),
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parse_runs (
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

CREATE INDEX IF NOT EXISTS idx_parse_runs_document_config
    ON parse_runs(document_id, parser, config_fingerprint, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_parse_runs_one_active_config
    ON parse_runs(document_id, parser, config_fingerprint)
    WHERE status IN ('queued', 'submitted', 'running', 'result_pending');
