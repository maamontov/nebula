import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.api.app import app, get_repository
from backend.db.database import Database
from backend.db.migrations import get_current_migration_version, run_migrations
from backend.db.repository import Repository
from backend.workers.pipeline import PipelineWorker
from contracts.domain import InterviewStatus


def _setup_interview(repo: Repository, interview_id: str = "inv-stage2-1") -> str:
    plan = {
        "title": "Stage 2 Interview",
        "questions": [
            {
                "id": "q1",
                "title": "Architecture",
                "prompt": "Расскажите про Event Loop",
                "criteria": [
                    {
                        "id": "c1",
                        "title": "Event Loop",
                        "description": "Понимание цикла событий",
                        "min_score": 1,
                        "max_score": 5,
                        "weight": 1.0,
                    }
                ],
            }
        ],
    }
    repo.create_interview(
        interview_id=interview_id,
        title="Software Engineer",
        candidate_name="Иван",
        role="Backend",
        status=InterviewStatus.RECORDING,
    )
    repo.save_plan(f"plan-{interview_id}", interview_id, plan, version=1)
    return interview_id


def test_migration_011_job_telemetry(tmp_path):
    """Verifies migration 11 adds started_at and completed_at preserving data, is idempotent and passes integrity check."""
    db_path = tmp_path / "test_mig.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Create schema up to version 10 without started_at, completed_at
    conn.executescript(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        INSERT INTO schema_migrations (version, name, applied_at) VALUES (10, '010_previous', '2026-09-11T12:00:00Z');

        CREATE TABLE interviews (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            candidate_name TEXT NOT NULL,
            role TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            interview_id TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            error_message TEXT,
            locked_until TEXT,
            locked_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        INSERT INTO jobs (id, type, interview_id, status, payload_json, attempts, max_attempts, created_at, updated_at)
        VALUES ('job-legacy-1', 'TRANSCRIBE_AUDIO', 'inv-1', 'PENDING', '{}', 0, 3, '2026-09-11T12:00:00Z', '2026-09-11T12:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    db = Database(db_path)
    # Apply migration to version 11
    run_migrations(db)

    # Verify column existence and preserved legacy data
    with db.transaction() as cur:
        assert get_current_migration_version(cur) >= 11
        row = cur.execute("SELECT id, status, started_at, completed_at FROM jobs WHERE id = 'job-legacy-1'").fetchone()
        assert row is not None
        assert row["id"] == "job-legacy-1"
        assert row["status"] == "PENDING"
        assert row["started_at"] is None
        assert row["completed_at"] is None

        # Check integrity
        integrity = cur.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok"

    # Verify idempotency
    run_migrations(db)
    with db.transaction() as cur:
        assert get_current_migration_version(cur) >= 11


def test_job_started_at_and_completed_at_lifecycle(tmp_path):
    """
    Verifies that:
    1. started_at is populated on claim_next_job and NOT overwritten by heartbeat lease renewal.
    2. completed_at is populated on complete_job and terminal fail_job.
    3. retry fail_job leaves completed_at NULL.
    4. elapsed time does not break when heartbeat touches updated_at.
    """
    db = Database(tmp_path / "telemetry_test.db")
    db.init_schema()
    repo = Repository(db)
    _setup_interview(repo, "inv-tel-1")

    # Enqueue two jobs
    past_time = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    repo.enqueue_job("job-1", "TRANSCRIBE_TURN", "inv-tel-1", {"question_id": "q1"})
    repo.enqueue_job("job-2", "EVALUATE_QUESTION", "inv-tel-1", {"question_id": "q1"})

    # Artificially set job-1 created_at 10 seconds ago to simulate queue wait
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET created_at = ? WHERE id = 'job-1'", (past_time,))

    # Claim job-1
    claimed = repo.claim_next_job()
    assert claimed is not None
    assert claimed["id"] == "job-1"
    token = claimed["locked_by"]

    # Check that started_at is set
    with db.transaction() as conn:
        row1 = conn.execute("SELECT started_at, updated_at FROM jobs WHERE id = 'job-1'").fetchone()
        orig_started_at = row1["started_at"]
        orig_updated_at = row1["updated_at"]
        assert orig_started_at is not None

    # Simulate heartbeat lease renewal
    time.sleep(0.01)
    renewed = repo.renew_job_lease("job-1", token, extension_sec=30)
    assert renewed is True

    with db.transaction() as conn:
        row1_after_hb = conn.execute("SELECT started_at, updated_at FROM jobs WHERE id = 'job-1'").fetchone()
        # started_at must stay the same, while updated_at changed
        assert row1_after_hb["started_at"] == orig_started_at
        assert row1_after_hb["updated_at"] >= orig_updated_at

    # Complete job-1
    repo.complete_job("job-1", owner_token=token)
    with db.transaction() as conn:
        row1_done = conn.execute("SELECT status, started_at, completed_at FROM jobs WHERE id = 'job-1'").fetchone()
        assert row1_done["status"] == "COMPLETED"
        assert row1_done["started_at"] == orig_started_at
        assert row1_done["completed_at"] is not None

    # Claim job-2, fail with non-terminal retry
    claimed2 = repo.claim_next_job()
    assert claimed2["id"] == "job-2"
    token2 = claimed2["locked_by"]

    repo.fail_job("job-2", "Transient timeout", owner_token=token2, retry_delay_sec=5, is_terminal=False)
    with db.transaction() as conn:
        row2_retry = conn.execute("SELECT status, started_at, completed_at FROM jobs WHERE id = 'job-2'").fetchone()
        assert row2_retry["status"] == "PENDING"
        assert row2_retry["completed_at"] is None

    # Claim job-2 again (simulate timeout expired)
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET locked_until = NULL WHERE id = 'job-2'")
    claimed2_again = repo.claim_next_job()
    token2_again = claimed2_again["locked_by"]
    repo.fail_job("job-2", "Fatal error", owner_token=token2_again, is_terminal=True)
    with db.transaction() as conn:
        row2_terminal = conn.execute("SELECT status, started_at, completed_at FROM jobs WHERE id = 'job-2'").fetchone()
        assert row2_terminal["status"] == "FAILED"
        assert row2_terminal["completed_at"] is not None


def test_get_interview_jobs_status_backward_compatible_and_detailed(tmp_path):
    """
    Verifies that get_interview_jobs_status:
    1. Returns backward-compatible fields: interview_id, counts, pending_or_processing, is_pipeline_idle.
    2. Returns new telemetry fields: counts_by_type, oldest_pending_age_sec, active_jobs.
    3. Respects job_ids filter and limit, preserving strict interview_id isolation.
    4. Does not leak raw prompts or audio from payload_json into active_jobs.
    """
    db = Database(tmp_path / "jobs_status_test.db")
    db.init_schema()
    repo = Repository(db)
    inv_id = _setup_interview(repo, "inv-jobs-1")
    _setup_interview(repo, "inv-other-interview")

    # Enqueue a job in other interview (must be isolated)
    # Enqueue jobs in target interview first
    repo.enqueue_job(
        "job-t1",
        "TRANSCRIBE_TURN",
        inv_id,
        {"question_id": "q1", "rubric_revision_id": "rub-1", "transcript_revision_id": "tr-1", "secret_audio": "1234"},
    )
    repo.enqueue_job(
        "job-e1",
        "EVALUATE_QUESTION",
        inv_id,
        {"question_id": "q1", "rubric_revision_id": "rub-1", "transcript_revision_id": "tr-1"},
    )
    # Enqueue a job in other interview (must be isolated)
    repo.enqueue_job("job-foreign", "TRANSCRIBE_AUDIO", "inv-other-interview", {"prompt": "SECRET"})

    # Claim job-t1
    claimed = repo.claim_next_job(include_types=["TRANSCRIBE_TURN"])
    assert claimed["id"] == "job-t1"
    token = claimed["locked_by"]

    # Check status without filter
    status = repo.get_interview_jobs_status(inv_id)
    # 1. Backward-compatible fields
    assert status["interview_id"] == inv_id
    assert status["pending_or_processing"] == 2
    assert status["is_pipeline_idle"] is False
    assert status["counts"]["PROCESSING"] == 1
    assert status["counts"]["PENDING"] == 1

    # 2. Telemetry fields
    assert "TRANSCRIBE_TURN" in status["counts_by_type"]
    assert status["counts_by_type"]["TRANSCRIBE_TURN"]["PROCESSING"] == 1
    assert "EVALUATE_QUESTION" in status["counts_by_type"]
    assert status["counts_by_type"]["EVALUATE_QUESTION"]["PENDING"] == 1
    assert status["oldest_pending_age_sec"] is not None
    assert status["oldest_pending_age_sec"] >= 0.0

    # 3. Active jobs details
    active = status["active_jobs"]
    assert len(active) == 2
    job_map = {j["id"]: j for j in active}
    assert "job-t1" in job_map
    assert "job-e1" in job_map
    assert "job-foreign" not in job_map

    t1 = job_map["job-t1"]
    assert t1["status"] == "PROCESSING"
    assert t1["started_at"] is not None
    assert t1["elapsed_sec"] is not None
    assert t1["question_id"] == "q1"
    assert t1["rubric_revision_id"] == "rub-1"
    # Ensure no audio or secret leaks
    assert "secret_audio" not in t1
    assert "payload" not in t1

    # 4. Filter by specific job_ids
    filtered = repo.get_interview_jobs_status(inv_id, job_ids=["job-t1", "job-foreign"])
    # job-foreign belongs to another interview and must NOT be returned
    assert len(filtered["active_jobs"]) == 1
    assert filtered["active_jobs"][0]["id"] == "job-t1"

    # Complete job-t1
    repo.complete_job("job-t1", owner_token=token)
    # When requesting job-t1 by id specifically, completed job is returned
    status_after_complete = repo.get_interview_jobs_status(inv_id, job_ids=["job-t1"])
    assert len(status_after_complete["active_jobs"]) == 1
    completed_j = status_after_complete["active_jobs"][0]
    assert completed_j["status"] == "COMPLETED"
    assert completed_j["completed_at"] is not None
    assert completed_j["elapsed_sec"] is not None


def test_api_jobs_status_endpoint(tmp_path):
    """Tests FastAPI /jobs/status endpoint with query parameters job_ids and limit."""
    db = Database(tmp_path / "api_jobs_test.db")
    db.init_schema()
    repo = Repository(db)
    inv_id = _setup_interview(repo, "inv-api-1")

    repo.enqueue_job("job-a1", "TRANSCRIBE_AUDIO", inv_id, {"seq": 1})
    repo.enqueue_job("job-a2", "TRANSCRIBE_AUDIO", inv_id, {"seq": 2})

    client = TestClient(app)
    app.dependency_overrides[get_repository] = lambda: repo

    try:
        # Default request
        res = client.get(f"/api/v1/interviews/{inv_id}/jobs/status")
        assert res.status_code == 200
        data = res.json()
        assert data["interview_id"] == inv_id
        assert data["pending_or_processing"] == 2
        assert len(data["active_jobs"]) == 2

        # Request with job_ids filter
        res2 = client.get(f"/api/v1/interviews/{inv_id}/jobs/status?job_ids=job-a1")
        assert res2.status_code == 200
        data2 = res2.json()
        assert len(data2["active_jobs"]) == 1
        assert data2["active_jobs"][0]["id"] == "job-a1"

        # Request with limit
        res3 = client.get(f"/api/v1/interviews/{inv_id}/jobs/status?limit=1")
        assert res3.status_code == 200
        data3 = res3.json()
        assert len(data3["active_jobs"]) == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_pipeline_audit_event_telemetry(tmp_path):
    """
    Verifies that PipelineWorker records queue_wait_ms and execution_duration_ms in audit events
    for both completed and failed jobs.
    """
    db = Database(tmp_path / "pipeline_tel_test.db")
    db.init_schema()
    repo = Repository(db)
    inv_id = _setup_interview(repo, "inv-pipe-tel")

    # Enqueue an evaluation job (which cleanly completes as unanswered without external network)
    repo.enqueue_job("job-audit-eval", "EVALUATE_QUESTION", inv_id, {"question_id": "q1"})

    worker = PipelineWorker(repository=repo)
    did_work = await worker.process_one_job()
    assert did_work is True

    # Check audit events recorded
    events = repo.get_audit_events(inv_id)
    completed_events = [e for e in events if e["event_type"] == "JOB_COMPLETED_EVALUATE_QUESTION"]
    assert len(completed_events) == 1
    payload = json.loads(completed_events[0]["payload_json"])
    assert payload["job_id"] == "job-audit-eval"
    assert "queue_wait_ms" in payload
    assert "execution_duration_ms" in payload
    assert payload["execution_duration_ms"] >= 0
    assert payload.get("is_unanswered") is True

    # Enqueue a failing job to test JOB_FAILED event
    repo.enqueue_job("job-audit-fail", "INVALID_TYPE", inv_id, {})
    did_work_fail = await worker.process_one_job()
    assert did_work_fail is False

    events2 = repo.get_audit_events(inv_id)
    failed_events = [e for e in events2 if e["event_type"] == "JOB_FAILED_INVALID_TYPE"]
    assert len(failed_events) == 1
    fail_payload = json.loads(failed_events[0]["payload_json"])
    assert fail_payload["job_id"] == "job-audit-fail"
    assert "queue_wait_ms" in fail_payload
    assert "execution_duration_ms" in fail_payload
    assert "error" in fail_payload
