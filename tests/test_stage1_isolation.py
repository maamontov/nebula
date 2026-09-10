"""
Regression test suite for Stage 1: Contracts, Interview Isolation & Schema.
Набор регрессионных тестов для Этапа 1: Контракты, изоляция интервью и схема БД.
"""
import pytest
from fastapi.testclient import TestClient

from backend.api.app import app, get_repository
from backend.db.database import Database
from backend.db.repository import Repository
from contracts.domain import (
    PlannedQuestion,
    RubricCriterion,
    RubricRevision,
)


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "isolation_test.db"
    db = Database(str(db_file))
    db.init_schema()
    repo = Repository(db)
    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as client:
        yield {"client": client, "repo": repo, "db": db, "tmp_path": tmp_path}
    app.dependency_overrides.clear()


def test_r3_human_assessment_isolation_between_interviews(test_db):
    """
    R3: Human assessment in interview 2 must not overwrite interview 1.
    Оценка второго интервью не должна перезаписывать оценку первого интервью при одинаковых question_id.
    """
    client = test_db["client"]
    repo = test_db["repo"]

    # Interview 1
    client.post(
        "/api/v1/interviews",
        json={"id": "inv-iso-1", "title": "Dev 1", "candidate_name": "Ivan", "role": "Backend"},
    )
    # Interview 2
    client.post(
        "/api/v1/interviews",
        json={"id": "inv-iso-2", "title": "Dev 2", "candidate_name": "Petr", "role": "Backend"},
    )

    q_id = "q-common-system"

    # Save human assessment for Interview 1: score = 5.0
    repo.save_human_assessment(
        assessment_id=f"human-assess-{q_id}",
        interview_id="inv-iso-1",
        question_id=q_id,
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        scores=[{"criterion_id": "crit-1", "score": 5.0}],
        reviewer_notes="Ivan is senior",
    )

    # Save human assessment for Interview 2: score = 2.0 with common ID or question
    repo.save_human_assessment(
        assessment_id=f"human-assess-{q_id}",
        interview_id="inv-iso-2",
        question_id=q_id,
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        scores=[{"criterion_id": "crit-1", "score": 2.0}],
        reviewer_notes="Petr is junior",
    )

    # Interview 1 must STILL have score 5.0 and candidate Ivan's notes!
    assess1 = repo.get_human_assessment_for_question("inv-iso-1", q_id)
    assert assess1 is not None, "Interview 1 assessment was lost!"
    assert assess1["interview_id"] == "inv-iso-1"
    assert assess1["reviewer_notes"] == "Ivan is senior"
    assert assess1["scores"][0]["score"] == 5.0

    # Interview 2 must have score 2.0
    assess2 = repo.get_human_assessment_for_question("inv-iso-2", q_id)
    assert assess2 is not None
    assert assess2["interview_id"] == "inv-iso-2"
    assert assess2["reviewer_notes"] == "Petr is junior"
    assert assess2["scores"][0]["score"] == 2.0


def test_r4_segment_and_report_id_collisions_between_interviews(test_db):
    """
    R4: Identical logical segment IDs and report IDs across interviews must not conflict.
    Одинаковые segment_id и report_id между разными интервью не должны конфликтовать.
    """
    client = test_db["client"]
    repo = test_db["repo"]

    client.post("/api/v1/interviews", json={"id": "inv-col-1", "title": "A", "candidate_name": "A", "role": "A"})
    client.post("/api/v1/interviews", json={"id": "inv-col-2", "title": "B", "candidate_name": "B", "role": "B"})

    # Adding segment with identical segment_id 'seg-001' to both interviews
    repo.add_transcript_segment(
        segment_id="seg-001",
        interview_id="inv-col-1",
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=1000,
        text="Candidate A speaking",
    )

    # In interview 2, adding 'seg-001' should succeed and not raise UNIQUE constraint error
    repo.add_transcript_segment(
        segment_id="seg-001",
        interview_id="inv-col-2",
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=1000,
        text="Candidate B speaking",
    )

    # Check both exist and are distinct
    segs1 = repo.get_transcript_segments("inv-col-1")
    segs2 = repo.get_transcript_segments("inv-col-2")
    assert len(segs1) == 1
    assert segs1[0]["text"] == "Candidate A speaking"
    assert len(segs2) == 1
    assert segs2[0]["text"] == "Candidate B speaking"

    # Also verify two versions of the same segment in different revisions of the same interview
    repo.add_transcript_segment(
        segment_id="seg-001",
        interview_id="inv-col-1",
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=1000,
        text="Candidate A speaking (edited revision 2)",
        revision_id="trans-rev-2",
    )
    segs1_rev2 = repo.get_transcript_segments("inv-col-1", revision_id="trans-rev-2")
    assert len(segs1_rev2) == 1
    assert segs1_rev2[0]["text"] == "Candidate A speaking (edited revision 2)"


