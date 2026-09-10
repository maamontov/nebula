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
    active_rubric_revision_id TEXT DEFAULT 'rub-rev-1',
    active_transcript_revision_id TEXT DEFAULT 'trans-rev-1',
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
    id TEXT NOT NULL,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    track_id TEXT NOT NULL,
    start_time_ms INTEGER NOT NULL,
    end_time_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    is_final INTEGER NOT NULL DEFAULT 1,
    revision_id TEXT NOT NULL DEFAULT 'trans-rev-1',
    created_at TEXT NOT NULL,
    PRIMARY KEY (interview_id, revision_id, id)
);

CREATE TABLE IF NOT EXISTS transcript_revisions (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL DEFAULT 1,
    is_batch_final INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (interview_id, revision_number)
);

CREATE TABLE IF NOT EXISTS assessment_proposals (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    question_id TEXT NOT NULL,
    rubric_revision_id TEXT NOT NULL DEFAULT 'rub-rev-1',
    transcript_revision_id TEXT NOT NULL DEFAULT 'trans-rev-1',
    model_profile_id TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    critical_errors_json TEXT NOT NULL DEFAULT '[]',
    is_rejected INTEGER NOT NULL DEFAULT 0,
    validation_errors_json TEXT NOT NULL DEFAULT '[]',
    provider_id TEXT,
    fallback_metadata_json TEXT,
    is_approved INTEGER NOT NULL DEFAULT 0,
    is_stale INTEGER NOT NULL DEFAULT 0,
    stale_reason TEXT,
    is_manually_adjusted INTEGER NOT NULL DEFAULT 0,
    reviewed_scores_json TEXT,
    reviewer_notes TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS human_assessments (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    question_id TEXT NOT NULL,
    rubric_revision_id TEXT NOT NULL,
    transcript_revision_id TEXT NOT NULL,
    reviewer_id TEXT,
    scores_json TEXT NOT NULL,
    reviewer_notes TEXT,
    is_manually_adjusted INTEGER NOT NULL DEFAULT 0,
    is_stale INTEGER NOT NULL DEFAULT 0,
    stale_reason TEXT,
    is_excluded INTEGER NOT NULL DEFAULT 0,
    exclusion_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (interview_id, question_id)
);

CREATE TABLE IF NOT EXISTS summary_proposals (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    model_profile_id TEXT NOT NULL,
    summary_data_json TEXT NOT NULL,
    is_confirmed INTEGER NOT NULL DEFAULT 0,
    confirmed_by TEXT,
    confirmed_markdown TEXT,
    confirmed_recommendation TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS report_revisions (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL DEFAULT 1,
    final_score_100 REAL,
    coverage_percentage REAL NOT NULL,
    question_scores_json TEXT NOT NULL,
    summary_markdown TEXT NOT NULL,
    hiring_recommendation TEXT,
    confirmed_by TEXT,
    sha256_checksum TEXT NOT NULL,
    canonical_snapshot_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (interview_id, revision_number)
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
    locked_by TEXT,
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
    revision_id TEXT NOT NULL DEFAULT 'trans-rev-1',
    question_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    is_ambiguous INTEGER NOT NULL DEFAULT 0,
    is_manually_adjusted INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (interview_id, revision_id, segment_id)
        REFERENCES transcript_segments(interview_id, revision_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_transcript_interview ON transcript_segments(interview_id, start_time_ms);
CREATE INDEX IF NOT EXISTS idx_transcript_rev ON transcript_segments(interview_id, revision_id);
CREATE INDEX IF NOT EXISTS idx_assessment_interview ON assessment_proposals(interview_id, question_id);
CREATE INDEX IF NOT EXISTS idx_human_assessment ON human_assessments(interview_id, question_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status_locked ON jobs(status, locked_until);
CREATE INDEX IF NOT EXISTS idx_audit_interview ON audit_events(interview_id, created_at);
CREATE INDEX IF NOT EXISTS idx_assoc_interview_question ON question_associations(interview_id, question_id);

CREATE INDEX IF NOT EXISTS idx_report_revisions ON report_revisions(interview_id, revision_number);

CREATE TABLE IF NOT EXISTS audio_chunks (
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    track_id TEXT NOT NULL,
    capture_epoch INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    start_time_ms INTEGER NOT NULL,
    end_time_ms INTEGER NOT NULL,
    sample_rate INTEGER NOT NULL,
    channels INTEGER NOT NULL,
    sample_count INTEGER NOT NULL,
    format TEXT NOT NULL,
    checksum_sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (interview_id, track_id, capture_epoch, sequence)
);

CREATE INDEX IF NOT EXISTS idx_audio_chunks_interview ON audio_chunks(interview_id, track_id, sequence);


