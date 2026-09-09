-- Nebula SQLite Schema with WAL mode support
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS interviews (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    candidate_name TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PLANNED',
    consent_confirmed_at TEXT,
    consent_version TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interview_plans (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    version INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    track_id TEXT NOT NULL,
    start_time_ms INTEGER NOT NULL,
    end_time_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    is_final INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assessment_proposals (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    question_id TEXT NOT NULL,
    model_profile_id TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    critical_errors_json TEXT NOT NULL DEFAULT '[]',
    is_approved INTEGER NOT NULL DEFAULT 0,
    reviewed_scores_json TEXT,
    reviewer_notes TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    interview_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    locked_until TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS question_associations (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    question_id TEXT NOT NULL,
    segment_id TEXT NOT NULL REFERENCES transcript_segments(id) ON DELETE CASCADE,
    confidence REAL NOT NULL DEFAULT 1.0,
    is_ambiguous INTEGER NOT NULL DEFAULT 0,
    is_manually_adjusted INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transcript_interview ON transcript_segments(interview_id, start_time_ms);
CREATE INDEX IF NOT EXISTS idx_assessment_interview ON assessment_proposals(interview_id, question_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status_locked ON jobs(status, locked_until);
CREATE INDEX IF NOT EXISTS idx_audit_interview ON audit_events(interview_id, created_at);
CREATE INDEX IF NOT EXISTS idx_assoc_interview_question ON question_associations(interview_id, question_id);

