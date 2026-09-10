"""
Versioned SQLite Schema Migrations for Nebula.
Версионируемые миграции схемы SQLite для Nebula с автоматическим предварительным бэкапом.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from backend.db.database import Database

logger = logging.getLogger("nebula.migrations")


def get_current_migration_version(conn: sqlite3.Connection) -> int:
    """Returns current schema version or 0 if schema_migrations table does not exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] if row and row[0] is not None else 0


def run_migrations(db: Database) -> int:
    """
    Applies all pending migrations transactionally with pre-migration backup.
    Применяет все незавершенные миграции в транзакции с предварительным бэкапом.
    """
    conn = db.get_connection()
    try:
        current_version = get_current_migration_version(conn)
    finally:
        conn.close()

    if current_version < 1:
        # Make verified pre-migration backup if not in-memory
        if db.db_path != ":memory:" and Path(db.db_path).exists():
            backup_dir = Path(os.getenv("NEBULA_BACKUP_DIR", "data/backups")).resolve()
            backup_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup_path = backup_dir / f"pre_migration_v0_to_v1_{timestamp}.db"
            db.backup(str(backup_path))
            logger.info("Created pre-migration backup at %s", backup_path)

        with db.transaction() as tx_conn:
            tx_conn.execute("PRAGMA foreign_keys = OFF;")

            # 1. Update interviews table
            columns = [row["name"] for row in tx_conn.execute("PRAGMA table_info(interviews)").fetchall()]
            if "active_rubric_revision_id" not in columns:
                tx_conn.execute("ALTER TABLE interviews ADD COLUMN active_rubric_revision_id TEXT DEFAULT 'rub-rev-1';")
            if "active_transcript_revision_id" not in columns:
                tx_conn.execute("ALTER TABLE interviews ADD COLUMN active_transcript_revision_id TEXT DEFAULT 'trans-rev-1';")

            # 2. Migrate transcript_segments to composite PRIMARY KEY (interview_id, revision_id, id)
            tx_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transcript_segments_v1 (
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
                )
                """
            )
            tx_conn.execute(
                """
                INSERT OR IGNORE INTO transcript_segments_v1 (
                    id, interview_id, track_id, start_time_ms, end_time_ms, text, is_final, revision_id, created_at
                )
                SELECT id, interview_id, track_id, start_time_ms, end_time_ms, text, is_final, revision_id, created_at
                FROM transcript_segments
                """
            )
            tx_conn.execute("DROP TABLE transcript_segments;")
            tx_conn.execute("ALTER TABLE transcript_segments_v1 RENAME TO transcript_segments;")

            # 3. Migrate human_assessments to UNIQUE (interview_id, question_id) with provenance tracking
            tx_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS human_assessments_v1 (
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
                )
                """
            )
            tx_conn.execute(
                """
                INSERT OR REPLACE INTO human_assessments_v1 (
                    id, interview_id, question_id, rubric_revision_id, transcript_revision_id,
                    reviewer_id, scores_json, reviewer_notes, is_manually_adjusted,
                    is_stale, stale_reason, created_at, updated_at
                )
                SELECT
                    id, interview_id, question_id, rubric_revision_id, transcript_revision_id,
                    reviewer_id, scores_json, reviewer_notes, is_manually_adjusted,
                    is_stale, stale_reason, created_at, updated_at
                FROM human_assessments
                """
            )
            tx_conn.execute("DROP TABLE human_assessments;")
            tx_conn.execute("ALTER TABLE human_assessments_v1 RENAME TO human_assessments;")

            # 4. Migrate report_revisions to UNIQUE (interview_id, revision_number)
            tx_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS report_revisions_v1 (
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
                )
                """
            )
            tx_conn.execute(
                """
                INSERT OR REPLACE INTO report_revisions_v1 (
                    id, interview_id, revision_number, final_score_100, coverage_percentage,
                    question_scores_json, summary_markdown, hiring_recommendation,
                    confirmed_by, sha256_checksum, created_at
                )
                SELECT
                    id, interview_id, revision_number, final_score_100, coverage_percentage,
                    question_scores_json, summary_markdown, hiring_recommendation,
                    confirmed_by, sha256_checksum, created_at
                FROM report_revisions
                """
            )
            tx_conn.execute("DROP TABLE report_revisions;")
            tx_conn.execute("ALTER TABLE report_revisions_v1 RENAME TO report_revisions;")

            # 5. Recreate indexes
            tx_conn.execute("CREATE INDEX IF NOT EXISTS idx_transcript_interview ON transcript_segments(interview_id, start_time_ms);")
            tx_conn.execute("CREATE INDEX IF NOT EXISTS idx_transcript_rev ON transcript_segments(interview_id, revision_id);")
            tx_conn.execute("CREATE INDEX IF NOT EXISTS idx_assessment_interview ON assessment_proposals(interview_id, question_id);")
            tx_conn.execute("CREATE INDEX IF NOT EXISTS idx_human_assessment ON human_assessments(interview_id, question_id);")
            tx_conn.execute("CREATE INDEX IF NOT EXISTS idx_report_revisions ON report_revisions(interview_id, revision_number);")

            # 6. Record migration version 1
            now_iso = datetime.now(timezone.utc).isoformat()
            tx_conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (1, '001_interview_isolation', ?)",
                (now_iso,),
            )
            tx_conn.execute("PRAGMA foreign_keys = ON;")

        if not db.verify_integrity():
            raise RuntimeError("Database integrity check failed after running migration 001!")
        current_version = 1

    if current_version < 2:
        # Migration 2: Add canonical_snapshot_json to report_revisions and exclusions to human_assessments
        with db.transaction() as tx_conn:
            rep_cols = [row["name"] for row in tx_conn.execute("PRAGMA table_info(report_revisions)").fetchall()]
            if "canonical_snapshot_json" not in rep_cols:
                tx_conn.execute("ALTER TABLE report_revisions ADD COLUMN canonical_snapshot_json TEXT;")

            ha_cols = [row["name"] for row in tx_conn.execute("PRAGMA table_info(human_assessments)").fetchall()]
            if "is_excluded" not in ha_cols:
                tx_conn.execute("ALTER TABLE human_assessments ADD COLUMN is_excluded INTEGER NOT NULL DEFAULT 0;")
            if "exclusion_reason" not in ha_cols:
                tx_conn.execute("ALTER TABLE human_assessments ADD COLUMN exclusion_reason TEXT;")

            now_iso = datetime.now(timezone.utc).isoformat()
            tx_conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (2, '002_report_snapshot_and_exclusions', ?)",
                (now_iso,),
            )
        if not db.verify_integrity():
            raise RuntimeError("Database integrity check failed after running migration 002!")
        current_version = 2

    if current_version < 3:
        # Migration 3: Add audio_chunks table
        with db.transaction() as tx_conn:
            tx_conn.execute(
                """
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
                """
            )
            tx_conn.execute("CREATE INDEX IF NOT EXISTS idx_audio_chunks_interview ON audio_chunks(interview_id, track_id, sequence);")

            now_iso = datetime.now(timezone.utc).isoformat()
            tx_conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (3, '003_audio_chunks', ?)",
                (now_iso,),
            )
        if not db.verify_integrity():
            raise RuntimeError("Database integrity check failed after running migration 003!")
        current_version = 3

    if current_version < 4:
        # Migration 4: Stage 7 evidence validation and atomic job lease
        with db.transaction() as tx_conn:
            # 1. Add locked_by column to jobs if missing
            job_cols = [row["name"] for row in tx_conn.execute("PRAGMA table_info(jobs)").fetchall()]
            if "locked_by" not in job_cols:
                tx_conn.execute("ALTER TABLE jobs ADD COLUMN locked_by TEXT;")

            # 2. Add stage 7 columns to assessment_proposals if missing
            prop_cols = [row["name"] for row in tx_conn.execute("PRAGMA table_info(assessment_proposals)").fetchall()]
            if "is_rejected" not in prop_cols:
                tx_conn.execute("ALTER TABLE assessment_proposals ADD COLUMN is_rejected INTEGER NOT NULL DEFAULT 0;")
            if "validation_errors_json" not in prop_cols:
                tx_conn.execute("ALTER TABLE assessment_proposals ADD COLUMN validation_errors_json TEXT NOT NULL DEFAULT '[]';")
            if "provider_id" not in prop_cols:
                tx_conn.execute("ALTER TABLE assessment_proposals ADD COLUMN provider_id TEXT;")
            if "fallback_metadata_json" not in prop_cols:
                tx_conn.execute("ALTER TABLE assessment_proposals ADD COLUMN fallback_metadata_json TEXT;")

            now_iso = datetime.now(timezone.utc).isoformat()
            tx_conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (4, '004_stage7_evidence_and_lease', ?)",
                (now_iso,),
            )
        if not db.verify_integrity():
            raise RuntimeError("Database integrity check failed after running migration 004!")
        current_version = 4

    logger.info("Successfully ensured database schema up to version %d", current_version)
    return current_version