def test_r5_desktop_plan_format_and_finalize_without_key_error(test_db):
    """
    R5: SetupScreen plan format (id, text, criteria) must finalize without KeyError or 500.
    План из SetupScreen с полями (id, text, criteria) должен финализироваться без KeyError / 500.
    """
    client = test_db["client"]
    repo = test_db["repo"]

    plan = {
        "questions": [
            {
                "id": "q-design",
                "text": "How do you design scalable APIs?",
                "weight": 1.0,
                "criteria": [
                    {"id": "c1", "title": "API Design", "min_score": 1.0, "max_score": 5.0, "weight": 1.0}
                ],
            }
        ]
    }

    create_res = client.post(
        "/api/v1/interviews",
        json={"id": "inv-r5-test", "title": "API Lead", "candidate_name": "Daria", "role": "Lead", "plan": plan},
    )
    assert create_res.status_code == 200

    # Save human review for q-design
    repo.save_human_assessment(
        assessment_id="human-assess-q-design-1",
        interview_id="inv-r5-test",
        question_id="q-design",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        scores=[{"criterion_id": "c1", "score": 5.0}],
        reviewer_notes="Great design",
    )

    # Transition to REVIEW status
    repo.update_interview_status("inv-r5-test", new_status="review")

    # Finalize report endpoint
    fin_res = client.post(
        "/api/v1/interviews/inv-r5-test/report/finalize",
        json={
            "confirmed_by": "lead-1",
            "hiring_recommendation": "STRONG_HIRE",
            "summary_markdown": "Candidate demonstrated required knowledge.",
        },
    )
    assert fin_res.status_code == 200, f"Finalize failed: {fin_res.text}"
    fin_data = fin_res.json()
    assert fin_data["final_score_100"] == 100.0
    assert fin_data["coverage_percentage"] == 100.0


def test_versioned_migration_preserves_data_and_is_idempotent(tmp_path, monkeypatch):
    """
    Migration must safely convert legacy DB schema to v1 with backup, integrity check,
    and subsequent runs must be idempotent.
    Миграция должна безопасно перенести старую схему в v1 с бэкапом, проверкой целостности,
    а повторный запуск должен быть идемпотентным.
    """
    backup_dir = tmp_path / "backups"
    monkeypatch.setenv("NEBULA_BACKUP_DIR", str(backup_dir))

    db_path = tmp_path / "legacy.db"
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    # Create legacy schema without schema_migrations or composite keys
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE interviews (
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
        CREATE TABLE transcript_segments (
            id TEXT PRIMARY KEY,
            interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
            track_id TEXT NOT NULL,
            start_time_ms INTEGER NOT NULL,
            end_time_ms INTEGER NOT NULL,
            text TEXT NOT NULL,
            is_final INTEGER NOT NULL DEFAULT 1,
            revision_id TEXT NOT NULL DEFAULT 'trans-rev-1',
            created_at TEXT NOT NULL
        );
        CREATE TABLE human_assessments (
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
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE report_revisions (
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
            created_at TEXT NOT NULL
        );
        CREATE TABLE jobs (
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
        CREATE TABLE audit_events (
            id TEXT PRIMARY KEY,
            interview_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE assessment_proposals (
            id TEXT PRIMARY KEY,
            interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
            question_id TEXT NOT NULL,
            rubric_revision_id TEXT NOT NULL DEFAULT 'rub-rev-1',
            transcript_revision_id TEXT NOT NULL DEFAULT 'trans-rev-1',
            model_profile_id TEXT NOT NULL,
            scores_json TEXT NOT NULL,
            critical_errors_json TEXT NOT NULL DEFAULT '[]',
            is_approved INTEGER NOT NULL DEFAULT 0,
            is_stale INTEGER NOT NULL DEFAULT 0,
            stale_reason TEXT,
            is_manually_adjusted INTEGER NOT NULL DEFAULT 0,
            reviewed_scores_json TEXT,
            reviewer_notes TEXT,
            created_at TEXT NOT NULL,
            reviewed_at TEXT
        );

        INSERT INTO interviews (id, title, candidate_name, role, status, created_at, updated_at)
        VALUES ('inv-leg-1', 'Legacy Tech', 'Legacy Candidate', 'Backend', 'REVIEW', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z');

        INSERT INTO transcript_segments (id, interview_id, track_id, start_time_ms, end_time_ms, text, is_final, revision_id, created_at)
        VALUES ('seg-001', 'inv-leg-1', 'candidate', 0, 5000, 'Legacy speech', 1, 'trans-rev-1', '2026-09-01T00:00:00Z');

        INSERT INTO human_assessments (id, interview_id, question_id, rubric_revision_id, transcript_revision_id, reviewer_id, scores_json, reviewer_notes, created_at, updated_at)
        VALUES ('ha-legacy-1', 'inv-leg-1', 'q1', 'rub-rev-1', 'trans-rev-1', 'rev-1', '[{"criterion_id": "c1", "score": 4.0}]', 'Legacy note', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z');
        """
    )
    conn.close()

    # Now open with Database and run migrations
    db = Database(str(db_path))
    from backend.db.migrations import run_migrations
    v = run_migrations(db)
    assert v >= 1

    # Verify backup was created
    backups = list(backup_dir.glob("pre_migration_*.db"))
    assert len(backups) == 1, "Pre-migration backup was not created!"

    # Verify data preserved in new schema
    repo = Repository(db)
    inv = repo.get_interview("inv-leg-1")
    assert inv is not None
    assert inv["active_transcript_revision_id"] == "trans-rev-1"

    segs = repo.get_transcript_segments("inv-leg-1")
    assert len(segs) == 1
    assert segs[0]["text"] == "Legacy speech"

    ha = repo.get_human_assessment_for_question("inv-leg-1", "q1")
    assert ha is not None
    assert ha["scores"][0]["score"] == 4.0

    # Verify idempotency: run_migrations again
    v2 = run_migrations(db)
    assert v2 == v
    # No extra backup created
    assert len(list(backup_dir.glob("pre_migration_*.db"))) == 1

