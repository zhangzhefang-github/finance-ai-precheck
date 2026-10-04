PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS review_feedback (
    id TEXT PRIMARY KEY,
    precheck_run_id TEXT NOT NULL REFERENCES precheck_runs(id),
    outcome TEXT NOT NULL CHECK (
        outcome IN ('CONFIRMED_ISSUE', 'NEEDS_MORE_EVIDENCE', 'PRECHECK_INCORRECT')
    ),
    note TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_feedback_run
    ON review_feedback(precheck_run_id, created_at, id);

CREATE TRIGGER IF NOT EXISTS review_feedback_no_update
BEFORE UPDATE ON review_feedback
BEGIN
    SELECT RAISE(ABORT, 'review feedback is append-only');
END;

CREATE TRIGGER IF NOT EXISTS review_feedback_no_delete
BEFORE DELETE ON review_feedback
BEGIN
    SELECT RAISE(ABORT, 'review feedback is append-only');
END;
