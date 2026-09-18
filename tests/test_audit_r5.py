from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.db.database import Database
from backend.db.repository import Repository, RepositoryConflictError


def _setup_interview(repo: Repository, interview_id: str = "inv-r5"):
    repo.create_interview(
        interview_id=interview_id,
        title="R5 Test Interview",
        candidate_name="Alice",
        role="Backend Engineer",
        capture_mode="dual_source",
    )
    repo.update_interview_status(interview_id, "ready")
    repo.update_interview_status(interview_id, "recording")


def test_r5_expired_job_max_attempts_not_exceeded(tmp_path):
    """
    If a job has max_attempts=1 and its lease expires, subsequent claim must NOT execute it again.
    Instead, it must be transitioned to terminal FAILED.
    """
    db_path = str(tmp_path / "r5_test.db")
    db = Database(db_path)
    db.init_schema()
    repo = Repository(db)
    interview_id = "inv-r5-max-att"
    _setup_interview(repo, interview_id)

    job_id = "job-att-1"
    repo.enqueue_job(
        job_id=job_id,
        job_type="EVALUATE_QUESTION",
        interview_id=interview_id,
        payload={"question_id": "q1"},
        max_attempts=1,
    )

    # 1st claim
    job1 = repo.claim_next_job(lock_duration_sec=60)
    assert job1 is not None
    assert job1["id"] == job_id
    assert job1["attempts"] == 1

    # Simulate lease expiry in DB
    past_dt = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    with repo.db.transaction() as conn:
        conn.execute("UPDATE jobs SET locked_until = ? WHERE id = ?", (past_dt, job_id))

    # 2nd claim: should NOT claim this job again because attempts (1) >= max_attempts (1)
    job2 = repo.claim_next_job(lock_duration_sec=60)
    assert job2 is None

    # Verify status transitioned to FAILED
    with repo.db.transaction() as conn:
        row = conn.execute("SELECT status, attempts, max_attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["status"] == "FAILED"
        assert row["attempts"] == 1


def test_r5_save_assessment_proposal_requires_active_lease(tmp_path):
    """
    Worker 1 loses lease (lease expired or stolen by worker 2).
    Worker 1 attempting to save assessment proposal must fail with RepositoryConflictError,
    and no proposal must be persisted.
    """
    db_path = str(tmp_path / "r5_test2.db")
    db = Database(db_path)
    db.init_schema()
    repo = Repository(db)
    interview_id = "inv-r5-lease-save"
    _setup_interview(repo, interview_id)

    job_id = "job-eval-1"
    repo.enqueue_job(
        job_id=job_id,
        job_type="EVALUATE_QUESTION",
        interview_id=interview_id,
        payload={"question_id": "q1"},
        max_attempts=2,
    )

    job1 = repo.claim_next_job(lock_duration_sec=60)
    assert job1 is not None
    owner1 = job1["locked_by"]

    # Simulate lease expiry and claim by worker 2
    past_dt = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    with repo.db.transaction() as conn:
        conn.execute("UPDATE jobs SET locked_until = ? WHERE id = ?", (past_dt, job_id))

    job2 = repo.claim_next_job(lock_duration_sec=60)
    assert job2 is not None
    owner2 = job2["locked_by"]
    assert owner1 != owner2

    # Worker 1 tries to save assessment proposal with owner1
    with pytest.raises(RepositoryConflictError):
        repo.save_assessment_proposal(
            proposal_id="prop-late-1",
            interview_id=interview_id,
            question_id="q1",
            model_profile_id="test-model",
            scores=[{"criterion_id": "c1", "score": 4.0, "explanation": "ok", "evidence": []}],
            critical_errors=[],
            owner_token=owner1,
            job_id=job_id,
        )

    # Verify prop-late-1 was NOT saved
    props = repo.get_assessment_proposals(interview_id, question_id="q1")
    assert len(props) == 0


def test_r5_claim_adjacent_only_consecutive_same_track_epoch(tmp_path):
    """
    claim_adjacent_transcribe_jobs must:
    1. Filter strictly by track_id and epoch in SQL before ordering and limiting.
    2. Only group consecutive chunks where chunk[i+1].start_ms == chunk[i].end_ms (or within tolerance).
    3. Not skip over different track jobs or return non-consecutive chunks.
    """
    db_path = str(tmp_path / "r5_test3.db")
    db = Database(db_path)
    db.init_schema()
    repo = Repository(db)
    interview_id = "inv-r5-adj"
    _setup_interview(repo, interview_id)

    # Add jobs:
    # 1: candidate, 0-1000ms, epoch 1
    # 2: candidate, 1000-2000ms, epoch 1 (consecutive)
    # 3: interviewer, 2000-3000ms, epoch 1 (different track)
    # 4: candidate, 2000-3000ms, epoch 2 (different epoch)
    # 5: candidate, 5000-6000ms, epoch 1 (gap! non-consecutive with #2)

    repo.enqueue_job("job-1", "TRANSCRIBE_AUDIO", interview_id, {"track_id": "candidate", "start_ms": 0, "end_ms": 1000, "epoch": 1})
    repo.enqueue_job("job-2", "TRANSCRIBE_AUDIO", interview_id, {"track_id": "candidate", "start_ms": 1000, "end_ms": 2000, "epoch": 1})
    repo.enqueue_job("job-3", "TRANSCRIBE_AUDIO", interview_id, {"track_id": "interviewer", "start_ms": 2000, "end_ms": 3000, "epoch": 1})
    repo.enqueue_job("job-4", "TRANSCRIBE_AUDIO", interview_id, {"track_id": "candidate", "start_ms": 2000, "end_ms": 3000, "epoch": 2})
    repo.enqueue_job("job-5", "TRANSCRIBE_AUDIO", interview_id, {"track_id": "candidate", "start_ms": 5000, "end_ms": 6000, "epoch": 1})

    # Worker claims job-1
    first_job = repo.claim_next_job(lock_duration_sec=60)
    assert first_job["id"] == "job-1"

    # Now claim adjacent for candidate, starting from end_ms=1000, epoch=1
    adjacent = repo.claim_adjacent_transcribe_jobs(
        interview_id=interview_id,
        track_id="candidate",
        last_end_ms=1000,
        epoch=1,
        max_additional=5,
        owner_token=first_job["locked_by"],
    )

    # Only job-2 is eligible! (job-3 is interviewer, job-4 is epoch 2, job-5 has gap)
    assert len(adjacent) == 1
    assert adjacent[0]["id"] == "job-2"
