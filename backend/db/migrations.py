"""
Versioned SQLite Schema Migrations for Nebula.
Версионируемые миграции схемы SQLite для Nebula с автоматическим предварительным бэкапом.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

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


TARGET_VERSION = 13


def run_migrations(db: Database) -> int:
    """
    Applies incremental migrations to the database.
    Returns the new schema version after all pending migrations are applied.
    """
    conn = db.get_connection()
    try:
        current_version = get_current_migration_version(conn)
    finally:
        conn.close()

    if current_version < TARGET_VERSION and db.db_path != ":memory:" and Path(db.db_path).exists():
        # Make verified pre-migration backup if not in-memory
        backup_dir = Path(os.getenv("NEBULA_BACKUP_DIR", "data/backups")).resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"pre_migration_v{current_version}_to_v{TARGET_VERSION}_{timestamp}.db"
        db.backup(str(backup_path))
        logger.info("Created pre-migration backup at %s", backup_path)

    if current_version < 1:
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
            now_iso = datetime.now(UTC).isoformat()
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

            now_iso = datetime.now(UTC).isoformat()
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

            now_iso = datetime.now(UTC).isoformat()
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

            now_iso = datetime.now(UTC).isoformat()
            tx_conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (4, '004_stage7_evidence_and_lease', ?)",
                (now_iso,),
            )
        if not db.verify_integrity():
            raise RuntimeError("Database integrity check failed after running migration 004!")
        current_version = 4

    if current_version < 5:
        # Migration 5: Single source capture mode, expected tracks, and speaker role provenance
        with db.transaction() as tx_conn:
            # 1. Update interviews table
            int_cols = [row["name"] for row in tx_conn.execute("PRAGMA table_info(interviews)").fetchall()]
            if "capture_mode" not in int_cols:
                tx_conn.execute("ALTER TABLE interviews ADD COLUMN capture_mode TEXT NOT NULL DEFAULT 'dual_source';")
            if "expected_tracks_json" not in int_cols:
                tx_conn.execute("ALTER TABLE interviews ADD COLUMN expected_tracks_json TEXT NOT NULL DEFAULT '[\"interviewer\", \"candidate\"]';")

            # 2. Update transcript_segments table
            seg_cols = [row["name"] for row in tx_conn.execute("PRAGMA table_info(transcript_segments)").fetchall()]
            if "speaker_role" not in seg_cols:
                tx_conn.execute("ALTER TABLE transcript_segments ADD COLUMN speaker_role TEXT NOT NULL DEFAULT 'unknown';")
            if "parent_segment_id" not in seg_cols:
                tx_conn.execute("ALTER TABLE transcript_segments ADD COLUMN parent_segment_id TEXT;")

            # 3. For existing dual-source segments, populate speaker_role from track_id
            tx_conn.execute("""
                UPDATE transcript_segments
                SET speaker_role = track_id
                WHERE track_id IN ('interviewer', 'candidate') AND (speaker_role IS NULL OR speaker_role = 'unknown');
            """)

            now_iso = datetime.now(UTC).isoformat()
            tx_conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (5, '005_single_source_and_speaker_roles', ?)",
                (now_iso,),
            )
        if not db.verify_integrity():
            raise RuntimeError("Database integrity check failed after running migration 005!")
        current_version = 5

    if current_version < 6:
        # Migration 6: Align question_associations foreign key with composite transcript_segments (interview_id, revision_id, id)
        with db.transaction() as tx_conn:
            tx_conn.execute("PRAGMA foreign_keys = OFF;")
            tx_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS question_associations_v6 (
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
                """
            )
            # Copy existing data if question_associations table exists
            table_exists = bool(
                tx_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='question_associations'"
                ).fetchone()
            )
            if table_exists:
                existing_cols = [row["name"] for row in tx_conn.execute("PRAGMA table_info(question_associations)").fetchall()]
                if "revision_id" in existing_cols:
                    tx_conn.execute(
                        """
                        INSERT OR IGNORE INTO question_associations_v6 (
                            id, interview_id, revision_id, question_id, segment_id,
                            confidence, is_ambiguous, is_manually_adjusted, notes, created_at
                        )
                        SELECT id, interview_id, revision_id, question_id, segment_id,
                               confidence, is_ambiguous, is_manually_adjusted, notes, created_at
                        FROM question_associations;
                        """
                    )
                else:
                    tx_conn.execute(
                        """
                        INSERT OR IGNORE INTO question_associations_v6 (
                            id, interview_id, revision_id, question_id, segment_id,
                            confidence, is_ambiguous, is_manually_adjusted, notes, created_at
                        )
                        SELECT qa.id, qa.interview_id,
                               COALESCE(ts.revision_id, 'trans-rev-1'),
                               qa.question_id, qa.segment_id,
                               qa.confidence, qa.is_ambiguous, qa.is_manually_adjusted, qa.notes, qa.created_at
                        FROM question_associations qa
                        LEFT JOIN transcript_segments ts ON ts.interview_id = qa.interview_id AND ts.id = qa.segment_id;
                        """
                    )
                tx_conn.execute("DROP TABLE question_associations;")
            tx_conn.execute("ALTER TABLE question_associations_v6 RENAME TO question_associations;")
            tx_conn.execute("CREATE INDEX IF NOT EXISTS idx_assoc_interview_question ON question_associations(interview_id, question_id);")

            now_iso = datetime.now(UTC).isoformat()
            tx_conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (6, '006_question_associations_composite_fk', ?)",
                (now_iso,),
            )
            tx_conn.execute("PRAGMA foreign_keys = ON;")

        if not db.verify_integrity():
            raise RuntimeError("Database integrity check failed after running migration 006!")
        current_version = 6

    if current_version < 7:
        # Migration 7: Job templates and interview links
        with db.transaction() as tx_conn:
            # 1. Create job_templates table
            tx_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job_templates (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    role TEXT NOT NULL,
                    level TEXT NOT NULL DEFAULT 'Middle',
                    description TEXT NOT NULL DEFAULT '',
                    questions_json TEXT NOT NULL DEFAULT '[]',
                    version INTEGER NOT NULL DEFAULT 1,
                    is_archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            tx_conn.execute("CREATE INDEX IF NOT EXISTS idx_job_templates_archived ON job_templates(is_archived);")

            # 2. Add template_id and template_version to interviews table if not present
            int_cols = [row["name"] for row in tx_conn.execute("PRAGMA table_info(interviews)").fetchall()]
            if "template_id" not in int_cols:
                tx_conn.execute("ALTER TABLE interviews ADD COLUMN template_id TEXT REFERENCES job_templates(id) ON DELETE SET NULL;")
            if "template_version" not in int_cols:
                tx_conn.execute("ALTER TABLE interviews ADD COLUMN template_version INTEGER;")

            # 3. Seed default template if empty
            count = tx_conn.execute("SELECT COUNT(*) FROM job_templates").fetchone()[0]
            if count == 0:
                now_iso = datetime.now(UTC).isoformat()
                default_questions = [
                    {
                        "id": "q-backend-saga",
                        "title": "Распределённые транзакции (Saga)",
                        "prompt": "Как вы проектируете распределённые транзакции в микросервисах? Расскажите про паттерн Saga и механизмы компенсации.",
                        "text": "Как вы проектируете распределённые транзакции в микросервисах? Расскажите про паттерн Saga и механизмы компенсации.",
                        "order_index": 0,
                        "weight": 1.5,
                        "criteria": [
                            {
                                "id": "crit-arch-saga",
                                "title": "Понимание Saga & Orchestration",
                                "description": "Понимание различий оркестрации и хореографии, идемпотентность компенсирующих транзакций",
                                "min_score": 1.0,
                                "max_score": 5.0,
                                "weight": 1.0,
                                "levels_description": {
                                    1: "Нет понимания паттерна",
                                    3: "Базовые знания хореографии",
                                    5: "Глубокое понимание оркестрации и компенсаций"
                                }
                            }
                        ]
                    },
                    {
                        "id": "q-backend-db",
                        "title": "Выбор хранилища и партиционирование",
                        "prompt": "В каких случаях вы выбираете PostgreSQL вместо NoSQL и как организуете партиционирование при высоких нагрузках?",
                        "text": "В каких случаях вы выбираете PostgreSQL вместо NoSQL и как организуете партиционирование при высоких нагрузках?",
                        "order_index": 1,
                        "weight": 1.0,
                        "criteria": [
                            {
                                "id": "crit-db-tradeoffs",
                                "title": "Выбор хранилища и шардирование",
                                "description": "Знание ACID, индексов, типов репликации и партиций по диапазонам/хешу",
                                "min_score": 1.0,
                                "max_score": 5.0,
                                "weight": 1.0,
                                "levels_description": {
                                    1: "Слабое представление об индексах",
                                    3: "Знание репликации и шардинга",
                                    5: "Глубокий опыт тюнинга и отказоустойчивости"
                                }
                            }
                        ]
                    }
                ]
                tx_conn.execute(
                    """
                    INSERT INTO job_templates (
                        id, title, role, level, description, questions_json, version, is_archived, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
                    """,
                    (
                        "tpl-backend-senior",
                        "Senior Backend / Systems Engineer",
                        "Senior Backend Engineer",
                        "Senior",
                        "Проектирование высоконагруженных распределённых систем и хранилищ данных.",
                        json.dumps(default_questions, ensure_ascii=False),
                        now_iso,
                        now_iso,
                    )
                )

            now_iso = datetime.now(UTC).isoformat()
            tx_conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (7, '007_job_templates_and_interview_links', ?)",
                (now_iso,),
            )

        if not db.verify_integrity():
            raise RuntimeError("Database integrity check failed after running migration 007!")
        current_version = 7

    if current_version < 8:
        with db.transaction() as tx_conn:
            tx_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS summary_proposals (
                    id TEXT PRIMARY KEY,
                    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
                    transcript_revision_id TEXT NOT NULL DEFAULT 'trans-rev-1',
                    rubric_revision_id TEXT NOT NULL DEFAULT 'rub-rev-1',
                    model_profile_id TEXT NOT NULL,
                    summary_data_json TEXT NOT NULL,
                    decisions_snapshot_hash TEXT,
                    is_confirmed INTEGER NOT NULL DEFAULT 0,
                    confirmed_by TEXT,
                    confirmed_markdown TEXT,
                    confirmed_recommendation TEXT,
                    is_stale INTEGER NOT NULL DEFAULT 0,
                    stale_reason TEXT,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT
                )
                """
            )
            columns = [row["name"] for row in tx_conn.execute("PRAGMA table_info(summary_proposals)").fetchall()]
            if "transcript_revision_id" not in columns:
                tx_conn.execute("ALTER TABLE summary_proposals ADD COLUMN transcript_revision_id TEXT NOT NULL DEFAULT 'trans-rev-1';")
            if "rubric_revision_id" not in columns:
                tx_conn.execute("ALTER TABLE summary_proposals ADD COLUMN rubric_revision_id TEXT NOT NULL DEFAULT 'rub-rev-1';")
            if "decisions_snapshot_hash" not in columns:
                tx_conn.execute("ALTER TABLE summary_proposals ADD COLUMN decisions_snapshot_hash TEXT;")
            if "is_stale" not in columns:
                tx_conn.execute("ALTER TABLE summary_proposals ADD COLUMN is_stale INTEGER NOT NULL DEFAULT 0;")
            if "stale_reason" not in columns:
                tx_conn.execute("ALTER TABLE summary_proposals ADD COLUMN stale_reason TEXT;")

            now_iso = datetime.now(UTC).isoformat()
            tx_conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (8, '008_summary_proposals_revisions_and_stale', ?)",
                (now_iso,),
            )

        if not db.verify_integrity():
            raise RuntimeError("Database integrity check failed after running migration 008!")
        current_version = 8

    if current_version < 9:
        with db.transaction() as tx_conn:
            tx_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transcript_assembly_state (
                    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
                    track_id TEXT NOT NULL,
                    capture_epoch INTEGER NOT NULL,
                    next_sequence INTEGER NOT NULL DEFAULT 0,
                    next_sample_offset INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (interview_id, track_id, capture_epoch)
                )
                """
            )
            tx_conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transcript_assembly ON transcript_assembly_state(interview_id, track_id, capture_epoch);"
            )
            now_iso = datetime.now(UTC).isoformat()
            tx_conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (9, '009_transcript_turn_assembly', ?)",
                (now_iso,),
            )

        if not db.verify_integrity():
            raise RuntimeError("Database integrity check failed after running migration 009!")
        current_version = 9

    if current_version < 10:
        with db.transaction() as tx_conn:
            tx_conn.execute("PRAGMA foreign_keys = OFF;")
            tx_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS followup_requests (
                    id TEXT PRIMARY KEY,
                    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
                    question_id TEXT NOT NULL,
                    rubric_revision_id TEXT NOT NULL,
                    transcript_revision_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    candidate_fingerprint TEXT,
                    context_hash TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    job_id TEXT UNIQUE REFERENCES jobs(id) ON DELETE SET NULL,
                    outcome TEXT,
                    model_profile_id TEXT,
                    provider_id TEXT,
                    prompt_version TEXT NOT NULL DEFAULT 'v1',
                    schema_version TEXT NOT NULL DEFAULT 'v1',
                    usage_tokens INTEGER,
                    latency_ms INTEGER,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE (interview_id, question_id, mode, context_hash)
                )
                """
            )
            tx_conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_followup_requests_interview_q ON followup_requests(interview_id, question_id, created_at);"
            )
            tx_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS followup_suggestions (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL REFERENCES followup_requests(id) ON DELETE CASCADE,
                    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
                    question_id TEXT NOT NULL,
                    rubric_revision_id TEXT NOT NULL,
                    transcript_revision_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    question_text TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    criterion_ids_json TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'suggested',
                    asked_text TEXT,
                    ordinal INTEGER NOT NULL DEFAULT 0,
                    decision_version INTEGER NOT NULL DEFAULT 1,
                    decided_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE (request_id, ordinal)
                )
                """
            )
            tx_conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_followup_suggestions_interview_q ON followup_suggestions(interview_id, question_id, status);"
            )
            now_iso = datetime.now(UTC).isoformat()
            tx_conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (10, '010_adaptive_followups', ?)",
                (now_iso,),
            )

        if not db.verify_integrity():
            raise RuntimeError("Database integrity check failed after running migration 010!")
        current_version = 10

    if current_version < 11:
        with db.transaction() as tx_conn:
            columns = [row["name"] for row in tx_conn.execute("PRAGMA table_info(jobs)").fetchall()]
            if "started_at" not in columns:
                tx_conn.execute("ALTER TABLE jobs ADD COLUMN started_at TEXT;")
            if "completed_at" not in columns:
                tx_conn.execute("ALTER TABLE jobs ADD COLUMN completed_at TEXT;")
            now_iso = datetime.now(UTC).isoformat()
            tx_conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (11, '011_job_telemetry', ?)",
                (now_iso,),
            )

        if not db.verify_integrity():
            raise RuntimeError("Database integrity check failed after running migration 011!")
        current_version = 11

    if current_version < 12:
        with db.transaction() as tx_conn:
            tx_conn.execute("PRAGMA foreign_keys = OFF;")
            tx_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transcript_revisions_v12 (
                    id TEXT NOT NULL,
                    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
                    revision_number INTEGER NOT NULL DEFAULT 1,
                    is_batch_final INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (interview_id, id),
                    UNIQUE (interview_id, revision_number)
                );
                """
            )
            # 1. Copy existing transcript_revisions if present
            table_exists = bool(
                tx_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='transcript_revisions'"
                ).fetchone()
            )
            if table_exists:
                tx_conn.execute(
                    """
                    INSERT OR IGNORE INTO transcript_revisions_v12 (
                        id, interview_id, revision_number, is_batch_final, created_at
                    )
                    SELECT id, interview_id, revision_number, is_batch_final, created_at
                    FROM transcript_revisions;
                    """
                )

            # 2. Recover any missing revisions referenced in transcript_segments or interviews.active_transcript_revision_id
            now_iso = datetime.now(UTC).isoformat()
            interviews_exists = bool(
                tx_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='interviews'"
                ).fetchone()
            )
            segments_exists = bool(
                tx_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='transcript_segments'"
                ).fetchone()
            )
            interviews_rows = []
            has_active_rev = False
            if interviews_exists:
                inv_cols = [r["name"] for r in tx_conn.execute("PRAGMA table_info(interviews)").fetchall()]
                has_active_rev = "active_transcript_revision_id" in inv_cols
                query = (
                    "SELECT id, active_transcript_revision_id, created_at FROM interviews"
                    if has_active_rev
                    else "SELECT id, created_at FROM interviews"
                )
                interviews_rows = tx_conn.execute(query).fetchall()

            for inv in interviews_rows:
                inv_id = inv["id"]
                inv_created = inv["created_at"] or now_iso
                active_rev = inv["active_transcript_revision_id"] if has_active_rev else None

                v12_rows = tx_conn.execute(
                    "SELECT id, revision_number FROM transcript_revisions_v12 WHERE interview_id = ?",
                    (inv_id,),
                ).fetchall()
                existing_rev_ids = {r["id"] for r in v12_rows}
                existing_nums = {r["revision_number"] for r in v12_rows}

                seg_revs = []
                if segments_exists:
                    seg_revs = [
                        row[0]
                        for row in tx_conn.execute(
                            "SELECT DISTINCT revision_id FROM transcript_segments WHERE interview_id = ? AND revision_id IS NOT NULL",
                            (inv_id,),
                        ).fetchall()
                    ]
                needed_revs = set(seg_revs)
                if active_rev:
                    needed_revs.add(active_rev)

                for r_id in sorted(needed_revs):
                    if r_id not in existing_rev_ids:
                        rev_num = 1
                        if r_id.startswith("trans-rev-"):
                            import contextlib
                            with contextlib.suppress(ValueError):
                                rev_num = int(r_id.split("-")[-1])
                        if rev_num in existing_nums:
                            rev_num = (max(existing_nums) if existing_nums else 0) + 1

                        tx_conn.execute(
                            """
                            INSERT INTO transcript_revisions_v12 (
                                id, interview_id, revision_number, is_batch_final, created_at
                            ) VALUES (?, ?, ?, 0, ?)
                            """,
                            (r_id, inv_id, rev_num, inv_created),
                        )
                        existing_rev_ids.add(r_id)
                        existing_nums.add(rev_num)

            if table_exists:
                tx_conn.execute("DROP TABLE transcript_revisions;")
            tx_conn.execute("ALTER TABLE transcript_revisions_v12 RENAME TO transcript_revisions;")
            tx_conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transcript_revisions_interview ON transcript_revisions(interview_id, revision_number);"
            )

            tx_conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (12, '012_transcript_revisions_composite_pk', ?)",
                (now_iso,),
            )
            tx_conn.execute("PRAGMA foreign_keys = ON;")

        if not db.verify_integrity():
            raise RuntimeError("Database integrity check failed after running migration 012!")
        current_version = 12

    if current_version < 13:
        with db.transaction() as tx_conn:
            tx_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    transcription_config_json TEXT NOT NULL,
                    text_analysis_config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            now_iso = datetime.now(UTC).isoformat()
            tx_conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (13, '013_ai_settings', ?)",
                (now_iso,),
            )

        if not db.verify_integrity():
            raise RuntimeError("Database integrity check failed after running migration 013!")
        current_version = 13

    logger.info("Successfully ensured database schema up to version %d", current_version)
    return current_version

